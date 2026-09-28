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
| Release ROM `pokeemerald.gba` | `0ccaaad0878a9cbfb36fbefff800e9b6dd66055924b7595038ede1e8771ec5a2` |
| Test ELF headless `pokeemerald-test-headless.elf` | `b26561e3f0590de47167730f93ed11427450dbc835a306272c9b898d39b473db` |
| mGBA worker `rom_worker.exe` | `9086977abe27597aa67ed8311c82f1e7f5e66a5f0b2fe4f665b7518a1c82fe86` (wire protocol 2, worker version 0.2.0; previous v1 binary retained as `rom_worker-v1-a3f9fca8.exe`) |
| Test ELF v8 (STAGED, not yet pinned) | `3b34ac36db5b4139cde8a9308e2dba329fe9e87fecc812c046d0ebe31848d818` at `native/rom_worker/build/mingw/pokeemerald-test-v8.elf` (ROM hook protocol 8, mailbox 864 B) |

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
| Phase 1 | partial | A landed (commit `1aa0c82`): v8 ops/mode/policy, choice boundary, state export, RNG injection, real AI + no-leakage verified. Open: trainer items in a resolved turn (blocked, documented in `tools/rom_oracle/README.md`), forced-switch/faint boundary unverified, logic-only is a coarse 4x pump (130 vs 205 frames) not a presentation audit, outcome-weight enumeration not yet in the ROM layer |
| Phase 2 | open | C starting on the frozen v8 layout |
| Phase 3 | open | — |
| Phase 4 | open | fixture matrix defined in `tools/rom_oracle/FIXTURE_MATRIX.md`; release method decided in `RELEASE_HARNESS_PLAN.md` |
| Worker pin flip (protocol 2) | done | `rom_bridge.WORKER_SHA256` = `9086977A…`, `rom_worker.exe` = v2. Verified: focused set 75 passed/2 skipped; full suite `--ignore=work` 289 passed/7 skipped; `app.py --self-check` OK |

## Baseline limitations to keep visible (not to be papered over)

One replay turn; active slot zero only; one in-memory savestate slot restored for every batch entry; partial
state export (no terrain/trick-room/screens/hazards readable fields, no trainer bag inventory in the
resolved trace, no battle outcome marker); no logic-only mode or presentation bypass; one pooled worker per
request with at most 16 samples; trainer items predicted by queries but excluded from resolved
recommendations.
