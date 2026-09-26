# Calc interface contract

## v2 ROM-backed interface (implementation target)

This section is the shared interface for the workstreams in
`CALC_IMPLEMENTATION_PLAN.md`. The v1 section below describes the existing
Python model only. The current Calc must not label v1 output as ROM-verified.

The UI builds a `BattleSnapshot` with `schema_version: 2` by extending the v1
shape below. It retains `source_sha`, difficulty, progress, parties, field,
history, and planned action. New fields are:

```python
{
  "schema_version": 2,
  "state_origin": "battle-start" | "manual-scenario",
  "rng": {"main": None | [u32, u32, u32, u32],
          "secondary": None | [u32, u32, u32, u32]},
  "history": {"turns": [OBSERVED_TURN, ...]},
}
```

`None` means unknown, including current HP and either RNG stream. The UI only
uses `state_origin: "battle-start"` after the user explicitly starts a battle
at full HP; observed turns replay from that setup. Direct midbattle edits use
`manual-scenario` and are labelled as such. Focused roster cards do not change
the snapshot's active indexes. The worker may reject either origin with
`incomplete-state` when it cannot reconstruct consequential volatile state.

The local worker exchanges one JSON object per line with `rom_bridge.py`:

```python
BattleRequest = {
  "protocol_version": 1, "request_id": str,
  "source_sha": str, "rom_sha256": str,
  "snapshot": BattleSnapshot, "player_action": ACTION,
  "rng_main": [u32, u32, u32, u32],
  "rng_secondary": [u32, u32, u32, u32],
  "deadline_ms": int,
}
TurnTrace = {
  "protocol_version": 1, "request_id": str,
  "rom_sha256": str, "enemy_action": ACTION,
  "events": [EVENT, ...], "resulting_state": dict,
  "rng_main": [u32, u32, u32, u32],
  "rng_secondary": [u32, u32, u32, u32],
  "terminal": None | "player-win" | "enemy-win",
}
```

The adapter validates source and ROM identity and returns a structured error
if an ID or required state cannot be mapped. The ROM worker runs the complete
turn and emits ordered events, including choices, switches, item use, damage,
status, ability activation, field changes, and the resulting state. A fixed
seed may be called verified only after its full trace matches the ROM oracle.

`calc_service.py` keeps the existing top-level card names (`player_moves`,
`enemy_moves`, `ai_actions`, `recommendation`) and serves cards independently.
Each card or move entry has one of these statuses:

```python
{"status": "verified-fixed-seed", "rom_sha256": str, ...}
{"status": "estimated", "rom_sha256": str,
 "sampler_id": str, "num": int, "den": int,
 "effective_samples": int,
 "interval_95": {"low": float, "high": float}, ...}
{"status": "unavailable", "reason": str,
 "detail": str, "missing": [str, ...], ...}
```

`num/den` is an observed scenario frequency; `interval_95` bounds the
estimated proportion between 0 and 1. The UI shows the sample count, interval,
and sampler description and calls it a **model-based estimate**. It never calls
an unknown-seed frequency exact or a calibrated probability of the user's
next action. `incomplete-state`, `insufficient-samples`, `timeout`,
`source-mismatch`, and `engine-error` are required unavailable reasons.
The cache key includes the normalized snapshot, source SHA, ROM hash, worker
and sampler versions, and planned action. A later edit cannot display an older
request's cards. Recommendations have a 10-second execution limit and require
at least 400 valid trajectories per candidate plus a resolved paired 95% score
difference; otherwise their ranking is uncertain or unavailable.

## v1 Python interface (legacy)

Owned by the orchestrator. Agents read this; only the orchestrator edits it.
Source of truth for the source pin: `data/source_lock.json`
(`2576d0f3f4c861c760f0c55198e37e6d7b91683e`).

Baseline recorded before work: `python app.py --self-check` -> "party cap check
passed"; `python -m pytest -q` -> 9 passed. No GBA toolchain on PATH
(`make`, `gcc`, `arm-none-eabi-gcc` all absent) and `../pokeemerald-expansion-ref`
has no `test/` directory, so no ROM build oracle exists locally.

