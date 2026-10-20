# In-game QA

This harness runs official mGBA Python bindings in-process, drives real game
inputs, and checks RAM-backed assertions. It is separate from `make check` and
does not change gameplay configuration. Video and screenshots are evidence,
not pass/fail oracles.

## Prerequisites

Use the rebuilt devcontainer described in [DEVCONTAINER.md](../../DEVCONTAINER.md).
It provides `qa`, a private `/opt/qa-venv`, FFmpeg/ffprobe, and a pinned mGBA build.
The two small downstream binding repairs and their maintenance requirements are
documented in [patches/README.md](patches/README.md).

No desktop emulator, ROM BIOS file, port, server, nested container engine, or
manual emulator operator is needed. The existing native test runner is unchanged.

## Build the exact source you intend to test

```sh
qa build
```

This refuses a dirty checkout instead of silently ignoring changes. Choose
explicitly when necessary:

```sh
# Committed code only, excluding local edits:
qa build --ref HEAD

# Include tracked working-tree edits and deletions:
qa build --include-dirty

# Also include individually selected untracked source files:
qa build --include-dirty --include-untracked src/tempered/example.c
```

Dirty snapshots are based on the invoking HEAD. They use a separate Git index
and detached worktree, do not run checkout hooks, and never reset, stage, or
overwrite the invoking checkout. Unselected untracked and ignored files are
excluded. The original index, branch, and files remain intact.

Each build emits JSON containing a `bundle` directory path and a separate
content-based `bundle_id`. Use the **path**, not the hash, below. The bundle
contains matching ROM, ELF, symbols, generated map constants, build log, and
integrity manifest from one fresh non-release `DINFO=1` build. No existing ROM
or object files in the invoking checkout are reused.

## Run and inspect

Set `BUNDLE` to the `bundle` path returned by the build:

```sh
qa run smoke --bundle "$BUNDLE"
```

The smoke flow boots, waits for a ready title screen, uses SELECT Quickstart,
waits for the opening truck sequence to finish, and opens/closes the normal
Start menu. It gates on game callbacks, tasks, palette fades, script/control
locks, and the map from the frozen bundle. It does not require catching a
one-frame initialization callback.

The command emits JSON with `run_dir`, `manifest`, `exit_code`, and `verdict`.
When a Docker host image ID is available, optionally set `QA_IMAGE_ID` to its
`sha256:...` identifier. This is an unverified, operator-supplied label recorded
in subsequent manifests, not proof that the image was built or validated.
Image verification still requires actual command results from the rebuilt image.
The run directory retains:

- `manifest.json`: case expectations, deadlines, observations, runtime/build
  identities, input frame indices, evidence hashes, and final verdict.
- `events.jsonl`, `native.log`, `ffmpeg.log`: input/case events and diagnostics.
- `video.mp4`: silent H.264/yuv420p, 4x nearest-neighbor 960x640 by default.
- Key PNGs: native 240x160 plus an `@4x.png` copy.
- `final.state`: a private, self-contained checkpoint after a successful flow.

The native frame rate is derived from the emulator's frequency/frame cycles:
`262144/4389` fps for this GBA core. `--interval N` records every Nth frame at
that rate divided by N. Every emulated frame still passes through the same
input/wait/recording path. The last sampled frame displays for a complete sample
interval. `--scale` changes video magnification; screenshot copies remain 4x.
Frame count, rate, codec, dimensions, pixel format, and duration are verified
with ffprobe before the MP4 is accepted.

## Stateless stepping

Set `RUN_DIR` to a successful run's returned directory:

```sh
qa step --bundle "$BUNDLE" --state "$RUN_DIR/final.state" \
  --tap START --wait 30 \
  --out qa-artifacts/steps/menu.state \
  --shot qa-artifacts/steps/menu.png
```

Each step starts a fresh bounded child process. Multiple `--tap` options execute
in order; chords use `+`, such as `R+START`. A tap includes at least two pressed
and two released frames, and requires game KEYINPUT polling plus matching raw
held-key state. Both emulator and game keys must be released at checkpoints.
Inputs and all extra polling frames are recorded; there are no silent retries.

