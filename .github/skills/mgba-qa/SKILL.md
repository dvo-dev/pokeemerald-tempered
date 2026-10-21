---
name: mgba-qa
description: Run deterministic in-game QA with official mGBA Python bindings, RAM oracles, isolated builds, checkpoint stepping, and verified video evidence.
---

# In-game QA with mGBA

Use this skill for unattended, manual-style game interaction or reproducible
in-game evidence. Read `tools/qa/README.md` and `DEVCONTAINER.md` first.
`make check` remains a separate native unit-test system.

## Prerequisites and boundaries

- Use the rebuilt devcontainer's `qa` and private Python runtime. Confirm whether
  manifests label the environment `image` or `ad-hoc`; do not claim image
  verification from an ad-hoc source build.
- Preserve and verify Copilot session, planning-file, and source-worktree backups
  before any container rebuild. Never rebuild automatically or mount a host
  engine socket. An overlay-only worktree is not a durable host checkout.
- Run builds/tests in background task subagents. Honor the session's requested
  reviewer/model rather than inventing a different review policy.
- Do not change game code/configuration or `.github/copilot-instructions.md`
  just to make a QA case pass. Ask before adding to that instructions file.
- Keep runtime dependency repairs minimal, pinned, checked, and documented under
  `tools/qa/patches/`; do not silently replace the backend or introduce a wrapper.

## Build and choose sources explicitly

Use `qa build` for a clean committed checkout. If dirty, choose one:

- `qa build --ref HEAD` intentionally excludes local edits.
- `qa build --include-dirty` freezes tracked edits/deletions.
- Add `--include-untracked path/to/file` for each selected new source file.

Never overwrite or reset working files, use existing checkout ROM/object files,
or run destructive scenario-reset scripts. The builder uses a separate index
and detached worktree. Keep the returned `bundle` path and `bundle_id`; do not
substitute the hash for the path.

## Execute and interpret

Run `qa run smoke --bundle "$BUNDLE"`. Read its JSON result and `manifest.json`;
never infer a pass from process startup, a screenshot, or an MP4 alone.
The smoke route is boot -> ready title task -> SELECT Quickstart -> controllable
truck -> Start menu -> return to field.

RAM readiness includes task/callback identity, palette fade, script/control
locks, and map state. Normalize Thumb bits for function comparisons. Use ELF
symbols and DWARF field/array/bitfield metadata, not remembered offsets.
This build's DWARF units are named `<stdin>`; source-qualified names such as
`main.c::gMain.callback2` are resolved through line-table provenance. Structure
padding is real: ten byte fields do not necessarily mean a ten-byte structure.

New cases must declare precondition, event, assertion, and frame deadline.
Use bounded waits and stable readiness; do not depend on one-frame initialization
callbacks. Cases left unexecuted are `not-run`, not passes.

| Exit | Interpretation |
|---|---|
| 0 | Assertions and requested evidence passed |
| 1 | Behavioral assertion/post-event deadline failed |
| 2 | Setup unreachable or cases not run |
| 3 | Harness/infrastructure/integrity failure |
| 64 | Bad usage |

Infrastructure failure overrides behavioral failure; behavioral failure overrides
inconclusive. Inspect both errors and unexecuted cases. An `incomplete` manifest
is never a pass. Control flows named `negative-*` intentionally return nonzero.

## Inputs, clock, and checkpoints

Use `qa step --bundle "$BUNDLE" --state "$STATE" --tap A --wait 30 \
--out qa-artifacts/steps/next.state --shot qa-artifacts/steps/next.png`.

Each command uses a fresh process, not a socket/server. Repeat `--tap` for
successive actions; `+` expresses a chord. Inputs include polled press/release
gaps so consecutive taps produce new edges. Account for every advanced frame.
Do not capture the stale post-load framebuffer without advancing.

The RTC is fixed inside mGBA with `TZ=UTC`, not by freezing the host process.
The default epoch is `2026-01-01T12:00:00Z`; record any explicit `--epoch`.
Game local time includes save offsets: use raw RTC/game observations to prove
clock control, not tint alone.

A checkpoint is one `.state` ZIP with native RTC-only `core.state`, cloned
`save.bin`, and a hashed identity manifest. Save bytes are installed before
core state, preserving controller state. Keys are released at its boundaries.
Do not load raw desktop states, change its epoch, mix runtime/bundle identities,
or bypass integrity failures. Never export/reload the in-memory anchor or RTC
save trailer; the save policy is a permanently read-only temporary mask.

## Evidence and cleanup

Default video is silent H.264/yuv420p, 4x nearest-neighbor 960x640. Native PNGs
are 240x160 with 960x640 copies. The true rate is emulator frequency/frame cycles,
divided by `--interval`; ffprobe must verify actual counts/rate/duration/format.
Pixels and video are review evidence, not statistical or behavioral oracles.

Native core execution is bounded and non-dumpable. If a signal, callback
traceback, or timeout occurs, stop and retain the exact logs; do not retry in a
loop or hide it by suppressing error text. Do not use this pin's Python logger
because its `va_list` wrapper is unsupported; native C output is captured.

Everything stays under gitignored `qa-artifacts/` until explicit cleanup.
Never remove broad roots, unrelated worktrees, or user saves. Only publish
verified MP4/PNG files and sanitized result logs/manifests, and invoke the
`github-pr-media` skill for requested PR uploads. Never commit/upload ROMs,
ELFs, source snapshots, `.sav` files, checkpoints, or whole artifact folders.

This first version does not validate AI tuning, statistical probabilities,
battle setup helpers, visual quality/feel, or audio. Menu/flow code may need
updating when upstream UI or symbols change.
