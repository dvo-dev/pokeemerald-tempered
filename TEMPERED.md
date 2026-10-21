# pokeemerald-tempered

`pokeemerald-tempered` is built on [pokeemerald-expansion](https://github.com/rh-hideout/pokeemerald-expansion).
The current goal is an enhanced "Emerald Plus" experience, which will serve as the foundation for larger gameplay systems added to this project later.
This document lists every change made to upstream's defaults and the reasoning behind each one.

## Design goals

- **Modern mechanics:** battle, Pokémon, and item mechanics follow upstream's latest-generation defaults unless noted below.
- **Quality of life:** upstream features that make the game smoother to play are turned on where they fit Emerald.
- **Expert difficulty:** the game is tuned for the intended **Expert** experience.
  A **Normal** (vanilla) mode and a way to choose between them are planned for later; see [Difficulty](#difficulty).

## How changes are marked

Settings changed from upstream keep their original comment, prefixed with `[tempered]` and the upstream value:

```c
#define I_REUSABLE_TMS  TRUE  // [tempered] upstream: FALSE. In Gens5-8, TMs are reusable. ...
```

New code lives in `src/tempered/` and `include/tempered/`.
Where upstream code has to call it, the change is kept to a line or two, each marked with a `// [tempered]` comment.

## Config review status

| Header | Status |
|--------|--------|
| `ai.h` | Reviewed: 7 changes |
| `battle.h` | Pending |
| `caps.h` | Pending |
| `contest.h` | Pending |
| `debug.h` | Pending |
| `dexnav.h` | Pending |
| `fishing.h` | Pending |
| `follower_npc.h` | Pending |
| `general.h` | Pending |
| `item.h` | Pending |
| `map_preview_screen.h` | Pending |
| `name_box.h` | Pending |
| `overworld.h` | Pending |
| `pokedex_plus_hgss.h` | Pending |
| `pokemon.h` | Pending |
| `pokerus.h` | Pending |
| `quickstart.h` | Pending |
| `save.h` | Pending |
| `species_enabled.h` | Pending |
| `summary_screen.h` | Pending |
| `test.h` | Pending |
| `text.h` | Pending |
| `wild_encounter.h` | Pending |

## Changes

In the tables below, **Difficulty** marks changes that are part of Expert difficulty, and **Save** marks changes that make older save files incompatible.

<details>
<summary><code>ai.h</code>: 7 changes. Trainers heal, switch and use priority moves more reliably.</summary>

Upstream uses chances below 100% for many AI decisions so the AI isn't perfectly predictable. Where we raised a chance, we aimed for about two-thirds: smarter, but still not fully predictable.
The four switching changes only apply to trainers with Smart Switching AI (see [Trainer AI](#trainer-ai)).

| Config | Upstream | Tempered | Motivation | Difficulty | Save |
|--------|----------|----------|------------|------------|------|
| `SHOULD_SWITCH_FREE_TURN_PERCENTAGE` | 50 | 66 | Switching while the player is mid-Fly/Dig or charging a move is nearly free, and it only happens when the AI is losing the matchup and has a good switch-in. | Yes | No |
| `SHOULD_SWITCH_BADLY_POISONED_PERCENTAGE` | 50 | 66 | Toxic damage keeps growing and switching resets it, so switching is usually the right call. | Yes | No |
| `SHOULD_SWITCH_CURSED_PERCENTAGE` | 50 | 66 | Curse takes a quarter of max HP every turn and switching removes it. | Yes | No |
| `SHOULD_SWITCH_INTIMIDATE_PERCENTAGE` | 25 | 33 | A little more Intimidate pressure from bosses, kept low to avoid tedious switching back and forth. | Yes | No |
| `SHOULD_RECOVER_CHANCE` | 50 | 100 | When it's safe, the AI always heals below 60% HP instead of half the time. Healing to survive a KO was already guaranteed. | Yes | No |
| `PRIORITIZE_LAST_CHANCE_CHANCE` | 50 | 100 | When the AI is slower and about to be knocked out, it always uses a priority move, rather than a stronger move it won't live to use. | Yes | No |
| `LAST_MON_PREFERS_NOT_SACRIFICE` | FALSE | TRUE | A trainer's last Pokémon avoids Explosion or Final Gambit, which would hand the player the win. | Yes | No |

One upstream test, *"AI's Explosion scoring handles multiple move effects and the Explosion defense config"* in `test/battle/ai/ai.c`, now skips when `LAST_MON_PREFERS_NOT_SACRIFICE` is TRUE, like the three similar tests next to it.

**Known limitation:** in double battles, upstream treats a trainer with no Pokémon left in reserve as being on its "last Pokémon" even while its partner is still on the field.
In vanilla this affects Explosion users in the Gym Leader double-battle rematches (Roxanne, Wattson, Flannery, Tate & Liza).
It has little effect in practice: upstream never counts Explosion as a KO in double battles, and Explosion also hits the AI's own partner.

</details>

## Flags and vars in use

| Flag/Var | Used by | Purpose |
|----------|---------|---------|

## Code changes

Changes outside the config headers.

### Agent QA tooling

The [QA terminology reference](tools/qa/README.md#terminology-and-upstream-references)
explains ABI, DWARF, CFFI, and other terms below, with links to their upstream
documentation and specifications.

| Configuration | Change | Motivation | Difficulty | Save |
|---------------|--------|------------|------------|------|
| `.gitignore` | Ignore `/qa-artifacts/` | Keep isolated QA source/build snapshots and emulator evidence local without deleting them automatically. | No | No |
| `tools/qa/qa/mgba-python.patch` | Pin and repair the official mGBA Python bindings | Zero-initialize unused callbacks, match the native ABI, and use a private runtime library path without maintaining a separate wrapper or changing game code. | No | No |
| `.devcontainer/Dockerfile`, `devcontainer.json`, `.dockerignore` | Build a private pinned QA runtime in a separate stage with a narrowly allowlisted context | Provide official mGBA bindings, recording tools, and static ABI/provenance checks without replacing the native test runner, exposing credentials to the build, or requiring nested Docker. | No | No |
| `tools/qa/requirements.lock`, `pyproject.toml` | Isolate and pin QA Python dependencies | Keep the emulator, image, and DWARF tooling reproducible without installing into system Python. | No | No |
| `.devcontainer/Dockerfile`, `tools/qa/check_image.py` | Include `libpython3.12t64` defensively and check the private native import in the final stage with core dumps disabled | Cover the embedding runtime's shared-library requirements and fail an incomplete image build early without requiring Dockerfile heredoc support; actual rebuilt-image verification remains a separate gate. | No | No |
| `tools/qa/pyproject.toml`, `qa/mgba-python.patch`, `.dockerignore` | Ship the unchanged binding patch as package data and allow its new path into the image build context | Let installed-wheel and checkout entry points verify the same runtime provenance without depending on an adjacent source checkout. | No | No |
| `tools/qa/build_runtime.sh` | Check install-prefix and virtual-environment destinations before any runtime writes | Reject reused destinations without overwriting a previously usable library or its provenance. | No | No |

### Trainer AI

<details>
<summary>Bosses and strong trainers use Smart Trainer AI; everyone else uses Basic Trainer AI.</summary>

In vanilla Emerald, most route trainers only avoid obviously bad moves ("Check Bad Move"), and only Gym Leaders, rivals, Cooltrainers and a few others also pick effective moves and go for KOs ("Basic Trainer").
Expansion's smarter AI ("Smart Trainer") isn't given to any vanilla trainer.

| Trainer classes | AI | Difficulty | Save |
|-----------------|----|------------|------|
| Gym Leader, Elite Four, Champion, Rival, Team Aqua/Magma Leader and Admin, Cooltrainer, Expert, Dragon Tamer, Old Couple, Winstrate, Pokémon Ranger | Smart Trainer: switches out of bad matchups, picks better switch-ins, doesn't fall for PP stalling, and knows the player's moves, abilities and items | Yes | No |
| Every other class | Basic Trainer: picks effective moves and goes for KOs | Yes | No |

- The extra AI is added on top of each trainer's own AI, so per-trainer behaviour such as some Gym Leaders' "Risky" AI is kept.
- Every trainer in a smart zone uses Smart Trainer AI, whatever their class. Currently: Victory Road. Zones are listed in `sSmartTrainerZones`.
- In-game partners follow the same rule, so Steven (listed as a Rival) is a Smart Trainer in the Mossdeep Space Center battle, matching Maxie and Tabitha.
- Battle Frontier, Trainer Hill and Secret Base battles keep their own fixed AI.

Code: `src/tempered/trainer_ai.c`, called from `GetAiFlags` in `src/battle_ai_main.c`.
The class rule is unit-tested in `test/tempered/trainer_ai.c`. Upstream's AI tests replay recorded battles, which use their own AI flags, so they don't exercise this code.

</details>

## Difficulty

The current settings implement **Expert** difficulty.
A future **Normal** mode would undo or switch off each change marked as a Difficulty change above.
This table notes how each Expert setting could become switchable in-game.

| Expert setting | How it's set now | How Normal mode could switch it off |
|----------------|------------------|-------------------------------------|
| AI switching chances (`ai.h`: free turn, Toxic, Curse, Intimidate) | Config values | All switch chances are read through `GetSwitchChance` in `src/battle_ai_switch.c`, which upstream marks as the place to vary them by difficulty; return the upstream values in Normal mode. |
| AI healing, priority and self-KO choices (`ai.h`: `SHOULD_RECOVER_CHANCE`, `PRIORITIZE_LAST_CHANCE_CHANCE`, `LAST_MON_PREFERS_NOT_SACRIFICE`) | Config values | Read directly where used in `src/battle_ai_main.c` and `src/battle_ai_util.c`; replace each use with a small helper that returns the upstream value in Normal mode. |
| Smarter trainer AI | `Tempered_GetExtraTrainerAiFlags` in `src/tempered/trainer_ai.c` | Return no extra flags in Normal mode, which restores each trainer's vanilla AI. |

### Planned for later

- Normal (vanilla) difficulty mode.
- Choosing a difficulty in-game, e.g. at the start of a new game.
- Expert-specific trainer teams, using upstream's `B_VAR_DIFFICULTY`.
- Trainer AI knowledge: Smart Trainer AI currently knows all of the player's moves, abilities and items ("Omniscient").
  Consider only what it has seen, educated guesses (upstream's Assume STAB, Assume Status Moves and Weigh Ability Prediction AI), or knowing a random subset.
- Whether the AI should switch out of matchups it would lose (`SHOULD_SWITCH_LOSES_1V1_PERCENTAGE`, currently 0).
