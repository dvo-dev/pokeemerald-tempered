import itertools
import os
from pathlib import Path
from types import SimpleNamespace
import unittest

from tools.qa.qa.build import Bundle
from tools.qa.qa.errors import QAError
from tools.qa.qa.flows.smoke import Oracles
from tools.qa.qa.memory import (
    Memory, Symbols, View, array_bounds, concrete, type_of,
)


class Die:
    offsets = itertools.count()

    def __init__(self, tag, attrs=None, children=(), target=None, refs=None):
        self.tag = tag
        self.offset = next(self.offsets)
        self.attributes = {key: SimpleNamespace(value=value)
                           for key, value in (attrs or {}).items()}
        self.children = list(children)
        self.refs = dict(refs or {})
        if target is not None:
            self.refs["DW_AT_type"] = target
            self.attributes["DW_AT_type"] = SimpleNamespace(value=target.offset)
        self.cu = {"address_size": 4}

    def iter_children(self):
        return iter(self.children)

    def get_DIE_from_attribute(self, key):
        return self.refs[key]


def scalar(size=4, encoding=7):
    return Die("DW_TAG_base_type", {"DW_AT_byte_size": size, "DW_AT_encoding": encoding})


def member(name, target, offset=0, **attrs):
    attrs = dict(attrs, DW_AT_name=name.encode(), DW_AT_data_member_location=offset)
    return Die("DW_TAG_member", attrs, target=target)


def aggregate(*members, size=32):
    return Die("DW_TAG_structure_type", {"DW_AT_byte_size": size}, members)


