# Pinned pokeemerald AI oracle

This directory pins the ROM source and carries a deterministic smart-AI battle fixture with test-only trace hooks. The release ROM hash is a build identity. The trace is emitted by the separate upstream test ELF, `pokeemerald-test-headless.elf`; no release-ROM trace parity is claimed. The hook patch compiles only for the test build.

## Pin and build

The source pin is recorded in `pinned_source.json`. Create a full checkout on a Linux filesystem (WSL ext4 is recommended; `/mnt/c` made the upstream suite I/O bound):

```sh
git clone https://github.com/iriv24/pokeemerald-expansion.git /home/$USER/pokeemerald-oracle
git -C /home/$USER/pokeemerald-oracle checkout 2576d0f3f4c861c760f0c55198e37e6d7b91683e
```

Install the upstream build tools required by that commit, including `arm-none-eabi-gcc`, `arm-none-eabi-binutils`, `make`, `git`, `libpng-dev`, `pkg-config`, Go, and the project's native build dependencies. Poryscript is a build-time-only tool. Build the pinned compatible release and install it into this disposable source checkout:

```sh
git clone https://github.com/huderlem/poryscript.git /tmp/poryscript
git -C /tmp/poryscript checkout d9bc56bc84db479dc14c872a5b65e1e773fce69b
cd /tmp/poryscript
go build -o poryscript .
./install.sh /home/$USER/pokeemerald-oracle
```

Build the release ROM before applying the test overlay, then verify its identity:

```sh
cd /home/$USER/pokeemerald-oracle
make -j2
sha256sum pokeemerald.gba
# Expected: 0ccaaad0878a9cbfb36fbefff800e9b6dd66055924b7595038ede1e8771ec5a2
```

The verified build used `arm-none-eabi-gcc 13.2.1` on Ubuntu 24.04. The source Makefile selects its modern build by default. Do not commit generated ROM or ELF files.

## Run the targeted oracle

Run under WSL/Linux with Python 3 and `pytest` available for the parser tests:

```sh
python3 tools/rom_oracle/run_trace.py --source /home/$USER/pokeemerald-oracle
```

The runner requires the exact source commit, refuses unrelated dirty paths, and verifies the source diff against `trace_hooks.patch` before building. It installs the fixture into the disposable checkout, runs upstream `make check` with the single test selected, checks both generated test ELF hashes, executes mGBA headlessly, byte-compares the raw records with `expected_trace.raw.txt`, then compares parsed state with `expected_trace.json`. The selected test must report exactly one pass.

An existing upstream smart-switch test is also a useful regression gate after the overlay is applied:

```sh
cd /home/$USER/pokeemerald-oracle
make -j4 check TESTS="AI_FLAG_SMART_SWITCHING: AI will switch out if it can't deal damage to a mon with Wonder Guard"
```

At the recorded pin this test passes its 2 parametrizations. The broad upstream `make check` was started but stopped after roughly 2,604 status lines; it had unrelated failures and no aggregate result. It is not represented as green by this gate.

## Trace contents and boundary

The fixture runs two turns with Bulbasaur, Charmander, and bench Heatran. Its turn-indexed events include Intimidate, the player's Will-O-Wisp, Charmander's Tackle and HP loss, the AI selecting opponent party slot 1, the player's next move selection, the settled switch-in, and Heatran's subsequent Tackle. The `switch-settled` hook runs immediately after upstream `SwitchInClearSetData`; it records battler 1 as species 485 (Heatran). The upstream party-order mapping reports that active battler as internal party index 0 after selecting slot 1, so the settled species and full party snapshot are included to disambiguate it.

At switch settlement and after the next turn, the trace records all six party slots per side (readable battle fields plus chunked raw `struct Pokemon` bytes), active `BattlePokemon` state (readable combat fields plus raw bytes), `DisableStruct`, `ProtectStruct`, `SpecialStatus`, side timers, field/weather values, and both RNG streams. Raw records are pinned-build byte layouts intended for exact comparisons; their meaning depends on the source commit and toolchain pin. `run_trace.py` compares all 170 newline-delimited records byte-for-byte with the raw capture (SHA-256 `e4051f1eb74a2dc61089c0bf15cf98a5472ad3b985a19f178235dfb98ae38034`) and compares their parsed state with the JSON capture. The generated test ELF and headless ELF hashes are also pinned in `pinned_source.json`.

The trace does not snapshot every global byte or prove that the release ROM follows the same path. It is a deterministic, broad state capture for this bounded test scenario, not a full RAM snapshot, universal replay format, or the complete calculation contract.

