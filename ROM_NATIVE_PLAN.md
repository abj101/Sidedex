# ROM native engine + MCTS plan (supersedes the remaining work in ROM_TRANSITION_FINISH_PLAN.md)

Written 2026-09-28. Decisions confirmed by the user: native compile, WSL Linux first, Python MCTS with batched calls, ROM AI plays the player in rollouts, moves + voluntary switches, anytime/streaming recommendation, full information (RNG is the only uncertainty). The orchestrator chose the drop list (section 3).

## 1. Why the old plan stalled at ~40%

1. **It optimized everything except the cost.** B0 measured emulation at >=99% of every transition (45-75 ms, 135-430 frames). The plan still spent its waves on the SDR2 binary format, wire protocol 3, contract appendices, release-ROM harness, identity pins and a binary hot path (measured saving 0.9-6 ms). The in-ROM logic-only mode was *slower* (P1: 54 ms vs 45 ms). C5 was restated downward to fit the ceiling ("C5 physics"). Even at 100% the result is ~25 transitions/s per worker. A 3-second MCTS on 3 workers gets ~225 transitions, which is not a search.
2. **The simulator is a unit-test runner bent into a stateful engine.** R2 (recorded-battle record exhausted when chaining), R3 (lost hold-release RNG re-seed), the v8/v9 mailbox split, and "the v9 ELF rejects v8 requests" all came from forcing the expansion test runner to hold, release and re-enter turns under mGBA.
3. **Every hook change is expensive.** IWRAM is 99.5% full. The hook lives in WSL git and ships to Windows as a patch. Each change means rebuild, patchelf, hash, re-pin in about 36 places, and a re-run of the fixture matrices. Mixed-object builds made v9a unreproducible.
4. **Process overhead and rework.** Ten worktrees, a split Windows/WSL source of truth, candidate/active pin gates (V0/V1/V2/F), and weak-agent evidence (tautological tests, a no-op parity test, a dropped guard) that forced audits and R1-R4 fixes.

**Fix:** stop emulating an ARM CPU. The battle engine is C source (pinned pokeemerald-expansion `2576d0f3f4` + the Sidedex hook). Compile the same source natively and keep mGBA only as the oracle. One byte-equality gate replaces the verification ceremony.

## 2. Target architecture

```
Python (Windows)                                    WSL: sidedex-native (i386 ELF, N processes)
calc_service ─ planner.py (open-loop UCT, CRN) ──►  same JSON-lines ops as rom_worker.exe
             ─ rom_query  (exact cards)         ──►    status, batch_turns_v3, save/load/drop/export/import_state
                                                       + NEW rollout op
                                                    engine = pinned source + hook, compiled -m32
                                                    snapshot = memcpy of the writable data segment
mGBA rom_worker.exe + test ELF (same hook)  ──────  oracle only: differential tests, SIDEDEX_ENGINE=mgba
```

- **Same bytes in, same bytes out.** The native binary receives the same 876-byte v9 request and writes the same SDR2 result through the same hook code, so `tools/rom_oracle/sdr2.py`, `rom_pool.py` and `rom_bridge.py` work unchanged. Validation is **SDR2 byte equality** against mGBA.
- **Why i386 (-m32):** it matches ARM's ILP32 model, so `.4byte` pointers in `data/battle_scripts_*.s` (plain gas directives only: `.byte/.2byte/.4byte/.macro/.if`) assemble unchanged with `as --32`, and `u32 <-> pointer` casts stay valid. Non-PIE static linking puts every global at a fixed address, so **restoring a snapshot is a memcpy that keeps every internal pointer valid**. There is no serializer.
- **Frames cost almost nothing natively.** The test build does not advance RNG per frame (`src/main.c:407` is gated on `!gTestRunnerEnabled`), and headless mode already skips strings and animations (`battle_controllers.c:2695`, `battle_interface.c:2739`, `battle_anim.c:327`). The native loop runs callbacks with no VBlank wait. No logic-only mode is needed.
- **Open-loop MCTS means no child-state explosion.** Each iteration restores the root snapshot, injects a sampled RNG seed, replays the tree's player-action prefix, then lets the ROM AI play both sides to the end. That is one IPC call per iteration, and only root snapshots are stored. Child tokens are kept for query cards and debugging, not for search.