class FakeSymbols:
    def __init__(self):
        self.variables = {}

    def add(self, name, target, address=0x02000000):
        self.variables[name] = (address, Die("DW_TAG_variable", target=target))

    def lookup(self, name):
        return self.variables[name]


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.symbols = FakeSymbols()
        self.ram = bytearray(0x48000)
        self.calls = []
        self.memory = Memory(self.symbols, self.reader, 0x2000000)

    def reader(self, address, size):
        self.calls.append((address, size))
        offset = address - 0x02000000
        if address >= 0x03000000:
            offset = 0x40000 + address - 0x03000000
        return bytes(self.ram[offset:offset + size])

    def test_little_endian_u64_reads_all_eight_bytes(self):
        self.symbols.add("value", scalar(8))
        self.ram[:8] = bytes.fromhex("efcdab8967452301")
        self.assertEqual(self.memory.read("value"), 0x0123456789ABCDEF)
        self.assertEqual(self.calls, [(0x02000000, 8)])

    def test_typedef_qualifier_chain(self):
        base = scalar(2)
        qualified = Die("DW_TAG_const_type", target=base)
        alias = Die("DW_TAG_typedef", target=qualified)
        self.symbols.add("value", alias)
        self.ram[:2] = b"\x34\x12"
        self.assertIs(concrete(alias), base)
        self.assertEqual(self.memory.read("value"), 0x1234)

    def test_dwarf5_signed_bitfield_uses_absolute_bit_offset(self):
        field = member("bits", scalar(4, 5), offset=4,
                       DW_AT_bit_size=3, DW_AT_data_bit_offset=5)
        self.symbols.add("root", aggregate(field))
        self.ram[0] = 0xE0
        self.assertEqual(self.memory.read("root.bits"), -1)
        self.assertEqual(self.calls[-1], (0x02000000, 1))

    def test_legacy_little_endian_bitfield(self):
        field = member("bits", scalar(), offset=4,
                       DW_AT_bit_size=3, DW_AT_bit_offset=26)
        self.symbols.add("root", aggregate(field))
        self.ram[4] = 0x28
        self.assertEqual(self.memory.read("root.bits"), 5)

    def test_anonymous_union_member(self):
        union_member = Die("DW_TAG_member", {"DW_AT_name": b"value"}, target=scalar())
        union = Die("DW_TAG_union_type", {"DW_AT_byte_size": 4}, [union_member])
        anonymous = Die("DW_TAG_member", {"DW_AT_data_member_location": 4}, target=union)
        self.symbols.add("root", aggregate(anonymous))
        self.ram[4:8] = (123).to_bytes(4, "little")
        self.assertEqual(self.memory.read("root.value"), 123)

    def test_ambiguous_anonymous_field_fails(self):
        first = member("value", scalar(), offset=0)
        second = member("value", scalar(), offset=4)
        self.symbols.add("root", aggregate(first, second))
        with self.assertRaisesRegex(QAError, "ambiguous"):
            self.memory.read("root.value")

    def test_definition_type_overrides_incomplete_declaration(self):
        element = scalar()
        incomplete = Die("DW_TAG_array_type", children=[Die("DW_TAG_subrange_type")],
                         target=element)
        complete = Die("DW_TAG_array_type", children=[
            Die("DW_TAG_subrange_type", {"DW_AT_upper_bound": 3})
        ], target=element)
        declaration = Die("DW_TAG_variable", target=incomplete)
        definition = Die("DW_TAG_variable", {"DW_AT_specification": declaration.offset},
                         target=complete, refs={"DW_AT_specification": declaration})
        self.assertIs(type_of(definition), complete)
        self.assertEqual(array_bounds(type_of(definition)), (0, 4))

    def test_array_element_size_and_bounds(self):
        array = Die("DW_TAG_array_type", children=[
            Die("DW_TAG_subrange_type", {"DW_AT_count": 3})
        ], target=scalar(2))
        self.symbols.add("values", array)
        self.ram[4:6] = b"\x55\x44"
        self.assertEqual(self.memory.read("values[2]"), 0x4455)
        with self.assertRaisesRegex(QAError, "out of bounds"):
            self.memory.read("values[3]")
        self.assertEqual(self.memory.array_length("values"), 3)

    def test_unbounded_array_fails(self):
        array = Die("DW_TAG_array_type", children=[Die("DW_TAG_subrange_type")],
                    target=scalar())
        self.symbols.add("values", array)
        with self.assertRaisesRegex(QAError, "Unbounded"):
            self.memory.read("values[0]")

    def test_pointer_dereference_validates_full_target_extent(self):
        target = aggregate(member("value", scalar()), size=8)
        pointer = Die("DW_TAG_pointer_type", {"DW_AT_byte_size": 4}, target=target)
        self.symbols.add("pointer", pointer)
        self.ram[:4] = (0x02000010).to_bytes(4, "little")
        self.ram[16:20] = (321).to_bytes(4, "little")
        self.assertEqual(self.memory.read("pointer->value"), 321)
        self.assertEqual(self.calls, [(0x02000000, 4), (0x02000010, 4)])
        self.ram[:4] = (0x0203FFFC).to_bytes(4, "little")
        with self.assertRaisesRegex(QAError, "Unsafe memory range"):
            self.memory.read("pointer->value")

    def test_null_and_io_pointers_are_never_read(self):
        pointer = Die("DW_TAG_pointer_type", {"DW_AT_byte_size": 4},
                      target=aggregate(member("value", scalar())))
        self.symbols.add("pointer", pointer)
        for value in (0, 0x04000000):
            self.ram[:4] = value.to_bytes(4, "little")
            self.calls.clear()
            with self.assertRaisesRegex(QAError, "Unsafe memory range"):
                self.memory.read("pointer->value")
            self.assertEqual(self.calls, [(0x02000000, 4)])

    def test_io_and_cross_boundary_and_negative_ranges_fail(self):
        for address, size in ((0x04000000, 1), (0x0203FFFF, 2), (0x02000000, -1)):
            with self.subTest(address=address), self.assertRaises(QAError):
                self.memory.bytes_at(address, size)
        self.assertEqual(self.calls, [])

    def test_short_read_fails(self):
        memory = Memory(self.symbols, lambda address, size: b"", 0x2000000)
        with self.assertRaisesRegex(QAError, "Short memory read"):
            memory.bytes_at(0x02000000, 4)

    def test_unsupported_float_and_aggregate_scalar_reads_fail(self):
        for target in (scalar(4, 4), aggregate(member("value", scalar()))):
            self.symbols.add("value", target)
            with self.assertRaises(QAError):
                self.memory.read("value")

    def test_cyclic_typedef_fails(self):
        alias = Die("DW_TAG_typedef")
        alias.refs["DW_AT_type"] = alias
        alias.attributes["DW_AT_type"] = SimpleNamespace(value=alias.offset)
        with self.assertRaisesRegex(QAError, "Cyclic"):
            concrete(alias)


