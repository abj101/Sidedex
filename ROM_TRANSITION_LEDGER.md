# ROM transition orchestration ledger

Orchestrator-owned. One place for the baseline, pins, ownership, and gate state. Update as work lands.

## Baseline

| Item | Value |
| --- | --- |
| Integration branch | `codex/rom-transition-phases-1-4` |
| Worktree | `C:\Users\Abj1\.codex\worktrees\rom-transition-baseline\Sidedex` |
| Content baseline commit | `c3ca691effcce73328305452637068258c78ad5b` |
| Plan-on-top commit | `51f40a50bd995dd8978cecc7cb0ada820608ffb8` |
| Parent (original checkout HEAD) | `3a1c73c0af40bf44f03015b8d72e7c2606fb50cd` |
| Original checkout branch | `feat/rom-backed-calc` (still dirty; kept intact, never reset) |
| Baseline test state | `pytest test_rom_pool.py test_rom_query.py test_rom_oracle_dynamic.py test_rom_eval.py` → 50 passed (re-verified by orchestrator). Full suite at baseline: 282 passed, 6 skipped. `python app.py --self-check` OK |

Excluded from the baseline, still recoverable in the original checkout (and in
`snapshot/pre-rom-engine-2026-09-26`): the portrait import (`assets/**`, `tools/import_pokemon_portraits.py`),
the older `handoff.md`, and scratch `work/**`. Bring the portraits over only if a UI task needs them.

## Pins (verified by orchestrator)

| Artifact | SHA-256 |
| --- | --- |
| Source `pokeemerald-expansion` | `2576d0f3f4c861c760f0c55198e37e6d7b91683e` |
| Release ROM `pokeemerald.gba` | `0ccaaad0878a9cbfb36fbefff800e9b6dd66055924b7595038ede1e8771ec5a2` — the patch is release-neutral: a `TEST=0` build of the pinned source + patch reproduces this byte-for-byte (H1 now passes) |
| Test ELF headless `pokeemerald-test-headless.elf` | `422ade3e159916f9cc8bc1350ad274201b6af6cb9b9d38c14d073f85e982bc77` (v8c; ROM hook protocol 8, mailbox 864 B; pre-patchelf `pokeemerald-test.elf` = `23c34b8357eba2cc6a1b486d444a56fcd50852acb8846a9e585d3fdbce7ae3c1`) |
| mGBA worker `rom_worker.exe` | `0601297e8bed7ca1319dc9be86a547aa01b16624ce0de5431093e6b59d7e5122` (v3; wire protocol 2; canonical `ROMTRACE|legal|` survives the slim filter, `worker_id`, per-batch slot handle) |
| Superseded (retained for repro) | `rom_worker-v1-a3f9fca8.exe`, `rom_worker-v2-9086977a.exe`, `pokeemerald-test-v7-headless-b26561e3.elf`, `pokeemerald-test-v8.elf` (`3b34ac36…`), `pokeemerald-test-v8b.elf` (`9ca90e16…`) |

The WSL checkout `/home/abj1/rom-oracle-checkout` sits on the pinned source commit and its `git diff HEAD`
content-matches `tools/rom_oracle/dynamic_request.patch`, which applies cleanly to the pinned tree. The
patch is the reproducible source for the final build. Its `pokeemerald-test-headless.elf` hashes to the
pinned ELF. Untracked WSL fixture `test/battle/ai/sidedex_trace.c` is preserved untouched.

## Contract

`ROM_TRANSITION_CONTRACT.md` (v1) is frozen. Packages code against it; changes need an orchestrator merge
and a version bump.

## Work-package ownership (nonoverlapping)

| Package | Owns | Must not touch |
| --- | --- | --- |
| A — ROM hook + logic-only | `tools/rom_oracle/dynamic_request.patch`, WSL source edits, `tools/rom_oracle/README.md`, contract appendix for the v8 layout | pins, `rom_bridge.py`, Python parsers |
| B — worker state/transport | `native/rom_worker/*`, `rom_bridge.py`, `test_rom_bridge.py`, `test_rom_worker_smoke.py` | ROM patch, `rom_eval.py` |
| C — exact query API | `rom_query.py`, `tools/rom_oracle/run_dynamic.py`, `test_rom_query.py`, `test_rom_oracle_dynamic*.py` | ROM patch, worker, pool |
| D — scheduler + adapter | `rom_pool.py`, new transition adapter, `rom_eval.py`, `calc_service.py`, `test_rom_pool.py`, `test_rom_eval.py`, `test_calc_*` | ROM patch, worker protocol, query parser |
| E — differential verification | new fixtures/tests under `tools/rom_oracle/` and `test_rom_*`, release harness | production code (unless orchestrator approves) |

Orchestrator owns: pins (`pinned_source.json`, `rom_bridge.py` identity constants), this ledger, the
contract, cross-agent merges, and the acceptance checklist. Resolve shared `run_dynamic.py`,
`rom_bridge.py`, and fixture edits in the order A → B → C → D → E.

## Wave plan (dependency-ordered)

| Wave | Package | Owner | Gate to start |
| --- | --- | --- | --- |
| 1 | A — ROM hook v8, boundary, state export, logic-only | agent `rom-hook-a` | contract v1 (done) |
| 1 | B — worker named slots, portable blobs, boundary-aware batches, release-harness ops | agent `worker-transport-b` | contract v1 (done; ROM marker spellings frozen) |
| 2 | orchestrator repin | orchestrator | A + B artifacts reviewed |
| 2 | C — split query ops, ROM-layer outcome enumeration, parser adaptation | next | A's v8 ELF + repin |
| 3 | D — pool scheduler + transition adapter | next | B API + C card contract |
| 3 | E — differential fixtures (ELF pair) | next | A/B normal mode |
| 4 | E — release-ROM harness | next | B release ops + RELEASE_HARNESS_PLAN.md |

