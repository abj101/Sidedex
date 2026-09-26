# ROM-backed Calc: orchestration handoff

Status: implementation plan, not a claim that ROM-backed results work yet.
Working branch: `feat/rom-backed-calc` (branched from the saved plan commit).
Recoverable pre-engine snapshot: `snapshot/pre-rom-engine-2026-09-26` at `bc66ce9`.
Source of truth: `data/source_lock.json`, Emerald Imperium commit
`2576d0f3f4c861c760f0c55198e37e6d7b91683e`. Keep this pin; do not
silently update to upstream `master` or claim parity with a different played ROM.

## Implementation checkpoint (2026-09-26)

- A disposable full source checkout builds the pinned ROM under WSL. The built
  ROM is local only; Gate 0 still requires the existing `make check` suite and
  a repeatable fixed-seed complete-turn trace.
- The Calc UI is moving to schema v2. `calc_service.py` now routes v2 requests
  separately and returns unavailable cards until a ROM evaluator proves trace
  parity. The v1 Python evaluator remains covered as a legacy model.
- The mGBA worker is limited to process, reset, checkpoint, and memory
  primitives until a battle hook can reconstruct state and compare full turns.
  A working ROM build or emulator process alone does not pass Gate 1.
- `python -m pytest -q` passes 192 tests with `SIDEDEX_ROM_PATH` pointing to
  the locally built pinned ROM, and `python app.py --self-check`
  passes at this checkpoint; these checks do not establish ROM prediction
  accuracy or all-ability coverage.
- `data/ability_coverage.csv` inventories all 328 extracted ability IDs. It
  records species references and existing source test files; 11 abilities now
  have source-anchored singles-inert or unreachable classifications, 317 need
  reachability review, and every ROM replay remains unverified. Regenerate it
  with `python tools/build_ability_coverage.py
  PATH_TO_FULL_PINNED_SOURCE`.
- A native Windows mGBA 0.10.5 worker now passes a real-ROM lifecycle smoke
  test (load, frame advance, reset, checkpoint, and memory read). Its JSONL
  interface is a feasibility spike only, without battle setup or `TurnTrace`.
- `rom_bridge.py` keeps one worker process alive, strictly validates responses,
  and checks the source lock and local ROM SHA-256 before launch. It exposes
  emulator primitives only; no card evaluation or battle state mapping exists.
- `tools/rom_oracle/` now reproduces a fixed-seed AI test trace twice
  byte-identically; the selected test passes 1/1 and an existing Wonder Guard
  smart-switch test passes 2/2. The trace is emitted by a test-only ELF hook,
  not the release ROM. It covers selected action/ability/HP callbacks and two
  active battlers, but lacks settled switch state, complete parties, volatile
  state, and secondary RNG. The broad upstream suite was stopped after about
  2,604 statuses with unrelated failures; no green aggregate is claimed.
  Build the release ROM in a clean source checkout before applying the test
  overlay; shared test artifacts can make a later release link fail.
- The persistent worker now loads the instrumented test ELF and captures its
  raw `ROMTRACE|` messages. Eight emitted lines match the fixture transport;
  1,020 emulated frames took about 186 ms after roughly 500 ms startup in the
  measured run. This validates trace transport, not state reconstruction in
  the release ROM or full `TurnTrace` parity.

## Objective and non-negotiable behavior

Make the four existing Calc cards useful in local trainer singles: conditional
player move KO estimates, trainer action estimates (moves, switches, items),
conditional enemy move KO estimates, and a recommended legal player move or
party switch. The recommendation retains the existing two-turn lookahead and
HP/survivor score in `CALC_CONTRACT.md`; it is not whole-battle optimal play.
All ability effects reachable in these trainer singles must run through the
pinned game's battle code, including their move, item, status, field, and
switch interactions. Player bag actions, doubles, whole-battle win probability,
and hosted deployment are outside this delivery.

Unknown RNG produces **model-based estimates**, never an `exact` percentage.
Display sample count, uncertainty, source/ROM identity, and the assumed sampler.
A fixed-seed result may be called ROM-verified only after full trace parity.
Missing consequential state is unknown, not zero or full HP. The UI must not
invent an RNG state. A newer state must never show a cached older result.

## Ground truth and architecture