## File ownership (do not edit another role's files)

| Role | Files |
| --- | --- |
| orchestrator | `CALC_CONTRACT.md`, `calc_service.py`, `test_calc_integration.py`, `README.md` |
| rom_source | `tools/extract_game_data.py`, `data/game_data.json`, `data/rom_fixtures.json`, `test_rom_source.py` |
| battle_core | `battle_engine.py`, `test_battle_engine.py` |
| calc_ui | `planner_ui.py`, `app.py`, `ui.css`, `calc_config_component.py`, `test_calc_ui.py` |

Interface changes go through the orchestrator. Everything is Python 3 only
plus the existing `streamlit` dependency; `battle_engine.py` must not import
`streamlit`.

## 1. `battle_engine.py` public surface

```python
ENGINE_VERSION = "1.3.2"          # bump on any behaviour change (currently 1.3.2)
SOURCE_SHA = data/source_lock.json commit

def normalize_snapshot(snapshot) -> dict      # canonical copy; raises ValueError if malformed
def evaluate_head_to_head(snapshot, game_data, deadline=None) -> dict
def legal_player_actions(snapshot, game_data) -> dict  # {"moves": [...], "switches": [...]}
def simulate_party_level_cap(party, level_cap, game_data) -> list  # ROM raise-to-cap on a copy
```

`deadline` is an absolute `time.monotonic()` value in seconds, or `None`.
Checks are polled inside every search loop; an expired deadline only affects
results that are still unfinished.

`evaluate_head_to_head` is pure: no globals, no clock reads other than the
deadline, no file IO, no network. It returns plain JSON-serializable dicts.
No float may appear in any probability or score.

## 2. BattleSnapshot

`None` means **unknown** — never guessed, never coerced to zero. Unknown is not
`0`, `""`, `False`, or `[]`.

```python
{
  "schema_version": 1,
  "source_sha": str,                       # must equal SOURCE_SHA or the result is unavailable
  "difficulty": "Easy" | "Normal",
  "progress": {"milestone_id": str, "flags": {str: bool}},   # flags = achieved progression flags
  "player": {"party": [BUILD, ...], "active_index": int},
  "enemy": {"trainer_id": str, "party": [BUILD, ...], "active_index": int,
            "items": {str: int}, "ai_flags": [str]},
  "field": {"weather": None, "terrain": None, "trick_room": None,
            "player_screens": None, "enemy_screens": None,
            "player_hazards": None, "enemy_hazards": None},
  "rng": {"state": None},                  # 4 ints (SFC32 words) or None = unknown seed
  "history": {"turns": [ ... ]},           # ordered observed turn log, may be empty
  "planned_action": None | {"kind": "move", "move_id": str, "target_index": int | None}
                  | {"kind": "switch", "slot": int},
}
```

`BUILD`:

```python
{
  "slot_id": int | None,          # owned row id (player), None for enemy
  "species": "SPECIES_X",
  "level": int | None,
  "ability": "ABILITY_X" | None,
  "item": "ITEM_X" | None,
  "nature": "NATURE_X" | None,
  "ivs": {stat: int},             # absent key = unknown; stat in hp/attack/defense/sp_attack/sp_defense/speed
  "evs": {stat: int},
  # Player builds: the saved build form displays absent IV/EV keys as 0,
  # so the app expands those form defaults to explicit zeroes in the snapshot.
  # Trainer-party builds: all six keys are always present, because the ROM
  # applies the table's packed IV word unconditionally (src/battle_main.c:2100)
  # and leaves EVs at CreateMon's zeroes when the table has no .ev pointer.
  "moves": ["MOVE_X", ...],       # at most 4, no duplicates
  "pp": {move_id: int | None},    # missing key = unknown PP
  "max_hp": int | None,
  "hp": int | None,
  "status": str | None,           # "none" at battle start or a STATUS1_* id
  "stat_stages": {stat: int},     # absent key = 0
  "use_item": bool,               # False = no held item, simulates item removal
  # item semantics: use_item False means the Pokémon holds nothing, whatever
  # `item` says. item None with use_item True = unknown -> missing-input.
}
```

