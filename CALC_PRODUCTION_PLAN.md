# ROM Calc: local production plan

Status: plan for the next implementation run, 2026-09-26. Production here means
the local Sidedex app with all four Calc cards usable for supported trainer
singles. Hosted deployment is a separate project. This plan supersedes the
agent schedule in CALC_IMPLEMENTATION_PLAN.md.

## Implementation checkpoint (2026-09-27)

Gates 1-5 are implemented in the working tree. The v4 dynamic hook
(`run_dynamic.py`, `dynamic_request.patch`, `numeric_id_map.json`) encodes
complete trainer-single parties, legal move/switch candidates, IVs/EVs, HP, PP,
status, and one replayed history turn. `rom_bridge.batch_turns` and the worker
batch path carry the fixture without dropped events. The UI focuses battlers as
the active pair and defaults new scenarios to full editable HP.

Gate 4's evaluator (`rom_eval.py`, `test_rom_eval.py`) now executes: it was
shipped with an `IndentationError` and had no test, so no card could be
produced. It now parses complete turns, counts conditional KO and AI-action
frequencies with Wilson intervals, scores candidates with a paired bootstrap,
and returns `engine-fixed-seed` or `estimated` per card. Verified against the
pinned artifacts: a known-seed battle-start snapshot filled all four cards in
0.28 s, and an unknown-seed snapshot filled them as `estimated` with
effective_samples 8 in 5.45 s (under the 10 s ceiling).

**Open blocker (two-turn scoring).** The v4 hook completes at
`battleTurnCounter >= replayCount + 1` and emits that counter's after-turn
snapshot, so exactly one candidate turn resolves per request. The
recommendation therefore reports `scored_turns: 1` and labels itself
`One-turn HP and survivor objective`; it does not fake the plan's two-turn
objective. Closing it needs a hook/overlay change that resolves a second
candidate turn and a rebuilt, re-pinned ELF.

Checks at this checkpoint: `python -m pytest -q` (218 passed, 4 skipped, 7
subtests) and `python app.py --self-check` both pass.

## What is proven

- The source is pinned to Emerald Imperium commit
  2576d0f3f4c861c760f0c55198e37e6d7b91683e. A release ROM, a test ELF,
  and a Windows mGBA worker have been built locally. The worker can reset,
  checkpoint, read EWRAM, and now write a bounded EWRAM request.
- The dynamic oracle overlay accepts an 80-byte request in the instrumented
  test ELF. It controls two species, levels, natural abilities, items, four
  moves each, AI flags, the player move, and both four-word RNG streams. The
  first host proof returned 89 trace records reproducibly; changing level or
  main RNG changed damage/final HP. This is real compiled ARM battle code.
- The earlier fixed oracle trace has 170 records over two turns including a
  switch, ability, party/active state, and both RNG streams. The persistent
  worker transports it without overflow.
- Calc already builds schema-v2 snapshots and draws four independent card
  slots, but calc_service.py cannot evaluate them: rom_bridge.evaluate does not
  exist. The v1 Python model must never be used as a silent fallback.
- Focus and HP behavior in planner_ui.py still differ from the recorded product
  decision: focus does not select actives, and HP stays unknown until the user
  presses the full-HP action. A direct numeric level cap also needs a defined
  mapping to the ROM progression milestone.
- The current dynamic runner launches one worker per request and reads debug
  text in frame batches. Its caller supplies the ELF hash. That is a proof
  harness, not a production evaluator or a pinned local artifact.

The test ELF has not been shown equivalent to a release-ROM turn for arbitrary
states. The mailbox covers one active Pokémon per side and one turn. It does
not cover full parties, switches, IVs/EVs, current HP, PP, status, field,
history, or two-turn recommendations. No four-card result is production ready.

## Smallest viable architecture

