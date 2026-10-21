# In-game QA

This harness runs official mGBA Python bindings in-process, drives real game
inputs, and checks RAM-backed assertions. It is separate from `make check` and
does not change gameplay configuration. Video and screenshots are evidence,
not pass/fail oracles.

## How an agent uses the harness

The harness is an emulator controller plus a regression-test foundation, not an
autonomous player. The agent decides what to try and how to interpret the
results. It can use either of these modes without changing the backend:

- **Agent-guided exploration:** issue `qa step`, inspect its observations and
  screenshots, then choose the next bounded action. A new Python flow is not
  required for every route or interaction.
- **Scripted regression:** run an implemented flow such as `qa run smoke`.
  Python performs the actions and evaluates predefined assertions without
  asking the agent to decide after every action.

The agent-guided loop is:

```text
Agent chooses inputs and a frame budget
    |
    v
qa step: verify checkpoint -> restore -> input/advance -> capture/save
    |
    +-- failure -> retain diagnostics; inspect the error before proceeding
    |
    `-- success [outputs]: next checkpoint, RAM observations, PNG/MP4
                              |
                              v
                       Agent inspects results
                              |
                              +-- goal observed -> report evidence and limits
                              `-- more needed -> choose the next bounded step
```

This is frame-stepped, not wall-clock play. `--wait 30` advances 30 emulated
frames after the taps; it does not hold a key or sleep for 30 seconds. Taps also
advance their own press/release frames. Execution speed depends on the host and
recording overhead; videos play back at real game speed. Between commands there
is no running emulator session: the checkpoint preserves the game while the
agent reasons.

An *oracle* is an explicit game-state check, not a special marker inserted into
the game. In a scripted flow, `Session.wait_until` advances one frame and checks
a RAM predicate until it holds for two consecutive frames by default, or a
declared frame deadline expires. For example, menu readiness checks its task,
initialization state, input handler, and palette fade. This loop runs inside
Python, not through one model call per frame. `qa step --wait N` instead advances
a fixed number of frames; it does not accept an arbitrary readiness predicate.
See the [session implementation](qa/emu.py), [CLI](qa/cli.py), and implemented
[smoke predicates and flow](qa/flows/smoke.py).

## Why these constructs exist

These guarantees support unattended, reproducible checks; they are not all
prerequisites for an agent to press buttons and inspect a screenshot.

| Construct | Motivation | Cost or boundary |
|---|---|---|
| Pinned private mGBA/Python runtime | Control the core directly without a desktop operator or command server, and keep native dependencies separate from the game test runner. | Binding repairs and ABI checks need maintenance. This harness requires Python; agent playtesting in general does not. See [patch provenance](patches/README.md). |
| Isolated build bundle | Know exactly which sources produced the ROM, symbols, and type information without disturbing the working checkout. | Build the intended snapshot explicitly; existing checkout binaries are not reused. |
| ELF/DWARF-backed RAM reads | Check actual callbacks, fields, and bitfields without guessing addresses, offsets, or structure padding. | Game-specific predicates still need authoring and can need updates when upstream changes. |
| Fresh processes and complete checkpoints | Let the agent stop, inspect, branch, and resume without managing a live emulator service; preserve save/controller/RTC state as well as RAM. | Checkpoints require the matching bundle and runtime. Generate equivalent starting states for each changed build rather than loading an old checkpoint into a new ROM. |
| Fixed RTC, frame accounting, and input edges | Make timing and replays reproducible, and ensure consecutive button taps are seen as separate presses. | A fixed clock is not statistical coverage; replaying one checkpoint repeats its captured RNG state, not independent trials. |
| Bounded RAM waits and structured verdicts | Distinguish readiness, behavioral failure, unreachable setup, and broken infrastructure instead of hanging or treating partial execution as a pass. | Only declared assertions establish a behavioral pass. Successful stepping alone does not test an arbitrary feature. |
| Verified recordings and retained artifacts | Give reviewers visible evidence and enough diagnostics to investigate a result. | Screenshots/video do not replace assertions; binary build/save/checkpoint artifacts remain private. |

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

