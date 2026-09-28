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

## Dynamic battle request v9

The v9 test hook keeps the v8 request bytes through offset 863 and appends
`resultAddress:u32`, `resultCapacity:u32`, `resultVersion:u16=1`, and
`flags:u16` at 864, 868, 872, and 874 (876 bytes total). The hook announces
`ROMTRACE|ready|<request_addr>|876|9|3|<result_addr>|8192`; the result array
is an aligned 8192-byte allocation from `gHeap`, and requests must echo that exact address
and capacity with compact-result bit 0 set. The ELF announces its runtime
address so the worker never guesses its location.

The reserved result buffer is transport storage, not a decoded state or a
replacement savestate. A transition result uses `SDR2`, format version 1 and a
64-byte header; records use the contract Appendix B tags 1–11. Compact result
serialization and the final `binary-ready` marker are still incomplete, so the
v9 path must remain fail-closed until both are present.

#### v9 compact result record schema

Typed records use `entry_count:u16, words_per_entry:u16` followed by little-endian
`u32` words:

| Tag | Count and field order |
|---|---|
| 1 party | 12 × 32: side, slot, species, level, HP, max HP, status1, item, ability number, form, nature, moves 1–4, PP 1–4, six IVs, six EVs, present bit |
| 2 active | two × 33 for singles: battler, party slot, species, HP, max HP, status1/status2/status3, ability, item, form, three types, seven stat stages, four moves, four PP, volatile flags, disable/protect/special-status fields, substitute HP, encore and taunt timers |
| 3 field | one × 6: weather/timer, terrain/timer, trick-room/timer |
| 4 side | two × 12: side, screens, hazards/layers, conditions, reflect, light screen, aurora veil, mist, safeguard, tailwind, follow-me timers |
| 5 battle | one × 10: turn, outcome, battle type flags, battler count, trainer AI flags, item count, four trainer item IDs |
| 6 RNG | one × 8: main and secondary four-word streams; duplicates the header pair |
| 7 legal | N × 4: `kind,id,pp_or_species,target_or_slot`; kind 0 move, 1 switch, 2 item |
| 8 trainer action | one × 3: kind, id, target |
| 9 events | concatenated `event_kind:u16,payload_words:u16,payload:u32[]` subrecords |
| 10 terminal | one × 2: result and reason code |
| 11 unsupported | one × 2: reason code and source code |

Tags 1–7 are required for a successful nonterminal result. The current test hook
only emits a nonzero status with tag 11 reason 1 (`incomplete-state`) and source
1 (export coverage), with no child token. The compact header and error record are
CRC checked; typed successful projections are still unimplemented.
## Dynamic battle request v8

The mailbox layout below is unchanged by the A2/A3 revisions (the A2 artifact
is `native/rom_worker/build/mingw/pokeemerald-test-v8b.elf`; the corrected A3
artifact is `native/rom_worker/build/mingw/pokeemerald-test-v8c.elf`, sha256
`422ade3e159916f9cc8bc1350ad274201b6af6cb9b9d38c14d073f85e982bc77`,
pre-patchelf `pokeemerald-test.elf` sha256
`23c34b8357eba2cc6a1b486d444a56fcd50852acb8846a9e585d3fdbce7ae3c1`; the byte
layout and `SIDEDEX_DYNAMIC_VERSION` 8 are identical, so package C's encoder
needs no change).

### v8c: hold-release RNG re-seed (contract section 6.2)

A released boundary now overwrites BOTH `gRngValue` and `gRng2Value` from the
mailbox pair before the trainer AI chooses. The mGBA savestate carries the RNG
as of the held boundary, so without this a restored node kept the saved pair and
the injected pair was ignored (measured on v8b: two different injected pairs
produced byte-identical `rng|1|` records and identical HP). Verified on v8c from
one saved node: injected main/secondary pairs P1 and P2 give different
next-boundary `rng|1|` records and different player HP (68 vs 69); the emitted
gRng2Value equals the injected secondary exactly (0 advances that turn) and the
emitted gRngValue is the injected main advanced by exactly 12 sfc32 steps
(ctr 1->13 and 7->19). Harness: `work/a3_rng_restore.py` (not committed),
reports `work/a3_rng_v8c.json` / `work/a3_rng_v8b.json`.

