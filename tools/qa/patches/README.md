# mGBA Python binding repairs

`mgba-python.patch` targets the official
[`mgba-emu/mgba` source at `fd9dc5c2cbf234365068855851107e762fc6211b`](https://github.com/mgba-emu/mgba/tree/fd9dc5c2cbf234365068855851107e762fc6211b).
It is a downstream patch, not an upstream release or a separately maintained
wrapper. The source retains its MPL-2.0 license.

The patch changes two files:

- `src/platform/python/core.c`: zero-initialize callbacks. Upstream initializes
  only some function pointers after `malloc`; a later callback invocation can
  otherwise jump through an uninitialized pointer.
- `src/platform/python/CMakeLists.txt`: preprocess cffi declarations with the
  actual native feature definitions, and resolve the runtime library from the
  private installation prefix instead of a disposable build directory.

Normal FFmpeg support must be enabled. This makes upstream's EReader functions
available without patching their headers or duplicating their structure layouts.
Qt, SDL, OpenGL, and scripting are not required.

Apply only to a clean checkout of the exact pin. Check applicability before
applying, and fail rather than silently dropping a patch:

```sh
git -C "$MGBA_SOURCE" apply --check "$REPO/tools/qa/patches/mgba-python.patch"
git -C "$MGBA_SOURCE" apply "$REPO/tools/qa/patches/mgba-python.patch"
```

The static ABI gate does not import or execute mGBA:

```sh
python tools/qa/check_abi.py \
  --native "$PREFIX/lib/libmgba.so.0.11.0" \
  --binding "$PYTHON_BUILD/mgba/_pylib.abi3.so" \
  --output abi-report.json
```

It requires DWARF in both actual binaries and compares complete structure sizes,
member offsets, and bitfield locations. Missing or conflicting layouts fail the
check. Witness objects alone are not accepted as proof of the shipped ABI.

Before changing the pin or compiler options, review whether upstream has fixed
these defects and remove patches that are no longer needed. Compare native and
binding structure layouts, shared-library resolution, and symbol availability
before executing an emulator test. Rendering alone does not validate checkpoint
save-data/RTC restoration or deterministic continuation.

Record the upstream commit and patch hash in runtime provenance. Do not publish
the failed experimental builds, ROMs, saves, or checkpoints.
