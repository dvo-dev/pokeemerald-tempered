from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
import re

from elftools.dwarf.die import DIE
from elftools.dwarf.dwarf_expr import DWARFExprParser
from elftools.elf.elffile import ELFFile

from .errors import QAError


QUALIFIERS = {
    "DW_TAG_typedef", "DW_TAG_const_type", "DW_TAG_volatile_type",
    "DW_TAG_restrict_type", "DW_TAG_atomic_type",
}


def attribute_owner(die: DIE, name: str) -> DIE | None:
    visited = set()
    while die.offset not in visited:
        visited.add(die.offset)
        if name in die.attributes:
            return die
        reference = next((key for key in ("DW_AT_specification", "DW_AT_abstract_origin")
                          if key in die.attributes), None)
        if reference is None:
            return None
        die = die.get_DIE_from_attribute(reference)
    raise QAError("Cyclic DWARF specification/origin")


def attribute(die: DIE, name: str, default=None):
    owner = attribute_owner(die, name)
    return owner.attributes[name].value if owner is not None else default


def type_of(die: DIE) -> DIE:
    owner = attribute_owner(die, "DW_AT_type")
    if owner is None:
        raise QAError("DWARF definition has no concrete type")
    return owner.get_DIE_from_attribute("DW_AT_type")


def concrete(die: DIE) -> DIE:
    visited = set()
    while die.tag in QUALIFIERS:
        if die.offset in visited:
            raise QAError("Cyclic DWARF type")
        visited.add(die.offset)
        die = type_of(die)
    return die


def array_bounds(die: DIE) -> tuple[int, int]:
    die = concrete(die)
    if die.tag != "DW_TAG_array_type":
        raise QAError("Expected a DWARF array")
    ranges = [child for child in die.iter_children() if child.tag == "DW_TAG_subrange_type"]
    if len(ranges) != 1:
        raise QAError("Only one-dimensional DWARF arrays are supported")
    lower = attribute(ranges[0], "DW_AT_lower_bound", 0)
    count = attribute(ranges[0], "DW_AT_count")
    upper = attribute(ranges[0], "DW_AT_upper_bound")
    if not isinstance(lower, int):
        raise QAError("Dynamic DWARF array lower bound is unsupported")
    if isinstance(count, int):
        if count < 0:
            raise QAError("Invalid DWARF array length")
        return lower, count
    if not isinstance(upper, int) or upper < lower:
        raise QAError("Unbounded or dynamic DWARF array; no length is guessed")
    return lower, upper - lower + 1


def type_size(die: DIE) -> int:
    die = concrete(die)
    size = attribute(die, "DW_AT_byte_size")
    if isinstance(size, int) and size > 0:
        return size
    if die.tag == "DW_TAG_array_type":
        return array_bounds(die)[1] * type_size(type_of(die))
    if die.tag == "DW_TAG_pointer_type":
        return die.cu["address_size"]
    raise QAError("Incomplete or unsupported DWARF type size")


def member_offset(die: DIE, *, union: bool = False) -> int:
    if attribute(die, "DW_AT_data_bit_offset") is not None:
        return 0
    location = attribute(die, "DW_AT_data_member_location")
    if location is None and union:
        return 0
    if isinstance(location, int) and location >= 0:
        return location
    if isinstance(location, (list, bytes)):
        operations = DWARFExprParser(die.cu.structs).parse_expr(location)
        if (len(operations) == 1 and operations[0].op_name == "DW_OP_plus_uconst"
                and operations[0].args[0] >= 0):
            return operations[0].args[0]
    raise QAError("Unsupported DWARF member-location expression")


def find_member(die: DIE, name: str, depth: int = 0) -> tuple[DIE, int]:
    matches = members_named(die, name, depth)
    if len(matches) != 1:
        raise QAError(f"Missing or ambiguous DWARF field: {name}")
    return matches[0]


def members_named(die: DIE, name: str, depth: int) -> list[tuple[DIE, int]]:
    """Collect anonymous-member candidates without treating absence as an error."""
    if depth > 16:
        raise QAError("Anonymous aggregate nesting is too deep")
    die = concrete(die)
    if die.tag not in ("DW_TAG_structure_type", "DW_TAG_union_type"):
        raise QAError(f"Cannot select field {name} from a non-aggregate")
    matches = []
    for member in die.iter_children():
        if member.tag != "DW_TAG_member":
            continue
        member_name = attribute(member, "DW_AT_name")
        offset = member_offset(member, union=die.tag == "DW_TAG_union_type")
        if member_name == name.encode():
            matches.append((member, offset))
        elif member_name is None:
            nested_type = concrete(type_of(member))
            if nested_type.tag in ("DW_TAG_structure_type", "DW_TAG_union_type"):
                matches.extend(
                    (nested, offset + inner_offset)
                    for nested, inner_offset in members_named(nested_type, name, depth + 1)
                )
    return matches


