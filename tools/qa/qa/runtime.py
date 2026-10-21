import ctypes
from dataclasses import dataclass
import hashlib
from importlib.metadata import PackageNotFoundError, version
from importlib.resources import files
from pathlib import Path
import re
import resource
import sys
import sysconfig

from . import __version__
from .artifacts import content_id, read_json, sha256
from .errors import QAError


@dataclass(frozen=True)
class Runtime:
    metadata: dict
    identity: dict
    package: Path
    library: Path
    extension: Path


def package_hash(package: Path) -> str:
    sources = {
        path.relative_to(package).as_posix(): sha256(path)
        for path in sorted(package.rglob("*.py"))
    }
    if not {"__init__.py", "core.py", "gba.py", "image.py", "vfs.py"} <= sources.keys():
        raise QAError("The official mGBA wrapper sources are incomplete")
    return content_id(sources)


def load_runtime() -> Runtime:
    package = Path(sysconfig.get_path("purelib")) / "mgba"
    metadata = read_json(package / "_qa_runtime.json")
    if (type(metadata.get("schema_version")) is not int
            or metadata["schema_version"] != 1
            or metadata.get("status") != "built-static-checked"
            or metadata.get("environment") not in ("ad-hoc", "image")):
        raise QAError("The private mGBA runtime lacks valid build/static-check provenance")
    patch = files(__package__).joinpath("mgba-python.patch").read_bytes()
    lines = patch.decode("utf-8").splitlines()
    if not lines:
        raise QAError("The runtime patch provenance is empty")
    base = re.fullmatch(
        r"Base: mgba-emu/mgba ([0-9a-f]{40})", lines[0]
    )
    if (base is None or metadata.get("mgba_pin") != base.group(1)
            or metadata.get("patch_sha256") != hashlib.sha256(patch).hexdigest()):
        raise QAError("Runtime source/patch differs from this QA package; rebuild the QA runtime")
    library_name = metadata.get("library_path")
    if not isinstance(library_name, str) or not Path(library_name).is_absolute():
        raise QAError("Runtime library path is invalid")
    library = Path(library_name).resolve()
    extension = package / "_pylib.abi3.so"
    if (sha256(library) != metadata.get("library_sha256")
            or sha256(extension) != metadata.get("pylib_sha256")):
        raise QAError("Private runtime library or extension integrity mismatch")
    if package_hash(package) != metadata.get("package_sha256"):
        raise QAError("Private Python wrapper sources changed or lack provenance")
    packages = metadata.get("python_packages")
    if not isinstance(packages, dict) or not packages:
        raise QAError("Runtime Python dependency provenance is missing")
    try:
        for name, expected in packages.items():
            if version(name) != expected:
                raise QAError(f"Runtime dependency changed since build: {name}")
    except PackageNotFoundError as error:
        raise QAError(f"Runtime dependency is missing: {error}") from error
    identity = {
        key: metadata[key] for key in
        ("mgba_pin", "patch_sha256", "library_sha256", "pylib_sha256",
         "package_sha256", "python_packages")
    }
    identity.update({
        "harness_version": __version__, "python_abi": sys.implementation.cache_tag,
        "bios": "hle", "skip_bios": False, "rtc": "fixed-utc",
        "savedata_policy": "rtc-core+read-masked-flash-v1",
    })
    return Runtime(metadata, identity, package.resolve(), library, extension.resolve())


def guard_native_process() -> None:
    if sys.platform != "linux":
        raise QAError("The QA emulator host currently requires Linux")
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int] + [ctypes.c_ulong] * 4
    libc.prctl.restype = ctypes.c_int
    if libc.prctl(4, 0, 0, 0, 0) != 0 or libc.prctl(3, 0, 0, 0, 0) != 0:
        raise QAError("Cannot disable child core dumps; native import refused")
    libc.setvbuf.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t]
    libc.setvbuf.restype = ctypes.c_int
    stdout = ctypes.c_void_p.in_dll(libc, "stdout")
    if libc.setvbuf(stdout, None, 1, 0) != 0:
        raise QAError("Cannot line-buffer native stdout")


def verify_loaded_paths(runtime: Runtime, extension: str) -> None:
    if Path(extension).resolve() != runtime.extension:
        raise QAError("A different Python mGBA extension was imported")
    mapped = {
        Path(parts[5]).resolve()
        for line in Path("/proc/self/maps").read_text().splitlines()
        if len(parts := line.split(maxsplit=5)) == 6
        and Path(parts[5]).name.startswith("libmgba.so")
    }
    if mapped != {runtime.library}:
        raise QAError("The loaded libmgba does not match the verified private runtime")
