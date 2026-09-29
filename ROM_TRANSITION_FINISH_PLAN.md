# Finish phases 1–4: revised orchestration plan (v2)

Supersedes the "Finish phases 1–4: orchestrator execution plan" for execution.
Contract, ledger, and fixture matrix remain authoritative for semantics. Written
2026-09-28 after auditing the interrupted run.

## 1. Where the run actually stands

| Item | Location | State |
| --- | --- | --- |
| Integration branch | `codex/rom-transition-execution` @ `5e16c92`, worktree `C:\Users\Abj1\.codex\worktrees\rom-transition-orchestrator\Sidedex` | Clean. Contains Wave 0 (contract Appendix B `e129860`, `8a93911`; baselines `d75ec91`, `c6e16b1`) plus B merged and reverted |
| A (ROM hook) | `codex/rom-hook-a` @ `e129860`, worktree `…\rom-hook-a\Sidedex` | **Uncommitted**: v9 request tail, `gHeap` result reservation, SDR2 header, error record (tag 11), `binary-ready` marker, `SIDEDEX_LOGIC_ONLY` guard, P19/P25 fixture tests, `dynamic_trainer_item.json`. **No typed success records (tags 1–10).** Every v9 transition returns `incomplete-state` |
| B (worker) | `codex/rom-worker-b` @ `9589b6f` | `batch_turns_v3`, child capture before parent restore, export/import, SDR2 verify, `--save`. Merged as `5bc4744`, then reverted because no ELF could produce a successful SDR2 result |
| C (release root) | `codex/rom-release-c` @ `8f3db31` | Cold-boot script reaches `CB2_Overworld` at frame 15528 (map 25/40, the moving truck, party 0). There's no battle yet. The empty-save fallback was correctly rejected. H2/H3 are blocked |
| D, E | — | Not started |
| Pins | ELF v8c `422ade3e…`, worker v3 `0601297e…`, release ROM `0ccaaad0…` | Unchanged and valid. Suite: 377 passed / 9 skipped |

Wave 0 measurements (ledger):
- Transition p50 is 45–75 ms in normal mode. Logic-only is slower on 4 of 5 rows even though it uses 30–40% fewer frames.
- The query primitive itself takes about 12–13 ms. Building the root takes 136 ms. Restoring a savestate takes 0.2 ms, and a savestate is 528 KB.

## 2. Audit: why the first plan stalled, and what changes

1. **A and B ran in parallel against a wire format that had no reference bytes.** B finished a consumer before any producer existed, so its merge had to be reverted. **Fix:** the orchestrator publishes golden SDR2 byte vectors and a single reference codec (`tools/rom_oracle/sdr2.py`) before any more A or B work. A must emit these bytes and B, D, and E must parse them. B lands at the vertical-slice gate (§5, Gate V), not before.
2. **The critical path was hidden inside package A.** A had to deliver the complete typed projection *and* switch/item/logic-only mechanics through a single writer and a single WSL checkout. **Fix:** split A into **A1 (export producer, critical path)** and **A2 (mechanics)**. Each gets its own WSL git worktree on its own branch of the pinned pokeemerald source. The orchestrator merges those branches in WSL and regenerates `dynamic_request.patch` from `git diff <pinned_sha>`. Nobody hand-merges a patch file.
3. **The performance work targeted the wrong cost.** The measured end-to-end query time (~137 ms) is almost all root construction (136 ms). The query itself is ~12 ms, and restore is 0.2 ms. Logic-only reduced frames but increased wall time, so the cost per frame is CPU work, not frame count, and the 4× pump adds work. **Fix:** profile first (B0), then use:
   - warm per-worker root/ready checkpoint caching (D)
   - the binary hot path with no per-record `ROMTRACE` printf or JSON (A1/B)
   - headless mGBA settings: no video rendering, audio off, no sync (B0)
   
   Logic-only becomes a measured *option* with an explicit adopt-or-park rule (§6), not a phase-1 blocker. The original plan already allowed "a selected faster default".