class Symbols:
    def __init__(self, path: Path):
        with ExitStack() as stack:
            stream = stack.enter_context(path.open("rb"))
            self.elf = ELFFile(stream)
            if self.elf.elfclass != 32 or not self.elf.little_endian:
                raise QAError("QA requires a little-endian 32-bit game ELF")
            if self.elf.header["e_machine"] != "EM_ARM" or not self.elf.has_dwarf_info():
                raise QAError("A DINFO ARM ELF is required")
            self.table = self.elf.get_section_by_name(".symtab")
            if self.table is None:
                raise QAError("ELF symbol table is missing")
            self.dwarf = self.elf.get_dwarf_info()
            self.units = []
            for unit in self.dwarf.iter_CUs():
                top_name = attribute(unit.get_top_DIE(), "DW_AT_name", b"").decode()
                source = top_name
                program = self.dwarf.line_program_for_CU(unit)
                if top_name == "<stdin>" and program:
                    for entry in program.header["file_entry"]:
                        if entry.name.endswith(b".c"):
                            directory = ""
                            if entry.dir_index:
                                directory = program.header["include_directory"][
                                    entry.dir_index - 1
                                ].decode()
                            source = str(Path(directory) / entry.name.decode())
                            break
                self.units.append((source, unit))
            self.cache = {}
            self.stack = stack.pop_all()

    def close(self):
        self.stack.close()

    @staticmethod
    def split(name: str) -> tuple[str | None, str]:
        if "::" in name:
            source, name = name.rsplit("::", 1)
            if not source:
                raise QAError("Empty compilation-unit qualifier")
            return source, name
        return None, name

    def definitions(self, source: str | None, name: str, address: int, tag: str) -> list[DIE]:
        found = []
        for primary, unit in self.units:
            if source and not (primary == source or primary.endswith("/" + source)):
                continue
            for die in unit.iter_DIEs():
                if die.tag != tag or attribute(die, "DW_AT_name") != name.encode():
                    continue
                if tag == "DW_TAG_variable":
                    expression = attribute(die, "DW_AT_location")
                    if not isinstance(expression, (bytes, list)):
                        continue
                    operations = DWARFExprParser(unit.structs).parse_expr(expression)
                    if len(operations) != 1 or operations[0].op_name != "DW_OP_addr":
                        continue
                    location = operations[0].args[0]
                else:
                    location = attribute(die, "DW_AT_low_pc")
                    if location is None:
                        continue
                    location &= ~1
                if location == address:
                    found.append(die)
        return found

    def lookup(self, qualified: str, *, function: bool = False) -> tuple[int, DIE | None]:
        key = (qualified, function)
        if key in self.cache:
            return self.cache[key]
        source, name = self.split(qualified)
        expected_type = "STT_FUNC" if function else "STT_OBJECT"
        candidates = {
            int(symbol["st_value"]) & (~1 if function else -1)
            for symbol in self.table.get_symbol_by_name(name) or ()
            if symbol["st_shndx"] != "SHN_UNDEF"
            and symbol["st_info"]["type"] == expected_type
        }
        if not candidates:
            raise QAError(f"Unknown ELF symbol: {qualified}")
        if source is None and len(candidates) != 1:
            raise QAError(f"Ambiguous ELF symbol; use source.c::name: {qualified}")
        if function and source is None:
            result = (next(iter(candidates)), None)
        else:
            tag = "DW_TAG_subprogram" if function else "DW_TAG_variable"
            matches = [
                (address, die) for address in candidates
                for die in self.definitions(source, name, address, tag)
            ]
            if len(matches) != 1:
                raise QAError(f"Missing or ambiguous concrete DWARF definition: {qualified}")
            result = matches[0]
        self.cache[key] = result
        return result


@dataclass(frozen=True)
class View:
    address: int
    die: DIE
    bit_size: int | None = None
    bit_shift: int = 0


