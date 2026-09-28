# ROM transition orchestration ledger

Orchestrator-owned. One place for the baseline, pins, ownership, and gate state. Update as work lands.

## Baseline

### 2026-09-28 execution restart at `d043609d84219fc9cac4ed1060e4ed817ce24f49`

The active integration worktree is `C:\Users\Abj1\.codex\worktrees\rom-transition-orchestrator\Sidedex`
on `codex/rom-transition-execution`. The original dirty `feat/rom-backed-calc`
checkout and its untracked `work/` remain untouched. Pinned release ROM,
headless ELF, and worker SHA-256 are respectively `0ccaaad0878a9cbfb36fbefff800e9b6dd66055924b7595038ede1e8771ec5a2`,
`422ade3e159916f9cc8bc1350ad274201b6af6cb9b9d38c14d073f85e982bc77`,
and `0601297e8bed7ca1319dc9be86a547aa01b16624ce0de5431093e6b59d7e5122`.
All match both pin locations. `python -m pytest --ignore=work -q`:
377 passed, 9 skipped, 33 subtests passed (43.20 s).
`python app.py --self-check`: passed.

Contract Appendix B freezes the v2 child/binary integration interface before
implementation. Benchmark fixture IDs are P1 (root move), P29 (deeper move
and branch), P25 (voluntary switch), P26 (faint/forced replacement), P19
(trainer item), and P27/P28 (terminal win/loss), using the existing
`tools/rom_oracle/fixtures/` JSON files and their fixed dual RNG seeds in
both normal and logic-only modes. P19/P25 are unsupported at baseline, so
their baseline latency/throughput is undefined, not zero. The prior measured
root query is p50 136.7 ms/p95 144.8 ms; one same-input transition measured
normal 205 frames/43.5 ms and logic-only 130 frames/51.3 ms. A fresh
per-fixture p50/p95 table is required at the final pin; these older single
transition numbers are comparison context, not acceptance measurements.

Fresh v8c baseline on the pinned worker: five repeated transitions per mode
from each fixture's fixed root, measured by `DiffSession.transition` with the
same request and both RNG streams; p95 is the largest of five observations.
This excludes root construction and savestate restore, which the final
benchmark must report separately.

| Fixture | Normal p50/p95 ms | Logic-only p50/p95 ms | Normal/logic frames |
| --- | ---: | ---: | ---: |
| P1 root move | 44.98 / 45.51 | 53.85 / 60.15 | 205 / 135 |
| P29 first branch move | 46.31 / 51.35 | 52.39 / 54.27 | 205 / 135 |
| P26 faint → forced switch | 53.27 / 53.99 | 63.35 / 64.27 | 295 / 170 |
| P27 terminal win | 70.38 / 72.09 | 80.81 / 81.45 | 325 / 285 |
| P28 terminal loss | 75.11 / 75.96 | 75.20 / 76.11 | 430 / 325 |

P29's *deeper* transition cannot be timed through the current public API,
which has no child token. P25 and P19 remain unsupported. This benchmark
confirms that the current logic-only mode is slower in wall time on four of
five fixture rows and has no established throughput win.

P1 fixed-root query microbenchmark (five runs, root savestate restored before
each, timing the ROM query execution only): `QUERY_DAMAGE` p50/p95
12.75/13.34 ms, `QUERY_AI_ACTION` 11.98/12.22 ms, 25 frames each.
Root construction was 136.23 ms in the same run. The 528,448-byte root
savestate restored in 0.19/0.22 ms p50/p95 over ten runs. These primitive
timings explain why the earlier end-to-end query was around 137 ms; they do
not establish end-to-end card latency at deeper nodes.

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

`ROM_TRANSITION_CONTRACT.md` Appendix B (v2) and Appendix C (v2.1) are the
frozen interface; changes need an orchestrator merge and a version bump.
Orchestration is governed by `ROM_TRANSITION_FINISH_PLAN.md` (v2), which
supersedes the earlier package plan.

## Ownership (one writer per file set)