- The sibling `pokeemerald-expansion-ref` checkout is at the source pin but is
  **sparse**. `test/battle/ai`, about 180 ability test files, `tools/mgba`, and
  `tools/mgba-rom-test-hydra` exist in Git even though they are not checked out.
  The old claim that this source has no ROM test suite was wrong. Create a
  disposable full checkout at the pin; keep the reference checkout and Sidedex
  source lock unchanged. Do not commit built ROMs, ELF files, or emulator binaries.
- WSL Ubuntu has `make` and host `gcc`, but no ARM compiler was found. Install
  the pinned project's build prerequisites in that build environment. Its
  `make check` builds a test ELF and runs the mGBA ROM test tool. Use existing
  battle tests as the first oracle before inventing a runtime bridge.
- Run the **compiled ARM ROM** in a persistent local mGBA core worker. A small
  native worker may wrap libmGBA; it must not reimplement the game's damage,
  AI, or ability logic. The source's battle functions use GBA globals and ARM
  assembly, so a host compilation of selected C files is not the default path.
  mGBA's core API supports ROM loading, frames, state, and memory access.
  Research references: [pinned Makefile](https://github.com/iriv24/pokeemerald-expansion/blob/2576d0f3f4c861c760f0c55198e37e6d7b91683e/Makefile),
  [mGBA core example](https://github.com/mgba-emu/mgba/blob/master/src/platform/example/client-server/server.c),
  [mGBA state API](https://mgba.io/docs/scripting.html).
- The current four-word snapshot does not represent every volatile battle
  field or the second RNG stream. The bridge must establish state sufficiency
  through battle-start setup plus replay or a verified emulator checkpoint.
  Arbitrary midbattle HP edits are scenario inputs, not proof of the actual
  game's hidden state. Return `incomplete-state` where reconstruction fails.

## Contract to freeze before parallel edits

The coordinator updates `CALC_CONTRACT.md` to v2 before agents edit callers.
Keep v1 behavior documented as legacy until its replacement passes tests.
Use versioned JSON messages across a persistent worker process:

- `BattleRequest`: source commit and ROM hash; resolved trainer, difficulty,
  progression, parties, and battle-start setup; ordered observed turns; player
  candidate action; optional known state for **both** RNG streams; deadline.
  Stable source IDs must map to ROM numeric IDs with explicit mismatch errors.
- `TurnTrace`: trainer action (move, destination, or item); ordered move,
  switch, item, damage, status, ability, and field events; resulting parties,
  actives, volatile state, and RNG states; terminal result. Record enough to
  replay and compare a full turn, not only damage and the chosen move.
- `CalcResult`: independent four-card statuses `verified-fixed-seed`,
  `estimated`, or `unavailable`; reason and missing paths for unavailable;
  ROM hash/engine version on every result. Estimates include numerator/count,
  denominator, sampler ID, effective sample size, and 95% interval. The UI
  never renders an estimate with the old `exact` label.
- Unknown-state sampler: generate reproducible 32-bit seeds for the ROM's two
  stream initializers, replay the battle from the declared start, and retain
  only trajectories consistent with entered observations. This is an explicit
  scenario prior, **not** a calibrated probability for the user's hidden RNG.
  If history filtering yields too few valid trajectories, show unavailable.
  Preserve correlations by evaluating full joint turns, including trainer AI.

## Agent schedule, ownership, and gates

Run at most three Luna-high implementation agents alongside the coordinator.
The coordinator alone edits the contract, `calc_service.py`, integration tests,
README, and this plan. Agents report changed files, commands, fixed-seed traces,
latency, and blockers at each gate. Do not share ownership of a file; merge each
workstream only after its gate. A failing gate blocks dependent work, not an
excuse to label the old Python calculation ROM-backed.

### Gate 0 — ROM build and oracle (ROM/source agent)

Ownership: reproducible build scripts or patch overlays under
`tools/rom_oracle/`, ROM fixtures, and dedicated oracle tests. Work in a
disposable full checkout of the pinned source. Build the ROM and run its
existing `make check` suite. Add **one** deterministic trainer singles test
with smart AI and a fixed RNG state using the existing battle test helpers;
capture ordered action, ability events, HP/status changes, and final state.
Repeat it and record source commit, build flags, ROM hash, and trace. Do not
modify or commit the sibling reference checkout as the integration artifact.

Pass: pinned ROM and test ELF build reproducibly, existing relevant tests pass,
and the new complete-turn trace is identical across fixed-seed reruns.

### Gate 1 — Generic state bridge (ROM worker agent)

Ownership: native worker under `native/rom_worker/`, Python IPC adapter in
`rom_bridge.py`, and bridge tests. Start after the v2 contract and Gate 0.
Use a battle-start initializer/test-ROM hook or a verified checkpoint plus
replay, rather than guessing internal globals from the old snapshot. Accept
the JSON request, run the actual battle loop, emit `TurnTrace`, and validate
ROM hash and source IDs. Prove one opening and one midbattle turn, including a
switch or ability effect. Serialize requests per emulator core.

Pass: complete trace and final state match the oracle for multiple fixed seeds;
unknown or unrepresentable state fails closed. Benchmark batched reset/replay
before choosing a sampling rate. If a dynamic bridge cannot reproduce state,
stop and report the exact missing state or instrumentation obstacle.

### Gate 2 — Truthful Calc state (UI agent, parallel with Gate 1)

Ownership: `planner_ui.py`, `calc_config_component.py`, `ui.css`, and
`test_calc_ui.py`. Start after the v2 contract. Add explicit battle actives
separate from browsing focus; battle-start inputs, planned action, and ordered
turn history; visible unknown values; an explicit “start at full HP” action;
optional known RNG state entry. Remove `random.getrandbits` at
`planner_ui.py:1588` and automatic current-HP-to-max filling. Bump config
version and migrate old configs without inventing state. Render verified,
estimated, pending, uncertain, and unavailable cards distinctly.

Pass: focus changes do not change actives; old/blank configs retain unknown HP
and RNG; save/load round-trips state and history; every relevant edit changes
the snapshot key and hides stale results.

### Gate 3 — Sampling and recommendation (evaluation agent)

Ownership: `rom_eval.py` and `test_rom_eval.py`. Start after Gate 1. Run matched
starting states for each legal player move/switch, using full trainer-response
and turn-outcome traces. Show KO and trainer-action frequencies with a 95%
Wilson interval. For the existing two-turn score, use paired samples across
candidates and a reproducible paired bootstrap interval for each score
difference. Choose the best action only if its 95% interval beats every other
candidate; otherwise show an uncertain ranking. Use at least 400 valid
trajectories per candidate for a recommendation. Stop at 10 seconds, report
sample counts, and show insufficient evidence rather than a false winner.

Pass: probabilities are counted from complete joint trajectories, action
frequencies sum to one on supported states, a switch can outrank moves, and
timeouts/incomplete trajectories never become exact or confident results.

### Gate 4 — Integration and ability coverage (coordinator)

Connect the worker through `calc_service.py`: persistent process lifecycle,
bounded queue, per-request cancellation/timeout, exact-key cache including
ROM hash and sampler version, and independently completed cards. Keep the
Streamlit page responsive; do not cross the Python/native boundary per frame.
Create a reachability matrix for all 328 extracted ability IDs. Classify
singles-inert/unreachable abilities with a source reason; exercise every
reachable effect through the ROM, including important move, item, status,
field, and switch interactions. Reuse the existing ability and AI tests and
add gaps; do not count one isolated test per ID as full interaction coverage.
Update the README to state the measured coverage and remaining unavailable
states.

Pass: the current Python suite and `python app.py --self-check` pass; fixed-seed
bridge traces match the ROM; a representative opening and midbattle state fill
the four cards or give specific unavailable reasons; recommendation finishes
within 10 seconds or honestly reports insufficient evidence; no stale result
survives an edit. Do not claim “all abilities” until every reachable ability
path in the matrix has a ROM-backed result and its required state is representable.

## Performance and stop rules

Keep the ROM/core loaded, save a valid pre-choice checkpoint, restore it for
each sample, batch samples inside the worker, reuse the same sampled starting
states across candidate actions, and cache identical requests. Add multiple
isolated emulator cores only if a benchmark demonstrates throughput gain.
Measure build, reset, replay, per-turn, and full recommendation costs separately.

Do not publish sampled numbers as exact, silently discard unsupported actions,
infer unknown HP/RNG, or use source-transcribed Python fixtures as a ROM oracle.
If Gate 0 or Gate 1 fails, preserve the current working Calc and report the
feasibility evidence; dependent claims and cards remain unavailable.