class Memory:
    def __init__(self, symbols: Symbols, read_bytes: Callable[[int, int], bytes], rom_size: int):
        self.symbols = symbols
        self.reader = read_bytes
        self.rom_size = rom_size

    def validate_range(self, address: int, size: int) -> None:
        ranges = ((0x02000000, 0x02040000), (0x03000000, 0x03008000),
                  (0x08000000, 0x08000000 + self.rom_size))
        if (size < 0 or not any(start <= address and address + size <= end
                                for start, end in ranges)):
            raise QAError(f"Unsafe memory range: 0x{address:08x}+{size}")

    def bytes_at(self, address: int, size: int) -> bytes:
        self.validate_range(address, size)
        value = self.reader(address, size)
        if len(value) != size:
            raise QAError(f"Short memory read at 0x{address:08x}: {len(value)} != {size}")
        return value

    def integer(self, view: View) -> int:
        die = concrete(view.die)
        encoding = attribute(die, "DW_AT_encoding")
        if die.tag not in ("DW_TAG_base_type", "DW_TAG_enumeration_type", "DW_TAG_pointer_type"):
            raise QAError("Expected an integer, enum, or pointer, not an aggregate")
        if die.tag == "DW_TAG_base_type" and encoding not in (1, 2, 5, 6, 7, 8):
            raise QAError("Unsupported non-integer DWARF scalar")
        size = type_size(die)
        if view.bit_size is not None:
            size = (view.bit_size + view.bit_shift + 7) // 8
        value = int.from_bytes(self.bytes_at(view.address, size), "little")
        if view.bit_size is not None:
            value = (value >> view.bit_shift) & ((1 << view.bit_size) - 1)
            bits = view.bit_size
        else:
            bits = size * 8
        if encoding in (5, 6) and value & (1 << (bits - 1)):
            value -= 1 << bits
        return value

    def view(self, expression: str) -> View:
        source, expression = Symbols.split(expression)
        match = re.match(r"[A-Za-z_]\w*", expression)
        if not match:
            raise QAError(f"Invalid memory expression: {expression}")
        name = match.group()
        address, definition = self.symbols.lookup((source + "::" if source else "") + name)
        if definition is None:
            raise QAError(f"Variable has no DWARF definition: {name}")
        view = View(address, type_of(definition))
        remaining = expression[match.end():]
        while remaining:
            if remaining.startswith("["):
                index = re.match(r"\[(\d+)\]", remaining)
                if not index:
                    raise QAError(f"Invalid array subscript: {remaining}")
                lower, count = array_bounds(view.die)
                value = int(index.group(1))
                if not lower <= value < lower + count:
                    raise QAError(f"Array index out of bounds: {value}")
                element = type_of(concrete(view.die))
                view = View(view.address + (value - lower) * type_size(element), element)
                remaining = remaining[index.end():]
                continue
            field = re.match(r"(->|\.)([A-Za-z_]\w*)", remaining)
            if field is None:
                raise QAError(f"Invalid memory expression suffix: {remaining}")
            if field.group(1) == "->":
                pointer = concrete(view.die)
                if pointer.tag != "DW_TAG_pointer_type":
                    raise QAError("Cannot dereference a non-pointer")
                target = self.integer(view)
                pointee = type_of(pointer)
                self.validate_range(target, type_size(pointee))
                view = View(target, pointee)
            member, offset = find_member(view.die, field.group(2))
            bit_size = attribute(member, "DW_AT_bit_size")
            data_bit_offset = attribute(member, "DW_AT_data_bit_offset")
            member_type = type_of(member)
            shift = 0
            if bit_size is not None:
                if not isinstance(bit_size, int) or bit_size < 1:
                    raise QAError("Invalid DWARF bitfield size")
                if data_bit_offset is not None:
                    offset += data_bit_offset // 8
                    shift = data_bit_offset % 8
                else:
                    legacy = attribute(member, "DW_AT_bit_offset")
                    if not isinstance(legacy, int):
                        raise QAError("Bitfield has no supported DWARF bit offset")
                    shift = type_size(member_type) * 8 - legacy - bit_size
                    if shift < 0:
                        raise QAError("Invalid legacy DWARF bit offset")
            view = View(view.address + offset, member_type, bit_size, shift)
            remaining = remaining[field.end():]
        return view

    def read(self, expression: str) -> int:
        return self.integer(self.view(expression))

    def bytes(self, expression: str) -> bytes:
        view = self.view(expression)
        if view.bit_size is not None:
            raise QAError("Use an integer read for a bitfield")
        return self.bytes_at(view.address, type_size(view.die))

    def array_length(self, expression: str) -> int:
        return array_bounds(self.view(expression).die)[1]

    def function(self, name: str) -> int:
        address = self.symbols.lookup(name, function=True)[0]
        if not 0x08000000 <= address < 0x08000000 + self.rom_size:
            raise QAError(f"Code symbol is outside the bundle ROM: {name}")
        return address