`history.turns[i]` is `{"player_action": action|None, "enemy_action": action|None,
"events": [event, ...]}` where `action` uses the same shape as `planned_action`
and every event is `{"kind": str, ...}`. Any event the engine cannot represent
exactly makes seed-constrained results unavailable (`reason:
"unrepresentable-history"`).

## 3. Result dict

Each of the four cards is independently gated.

```python
{"status": "exact",
 "source_sha": SHA,
 "evidence": {"basis": "source-transcribed" | "oracle-verified",
              "oracle_verified": bool},
 "method": "known-seed" | "seed-independent",
 "probability": {"num": int, "den": int},       # den > 0, coprime not required
 ...card-specific fields...}

{"status": "unavailable",
 "reason": CODE,            # machine-readable enum, see below
 "detail": str,             # one readable sentence
 "missing": [str, ...],     # dotted snapshot paths, when reason == "missing-input"
 "mechanics": [str, ...]}   # unsupported move effects / abilities / items / field effects
```

Reason codes (closed set, defined in `battle_engine.REASONS`):
`missing-input`, `unknown-rng-seed`, `unrepresentable-history`,
`unsupported-mechanic`, `unsupported-move`, `unsupported-ability`,
`unsupported-item`, `unsupported-difficulty`, `conditional-party`,
`incomplete-search`, `timeout`, `no-legal-actions`, `source-mismatch`,
`engine-error`.

`method` semantics: `known-seed` = simulated from the entered RNG state;
`seed-independent` = the outcome is identical for every possible RNG state, so
the probability is exact without enumerating seeds. Anything that needs
seed enumeration but cannot exhaust it in budget returns `unknown-rng-seed`.

Card-specific fields:

- Gating is **per result and per entry**, never per snapshot. A card must not be
  suppressed by an input or mechanic that its own computation never reads:
  `player_moves`/`enemy_moves` need only the two actives, `ai_actions` needs the
  enemy active's legal actions (plus the enemy bench only when the trainer's AI
  flags can actually produce a switch, which must be proven from source), and a
  player switch candidate needs only that candidate's inputs.
- `player_moves` / `enemy_moves`:
  `{"moves": [{"move_id", "move_name", "probability": {"num","den"}, "method",
               "ko": str, "notes": [str, ...]}, ...]}`
  Each entry is gated on its own: an unsupported move carries `"status":
  "unavailable"` with `reason`/`mechanics` on the entry while the supported
  sibling moves stay exact. The card itself is `exact` when at least one entry
  is exact.
  `probability` is P(KO of the Pokémon active at turn start | this move is
  selected). `enemy_moves` also conditions on `planned_action`. `ko` is a short
  condition string, e.g. `"damage >= remaining HP of enemy active"`.
- `ai_actions`:
  `{"actions": [{"kind": "move"|"switch"|"item", "id", "label",
                 "probability": {"num","den"}, "top": bool}, ...],
    "conditioned_on": "entered battle state"}`
  Probabilities over legal AI actions sum to exactly 1.
- `recommendation`:
  `{"label": "two-turn lookahead",
    "actions": [{"kind", "id", "label", "score": {"num","den"}} ...],  # all tied maxima
    "breakdown": [str, ...],
    "scored_turns": 2}`
  Score = `sum(100*HP/maxHP) over player - same over enemy + 100*(living player - living enemy)`,
  exact rationals. Every legal first-turn player action is a reachable path: if
  any of them cannot be evaluated, the whole card is `unavailable`
  (`incomplete-search`, naming the action), because then the maximum is unknown.

## 4. game_data schema 2 (rom_source produces, battle_core consumes)

Top level: `schema_version` (2), `source`, `warnings`, `trainers`, `species`,
`abilities`, `moves`, `types`, `type_chart`, `regression_checks`, plus new:
`level_caps`, `difficulty_boss_levels`, `oracle`.

Every new field is `null` when the source does not resolve it unambiguously.
battle_core fails closed on `null`.