See `ROM_TRANSITION_FINISH_PLAN.md` section 4. Orchestrator owns: contract,
this ledger, the plan, `rom_identity.py`, `pinned_source.json`, `sdr2_vectors/`,
`make_sdr2_vectors.py`, `bench.py`, artifact pins/copies, WSL `sidedex/integration`.

## Current gate table (single source of truth)

| Gate / item | State | Evidence |
| --- | --- | --- |
| Baseline preserved, pins verified | done | this file; 377 passed / 9 skipped |
| Wave 0: A1 WIP frozen | done | `codex/rom-hook-a` `c82c948`; WSL `sidedex/a1-export` `4b1b3442` over `sidedex/base-v8c` `27497866` |
| Wave 0: contract v2.1, vectors, `rom_identity.py` | done | Appendix C, `tools/rom_oracle/sdr2_vectors/`, `test_sdr2_vectors.py`; suite 378 passed / 9 skipped |
| Gate 0 | see git log | the Gate 0 commit SHA is the base every agent prompt cites |
| B0 profile | dispatched | agent `pkg-b`; output `.codex/artifacts/B/B0-profile.md` |
| C closed-loop release root | dispatched | agent `pkg-c`; milestones under `.codex/artifacts/C` |
| A1 export producer (tags 1-10) | done (accepted with caveats) | `codex/rom-hook-a` `714c237`, WSL `sidedex/a1-export` `c9001b29`, ELF v9a `04060ed8`; caveats: acceptance ran on the old wire-2 worker with a scratch decoder, compact==trace is a framing check (audit) |
| B codec / rebase / perf follow-up | done, needs V0/V1 fixes | `codex/rom-worker-b` `968d677`, worker `1529b4de`; merged on integration `f3e37dc`; wire 2 is retired by this worker (wire 3 only) |
| C1 closed-loop release root | done | `codex/rom-release-c` `e78c6c9`; root sha is bound to worker 0601297e, must be regenerated after the worker flip |
| C2 H3 + comparison plumbing | in progress | agent `pkg-c` |
| Gate V0: v9a registered as CANDIDATE pin (no flip) | done | `80af947`; contract C.6 `39498fa`/`22d4f26` |
| Gate V1: worker flip (wire 3, `rom-v5`, worker `f7b1c1e3`) on the v8c ELF | done | integration `e7a83c7`, +C3 `a6efcbf`; suite 442 passed / 12 skipped, `app.py --self-check` ok |
| C1/C2/C3 release root, H3, comparison plumbing | done | `codex/rom-release-c` `329d92f`; readable release fields: party 30/32, active 32/37, battle 5/10, field 6/6, side 11/12, rng 8/8 |
| B, B2, B3 | done | `codex/rom-worker-b` `67c7351`, worker f7b1c1e3 reproducible |
| Gate V2 verifier (`codex/rom-gate-v2` `513f4aa`, 44 tests) | FAILING: 21 pass / 23 fail | findings R1 terminal tag rule (worker), R2 chaining from a child (ROM hook), R3 v9a differs from v8c on 19 green rows (ROM hook), R4 P11 encoder, O1 boundary enum. Not merged to integration (would turn the suite red) |
| R1+R4+O1 fixes | dispatched (Sonnet, `codex/rom-worker-b`) | new candidate worker to be re-pinned |
| R2 chaining fix | dispatched (Sonnet, WSL `sidedex/a1-fix-r2`) | |
| R3 divergence fix | dispatched (Sonnet, WSL `sidedex/a1-fix-r3`) | |
| A2 mechanics | in progress, must rebase onto the R2/R3 hook fixes | `pkg-a1` |
| D (Sonnet Agent), Gate F (single ELF flip: v9b + D), E, final | not started | D starts once V2 is green |

Superseded by the finish plan and removed from this table: the "3 known
failures" pin-flip row, the v8-only phase gates, and the logic-only frame
discrepancy item (replaced by the plan section 6 adopt-or-park rule).

## Baseline limitations to keep visible (not to be papered over)

One replay turn; active slot zero only; one in-memory savestate slot restored
for every batch entry; partial state export; no public child token; no
logic-only presentation audit; trainer items predicted by queries but excluded
from resolved recommendations; release-ROM root not yet at a battle.