Expected speed, to be measured at Gate N1: ~0.2-2 ms per transition (interpreted ARM to native x86 is typically 30-100x). Snapshot restore is ~300-400 KB of memcpy (~30 us). With 10 worker processes (12 cores), whole-battle rollouts of ~15 turns give roughly 300-3000 iterations/s.

## 3. Dropped or frozen (orchestrator decision)

| Stream | Decision | Reason |
| --- | --- | --- |
| SIDEDEX_LOGIC_ONLY, fast pump, instruction budget (A2 tail) | **Drop** | Native removes the frame cost; measured slower anyway |
| Binary hot path, further wire/protocol work | **Drop** | The native binary speaks the existing ops; one new `rollout` op |
| Gate V2/F candidate-pin ceremony, E 32-row matrix, H4/H5 | **Replace** with Gate N2 (byte equality) | One gate, stronger evidence |
| `rom_transition.py` public child-token design (D) | **Shrink** to what query cards need | Open-loop MCTS needs only root handles |
| Release harness H2/C7 (cold-boot route, release vs test ELF) | **Freeze**, run once at the end (W3) | An accuracy check of TESTING vs release, not on the critical path. H1 (source reproduces the release ROM hash) stays |
| `codex/rom-gate-v2` verifier | Keep only the child/chaining tests the oracle ELF needs; do not chase its 3 verifier-side defects | Superseded by N2 |
| Heuristic recommendation code (`turns_to_KO`, speed-order rules, faint-risk ranking, second-turn query) | **Delete** at W2c | Replaced by the planner |

## 4. Work plan

Agents: Claude Code `Agent` subagents in the background, with explicit worktree/WSL paths and no `isolation`. Recommended: **N1 on Opus** (hard porting spike, the one critical-path unknown); everything else on Sonnet 5.5 medium. The orchestrator reproduces every headline claim itself: byte-equal runs, timings, and `git diff` of shared hook files for silent deletions.

