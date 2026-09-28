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
| mGBA worker `rom_worker.exe` | `a3f9fca863136b9f1cb66c0493c692fc6a63d6e0c7fae178fb6089203f3cf2` |

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

## Gate state

| Gate | State | Evidence |
| --- | --- | --- |
| Baseline preserved, pins verified | done | this file |
| Contract v1 frozen | done | `ROM_TRANSITION_CONTRACT.md` |
| Phase 1 | open | — |
| Phase 2 | open | — |
| Phase 3 | open | — |
| Phase 4 | open | — |

## Baseline limitations to keep visible (not to be papered over)

One replay turn; active slot zero only; one in-memory savestate slot restored for every batch entry; partial
state export (no terrain/trick-room/screens/hazards readable fields, no trainer bag inventory in the
resolved trace, no battle outcome marker); no logic-only mode or presentation bypass; one pooled worker per
request with at most 16 samples; trainer items predicted by queries but excluded from resolved
recommendations.
