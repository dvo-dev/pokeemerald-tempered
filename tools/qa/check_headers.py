#!/usr/bin/env python3
"""Fail if native and cffi preprocessing disagree at the pinned mGBA surface."""

import argparse
import difflib
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys


def assignment(text: str, key: str) -> str:
    match = re.search(r"^" + re.escape(key) + r"\s*=\s*(.*)$", text, re.MULTILINE)
    if match is None:
        raise ValueError(f"Missing build assignment: {key}")
    return match.group(1)


def python_flags(text: str) -> list[str]:
    commands = [line for line in text.splitlines()
                if line.lstrip().startswith("cd ") and "_builder.py" in line]
    if len(commands) != 1:
        raise ValueError("Expected exactly one cffi header-generator command")
    values = [part.split("=", 1)[1] for part in shlex.split(commands[0])
              if part.startswith("CPPFLAGS=")]
    if len(values) != 1:
        raise ValueError("Cannot identify actual cffi CPPFLAGS")
    return shlex.split(values[0])


def normalized(text: str) -> list[str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return [line for line in lines if not re.fullmatch(
        r"extern long int __sysconf \([^;]*;", line
    )]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--build", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        source = args.source.resolve()
        build = args.build.resolve()
        cache = (build / "CMakeCache.txt").read_text()
        compiler_match = re.search(r"^CMAKE_C_COMPILER:FILEPATH=(.+)$", cache, re.MULTILINE)
        if compiler_match is None:
            raise ValueError("CMake C compiler is not recorded")
        compiler = compiler_match.group(1)
        native = (build / "CMakeFiles/mgba.dir/flags.make").read_text()
        native_flags = shlex.split(assignment(native, "C_DEFINES"))
        native_flags += shlex.split(assignment(native, "C_INCLUDES"))
        cffi_flags = python_flags(
            (build / "python/CMakeFiles/mgba-pylib.dir/build.make").read_text()
        )
        includes = ["-I" + str(source / "include"), "-I" + str(source / "src"),
                    "-I" + str(build)]
        for header in ("_builder.h", "lib.h"):
            path = source / "src/platform/python" / header
            outputs = [
                subprocess.run(
                    [compiler, "-E", "-fno-inline", "-P", *flags, *includes, str(path)],
                    check=True, capture_output=True, text=True,
                ).stdout
                for flags in (native_flags, cffi_flags)
            ]
            left, right = map(normalized, outputs)
            if left != right:
                difference = "\n".join(difflib.unified_diff(
                    left, right, fromfile="native/" + header, tofile="cffi/" + header,
                ))
                raise ValueError(f"Native/cffi header mismatch:\n{difference}")
        report = {
            "schema_version": 1, "status": "pass",
            "native_flags": native_flags, "cffi_flags": cffi_flags,
            "headers": ["_builder.h", "lib.h"],
            "ignored_libc_declarations": ["__sysconf"],
        }
        with args.output.open("x") as stream:
            json.dump(report, stream, indent=2)
            stream.write("\n")
        print("Native/cffi header surfaces match")
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Header safety check failed: {error}", file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            print(error.stderr, file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