4. **Most "unsupported" rows are harness limits, not ROM limits.** P20–P23 are unsupported only because the clear-field validator rejects preset weather, terrain, hazards, and screens. With child tokens, they can be reached honestly from the root → child → grandchild path through real moves (Rain Dance, Stealth Rock, Reflect…). **Fix:** E builds those rows as multi-step child-token scenarios once D1 lands. The request validator is not loosened.
5. **The release root had no closed-loop driver.** Fixed-frame key scripts drift. **Fix:** C drives input until a condition holds (callback, map, position, flags) and stores a hashed milestone savestate at each checkpoint. That means the truck → Littleroot → clock → Route 101 → starter → first battle route can be resumed and each hop is reproducible. The savestate is generated only by the unmodified pinned `.gba` running that script.
6. **Ownership had one split file.** `rom_bridge.py` had both B-owned code and orchestrator-owned identity constants. **Fix:** Wave 0 moves the identity constants into `rom_identity.py`, which the orchestrator owns. `rom_bridge.py` re-exports them.
7. **Integration was missing a few basics.** There was no artifact staging location, no handoff manifest, no test for whether a dependency is ready, and no provision for agents that can't see each other's worktrees. **Fix:** §7 defines the staging directory, manifests, and prompt template.
8. **The ledger still shows superseded gates.** For example, the "3 failed" pin-flip row and the logic-only frame discrepancy item. **Fix:** Wave 0 rewrites a single current gate table.

## 3. Completion definition (unchanged intent, measurable)

Phases 1–4 are complete only when every row below is green at the final pin. Otherwise the ledger names the failing fixture or metric and that phase stays partial.

| # | Criterion | Evidence |
| --- | --- | --- |
| C1 | Public `SIMULATE_TURN(token, action, rng)` returns an immutable child `DecisionState`: portable blob + SHA-256 + full identity, `decoded` from SDR2 tags 1–6, ROM legal actions (tag 7), both RNG streams, trainer action, and events. Terminal cases return no child | Live root→child→grandchild. The child is restored on a **different** worker and branched twice, deterministically |
| C2 | Voluntary switch (P25), faint/forced replacement (P26), switch-in effects (P24), trainer items per real ROM difficulty rules (P19), and terminal win/loss (P27/P28) all go through the public API | A differential fixture per case, both modes |
| C3 | `QUERY_DAMAGE` and `QUERY_AI_ACTION` work at the root and at deeper nodes. The source token is byte-identical after a query, and exact cards survive a transition failure | Tests plus p50/p95 at root and depth 2 |
| C4 | The hot path is binary SDR2 over batched `batch_turns_v3`. `ROMTRACE` is used for diagnostics only | IPC bytes, parse time, and a compact-vs-trace equality test |
| C5 | Performance is measured on parity-passing transitions only. Normal mode improves on the Wave 0 baseline, **and** warm end-to-end card latency is ≤ 40 ms p50 (was ~137 ms). Logic-only is adopted or parked under §6 | `tools/rom_oracle/bench.py` report committed to the ledger |
| C6 | 32-row matrix: every row is green in normal vs logic-only (if logic-only is kept), **or** has a source-cited ROM limitation. None is unsupported only because the harness lacks it | `test_rom_diff_elf.py` |
| C7 | Release root is reproducible from the pinned `.gba` and a hashed script. H3 detects the release boundary. H2 passes for an enumerated `release_ready` set. H1/H4/H5 stay green | `test_rom_release_live.py` evidence |
| C8 | Full suite passes (`python -m pytest --ignore=work -q`), `python app.py --self-check` passes, and a manual four-card Calc walkthrough works. The label stays **compiled-engine**, except where C7 proves narrower coverage | Ledger |

## 4. Package map and ownership (one writer per file set)