### v8c: release-ROM neutrality

The recorded-player injection call sites (`src/battle_controller_recorded_player.c`)
and the recorded-battle cursor guard (`src/recorded_battle.c`) are now
`#if TESTING`/`#else` guarded, so a `TEST=0` build of the pinned source plus
`dynamic_request.patch` is byte-identical to the pinned release ROM. Verified in
a scratch worktree: `make -j12 rom` then `sha256sum pokeemerald.gba` =
`0ccaaad0878a9cbfb36fbefff800e9b6dd66055924b7595038ede1e8771ec5a2`. The A2
recorded-battle fix and the recorded-player injection remain active in the test
ELF only; the release ROM is unchanged.

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
- The v7 `secondPresent` byte (842) still selects the historical paths: `0` a
  plain candidate turn, `1` a second scripted turn, `2` the pre-choice query.
  A2 adds `3`: a **forced-switch scenario** where the candidate turn is expected
  to faint the player's active monster and `secondSlot` (840) is the recorded
  compulsory-replacement party index (the ROM holds at the forced-switch
  boundary and injects the real choice).

### Choice-point boundary (v8 SIMULATE_TURN)

The ROM stops at the decision boundary defined by contract section 3: after
entry/end-of-turn effects and compulsory faint replacement have settled, at the
start of the next turn's action selection and **before the AI chooses**. At that
point it emits the legal-action records, the complete state export, and then

```
ROMTRACE|boundary|player-choice|<turn>|<legal-action-count>
ROMTRACE|phase|<turn>|boundary|player-choice|<count>   (legacy slim filter)
```

**Wire contract: the boundary marker is the LAST record of the boundary
emission.** The worker's `batch_turns` stop treats `ROMTRACE|boundary|` as "this
turn's evidence is complete" and breaks the frame loop as soon as it sees one,
so anything emitted after it is truncated. Emitting it first (the original v8
order) returned only 4 of 34 records through a real batch; emitting it last
returns all 37 (v8b, measured). The same rule applies to `terminal|`: `complete`
is emitted before the terminal marker. Both marker spellings are unchanged.

The machine then holds: `HandleTurnActionSelectionState` returns without
advancing until a submitted request with a different `requestId` arrives. The
hold is what makes the node snapshot-able — the mGBA worker saves the savestate
while the machine is stopped, so the boundary is exact. A forced switch after a
faint emits `boundary|forced-switch-player|...` and holds the pokémon-choice
controller the same way (switches only, `legal-action-count` counts switchable
party slots). If the battle has ended, the ROM emits
`ROMTRACE|terminal|win|loss|draw|<reason>` plus `ROMTRACE|complete|<turn>` and
never a boundary; a terminal/faint path finishes without the 24,000-frame
fallback (measured: win 325 frames, loss 415 frames).

### Legal player actions at a decision point

Contract section 2 requires the ROM-derived `legal_player_actions` list, so the
boundary reports each action explicitly (not just the count), for both a
player-choice boundary and the pre-choice query path:

```
ROMTRACE|legal|move|<move_id>|<pp>|<target>
ROMTRACE|legal|switch|<slot>|<species>
ROMTRACE|phase|<turn>|legal|move|<move_id>|<pp>|<target>   (slim-filter mirror)
ROMTRACE|phase|<turn>|legal|switch|<slot>|<species>       (slim-filter mirror)
```

`<target>` is the battler the move would hit (opponent-left in singles). At a
forced-switch boundary only `legal|switch` is emitted. The `legal|item` spelling
is reserved for a ROM that would allow a player item, but this build never emits
it: the fixture models no player bag, and a trainer battle outside easy mode
forbids item actions outright (`src/battle_main.c`).

Note for the worker owner (package B): `ROMTRACE|legal|` is **not** in
`worker.c`'s slim allow-list, so the canonical records are dropped in
`batch_turns`; only the `phase|<turn>|legal|...` mirrors survive. Add
`ROMTRACE|legal|` to the allow-list to carry the canonical spelling.

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

### Complete outcome weights (query, contract phase 2)