Use the pinned, instrumented ARM test ELF as the local calculation kernel.
Extend its existing battle-test request hook and let the game's battle code
resolve damage, AI, abilities, items, and switches. Keep the local mGBA core
alive. Send one validated, versioned battle-start request, replay observed
turns, save a pre-choice checkpoint, and run candidate actions and RNG seeds
from that checkpoint. Return structured turn outcomes to Python, where the
four cards are counted and scored. Do not start by injecting a complete
midbattle state into the unmodified release ROM or porting battle mechanics
to Python.

The default supported state is a fresh trainer single with complete displayed
party builds, focused active battlers, full editable HP, and unknown RNG.
Recorded turns may extend that state once replay works. An edited midbattle
state is accepted only when its consequential fields can be reconstructed;
otherwise that card states the exact missing input. This preserves useful
opening-battle results while keeping unsupported scenarios honest.

Pin the executable worker and engine ELF separately from the release-ROM
source identity. The UI may say “compiled game engine” after the test-ELF
oracle gate. Reserve “ROM verified” for fixed-seed results that also pass a
controlled release-ROM comparison. Unknown-seed output is always a
model-based estimate with sample count and interval.
The v3 result status needs an engine-fixed-seed value for the general
compiled-engine case; verified-fixed-seed is reserved for a state covered by
release-ROM comparison. The UI must render both explicitly.

## Gates and ownership

The coordinator owns CALC_CONTRACT.md, calc_service.py, integration tests,
README.md, and this plan. Freeze a compact v3 request/result contract first:
artifact hashes, numeric-ID mapping, battle-start state, replayed turns,
candidate action, both RNG streams, ordered outcome events, four independent
card statuses, error codes, and a hard deadline. The contract must say exactly
which fields the first supported scenario can represent. Save the current
uncommitted work as a recoverable checkpoint before parallel edits.

Run at most three Luna-high agents at once. Give each a nonoverlapping file
set. Agents report only changed files, the focused check result, measured
latency when relevant, and a concrete blocker. The coordinator integrates
only at the gates below.