| Pkg | Branch / worktree | Owns | Consumes | Must not touch |
| --- | --- | --- | --- | --- |
| **O** orchestrator | `codex/rom-transition-execution` / `rom-transition-orchestrator` | `ROM_TRANSITION_CONTRACT.md`, `ROM_TRANSITION_LEDGER.md`, this plan, `rom_identity.py`, `tools/rom_oracle/pinned_source.json`, `tools/rom_oracle/sdr2_vectors/**`, `tools/rom_oracle/make_sdr2_vectors.py`, `tools/rom_oracle/bench.py`, artifact pins/copies, WSL merge branch `sidedex/integration` | everything | — |
| **A1** export producer | `codex/rom-hook-a` / `rom-hook-a`; WSL worktree `/home/abj1/rom-a1` on `sidedex/a1-export` | hook export code in WSL branch, `tools/rom_oracle/README.md` v9 section, `test_rom_oracle_dynamic.py` v9 cases | contract App. B/C, golden vectors | pins, worker, Python host code, `dynamic_request.patch` (O regenerates) |
| **A2** mechanics | `codex/rom-hook-a2` (new, from integration after Gate V) / new worktree `rom-hook-a2`; WSL `/home/abj1/rom-a2` on `sidedex/a2-mechanics` | voluntary switch, trainer-item difficulty rules, switch-in, instruction budget, logic-only audit in WSL branch; `tools/rom_oracle/dynamic_*.json` request fixtures; README mechanics section | A1 export | same as A1 |
| **B** worker + codec | `codex/rom-worker-b` / `rom-worker-b` | `native/rom_worker/**`, `rom_bridge.py` (minus identity), `tools/rom_oracle/sdr2.py`, `tools/rom_oracle/run_dynamic.py` v9 encoder, `test_rom_bridge.py`, `test_rom_worker_smoke.py`, `test_sdr2.py` | golden vectors, A1 ELF | WSL source, `rom_eval.py`, pins |
| **C** release root | `codex/rom-release-c` / `rom-release-c` | `tools/rom_oracle/release_harness.py`, `release_input_script.json`, `release_scenarios.json`, `RELEASE_HARNESS_PLAN.md`, `test_rom_release_harness.py`, `test_rom_release_live.py` | B worker (`set_keys`, `--save`, savestates) | ROM patch, production evaluator |
| **D** host adapter | `codex/rom-host-d` (new, from integration after Gate 0) / new worktree `rom-host-d` | `rom_transition.py`, `rom_pool.py`, `rom_eval.py`, `rom_query.py`, `calc_service.py`, their tests | `sdr2.py`, B API, contract | worker C, WSL, pins |
| **E** verification | `codex/rom-verify-e` (new, after Gate D) / new worktree `rom-verify-e` | `test_rom_diff_elf.py`, `tools/rom_oracle/fixtures/**`, `rom_diff_harness.py`, release comparison evidence | everything as consumer | production code (O approval needed) |

Build isolation: each ROM agent builds only in its own WSL worktree. Generated ELF, ROM, EXE, savestate, and `.sav` files never enter Git. The original `feat/rom-backed-calc` checkout and every untracked `work/` directory stay untouched.

## 5. Waves and gates

At most **three implementers active** at once. The orchestrator never implements package code. It merges, pins, measures, and decides.

### Wave 0 — orchestrator (sequential, short)

- **0.1 Tooling.** Done 2026-09-28:
  - codegraph indexes exist in all four worktrees
  - the headroom proxy runs on `127.0.0.1:8787` (not durable; see §8)
  
  Create `.codegraph` in each new worktree with `codegraph init` (the SessionStart hook does this for the main checkout only).
- **0.2 Freeze A's WIP.** Commit A's uncommitted diff on `codex/rom-hook-a` as `wip(A1): v9 request tail, result reservation, error record` so nothing lives only in a working tree.
- **0.3 WSL branches.**
  - In `/home/abj1/rom-oracle-checkout`, commit the current v8c patch state as branch `sidedex/base-v8c`, and verify `git diff 2576d0f3…` equals `dynamic_request.patch`.
  - Commit A's WIP source as `sidedex/a1-export`.
  - Add `git worktree`s `/home/abj1/rom-a1` and `/home/abj1/rom-a2` (a2 is created later from integration).
  - Leave the untracked `test/battle/ai/sidedex_trace.c` alone.
