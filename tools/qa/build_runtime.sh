#!/usr/bin/env bash
set -euo pipefail

if [[ $# != 4 ]]; then
    printf 'Usage: bash build_runtime.sh SOURCE BUILD PREFIX VENV_PYTHON\n' >&2
    exit 64
fi

qa_dir="$(cd "$(dirname "$0")" && pwd)"
source_dir="$(realpath "$1")"
build_dir="$(realpath -m "$2")"
prefix="$(realpath -m "$3")"
python="$(realpath -ms "$4")"
pin=fd9dc5c2cbf234365068855851107e762fc6211b
patch="$qa_dir/qa/mgba-python.patch"

if [[ "$(git -C "$source_dir" rev-parse HEAD)" != "$pin" ]]; then
    printf 'mGBA source is not at the recorded pin\n' >&2
    exit 3
fi
if [[ -n "$(git -C "$source_dir" status --porcelain)" || -e "$build_dir/CMakeCache.txt" ]]; then
    printf 'Use a clean pinned source checkout and a fresh build directory\n' >&2
    exit 3
fi
mkdir -p "$prefix/share/qa"
cp "$patch" "$prefix/share/qa/mgba-python.patch"
cp "$qa_dir/requirements.lock" "$prefix/share/qa/requirements.lock"
git -C "$source_dir" apply --check "$patch"
git -C "$source_dir" apply "$patch"

include="$("$python" -c 'import sysconfig; print(sysconfig.get_path("include"))')"
library="$("$python" -c 'import sysconfig; from pathlib import Path; print(Path(sysconfig.get_config_var("LIBDIR")) / sysconfig.get_config_var("LDLIBRARY"))')"
test -f "$library"
export PIP_NO_INDEX=1
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PIP_NO_BUILD_ISOLATION=1

cmake -G "Unix Makefiles" -S "$source_dir" -B "$build_dir" \
    -DCMAKE_BUILD_TYPE=RelWithDebInfo -DCMAKE_INSTALL_PREFIX="$prefix" \
    -DBUILD_PYTHON=ON -DBUILD_SHARED=ON -DBUILD_STATIC=OFF \
    -DM_CORE_GBA=ON -DM_CORE_GB=ON \
    -DBUILD_QT=OFF -DBUILD_SDL=OFF -DBUILD_GL=OFF \
    -DBUILD_GLES2=OFF -DBUILD_GLES3=OFF -DBUILD_HEADLESS=OFF \
    -DBUILD_EXAMPLE=OFF -DBUILD_PERF=OFF -DBUILD_TEST=OFF \
    -DBUILD_SUITE=OFF -DBUILD_CINEMA=OFF -DBUILD_LIBRETRO=OFF \
    -DBUILD_DOCGEN=OFF -DBUILD_MAINTAINER_TOOLS=OFF \
    -DENABLE_SCRIPTING=OFF -DUSE_LUA=OFF -DUSE_FFMPEG=ON \
    -DUSE_DISCORD_RPC=OFF -DUSE_EDITLINE=OFF -DUSE_EPOXY=OFF \
    -DUSE_LIBZIP=OFF -DUSE_MINIZIP=OFF -DUSE_LZMA=OFF \
    -DUSE_SQLITE3=OFF -DUSE_JSON_C=OFF -DUSE_FREETYPE=OFF \
    -DUSE_PNG=ON -DUSE_ZLIB=ON -DUSE_ELF=OFF \
    -DUSE_PYTHON_VERSION=3.12 -DPYTHON_EXECUTABLE="$python" \
    -DPYTHON_INCLUDE_DIR="$include" -DPYTHON_LIBRARY="$library" \
    2>&1 | tee "$prefix/share/qa/configure.log"
grep -qE '(^| )-DUSE_FFMPEG( |$)' "$build_dir/CMakeFiles/mgba.dir/flags.make"
cmake --build "$build_dir" --parallel "$(nproc)" \
    2>&1 | tee "$prefix/share/qa/build.log"
cmake --install "$build_dir" 2>&1 | tee "$prefix/share/qa/install.log"

package="$("$python" - "$build_dir" <<'PY'
from pathlib import Path
import shutil
import sys
import sysconfig
packages = list((Path(sys.argv[1]) / "python").glob("lib.*/mgba"))
if len(packages) != 1:
    raise SystemExit("Expected one built mGBA Python package")
destination = Path(sysconfig.get_path("purelib")) / "mgba"
if destination.exists():
    raise SystemExit("Refusing to replace an existing mGBA Python package")
shutil.copytree(packages[0], destination)
print(destination)
PY
)"

"$python" "$qa_dir/check_headers.py" --source "$source_dir" --build "$build_dir" \
    --output "$prefix/share/qa/header-check.json"
"$python" "$qa_dir/check_abi.py" \
    --native "$prefix/lib/libmgba.so.0.11.0" \
    --binding "$package/_pylib.abi3.so" \
    --output "$prefix/share/qa/abi-check.json" \
    | tee "$prefix/share/qa/abi-check.log"
readelf -d "$package/_pylib.abi3.so" > "$prefix/share/qa/extension-dynamic.txt"
readelf -d "$prefix/lib/libmgba.so.0.11.0" > "$prefix/share/qa/library-dynamic.txt"
cp "$build_dir/CMakeCache.txt" "$prefix/share/qa/CMakeCache.txt"
mkdir -p "$prefix/share/licenses/mgba" "$prefix/share/qa/modified-source"
cp "$source_dir/LICENSE" "$prefix/share/licenses/mgba/LICENSE"
cp "$source_dir/src/platform/python/core.c" "$prefix/share/qa/modified-source/core.c"
cp "$source_dir/src/platform/python/CMakeLists.txt" \
    "$prefix/share/qa/modified-source/CMakeLists.txt"

PYTHONPATH="$qa_dir" "$python" - "$package" "$prefix" "$pin" <<'PY'
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import sys
from qa.runtime import package_hash
package, prefix, pin = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()
metadata = {
    "schema_version": 1,
    "status": "built-static-checked",
    "environment": os.environ.get("QA_BUILD_ENVIRONMENT", "ad-hoc"),
    "mgba_pin": pin,
    "source_url": "https://github.com/mgba-emu/mgba/tree/" + pin,
    "patch_sha256": digest(prefix / "share/qa/mgba-python.patch"),
    "library_path": str(prefix / "lib/libmgba.so.0.11.0"),
    "library_sha256": digest(prefix / "lib/libmgba.so.0.11.0"),
    "pylib_sha256": digest(package / "_pylib.abi3.so"),
    "package_sha256": package_hash(package),
    "python_packages": {name: version(name) for name in (
        "cffi", "cached-property", "Pillow", "pyelftools", "pycparser", "setuptools",
    )},
}
for path in (package / "_qa_runtime.json", prefix / "share/qa/runtime.json"):
    with path.open("x") as stream:
        json.dump(metadata, stream, indent=2, sort_keys=True)
        stream.write("\n")
PY