For every candidate move the query enumerates the COMPLETE joint distribution
over the target's remaining HP, so the Python layer only has to sum integers
instead of composing mechanics. The mechanics folded in-ROM are: the engine's
damage roll spread (16 rolls through `CalculateMoveDamage`), the real crit odds
(`GetCritHitOdds`), the composed accuracy percent (`GetTotalAccuracy`),
per-hit crit/damage independence, the engine's multi-hit count distribution
(`src/battle_util.c SetRandomMultiHitCounter`: `RandomWeighted` 7/7/3/3 for
2/3/4/5 hits, Loaded Dice `RandomUniform 4..5`, Skill Link 5), hit-count weights,
miss-ends-the-sequence, and Focus Sash / Sturdy at full HP (single-hit moves
only — a multi-hit move KOs through both).

```
ROMTRACE|query|outcomes|<owner>|<move>|<flags>|<chunk>|<chunks>|<denom>|
        <branches>|<hitsMode>|<minHits>|<maxHits>|<accuracy>|<hp>|<weight>|...
```

One record per chunk of at most 8 `(<hp>,<weight>)` branches; every record
repeats the self-describing header. `denom` is 1000000 (parts per million) and
the weights sum to it exactly (the largest branch absorbs integer rounding).
`flags` bits: 1 multi-hit, 2 Focus Sash folded in, 4 Sturdy folded in,
8 accuracy<100, 16 enumeration refused (0 branches), 32 an HP/damage value hit
the internal 256 axis bound (not reachable with these fixtures). `hitsMode`:
0 standard 2-5, 1 Loaded Dice, 2 Skill Link, 3 Battle Bond, 4 fixed.

Verified with the sample request (opponent Gyarados at 10/10 HP, Focus Sash):
Tackle (100% acc) → 1 branch HP 1 weight 1000000 (sash folded in, flags 2);
Growl (status) → 1 branch HP 10 weight 1000000; Fury Attack (2-5 hits) → HP 0,
flags 1; Hydro Pump (80% acc) → HP 1 weight 800000 and HP 10 weight 200000,
flags 10. All records sum to 1000000.

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
frames (~37% fewer), no error and the same boundary/state/RNG. Deviation from
contract 5: the runtime mode switch is present but there is no separate
`SIDEDEX_LOGIC_ONLY` compile-time define — the whole hook only exists in the test
build, so the test ELF is already the gate.

**Presentation audit (A2-4): attempted, not achieved; the 4× pump stands.**
Measured on the same scenario (branch A, Tackle from the root boundary):

| mode | frames to next boundary | wall ms |
| --- | --- | --- |
| normal | 205 | 43.5 |
| logic-only (4×, shipped) | 130 | 51.3 |
| logic-only (controller-flags gate) | 180 | 59.8 |

So logic-only genuinely reduces EMULATED FRAMES (~37%), which is what the
contract's 24,000-frame cap and frame-based budgets care about, but it does
**not** reduce wall time: the extra same-frame advances are CPU work on top of
the fixed per-frame emulation, and the waits it collapses are frame-counted, so
130 frames × 4 advances (520) does more total work than 205 × 1. This is a
frame-budget optimisation, not a latency optimisation, and callers should not
expect logic-only to be faster in wall-clock terms.