| Gate | Owner and files | Deliverable | Pass condition |
| --- | --- | --- | --- |
| 1. Engine request | Agent A: tools/rom_oracle/**, source overlay, dedicated oracle fixtures/tests, and extracted numeric-ID map | Extend the dynamic hook from one pair/one move to complete trainer singles parties, legal move/switch choices, source IDs, build stats, HP/PP/status, and battle-start field. Replay one prior turn rather than writing volatile RAM by guesswork. Emit a bounded structured turn result and pin the engine ELF hash/build recipe. | Two fixed-seed opening requests differing in level or RNG remain repeatable; one switch/ability turn and one replayed next turn agree with the game's selected battle tests. Invalid IDs/state fail closed. |
| 2. Worker path | Agent B: native/rom_worker/**, rom_bridge.py, test_rom_bridge.py, test_rom_worker_smoke.py | Add one batch-turn operation around the existing persistent core and checkpoint. Validate request/reply versions, identity, size, timeouts, and process recovery. Reuse the same pre-choice state for candidate/seed runs; do not cross Python/native per frame. | The Gate 1 fixture returns through the worker without dropped events, a second request works in the same process, and a bad request or worker exit gives a bounded error. Measure per-turn and batched replay time. |
| 3. Truthful UI state | Agent C: planner_ui.py, calc_config_component.py, ui.css, test_calc_ui.py | Make focused battlers the active pair, initialize new scenarios at full editable HP, preserve explicit saved HP, keep both RNG streams unknown unless entered, and map numeric cap to a real progression milestone or show a specific unsupported-context reason. Keep history and config migration intact. | Focus, HP, cap, planned action, save/load, and all four pending/unavailable/estimated render paths produce the exact current snapshot; a later edit cannot show an older result. |
| 4. Evaluation | Agent C after Gate 3 (or a new Luna-high agent with sole ownership): rom_eval.py and test_rom_eval.py | Parse full turns; calculate conditional player/enemy KO frequencies and the joint AI action distribution; score every legal first move/switch with the existing two-turn HP/survivor objective. Use paired seeds across candidates and a reproducible interval. Return an uncertain ranking when evidence overlaps. | A known-seed fixture fills the applicable four cards; unknown-seed fixtures show sample counts and intervals; AI frequencies sum to one; a switch can win; incomplete trajectories and timeouts never become confident results. |
| 5. Integration and local release | Coordinator: calc_service.py, CALC_CONTRACT.md, test_calc_integration.py, README.md | Wire the evaluator to the keyed background service with bounded work, cancellation of stale snapshots, per-card failure, startup artifact checks, and a clear local setup command. Retain the legacy model only for explicit v1 tests. | Representative trainer opening and replayed midbattle flows fill all four cards or show specific per-card reasons; no stale result; local restart recovers; artifact mismatch refuses numbers. |

Agent B can build the protocol and bounded batch path while A extends the
hook, but its final fixture check waits for Gate 1. Agent C's UI work is
independent. Evaluation starts only after the engine outcome format and
worker path are stable. The coordinator merges each owned file set once,
then runs one end-to-end gate before expanding scope.

## Exact agent briefs for the implementation run

**Luna-high A — engine oracle.** Work only in the Gate 1 files. Start from
dynamic_request.patch and run_dynamic.py. Produce a versioned request and
bounded result for complete trainer singles from battle start, including
switches and one replayed turn. Use the game's test helpers and native battle
code. Pin the built ELF; compare the focused deterministic cases with selected
upstream tests. Report the first state field that cannot be represented;
do not invent a RAM mapping or run the broad upstream suite.

**Luna-high B — persistent worker.** Work only in the Gate 2 files. Make
the worker consume a batch of requests from one valid checkpoint and return
structured bounded replies. Validate hashes, versions, lengths, deadlines,
and worker recovery. Use Agent A's fixture at the handoff. Give per-turn and
batch timings; do not add parallel cores until one core is measured.

**Luna-high C — Calc state, then evaluator.** First work only in the Gate 3
files to make snapshot semantics match the accepted focus/HP behavior and
preserve saved values. After Gate 3 merges and Gates 1–2 pass, own Gate 4's
two files. Count only completed ROM trajectories, preserve joint AI/action
correlations, and return per-card statuses. Use one known-seed, one sampled,
and one switch fixture; avoid a per-mechanic test suite.

## Focused verification and release decision

1. At each gate, run its named focused fixture/tests only. Do not run the
   thousands-case upstream suite during iteration; the prior aggregate had
   unrelated failures. Do not create a 328-ability checklist before the
   engine path works. Add a targeted regression only for a bug actually found.
2. Before enabling the “ROM verified” label, replay one controlled full turn
   against the pinned release ROM (same battle-start inputs and seeds) and
   compare ordered choices, damage, status, switching, and resulting state.
   If that comparison is not obtainable, ship the truthful “compiled game
   engine” label and retain the distinction in README.
3. Benchmark one persistent core with all legal first actions and shared
   seeds. Keep the existing ten-second recommendation limit. Choose sample
   count adaptively within that limit; show the actual count/interval and
   “ranking uncertain” when the winner is not resolved. Do not promise 400
   samples per candidate unless the benchmark proves they fit.
4. Run the Sidedex Python suite and app self-check once after integration,
   then one manual local walkthrough: select trainer, focus each side, edit
   HP, choose action, view all cards, switch, save/reload, and change a field
   while a request is running. Verify no old result appears.
5. Local release requires a documented build/launch recipe, pinned artifact
   hashes, a missing-artifact message that names the setup step, and a clean
   failure on mismatch/timeout. Do not commit or redistribute the game ROM,
   test ELF, or mGBA binary as source-controlled artifacts.

The shortest path is: version the current dynamic hook, extend its scenario
input, batch it in the persistent core, fill the four cards from complete
turns, and verify the local flow. Further mechanics and broader oracle
coverage follow concrete unsupported states observed in that flow.
