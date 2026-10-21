from contextlib import redirect_stderr
from copy import deepcopy
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.qa import check_abi


class AbiCheckTests(unittest.TestCase):
    def example(self):
        return {
            name: {"size": 8, "members": [{"name": "field", "DW_AT_data_member_location": 0}]}
            for name in check_abi.REQUIRED_TYPES
        }

    def test_matching_layouts_pass(self):
        native = self.example()
        with patch.object(check_abi, "layouts", side_effect=[native, deepcopy(native)]):
            report = check_abi.compare(Path("native"), Path("binding"))
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["layouts"], native)

    def test_same_size_but_different_member_offset_fails(self):
        native = self.example()
        binding = deepcopy(native)
        binding["mCore"]["members"][0]["DW_AT_data_member_location"] = 4
        with patch.object(check_abi, "layouts", side_effect=[native, binding]):
            with self.assertRaisesRegex(ValueError, "ABI mismatch: mCore"):
                check_abi.compare(Path("native"), Path("binding"))

    def test_different_bit_offset_fails(self):
        native = self.example()
        binding = deepcopy(native)
        binding["GBARTC"]["members"][0]["DW_AT_data_bit_offset"] = 1
        with patch.object(check_abi, "layouts", side_effect=[native, binding]):
            with self.assertRaisesRegex(ValueError, "GBARTC"):
                check_abi.compare(Path("native"), Path("binding"))

    def test_missing_dwarf_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "native"
            path.touch()
            with patch.object(check_abi, "ELFFile") as elf:
                elf.return_value.has_dwarf_info.return_value = False
                with self.assertRaisesRegex(ValueError, "DWARF is required"):
                    check_abi.layouts(path)

    def test_missing_complete_types_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "native"
            path.touch()
            with patch.object(check_abi, "ELFFile") as elf:
                elf.return_value.get_dwarf_info.return_value.iter_CUs.return_value = iter(())
                with self.assertRaisesRegex(ValueError, "missing complete DWARF types"):
                    check_abi.layouts(path)

    def test_report_cannot_overwrite_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            path.write_text("original")
            args = ["check_abi", "--native", "a", "--binding", "b", "--output", str(path)]
            with patch("sys.argv", args), patch.object(check_abi, "compare", return_value={}):
                with redirect_stderr(io.StringIO()):
                    self.assertEqual(check_abi.main(), 3)
            self.assertEqual(path.read_text(), "original")


if __name__ == "__main__":
    unittest.main()