- **0.4 Contract v2.1 (Appendix C).** Promote A's per-tag tables (README "v9 compact result record schema") into the contract, and add:
  - enum tables: event kinds, terminal reasons, unsupported reason/source codes, error codes, boundary kinds
  - child transport decision: the inline child summary is `{handle, bytes, sha256}`, and the blob comes from `export_state` on demand (B already built this). The public token always carries a blob or a still-live exportable handle before it leaves the worker's request scope
  - the §6 performance decision rule
  - frozen benchmark fixture IDs P1, P29, P25, P26, P19, P27, P28, each with dual RNG
- **0.5 Golden vectors.** `make_sdr2_vectors.py` writes byte files plus JSON expectations for:
  - choice (P1-shaped)
  - forced-switch (P26-shaped)
  - terminal win
  - unsupported status
  - negatives: bad CRC, bad length, duplicate tag, missing tag 7, oversize, wrong requestId
- **0.6 `rom_identity.py`.** Move the identity constants out of `rom_bridge.py` (re-exported, so there are no import changes elsewhere).
- **0.7 Ledger.** Replace the gate table with one current table and delete superseded open items.

**Gate 0:**
- contract v2.1, vectors, `rom_identity.py`, and WSL branches are committed/merged on integration
- the suite is still green (377/9)
- every agent prompt cites the v2.1 commit SHA

### Wave 1 — A1, B, C in parallel

**A1: produce real SDR2 (critical path).**
- Emit tags 1–10 at every boundary kind from named C sources. Emit tag 11 for anything still unreadable, never zero-fill.
- Commit the result, then emit `binary-ready` as the final record.
- In diagnostic mode, every compact field must equal its `ROMTRACE` counterpart.
- Measure how much time `ROMTRACE` printing costs per transition in the hot path.

Acceptance:
- a real ELF emits results that `sdr2.py` decodes for P1, P26, P27, P28, and P29's first move
- compact equals trace on all of them
- a `TEST=0` build of the regenerated patch hashes to `0ccaaad0…`

Deliver: WSL branch SHA, candidate headless and pre-patchelf ELF SHA-256, the patch diff O should regenerate, test output, and remaining tag-11 fields with sources.

**B: rebase, profile, codec.**
- **B0 profile** on the pinned v8c/v3 artifacts. For P1 and root construction, split wall time into:
  - emulation (`mCore` run loop, with cycle/instruction counts)
  - `mLog`/`ROMTRACE` I/O
  - JSONL IPC
  - Python parse
  - save/restore
  
  Then try headless settings (skip rendering, audio disabled, no sync) and report the delta. Keep a settings change only if full-state parity holds. This is the first deliverable, due before anything else in B.
- Rebase `9589b6f` onto integration.
- Make `sdr2.py` the only SDR2 parser. The bridge calls it.
- Encode the v9 request in `run_dynamic.py`.
- Pass all golden vectors (positive and negative) in unit tests without an ELF.
- Keep v8/trace APIs working.

Acceptance: vectors, worker limits, and identity-rejection tests pass, and the full suite is green on the v8c pin.

Deliver:
- the B0 profile table
- worker binary SHA-256 staged per §7
- test output

**C: closed-loop release root.**
- Replace fixed-frame steps after `CB2_Overworld` with condition-driven steps: `until` on callback address, map group/num, player x/y, flag or var, and `gBattleTypeFlags`.
- Each step has a frame bound and saves a hashed milestone savestate (outside Git).
- Route: truck exit → house/clock → Littleroot → Route 101 → Birch's bag starter → first wild battle, ending at the player's first choice.
- Verify you can write and read back `gBattleMons`, party, and both RNG streams at that root.
- On a failure, report the step, frame, callback, map, and position.

Acceptance: two fresh runs from cold boot reach the same root savestate SHA-256 on the pinned `.gba` and pinned worker. The loader rejects mismatched ROM, worker, or script identities.

### Gate V — vertical slice (orchestrator)

1. Build the integration WSL branch (`base-v8c` + `a1-export`) → candidate ELF v9a, and regenerate `dynamic_request.patch`.
2. Stage worker B.
3. Coordinated flip. In one commit, change all of these:
   - ELF + worker copies
   - `pinned_source.json`
   - `rom_identity.py`
   - cache identity (`rom-v5`)
