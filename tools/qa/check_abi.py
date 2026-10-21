#!/usr/bin/env python3
"""Compare native mGBA and cffi DWARF layouts without importing the bindings."""

import argparse
import json
from pathlib import Path
import sys

from elftools.common.exceptions import DWARFError, ELFError
from elftools.elf.elffile import ELFFile


REQUIRED_TYPES = (
    "mCore", "mCoreCallbacks", "mRTCGenericSource", "mRTCSource",
    "mCoreOptions", "VFile", "GBA", "GBAMemory", "GBACartridgeHardware",
    "GBARTC", "GBASavedata", "EReaderAnchorList", "EReaderBlockList",
    "EReaderScan", "mLogger", "GBACartEReader",
)


def layouts(path: Path) -> dict:
    found = {}
    with path.open("rb") as stream:
        elf = ELFFile(stream)
        if not elf.has_dwarf_info():
            raise ValueError(f"{path}: DWARF is required for the ABI safety check")
        for unit in elf.get_dwarf_info().iter_CUs():
            for die in unit.iter_DIEs():
                if die.tag not in ("DW_TAG_structure_type", "DW_TAG_union_type"):
                    continue
                attributes = die.attributes
                name = attributes.get("DW_AT_name")
                size = attributes.get("DW_AT_byte_size")
                if name is None or size is None:
                    continue
                name = name.value.decode("utf-8")
                if name not in REQUIRED_TYPES:
                    continue
                members = []
                for child in die.iter_children():
                    if child.tag != "DW_TAG_member":
                        continue
                    field = child.attributes
                    member_name = field.get("DW_AT_name")
                    member = {
                        "name": member_name.value.decode("utf-8") if member_name else None,
                    }
                    for attribute in (
                        "DW_AT_data_member_location", "DW_AT_data_bit_offset",
                        "DW_AT_bit_offset", "DW_AT_bit_size",
                    ):
                        value = field.get(attribute)
                        if value is not None:
                            if not isinstance(value.value, int):
                                raise ValueError(
                                    f"{path}: unsupported {attribute} in {name}"
                                )
                            member[attribute] = value.value
                    members.append(member)
                layout = {"size": size.value, "members": members}
                if name in found and found[name] != layout:
                    raise ValueError(f"{path}: conflicting complete layouts for {name}")
                found[name] = layout
    missing = sorted(set(REQUIRED_TYPES) - found.keys())
    if missing:
        raise ValueError(f"{path}: missing complete DWARF types: {', '.join(missing)}")
    return found


def compare(native: Path, binding: Path) -> dict:
    native_layouts = layouts(native)
    binding_layouts = layouts(binding)
    differences = [
        name for name in REQUIRED_TYPES
        if native_layouts[name] != binding_layouts[name]
    ]
    if differences:
        raise ValueError(f"native/cffi ABI mismatch: {', '.join(differences)}")
    return {
        "schema_version": 1,
        "status": "pass",
        "native": str(native.resolve()),
        "binding": str(binding.resolve()),
        "layouts": native_layouts,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native", type=Path, required=True)
    parser.add_argument("--binding", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        report = compare(args.native, args.binding)
        encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
        if args.output:
            with args.output.open("x", encoding="utf-8") as stream:
                stream.write(encoded)
        print(encoded, end="")
    except (OSError, ValueError, DWARFError, ELFError) as error:
        print(f"ABI safety check failed: {error}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