## Dynamic battle request v8

`dynamic_request.patch` applies to the pinned source on its own; it already
contains the trace hooks, so `trace_hooks.patch` is only used for the separate
170-record oracle. It adds a test-only, versioned **864-byte** EWRAM mailbox.
The exact byte-offset table is frozen in `ROM_TRANSITION_CONTRACT.md`
Appendix A. Build the pinned test ELF with the recipe in `pinned_source.json`;
do not commit either generated ELF. The host runner checks the pinned headless
ELF hash and source-derived IDs before writing any request.

**v8 is a protocol bump.** The v7 host path (`tools/rom_oracle/run_dynamic.py`,
`rom_bridge.RomWorkerClient.batch_turns`, `send_rom_request`) writes a 7-version
document and 856 bytes and is rejected by the v8 ELF (`ROMTRACE|invalid|1`).
The worker/transport package must update those writers; package A only owns the
ROM side. The v7 fields keep their v7 offsets; the v8 extension appends
`op` (856), `mode` (858), `criticalPolicy` (860), `requestId` (862).

### Operations, modes, and critical policy

- `op` (u16, offset 856): `0` `SIMULATE_TURN`, `1` `QUERY_DAMAGE`,
  `2` `QUERY_AI_ACTION`. A legacy request that leaves `op` at 0 and sets the v7
  `secondPresent` byte to `2` still reaches the query path.
- `mode` (u16, 858): `0` `normal`, `1` `logic-only`.
- `criticalPolicy` (u16, 860): `0` `normal` (real engine crit rolls, the parity
  mode), `1` `item-ability-only` (the previous default; ordinary crits are
  filtered and the engine-derived record is labelled
  `ROMTRACE|critical-policy|item-ability-only|battle-script-filtered`). The
  query damage path applies the same policy to its crit weights.
- `requestId` (u16, 862): caller-chosen monotonic token. The boundary hold
  releases only when a submitted request carries a different `requestId`.

### Choice-point boundary (v8 SIMULATE_TURN)

The ROM stops at the decision boundary defined by contract section 3: after
entry/end-of-turn effects and compulsory faint replacement have settled, at the
start of the next turn's action selection and **before the AI chooses**. At that
point it emits

```
ROMTRACE|boundary|player-choice|<turn>|<legal-action-count>
ROMTRACE|phase|<turn>|boundary|player-choice|<count>   (legacy slim filter)
```

The machine then holds: `HandleTurnActionSelectionState` returns without
advancing until a submitted request with a different `requestId` arrives. The
hold is what makes the node snapshot-able — the mGBA worker saves the savestate
while the machine is stopped, so the boundary is exact. A forced switch after a
faint emits `boundary|forced-switch-player|...` and holds the pokémon-choice
controller the same way (switches only, `legal-action-count` counts switchable
party slots). If the battle has ended, the ROM emits
`ROMTRACE|terminal|<win|loss|draw>|<reason>` plus `ROMTRACE|complete|<turn>` and
never a boundary; a terminal/faint path finishes without the 24,000-frame
fallback (measured: 325 frames).

### Decoded state export at the boundary

Every boundary emits the complete contract 2.2 field set. Records are prefixed
`ROMTRACE|phase|<turn>|state|` so the pinned worker's slim filter keeps them
(the canonical markers `boundary|`, `terminal|`, `unrepresentable|`,
`instruction-budget-exceeded|` are kept by the updated worker filter; the
`phase|<turn>|...` mirror is what the pinned v1 worker can carry):

| Record | Fields after the prefix | C source |
| --- | --- | --- |
| `state|party` | side, slot, species, level, HP, maxHP, status1, item, abilityNum, speciesOrEgg, nature, move1-4, pp1-4, ivHp/Atk/Def/Spd (23) | `struct Pokemon` via `GetMonData`, `gPlayerParty`/`gEnemyParty` |
| `state|active` | battler, partySlot, species, HP, maxHP, status1, status2, `gStatuses3`, ability, item, types 0-2, 7 stat stages, moves 0-3, pp 0-3 (28) | `struct BattlePokemon gBattleMons[]`, `gStatuses3[]` |
| `state|field` | weather, field statuses, trick room, screens p/e, hazards p/e, side statuses p/e (9) | `gBattleWeather`, `gFieldStatuses`, `gSideStatuses[]` |
| `state|side-timer` | side, reflect, light screen, aurora veil, mist, safeguard, tailwind, follow-me (8) | `struct SideTimer gSideTimers[]` |
| `state|battle` | turn counter, outcome, `itemsNo`, battle type flags, battler count, `requestId` (6) | `gBattleResults`, `gBattleOutcome`, `gBattleResources->battleHistory` |
| `state|ai-flags` | battler, flags (2) | `AI_THINKING_STRUCT->aiFlags[]` |
| `state|trainer-item` | index, item id, `itemsNo` (3) | `gBattleResources->battleHistory->trainerItems[]` |