- `moves[MOVE_ID]` gains: `category` (`"physical"|"special"|"status"|null`),
  `effect` (`"MOVE_EFFECT_*"|null`), `effect_argument` (dict|null),
  `priority` (int|null), `target` (`"TARGET_*"|null`),
  `secondary_effects` (`[{ "chance": int, "effect": "MOVE_EFFECT_*", ... }]|null`),
  `flags` (`["makesContact", ...]|null`, every TRUE boolean `MoveInfo` bitfield),
  `always_critical` (bool|null), `crit_ratio` (int|null),
  `strike_count` (int|null for multi-hit), `recoil` (int percentage, 0 = none).
  The engine must fail closed on any emitted flag or numeric field it does not
  actually model (e.g. `recoil != 0`, `minimizeDoubleDamage`) instead of
  assuming it is inert.
- `species[SPECIES]` keeps `name/types/stats/abilities`.
- `trainers[i]` gains `ai_flags` (`["AI_FLAG_*", ...]|null`, resolved from the
  active `AI_FLAGS(...)` table for singles) and keeps `flags`, `items`
  (repeat entries = repeatable), `conditional_party`, `context_required`.
  `trainers[i].party[j]` keeps `species/level/raw_level/moves/item/nature/ability`
  and its `ivs`/`evs` dicts are **fixed ROM values**, all six keys present. A
  table entry that omits `.iv` is not unknown: `struct TrainerMon` zero-initialises
  it and `src/battle_main.c:2100` applies `SetMonData(MON_DATA_IVS, ...)`
  unconditionally, so the IVs are 0; EVs stay 0 because the EV block only runs
  when `partyData[i].ev != NULL`. `ivs_encoding`/`evs_encoding` keep the raw
  table text. An omitted `.ability` resolves through
  `GetAbilityBySpecies` to the species' first non-`ABILITY_NONE` slot
  (`src/battle_main.c:2033-2131`, `src/pokemon.c:3772-3792`), which the extractor
  already emits as the resolved ability id.
- `level_caps`: ordered list `[{"id": "CAP_1", "flag": "FLAG_BADGE01_GET",
  "level": 15, "label": str}, ...]` transcribed from `src/caps.c`, plus
  `{"id": "CAP_MAX", "flag": null, "level": MAX_LEVEL}` last.
- `difficulty_boss_levels`: `{"Easy": {...}|null, "Normal": {...}|null,
  "Hard": null}` — Easy/Normal boss-level adjustment only, `null` if not
  resolvable from source (`Hard` is `null`; the pinned ROM has no Hard mode).
- `oracle`: `{"available": bool, "reason": str, "detail": str,
  "toolchain": {...}}`.
- `data/rom_fixtures.json`: `{"source": {...}, "oracle": {...},
  "fixtures": [{"id", "kind", "provenance": "path:line", "inputs": {...},
  "expected": {...}}, ...]}` — hand-derived expectations transcribed from the
  pinned source. `kind` in `"damage" | "stats" | "accuracy" | "ai-score" | "rng"`.
  Each fixture must carry the source path and line that justify it.

## 5. `calc_service.py` (orchestrator) — used by calc_ui

```python
BUDGET_SECONDS = 2.0
def snapshot_key(snapshot) -> str
def request(snapshot, game_data) -> None        # debounced, non-blocking
def poll(snapshot) -> dict | None                # exact-key match only, else None
def cached(snapshot) -> dict | None
def invalidate() -> None
def status() -> dict                             # {"running": [...], "last_key": ..., "last_ms": ...}
```

A newer `request` for a different key never lets the older result render:
`poll` returns a result only when its key equals the current snapshot key.
The deadline is `request` time + `BUDGET_SECONDS`.

## 6. Display rules (calc_ui)

- Percentages: `round(100*num/den)` shown with the exact fraction available;
  never a sampled or approximate value presented as exact.
- Every exact card shows its `basis`; `source-transcribed` renders as
  "source-transcribed, not emulator-verified".
- Unavailable cards show the concrete reason and the missing inputs.
- `Use Item` toggles feed `BUILD.use_item` into the calculation, not display only.
- A saved config with `difficulty == "Hard"` must be rejected on load until the
  user picks Easy or Normal.
- Missing HP/IV/EV/history in an old config stays missing; never invented.