Outputs must be under this checkout's `qa-artifacts/` and are never overwritten.
A step must advance a real frame before capturing a post-load image; it cannot
report the stale framebuffer left by a state load. Step success means the
requested operation and evidence succeeded, not that an arbitrary gameplay
feature has been tested.
Step case observations also include raw menu/field control inputs and interpreted
readiness flags, so a follow-up flow can assert the intended UI transition rather
than treating deterministic stepping alone as behavioral proof.

### What a `.state` file contains

It is a bounded, uncompressed ZIP, not a desktop-emulator `.sgm` file:

```text
core.state       native mGBA state with RTC, without embedded savedata/host metadata
save.bin         exactly 128 KiB of cloned cartridge save bytes
manifest.json    schema, build/runtime identities, component hashes, clock and frame
```

The loader checks every member and hash, then installs an in-memory save **before**
loading core state. This preserves flash-bank/controller state without another
upstream loader patch. Anchors and temporary masks stay private and read-only;
normal `.sav` files are not modified. Initial save inputs with RTC trailers are
rejected rather than silently changing clock offsets.

Checkpoints require the matching build bundle and exact runtime identity.
They do not include the ROM or ELF. A runtime rebuild or changed bundle can
invalidate old checkpoints. The checkpoint's fixed UTC clock wins; a conflicting
`--epoch` is an error. New runs default to `2026-01-01T12:00:00Z`; `--epoch`
accepts ISO-8601 dates in 2000-2099, treating a missing timezone as UTC.

## Verdicts and failure handling

| Exit | Meaning |
|---|---|
| 0 | All requested assertions and evidence passed |
| 1 | An assertion or post-event deadline failed |
| 2 | A prerequisite was unreachable, or a case was not run |
| 3 | Infrastructure, integrity, binding, encoder, or worker failure |
| 64 | Invalid command usage |

Infrastructure errors take priority over behavioral failure, then inconclusive.
Manifests begin as `incomplete`; crashes cannot leave a successful verdict.
Behavioral failure may still have valid, publishable evidence. An encoder/probe
failure cannot be turned into a pass just because RAM assertions succeeded.

Native execution uses a timed subprocess, core dumps disabled for that child,
line-buffered native output, and callback-exception checks. Do not install the
upstream Python logger at this pin: its opaque `va_list` path is unsupported.
The harness captures the native C logger verbatim instead. Stop on a native
crash, preserve diagnostics, and investigate; do not run a crash retry loop.

The deliberate control flows `negative-wrong-callback`, `negative-unreachable`,
and `negative-unknown-symbol` are expected to exit 1, 2, and 3 respectively.
They test harness failure reporting, not game behavior.

## Development and publication

Pure tests can run without importing mGBA:

```sh
/opt/qa-venv/bin/python -m unittest discover -s tools/qa/tests -v
QA_TEST_BUNDLE="$BUNDLE" /opt/qa-venv/bin/python \
  -m unittest discover -s tools/qa/tests -v
```

The second form also checks real DWARF projections against a verified bundle.
Agents run these commands, game builds, `make -j$(nproc)`, and
`make check -j$(nproc)` in background task subagents, as repository instructions
require. A source-built `ad-hoc` runtime is **not** final-image verification.

Retain artifacts until explicitly cleaned. Inspect exact owned paths before
removing registered source worktrees; never reset the user's checkout, prune
unrelated worktrees, or blanket-delete artifact roots.

Publish only verified MP4/PNGs and sanitized manifests/result logs, using the
existing `github-pr-media` skill when requested. Never upload a whole run or
bundle directory: ROMs, ELF files, source snapshots, save bytes, and `.state`
archives are private and must not be committed or attached to a PR.

AI battle scenarios, sampling/statistics, audio recording, visual/feel judgments,
and a real-time human viewer are outside this first implementation.