The both-RNG-stream pair is emitted at each boundary as
`ROMTRACE|rng|<turn>|a|b|c|ctr|a2|b2|c2|ctr2`, so consecutive boundaries give
the before/after pair for the transition. `state|restore-source` records that
the stable-restore bytes are the mGBA savestate taken at the held boundary
(measured 528,448 bytes for this test ELF), not the decoded projection.

Fields the exporter cannot read through an accessor are reported, never
defaulted:

```
ROMTRACE|unrepresentable|recorded-battle-cursor
ROMTRACE|unrepresentable|controller-exec-flags
ROMTRACE|unrepresentable|ai-scoring-scratch
```

(each also mirrored under `phase|<turn>|unrepresentable|`).

### Transition from a saved decision state (what a caller must supply)

Root creation still uses the mailbox party builder plus at most one replayed
turn. A **deeper** transition does not replay: the caller must

1. restore the saved decision state (the mGBA savestate taken while the ROM was
   held at that boundary) with the worker's named state slot,
2. write a fresh mailbox document with `op=SIMULATE_TURN`, a new `requestId`,
   the injected RNG pair (`mainRng`/`secondaryRng`), and the validated player
   action (`candidateKind`/`playerMove`/`playerSlot`),
3. let the machine run: the hold releases, the player action is injected into
   the recorded-player controller (the recorded bytes are still consumed so the
   playback cursor stays aligned), the real trainer AI chooses, the turn runs
   with switch-in and faint follow-ups, and the ROM holds at the next boundary
   with a fresh state export.

`replayCount > 1` is still rejected; >1 replay remains an unrepresentable
history. The one-replay dependency is no longer on the transition path.

Verified: one root boundary, then Tackle and Growl injected from the **same**
saved node. Both branches restore byte-for-byte the same state and RNG
(`rng|0|1764359885|589825119|1761919851|3|1|2|3|29`), the trainer AI chooses the
same `move|33` in both (no player-action leakage), and the next boundary differs
as the rules require (opponent HP 143 vs 155 and Attack stage 5 vs 6). Replaying
the same branch twice reproduces the identical boundary and RNG.

### Logic-only mode

`mode=logic-only` selects the bounded pump in `BattleMainCB1`: up to four
synchronous `gBattleMainFunc` + controller advances per emulated frame, stopping
at a boundary, a terminal, an unmechanical dependency, or the 2,000,000
instruction budget (`ROMTRACE|instruction-budget-exceeded|<n>`). Normal mode is
exactly one advance per frame, so it stays the differential baseline. Measured
on the same scenario: normal 205 frames to the next boundary, logic-only 130
frames, no error. Deviation from contract 5: the runtime mode switch is present
but there is no separate `SIDEDEX_LOGIC_ONLY` compile-time define — the whole
hook only exists in the test build, so the test ELF is already the gate. A
finer presentation/controller audit (which individual waits are pure
presentation) is not yet done; the pump is the coarse, safe version.

### Trainer items in a resolved turn (blocked, documented)

Enabling the request's `trainer_items` for a resolved `SIMULATE_TURN` was tried
in v8 and is **not safe in this harness**: the battle completes action selection
(both `ROMTRACE|action|` records are emitted) and then makes no further progress
for at least 1500 frames, with no hp/faint/boundary record. It reproduces for
any item list, including one the AI would not use (opponent at 80/155 HP with an
unused Potion), so the presence of a resolved-turn trainer bag itself breaks the
turn, not the heal decision. The hook therefore keeps the v7 behaviour: items
are copied into the battle history for the pre-choice **query** turn only, the
query returns the exact item prediction, and the resolved-turn recommendation
fails closed (`unsupported-mechanic`) when the predicted action is an item. The
`SetUpBattleVarsAndBirchZigzagoon` hook cannot currently be made safe; the exact
blocker is the interaction between a populated `battleHistory->trainerItems` and
the resolved turn's controller/action-selection loop, not `ShouldUseItem`'s
item consumption. The v8 restore path does undo the `ShouldUseItem` mutation
across branches, so that part of the old concern is resolved.