@unittest.skipUnless(os.environ.get("QA_TEST_BUNDLE"), "requires a real DINFO build bundle")
class RealDwarfTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = Bundle.load(Path(os.environ["QA_TEST_BUNDLE"]))
        cls.symbols = Symbols(cls.bundle.file("elf"))

    @classmethod
    def tearDownClass(cls):
        cls.symbols.close()

    def setUp(self):
        self.ram = bytearray(0x48000)
        self.memory = Memory(self.symbols, self.reader, self.bundle.file("rom").stat().st_size)

    def reader(self, address, size):
        offset = address - 0x02000000
        if address >= 0x03000000:
            offset = 0x40000 + address - 0x03000000
        return bytes(self.ram[offset:offset + size])

    def write(self, address, value):
        offset = address - 0x02000000
        if address >= 0x03000000:
            offset = 0x40000 + address - 0x03000000
        self.ram[offset:offset + len(value)] = value

    def test_stdin_compilation_unit_and_thumb_callback(self):
        callback = self.memory.function("title_screen.c::Task_TitleScreenPhase3")
        view = self.memory.view("main.c::gMain.callback2")
        self.write(view.address, (callback | 1).to_bytes(4, "little"))
        self.assertEqual(self.memory.read("main.c::gMain.callback2") & ~1, callback)

    def test_real_bitfield_layout(self):
        view = self.memory.view("palette.c::gPaletteFade.active")
        self.assertEqual(view.bit_size, 1)
        self.write(view.address, bytes([1 << view.bit_shift]))
        self.assertEqual(self.memory.read("palette.c::gPaletteFade.active"), 1)

    def test_real_definition_array_bound(self):
        self.assertEqual(self.memory.array_length("task.c::gTasks"), 16)
        self.assertGreater(len(self.memory.bytes("random.c::gRngValue")), 8)

    def test_real_saveblock_pointer_and_nested_field(self):
        root = self.memory.view("load_save.c::gSaveBlock1Ptr")
        self.write(root.address, (0x02000000).to_bytes(4, "little"))
        field = self.memory.view("load_save.c::gSaveBlock1Ptr->location.mapGroup")
        self.write(field.address, b"\x19")
        self.assertEqual(
            self.memory.read("load_save.c::gSaveBlock1Ptr->location.mapGroup"), 25
        )

    def test_all_smoke_oracle_symbols_and_generated_map_are_resolvable(self):
        oracles = Oracles(SimpleNamespace(memory=self.memory, bundle=self.bundle))
        self.assertEqual(oracles.truck_map, (25, 40))
        for expression in (
            "main.c::gMain.callback2", "palette.c::gPaletteFade.active",
            "script.c::sLockFieldControls", "script.c::sGlobalScriptContextStatus",
            "start_menu.c::gMenuCallback", "task.c::gTasks[0].isActive",
            "task.c::gTasks[0].func", "task.c::gTasks[0].data[0]",
        ):
            with self.subTest(expression=expression):
                self.memory.view(expression)
        diagnostic = oracles.observe()
        self.assertIsNone(diagnostic["location"])
        self.assertEqual(diagnostic["ready"], {"title": False, "menu": False, "field": False})


if __name__ == "__main__":
    unittest.main()