Frozen marker spellings all packages code against: `ROMTRACE|boundary|<kind>|<turn>|<legal-action-count>`,
`ROMTRACE|terminal|<result>|<reason>`, `ROMTRACE|unrepresentable|<field>`,
`ROMTRACE|instruction-budget-exceeded|<instructions>`.

Release-ROM decision: method and evidence gate recorded in `tools/rom_oracle/RELEASE_HARNESS_PLAN.md`.
The product keeps the `compiled-engine` label until H1/H2 and the scenario gates pass.

## Gate state

| Gate | State | Evidence |
| --- | --- | --- |
| Baseline preserved, pins verified | done | this file |
| Contract v1 frozen | done | `ROM_TRANSITION_CONTRACT.md` |
| Phase 1 | partial | A/A2 landed (`1aa0c82`, `8aee054`, `f0fa279`): v8 ops/mode/policy, choice boundary (marker emitted last), complete state export, ROM-derived `legal|` actions, real-AI no-leakage, faint/forced-switch + terminal verified, outcome-weight enumeration, trainer items root-caused (recorded_battle.c u16 cursor underflow) and now fail closed in 205 frames instead of stalling. **Open:** hold-release RNG seeding fix (A3), logic-only reduces frames (205→130) but not wall time (43.5→51.3 ms) and is a coarse 4x pump, `instruction-budget-exceeded` unexercised, voluntary player switch unimplemented |
| Phase 2 | done (wave 1) | C committed `fde93f5` + `cc3375c`: `QUERY_DAMAGE`/`QUERY_AI_ACTION`/`SIMULATE_TURN` split, v7/v8 encoders selected by the pin, `normal` + `item-ability-only` policies end to end (labels verified, mislabelled traces fail closed), ROM-enumerated `query|outcomes` consumer (the old Python composition was measurably wrong under normal crits), p50 136.7 ms / p95 144.8 ms query latency. Unsupported mechanics fail closed |
| Phase 3 | partial | D wave 1 (`3dfec57`): `EnginePool.run_jobs` scheduler (deterministic merge, 3 workers per request, worker-exit survival with retry dedupe, deadline partials, ownership/portable routing, limits), `rom_transition.py` adapter with CRN pairing. Live 3-worker demo: 11.5 verified transitions/s, 3-worker vs 1-worker byte-identical event lists. **Open:** rom_eval/calc_service integration (wave 2), stable `owner_worker` id (B2) |
| Phase 4 | partial | E wave 1 (`49e4918`): harness + 32-row fixture matrix, 8 rows green on both v8 and v8b with 0 diffs, AI-leakage/restore-determinism/identity-rejection/query-immutability checks pass, H4/H5 pass, H1 FAILED (patch not release-neutral - A3 fixing), H3 release half blocked (needs a battle-entry input script), H2 not run. **Open:** full normal-vs-logic-only matrix, release harness |
| Worker pin flip (protocol 2) | superseded | see the coordinated flip below |
| Coordinated pin flip (ELF v8c + worker v3) | done, 3 known failures | `pinned_source.json` protocol_version 8 + both hashes, `rom_bridge` identity constants, artifacts swapped. Suite: **353 passed, 8 skipped, 3 failed**; the 3 are `test_rom_eval.py::PinnedEngineEvaluation` (`insufficient-samples`, 0 complete samples) because `rom_eval.py` still assumes the v7 after-turn mailbox — package D wave 2 owns it. `app.py --self-check` OK |

## Open items after wave 2 (owner)

1. Hold-release RNG seeding fix + v8c rebuild + pre-patchelf test_elf sha, then the coordinated pin flip (package A3 → orchestrator).
2. Release neutrality: guard the recorded-player call sites so a `TEST=0` build is byte-identical to `0ccaaad0…` (package A3).
3. Canonical `ROMTRACE|legal|` in the worker slim allow-list; `worker_id`; per-batch slot selection; v3 worker binary for repinning (package B2).
4. Pin flip is ONE coordinated step: copy v8c → `pokeemerald-test-headless.elf`, update `pinned_source.json` (`protocol_version` 8, `headless_elf_sha256`, `test_elf_sha256`) and `rom_bridge.ENGINE_ELF_SHA256`, plus the v3 worker sha (orchestrator), then re-run the full suite.
5. `rom_eval.py` / `calc_service.py` integration on the scheduler + adapter, with cache-key identity extended to the full contract §1 tuple (package D wave 2 — waits for the pin flip).
6. Release harness wave 2: battle-entry input script, then H2 and the scenario comparisons under the `RELEASE_HARNESS_PLAN.md` gate (package E wave 2).
7. Logic-only measurement discrepancy to settle on the pinned artifact: A measured 205→130 frames on a mid-battle branch, C measured 754→1086 frames on a root transition; the phase-1 gate needs one agreed measurement set.

## Baseline limitations to keep visible (not to be papered over)

One replay turn; active slot zero only; one in-memory savestate slot restored for every batch entry; partial
state export (no terrain/trick-room/screens/hazards readable fields, no trainer bag inventory in the
resolved trace, no battle outcome marker); no logic-only mode or presentation bypass; one pooled worker per
request with at most 16 samples; trainer items predicted by queries but excluded from resolved
recommendations.