### Bounded fixtures and unsupported boundaries

The bounded fixtures are `dynamic_sample.json` (opening),
`dynamic_switch.json` (candidate switch), and `dynamic_replay.json` (one
observed player/enemy action pair followed by a candidate turn). Up to six
party slots per side carry species, level, natural ability, item and an
authoritative `held_item_enabled` switch, nature, HP, status, moves/PP, IVs,
EVs, and seven active stat stages (Attack, Defense, Speed, Special Attack,
Special Defense, accuracy, evasion; each ranges from 0 through 12, neutral 6).
Requests carry both RNG states, source-mapped AI flags, and a verified clear
battle-start field. The initial active slot is fixed at party index 0 on both
sides; explicit nonzero active indices are rejected. Only clear
weather/terrain, no trick room, no screens/hazards, and at most one replayed
turn are supported as root construction. When a replayed player switch changes
the active monster, a candidate move is validated against that slot's moves.
Set `post_switch_query: true` with a replayed player switch and the opponent's
response to query the next turn after entry effects. Query active records
include current party slot, species, HP, Max HP, and moves. The result exposes
the bounded raw ROMTRACE event list; the Python transition/query adapter is
package B/C work. These boundaries are explicit and must not be treated as
trainer-battle parity or full state coverage.

### Reproducing the v8 evidence

The verification harness used by package A lives outside the repository
(`work/v8_verify.py`, not committed). It drives the v8 ELF through the worker's
`run_frames`/`write_memory`/`save_checkpoint`/`restore_state` operations and is
reproducible with the pinned worker and the v8 ELF; the raw report is
`work/v8_report.json` in the handoff. The build recipe is unchanged:
`make -j4 pokeemerald-test.elf TESTS='Sidedex oracle: dynamic AI singles request'`,
copy to the new artifact name, then
`tools/patchelf/patchelf <elf> gTestRunnerHeadless '\x01' gTestRunnerSkipIsFail "\x00"`.

## Query mode

Set the existing request field `secondPresent` to `2` (mailbox byte offset
842) to query from the opponent's pre-action choice checkpoint. The hook waits
until any replay turn has resolved before querying. It emits active-state and
active-move records, then
one `ROMTRACE|query|damage|side|move|numerator|denominator|flags|min|max|accuracy|critDen`
record per active move, then one `ROMTRACE|query|ai|move|moveId`,
`ROMTRACE|query|ai|switch|partySlot`, or `ROMTRACE|query|ai|item|itemId` record,
followed by
`ROMTRACE|query|complete` and `ROMTRACE|complete|0`. It does not resolve the
turn. Query records and resolved turn actions use the `item-ability-only`
critical policy: ordinary critical hits are removed while engine critical
effects changed by enabled held items or abilities remain. The action resolver
applies this filter after AI selection, leaving AI move choice unchanged.
The host runner attaches pinned-ELF policy evidence to each completed trace,
and the engine reports ordered faint events. Supported
KO fractions use `denominator = 1600 * critDen`: 16 damage rolls, policy-filtered
crit weights, and the engine accuracy percentage (including misses). Status
moves are `0/1600`. Flag bit 1 means an exact KO fraction is
unavailable and has a zero denominator. Flag bit 8 accompanies bit 1 when a
standard multi-hit move has an engine-derived total damage range: two to five
hits, adjusted for Skill Link or Loaded Dice, or a fixed hit count for ordinary
strike moves. Flag bit 16 marks an enumerated multi-hit record: a
`ROMTRACE|query|hits|side|move|mode|minHits|maxHits|nonCritWeight|critWeight`
record plus `ROMTRACE|query|hitroll|side|move|crit` records for crit 0 and 1,
and the host composes the exact KO fraction with the frozen protocol's
`16*(nonCritWeight + critWeight)` per-hit weight total. Each hitroll record
prints all 16 damage-modifier rolls for its crit state (22 pipe fields).
Other multi-hit effects with changing power and fixed-damage
moves remain unsupported. Flag bit 2 marks full-HP Focus Sash and bit 4 marks
full-HP Sturdy. The damage bounds cover nonzero-probability crit states and
all 16 damage rolls; multi-hit bounds sum the per-hit extremes assuming
damage modifiers do not change between hits.

AI output is the deterministic action selected from the request's seeded RNG
state at that checkpoint; query mode does not enumerate AI RNG outcomes or
advance through the action.