### W0: prep (orchestrator, ~1 h; blocking)
1. **User:** in WSL run `sudo apt install gcc-multilib` (32-bit libc/crt; the orchestrator cannot enter sudo passwords).
2. WSL: create `sidedex/native` = `sidedex/a1-fix-r2` (72953cc330: R2 + R3) merged with `sidedex/a2-mechanics` (P25 voluntary switch, difficulty flags, item refusal; it already has the R3 cherry-pick). This is **the only hook branch** from now on. Diff check: `git diff sidedex/base-v8c..sidedex/native -- src/recorded_battle.c` shows only the R2 block.
3. Clean rebuild of the oracle ELF from that branch in a fresh WSL worktree (delete `-DTESTING` objects; the TEST=0 build must still hash to `0CCAAAD0...`). Stage under `artifacts\O\native-oracle\` with MANIFEST.json. Run `test_rom_oracle_dynamic.py` plus the V2 child/chaining tests with the pin overridden. Pin it as the active ELF **once** (`rom_identity`, `pinned_source.json`, cache key `rom-v6`). D's v8-to-v9 consumer migration is folded into W2c.
4. Stop all old streams; mark the ledger gate table "superseded by ROM_NATIVE_PLAN.md".

### W1: three agents in parallel
**N1: native port spike (critical path, Opus, time-box: 1 agent-day).** WSL worktree of `sidedex/native`, new `Makefile.native` and a `native/` dir inside the expansion tree.
- Reuse the existing preprocessing pipeline (`preproc`, INCBIN expansion, generated headers from a normal `make`), then compile every `src/*.c` plus `test/test_runner*.c` and `test/battle/ai/sidedex_dynamic.c` with `gcc -m32 -O2 -funsigned-char -fno-pie -no-pie -fno-strict-aliasing -fwrapv -DTESTING -DSIDEDEX_NATIVE`. Link with `-Wl,--gc-sections -Wl,-Ttext-segment=0x10000000`. Exclude `m4a.c`, `librfu*`, `agb_flash*` and `libisagbprn.c` (replace with stubs).
- Assemble the needed `data/*.s` (battle scripts 1/2, battle anim scripts, whatever the linker demands) with `as --32` after rewriting ARM `.align n` to `.balign 2^n`. Skip thumb-code data such as the multiboot files.
- Native shims (one file each, `#ifdef SIDEDEX_NATIVE`): C `Random32` (`_SFC32_Next_Stream(&gRngValue, STREAM1)`, replacing the NAKED thumb asm in `src/random.c`); libagbsyscall in C (CpuSet, CpuFastSet, LZ77/RL decompress, Div/Mod/Sqrt/ArcTan2, VBlankIntrWait no-op, SoftReset abort); `svc 2` in `script.c` becomes a no-op; DMA macros become memcpy/memset; `MgbaPrintf` writes into a buffer the driver scans for `ROMTRACE|` markers. At startup, `mmap(MAP_FIXED_NOREPLACE)` 0x02000000-0x07FFFFFF so absolute REG/VRAM/OAM/PLTT/INTR_CHECK writes land in memory.
- Driver `native/main.c` replaces `AgbMain`'s loop with `NativeFrame()` (callbacks plus the VBlank handler, no waits) and implements `status`, `batch_turns_v3` and `save/load/drop_state` by lifting `native/rom_worker/worker.c` with its mGBA calls swapped (runFrame to NativeFrame, savestate to snapshot memcpy of `[__data_start, _end)` plus any mapped GBA region the determinism test shows is needed, `mCoreGetMemoryBlock` to identity pointer). Unresolved symbols: stub them with `abort()` and add real code only for those a battle actually hits.
- Struct-layout guard: a `sizes.c` compiled with both `arm-none-eabi-gcc` and `gcc -m32` prints `sizeof`/`offsetof` of `BattlePokemon`, `Pokemon`, `BattleStruct`, `DisableStruct`, `SideTimer`, `FieldTimer` and the request/result structs. They must match (add `-malign-double` if u64 alignment differs).
- **Gate N1:** P1 root move through `batch_turns_v3` returns **SDR2 byte-identical** to the oracle ELF. Report ms and frames per transition, snapshot size, and restore time. Snapshot determinism: restore, run A->B twice, and the memory hash and SDR2 are identical. **Kill criterion:** a divergence or link blocker not understood within the time-box. The agent reports it with file:line; the orchestrator decides whether to extend or fall back (the planner and W1-H work still apply to mGBA).

**H: hook additions (Sonnet, WSL `sidedex/native` sibling branch, merged by the orchestrator).** Every change stays inside `#if TESTING`. New statics go in EWRAM (IWRAM is full).
- `SIDEDEX_ACTION_AI` player-action code: the hook asks the expansion AI to choose the player battler's move, switch or forced replacement (AI data for that battler; flags = the opponent trainer's flags plus `AI_FLAG_SMART_SWITCHING | AI_FLAG_SMART_MON_CHOICES`). `B_FLAG_AI_VS_AI_BATTLE` is compiled to 0 in this hack, so do not rely on it.
- Confirm that chaining with injected RNG = the previous result's tag-6 quads reproduces the uninterrupted RNG stream. The rollout driver relies on this; add a test with a negative control.
- Tests: an AI-player turn through the oracle ELF (committed, with a negative control); TEST=0 still reproduces the release hash.

**P: planner (Sonnet, Windows worktree `codex/rom-native` from integration).** `planner.py` is engine-agnostic and tested against a toy stochastic engine.
- Open-loop UCT over player-action sequences. Root actions come from tag 7 (moves + voluntary switches); forced-replacement boundaries are player decision nodes.
- CRN at the root: each seed batch evaluates every root action. Adaptive batches of 8, 16, 32, 64 seeds and up. Stop when the best action's paired-difference 95% CI lower bound is above 0 against every other action, or when the budget runs out (default 3 s). First publish at the first batch (~250 ms target), then stream updates.
- Value: win = 1, loss = 0. At the turn cap (default 60) the leaf value is `0.5 + 0.25*((p_alive - o_alive) + (p_hp - o_hp))` from tag 1, and terminals always dominate. Normal ROM crits (`critical_policy=0`); altered crits are only an explicit alternate policy and are labelled that way.
- Output per root action: P(win) with CI, samples, and the leading set when it is unresolved.
- Leaf parallelism across W workers using virtual loss.

### W2: after Gate N1
**W2a: native worker completion (N1 agent continues).** Add `export/import_state`, identity (`sidedex-native/0.1.0`, `rom_identity.NATIVE_ENGINE_SHA256`, cache key `native-v1`), and the **`rollout` op**: `{handle, items:[{rng1, rng2, prefix:[action words], max_turns}]}`. Per item: restore the root, inject RNG, play the prefix, then `SIDEDEX_ACTION_AI` to terminal or the cap, re-injecting each turn's RNG from the previous tag 6. Returns `{outcome, turns, prefix_consumed, legal_at_prefix_end, leaf tag-1 summary}`. An illegal prefix action stops the prefix and reports `prefix_consumed`. SIGFPE and aborts become `{"ok":false,"error":"engine-fault"}`.

**W2b: Gate N2, differential (Sonnet).** `test_rom_native_diff.py`, live and non-skipped when `SIDEDEX_GATE=1`:
- Every green fixture row: native SDR2 == oracle SDR2 byte for byte.
- 300 random chained battles (random seed, random legal action or AI action each turn, run to terminal) through `batch_turns_v3` on both engines, with byte equality at every turn.
- A rollout op result equals the same sequence driven turn by turn.
- Negative controls: one flipped byte is detected, and a different seed diverges.
- The first divergence is reported with request bytes and turn index for triage. Fix classes to expect: unaligned ARM loads, divide-by-zero (ARM libgcc returns 0, x86 traps), signed-char assumptions, ROM-address range checks (`0x8000000` literals).

**W2c: app wiring (Sonnet, owns `rom_pool.py`, `rom_bridge.py`, `rom_query.py`, `rom_eval.py`, `calc_service.py`).**
- The pool spawns W = cores - 2 native workers through `wsl.exe -e <pinned path>`. `SIDEDEX_ENGINE=mgba` keeps the old path for verification.
- Warm root: build the decision-point snapshot once per battle state per worker and cache it by request hash.
- Query cards run on native. The recommendation card runs through `planner.py` with streaming. Migrate v8 consumers to v9 flags=0. Delete the heuristic recommendation code listed in section 3.

### W3: finish (orchestrator + 1 agent)
- `bench.py`: transitions/s per worker and pool, iterations/s, p50/p95 per transition, snapshot size, restore time, time to first publish.
- Full suite + `python app.py --self-check` + a manual four-card Calc walkthrough.
- H2 run once (the frozen release harness against the oracle ELF) as the TESTING-vs-release accuracy check.
- Update README and CALC_CONTRACT (label: native engine verified byte-equal to the mGBA test ELF; H1 release hash).

## 5. Gates

| Gate | Pass condition |
| --- | --- |
| W0 | `sidedex/native` merged, oracle ELF rebuilt clean and reproducible, TEST=0 = release hash, oracle tests green, pins flipped once |
| N1 | P1 SDR2 byte-equal native vs oracle; snapshot determinism; timing reported |
| H | AI-player turn + RNG-carry tests green on the oracle ELF, each with a negative control |
| N2 | Fixture rows + 300 random chained battles byte-equal; rollout == turn-by-turn; negative controls fail as designed |
| P | Planner tests: picks the known-best action on toy engines; CI stop rule; CRN pairing; streaming |
| Final | App on native, suite + self-check green, bench published, H2 run and recorded |

## 6. Risks

- **Native divergence** (UB, unaligned loads, div0, char sign): N2 finds them, and fixes go in `#ifdef SIDEDEX_NATIVE` shims, never in mechanics.
- **Build surface** (unresolved symbols, graphics/sound data): gc-sections plus abort stubs; only code a battle executes needs to be real.
- **Hook changes still cost an oracle ELF rebuild**; batch them (H lands once).
- **WSL IPC latency:** amortized by the rollout op (one call per iteration).
- **Snapshot size/memory:** only root snapshots are stored. Add a delta-vs-root store only if measured necessary.
- **The real game advances RNG per frame in battle** (`main.c:407` without TESTING), so "RNG known" only holds for the injected-seed model. This is the same limitation as today; it is documented, not solved.
