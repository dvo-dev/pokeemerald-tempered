import ctypes
from pathlib import Path
import resource


def main():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int] + [ctypes.c_ulong] * 4
    libc.prctl.restype = ctypes.c_int
    if libc.prctl(4, 0, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "Cannot disable native import core dumps")
    import mgba._pylib

    expected = Path("/opt/mgba/lib/libmgba.so.0.11.0").resolve()
    mapped = {
        Path(parts[5]).resolve()
        for line in Path("/proc/self/maps").read_text().splitlines()
        if len(parts := line.split(maxsplit=5)) == 6
        and Path(parts[5]).name.startswith("libmgba.so")
    }
    if mapped != {expected}:
        raise RuntimeError("Native import did not use the private mGBA library")


if __name__ == "__main__":
    main()