The attempted gate — yield as soon as `gBattleControllerExecFlags != 0` ("a
controller is still executing") — was tried, built and measured, and it is
strictly worse: frames rose 130 → 180 and latency rose to 59.8 ms, while still
not beating normal. It was reverted (the rebuilt ELF hashes identically to the
4× version, which also demonstrates the build is deterministic).

What blocks the stronger version is that the remaining frame cost is not a
classification problem the ROM hook can solve locally:

- The presentation handlers are `static` in `src/battle_controllers.c` and
  `src/battle_interface.c` (`BtlController_HandlePrintString`,
  `BtlController_HandleHealthBarUpdate`, ability pop-ups, move animations), so a
  hook cannot compare `gBattlerControllerFuncs[battler]` against them or tag them
  without editing every controller (a large, behaviour-risking change).
- The already-headless paths short-circuit the obvious cases (`gTestRunnerHeadless`
  returns early from print-string, ability pop-up and animation), so the residual
  waits are frame-counted `waitstate`/`waitmessage` counters whose semantics are
  per-command, not per-controller — collapsing them is exactly what the 4×
  iteration already does.
- `BattleMainCB2` still does real per-frame video/sprite work between
  `BattleMainCB1` calls; raising the iteration count without a proven-safe
  classification risks skipping side effects the battle logic depends on.

The honest status: coarse pump kept, stronger per-command audit blocked on
controller-handler attribution, with the table above as the baseline to beat.

### Trainer items in a resolved turn (root-caused and fixed)

A2 root-caused the v8 stall. It was a **base-code bug**, not an item-decision
problem: `RecordedBattle_ClearBattlerAction(battler, 1)` (called when the ROM
ruleset refuses an AI item action) decremented a `u16` record cursor that was
already 0 for the live-AI battler of a non-link recorded battle, underflowing to
65535 and executing `sBattleRecords[battler][65535] = 0xFF` — a write ~64 KiB
past the end of a `u8[MAX_BATTLERS_COUNT][BATTLER_RECORD_SIZE]` array, corrupting
EWRAM and wedging the turn. Fixed in `src/recorded_battle.c` by guarding the
cursor before the decrement (clear nothing when the cursor is already 0).

With that fixed, a resolved `SIMULATE_TURN` now carries the request's
`trainer_items` like a query does, so the AI makes the **real** item decision.
The ROM's own ruleset still refuses the resulting `B_ACTION_USE_ITEM` in a
trainer battle outside easy mode (`src/battle_main.c`), so the resolved turn
cannot *execute* the item: the AI falls back to a move and the ROM reports the
divergence, visibly and once per request, as

```
ROMTRACE|unrepresentable|trainer-item-decision
ROMTRACE|phase|<turn>|unrepresentable|trainer-item-decision
ROMTRACE|phase|<turn>|item-refused|<battler>|<item_id>
```

The turn then TERMINATES (measured: next boundary in 43 ms / 205 frames, no
stall). Callers must treat a transition carrying
`unrepresentable|trainer-item-decision` as `unsupported-mechanic`, because the
resolved AI action is the fallback move, not the item the query predicts.
Making the item actually execute would require forcing
`VAR_GAME_SETTING_DIFFICULTY_MODE` to easy, which also changes boss-trainer
behaviour (`src/battle_main.c` 2075/2087) and so is deliberately not done.

### Forced-switch boundary and terminal paths (verified)

`secondPresent == 3` declares a forced-switch scenario: the candidate turn is
expected to faint the player's active monster, and `secondSlot` is the recorded
compulsory-replacement party index (the ROM holds at the boundary and injects
the real choice, so the recorded slot is overridden). Verified end to end with
the sample request (player slot 0 at 1 HP, slot 1 healthy): the candidate Tackle
faints slot 0 (`hp|0|0|1|0`), the ROM emits
`ROMTRACE|boundary|forced-switch-player|0|1` plus `ROMTRACE|legal|switch|1|6` and
the full state export at 280 frames, then the injected switch (slot 1) advances
to the next `boundary|player-choice|1|2` at 230 frames with the new active
monster (species 6, 138/138) in the state export. Terminal win (opponent single
mon at 1 HP) and terminal loss (player single mon at 1 HP) both emit
`ROMTRACE|terminal|...` with no 24,000-frame fallback.

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

### Reproducing the v8b/v8c evidence

The A2 verification harness lives outside the repository (`work/v8b_verify.py`,
`work/priority0_batch.py`, and the A3 `work/a3_rng_restore.py`, not committed).
It drives the v8b/v8c ELF through the pinned protocol-2 worker via `rom_bridge`
(`run_frames`/`write_memory`/`save_checkpoint`/`restore_state`) and, for the
wire-contract check, through a real `batch_turns`. Raw reports:
`work/v8b_report.json`, `work/priority0_batch.json`, `work/a3_rng_v8c.json`.
The v8c batch check returns the complete 37-record boundary set with
`ROMTRACE|boundary|` last of the boundary emission, matching v8b. The build
recipe is unchanged: `make -j4 pokeemerald-test.elf TESTS='Sidedex oracle:
dynamic AI singles request'`, copy to the new artifact name, then
`tools/patchelf/patchelf <elf> gTestRunnerHeadless '\x01' gTestRunnerSkipIsFail
"\x00"`.

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