# Include tracked working-tree changes, including staged additions/renames:
qa build --include-dirty

# Also include individually selected untracked source files:
qa build --include-dirty --include-untracked src/tempered/example.c
```

Dirty snapshots are based on the invoking HEAD. They use a separate Git index
and detached worktree, do not run checkout hooks, and never reset, stage, or
overwrite the invoking checkout. Unselected untracked files, including ignored
files, are excluded. The original index, branch, and files remain intact.
Newly staged files and rename destinations use their current working-tree
contents, just like modifications to existing tracked files. Files removed from
the invoking index stay excluded even if their working copies remain on disk,
unless explicitly selected with `--include-untracked`.

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

### Worked example: checking a Start-menu change

Suppose a change should preserve opening the Start menu and returning control
to the field. This example uses today's implemented commands, not a proposed
battle API. Run from the repository root after completing the prerequisites.

First build the intended source snapshot using the build section above and set
`BUNDLE` to the returned `bundle` path. Run
`qa run smoke --bundle "$BUNDLE"` and set `RUN_DIR` to the successful smoke run's
`run_dir`. Stop if the build or smoke run fails; inspect the returned manifest
rather than assuming a checkpoint exists.

The smoke flow already asserts menu opening and closing automatically. The
following extra steps demonstrate how an agent explores from its final
controllable-truck checkpoint without writing another Python flow.

Create a fresh output directory because evidence files are never overwritten:

```sh
STEP_DIR=$(mktemp -d qa-artifacts/menu-example.XXXXXX)
qa step --bundle "$BUNDLE" --state "$RUN_DIR/final.state" \
  --tap START --wait 30 \
  --out "$STEP_DIR/menu.state" --shot "$STEP_DIR/menu.png"
```

The step returns a `manifest` path and writes native-size `menu.png` and
enlarged `menu@4x.png` in `STEP_DIR`.
For this build and starting point, `cases[0].game.ready.menu` should be `true`,
`cases[0].game.palette_fade_active` should be `0`, and
`cases[0].observed.keys` and `cases[0].observed.game_keys_raw` should be `0`.
The screenshot should show the open menu. Checking these observations is a
separate decision: a step can exit 0 even when the intended menu transition
did not occur.

Only after confirming that result, continue from the new checkpoint:

```sh
qa step --bundle "$BUNDLE" --state "$STEP_DIR/menu.state" \
  --tap B --wait 5 \
  --out "$STEP_DIR/field.state" --shot "$STEP_DIR/field.png"
```

The second step produces another manifest, `field.png`, and `field@4x.png`.
The observations and screenshots should show the menu closed, with
`cases[0].game.ready.field` equal to `true`,
`cases[0].game.ready.menu` equal to `false`, and both observed key fields equal
to `0`.
The `ready.field` predicate means general overworld field control, not battle
readiness. The smoke flow additionally checks that the map is the opening truck;
other steps can report field readiness on other maps.
An unexpected observation warrants diagnosis, not blindly sending more inputs.

A useful result includes the source bundle, both steps' manifest paths,
observations, and relevant PNG/MP4 evidence. This example supplies menu evidence,
not battle coverage or rebuilt-image verification. Turning a newly explored
route into a reusable regression means encoding its preconditions, actions,
assertions, and frame deadlines in a flow instead of relying on repeated
agent interpretation.

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
Battle regression coverage would additionally need reproducible encounter/party
setup, battle-specific readiness and turn-result predicates, and explicit
assertions such as move choice or HP changes. The controller can supply real
inputs, but it does not discover those conditions or provide a `qa run battle`
or `qa sample` command. Keep precise mechanics tests in the existing native
test suite and use in-game flows for complementary integration evidence.
