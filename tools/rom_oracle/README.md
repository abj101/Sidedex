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

## Dynamic battle request v7

`dynamic_request.patch` applies to the pinned source on its own; it already
contains the trace hooks, so `trace_hooks.patch` is only used for the separate
170-record oracle. It adds a test-only, versioned 856-byte EWRAM mailbox.
Build the pinned test ELF with the recipe in `pinned_source.json`; do not
commit either generated ELF. The host runner checks the pinned headless ELF
hash and source-derived IDs before writing any request.

The bounded fixtures are `dynamic_sample.json` (opening),
`dynamic_switch.json` (candidate switch), and `dynamic_replay.json` (one
observed player/enemy action pair followed by a candidate turn). Up to six
party slots per side carry species, level, natural ability, item and an
authoritative `held_item_enabled` switch, nature, HP, status, moves/PP, IVs,
EVs, and seven active stat stages (Attack, Defense, Speed, Special Attack,
Special Defense, accuracy, evasion; each ranges from 0 through 12, neutral 6).
Requests carry both RNG states, source-mapped
AI flags, and a verified clear battle-start field. Calc sends no
`second_action`; the mailbox field may remain in the hook for compatibility.
The compiled hook
validates party state and the second action, and emits bounded ROMTRACE
records; the host checks for a completed turn and required action, party,
active, and RNG records. A first-turn KO finishes `AfterLastTurn` while
`battleTurnCounter` is still 0 and used to skip `ROMTRACE|complete`; the hook
now emits complete for that case, and the worker also stops on the following
`ROMTRACE|ready` so an older ELF does not spin to `max_frames`. Focused host tests cover encoding, switch/replay
shape, second-turn encoding, and invalid IDs/state.

The initial active slot is fixed at party index 0 on both sides. Any explicit nonzero
`player_active_index` or `opponent_active_index` is rejected. Trainer identity
is not applied by the hook; a trainer's item list (`trainer_items`, expanded to
at most four slots with the counts carried in the request tail) is copied into
the battle history for the pre-choice query turn only. The resolved-turn
sampling path deliberately leaves items unapplied, because this harness cannot
safely re-run the AI item decision inside a resolved turn; the query therefore
carries the item prediction and the sampling-derived recommendation fails closed
(`unsupported-mechanic`) whenever the predicted action is an item.
Only clear weather/terrain, no trick room, no screens/hazards,
and at most one replayed turn are supported. When a replayed player switch
changes the active monster, a candidate move is validated against that slot's
moves. Set `post_switch_query: true` with a replayed player switch and the
opponent's response to query the next turn after entry effects. Query active
records include current party slot, species, HP, Max HP, and moves.
The result currently exposes the bounded raw ROMTRACE event list rather than a
normalized full-contract outcome. These boundaries are explicit and must not be
treated as trainer-battle parity or full state coverage.

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