4. Merge B.
5. Run live `batch_turns_v3` on P1/P26/P27/P28:
   - child present iff nonterminal
   - compact equals trace
   - A→B→restore A→C and B→D on another worker
6. Run the full suite and `--self-check`, then release-neutral check H1.

If any of these fails, revert the flip commit (not the packages) and send exact failures to the owner.

### Wave 2 — A2, D, C (continued) in parallel after Gate V

**D: public child tokens and warm roots.**
- `DecisionState.decoded` comes from `sdr2.py` tags 1–6, and `legal_player_actions` from tag 7. Reject contradictions.
- `SIMULATE_TURN(child, …)` works from arbitrary depth without assuming active slot 0 and without replaying battle start.
- Queries run at deeper nodes and leave the token byte-identical.
- Cache warm ready/root checkpoints per worker, keyed by (identity, canonical request hash). Prewarm on snapshot change and evict under the 3-slot/64 MiB limits by exporting before dropping.
- Keep the scheduler behaviour: 3 workers, paired seeds, honest partials, early card publication, four-card UI.

Acceptance:
- a live root→child→grandchild test across workers
- P25/P26 via the public API
- cards survive transition failure
- warm card latency p50/p95 reported against the ≤ 40 ms target
- full suite green

**A2: mechanics and logic-only decision.**
- P25 voluntary switch: resolve to the next boundary with the real AI and entry effects.
- P19 trainer items under the battle's real difficulty rules. Query and transition must agree. Add a supported item-use fixture if the source allows one, or give a source-cited refusal.
- P24 switch-in effects.
- P8/P9: priority and speed-tie seeds via real requests.
- P12/P13: confusion; disable/protect via moves.
- Exercise the `instruction-budget-exceeded` bound.
- Run the logic-only presentation audit (pure / stateful / logic) and apply the §6 rule, using B0 and A1 cost data.

Acceptance: each row resolves or has a source-cited ROM refusal, no valid action hits the 24,000-frame cap, and `TEST=0` stays neutral.

**C (continued): H3 and H2 harness.**
- Release-side boundary detection.
- Inject state and dual RNG at the release root.
- Build the projection comparison for `release_scenarios.json` rows using SDR2-equivalent fields read from release RAM (via the release map).
- Do not run H2 yet. Stop once H3 works and the comparison plumbing passes a self-comparison test.

### Gate F — final pin (orchestrator)

1. Merge WSL `a2-mechanics` into integration → ELF v9b, with a second coordinated flip if the bytes changed.
2. Merge D, then C.
3. Run the full suite and `--self-check`, check H1, and do the manual four-card walkthrough.

### Wave 3 — E (plus one fix-up slot)

**E: verification and evidence.**
- Re-run all 32 rows on the final pin.
- Build P20–P23 as child-token sequences through real moves.
- Every remaining unsupported row gets a source citation.
- Compare trainer action, full decoded state, events, boundary/terminal, both RNG streams, and deterministic RAM, in normal vs logic-only if logic-only is kept.
- Run H2: release root vs final normal-mode, normal-crit test ELF for every representable `release_ready` scenario, with per-scenario evidence and exclusion reasons.
- Run `bench.py` on parity-passing transitions only.

A fix-up agent is started only for a named failing row, owns only that row's package files, and is merged by O.

**Gate E / final:** C1–C8 are all green, the ledger has one gate table, and the label is set per C7.

## 6. Performance decision rule (frozen in v2.1)

- **Metrics**, per fixture and mode, from `bench.py`:
  - transition p50/p95 wall time and frames
  - instructions or cycles
  - restore time
  - root construction, cold and warm
  - query p50/p95 at root and depth 2
  - IPC bytes per transition
  - state bytes
  - verified transitions/s per worker and for the 3-worker pool
- **Order of attack:**
  1. warm root cache (D)
  2. binary hot path without trace printing (A1/B)
  3. headless mGBA settings (B0)
  4. batch sizing (D)
  5. logic-only (A2)
- **Adopting logic-only.** Logic-only becomes the planner default only if both hold:
  - it gives ≥ 20% more pool throughput than the *optimized* normal mode on the frozen fixtures
  - it passes full parity on every supported row
  
  Otherwise it stays a test-only build flag, documented as parked with its numbers, and the phase-1 speed criterion is met by normal-mode improvements.
- The 3–20 ms/turn target remains a stretch goal, reported separately from acceptance.

## 7. Orchestration mechanics

**Artifact staging.** Stage artifacts in `C:\Users\Abj1\.codex\artifacts\<pkg>\<short-sha>\` containing the binary and a `MANIFEST.json`. The manifest holds:
- `package`, `branch`, `commit`
- `wsl_branch`/`wsl_commit` where relevant
- `artifact`, `sha256`, `bytes`
- `built_from`, `build_cmd`
- `tests_run`, `result`

Only O copies an artifact from staging into pinned locations.

**Dependency readiness.** An agent starts only when its dependency's gate row in the ledger reads `done` with a commit SHA. Agents never read another agent's worktree. They only read the integration branch and staged manifests.

**Dispatch with Claude Code.**
- Use `Agent` with `subagent_type: general-purpose`, `run_in_background: true`, and **no** `isolation` flag. The built-in isolation branches from the main checkout (`feat/rom-backed-calc`), which is the wrong base.
- Each prompt names its existing worktree path. The shell cwd resets between calls, so agents must use absolute paths or `git -C <worktree>`.
- Up to three are active at once. Continue an agent with `SendMessage` and don't respawn it.
- For code navigation, agents use `mcp__codegraph__codegraph_explore` with `projectPath` set to **their own worktree**. They pass large logs or trace dumps through `mcp__headroom__headroom_compress` and quote the hash in reports.

**Prompt template** (the orchestrator fills the angle brackets):

```
You are package <PKG> in the Sidedex ROM transition finish plan.
Worktree: <abs path>   Branch: <branch>   Integration base: <sha>
WSL worktree (ROM pkgs only): <path> on <wsl branch>; pinned source 2576d0f3…
Read first: ROM_TRANSITION_FINISH_PLAN.md §<sections>, ROM_TRANSITION_CONTRACT.md App. B–C,
tools/rom_oracle/FIXTURE_MATRIX.md rows <rows>.
You own ONLY: <files>. Do not edit: <files>. Never change pins, never merge, never
touch feat/rom-backed-calc or any work/ dir. Use absolute paths; use codegraph_explore
with projectPath=<worktree>.
Task: <bullets from §5>.
Acceptance command(s): <cmds>. Baseline must stay: python -m pytest --ignore=work -q green.
Commit in small commits on your branch. Stage artifacts per §7 with MANIFEST.json.
Final report: commits, changed files, exact test output tail, artifact SHA-256s,
measurements (table), unresolved items with fixture ID + source citation, risks.
If blocked >1 attempt on the same failure, stop and report the exact step/frame/state.
```

**Merge order.** Gate 0 → A1 + B + C in parallel → Gate V (A1 ELF + B worker flip) → D + A2 + C → Gate F → E → final. After each merge, run the focused tests, the full suite, and `--self-check`. After each ROM change, check that the `TEST=0` build is still release-neutral.

## 8. Tooling status

- **codegraph**
  - The main checkout already had `.codegraph/`, which the `SessionStart` hook creates.
  - Indexes now also exist for `rom-transition-orchestrator`, `rom-hook-a`, `rom-worker-b`, and `rom-release-c` (53 files, ~2.1k nodes each), and `codegraph_explore` is verified against the orchestrator worktree.
  - `.codegraph/` ignores itself in each worktree.
  - New worktrees (`rom-hook-a2`, `rom-host-d`, `rom-verify-e`) need `codegraph init` at creation.
- **headroom**
  - The MCP server (`headroom mcp serve`, v0.24.0) is registered and `compress`/`retrieve` work.
  - Its proxy on `127.0.0.1:8787` was down and is now running for this session only. It dies with the session.
  - Making it durable (`headroom install apply` or `headroom init claude`) also reroutes Claude API traffic through the proxy. That's the user's decision and is not done.

## 9. Audit amendments (2026-09-28, after the first Gate V attempt; supersede earlier sections where they conflict)

Independent read-only audit (Opus) of the first flip attempt. Confirmed by O against code: the v9 ELF rejects every v8 request (`request->version != SIDEDEX_DYNAMIC_VERSION`, `dynamic_request.patch:650`), so the contract line "a v8 request uses the old trace path" is not implemented; legacy consumers cannot parse the 8-field ready line; identity literals live in ~36 places.

1. **Split flip.** V0 (done): v9a is a registered CANDIDATE pin; active pins stay v8c + worker 0601297e. V1: flip only the worker (wire 3, `sidedex-mgba-worker/0.3.0`, `rom-v5`) on the v8c ELF; wire 2 is retired (worker 1529 is wire-3-only). V2: committed, non-skipped live evidence for v9a x worker 1529 through `batch_turns_v3` decoded by `sdr2.py`. **The ELF flips once, at Gate F** (v9b from A2, with D's migration of `rom_eval.py` / `rom_query.py` to v9 flags=0 trace requests and the new ready line).
2. **V2 evidence must be committed tests**, not scratch drivers, each with a negative control, using the pinned consumers (`sdr2.py`, worker 1529): live P1/P26/P27/P28 decode; child present iff nonterminal; A->B->restore A->C and export/import on a second worker then B->D; a SEMANTIC equality test (sdr2 decode vs the independent v8 `state|party|active|...`, `rng|`, `legal|` records from the same flags=0 run; cross-ELF v8c-vs-v9a on the 20 green rows). `test_sdr2.py`'s live test must FAIL (not skip) in gate runs.
3. **Ownership grants (recorded, E does not exist yet):** B may edit `test_rom_oracle_dynamic_live.py`, `tools/rom_oracle/rom_diff_harness.py`, `test_rom_diff_elf.py` to read identity from `rom_identity` and drive v9 flags=0 for the V1/V2 work; C makes `release_harness.py`, `release_input_script.json`, `release_scenarios.json`, `test_rom_release_live.py` read `rom_identity` and regenerates the root with worker 1529. Fixture JSONs keep their recorded identity as provenance. O keeps `rom_identity.py`, `pinned_source.json`, `calc_service.py` cache key.
4. **C5 restated** (a card needs several transitions; warm transition is ~37-45 ms of pure emulation, queries ~12 ms): (i) query cards from a warm root p50 <= 40 ms; (ii) warm per-transition p50 <= 40 ms (normal mode, parity-passing); (iii) recommendation card first publish p50 <= 250 ms (<= 120 ms with RNG known) on the 3-worker pool, reported with transitions/s. "Binary hot path" is demoted from a perf lever (measured savings 0.9-6 ms).
5. **Independent rebuild:** O creates WSL `sidedex/integration` and rebuilds the ELF from a clean worktree at V2 and at Gate F and compares hashes; the worker build gets `-Wl,--no-insert-timestamp` and `-ffile-prefix-map` so it is reproducible (B).
6. **Acceptance rule for every package:** evidence is a committed test with a negative control, run against pinned consumers.
7. **A2 is paused**; corrections: logic-only audit must pass `mode=` to `encode_request` (the audit script compared normal to normal; README claim retracted); P19 difficulty is a missing request input (harness limit), model the ROM branch at `battle_main.c:4504` instead of a blanket refusal; P9 tie roll happens before RNG injection (harness bug, biases Calc samples; add `speedTieBreaks` handling); drive worker 1529 via `batch_turns_v3` and `sdr2.py` rather than the `ROMTRACE|complete|<status>` workaround; stage artifacts with MANIFEST.json.
8. Risks tracked: flash-agent evidence quality, flip coupling, semantic gaps (P19 difficulty, P9 tie roll, hidden state), C5 physics, C7 root bound to the old worker; IWRAM is full and the event ring shares the result buffer at offset 4096.
