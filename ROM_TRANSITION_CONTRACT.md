# ROM transition contract v1 (frozen)

Frozen by the orchestrator before any phase-1 work is assigned. This file is the interface every work
package codes against. Changes require a contract version bump and an orchestrator merge; downstream
agents never edit this file unilaterally.

Numbers existing at baseline: ROM hook mailbox protocol **7** (856-byte struct, magic `0x53445831`),
worker JSONL protocol **1**, single in-memory savestate slot in `worker.c`, no logic-only mode, no
presentation bypass, `replayCount > 1` rejected, 24,000-frame cap, one pooled worker per request.

## 1. Versions and identity

| Name | v1 value | Owner |
| --- | --- | --- |
| `transition_contract` | 1 | orchestrator |
| `rom_hook_protocol` | 8 (bumped from 7) | package A |
| `wire_protocol` (worker JSONL) | 2 (bumped from 1) | package B |
| `state_schema` | `sidedex-state-v1` | package B |
| `transition_schema` | `sidedex-transition-v1` | package B |
| `decode_schema` | `decode-v1` | package C |
| `save_format` | `mgba-savestate-savedata-rtc-v1` | package A/B |

`TransitionIdentity` is the tuple below. It is attached to every state token, transition result, query
result, and cache key. Any mismatch between the identity a token was minted with and the identity of the
worker/ELF/mode that is asked to use it is an `identity-mismatch` error, fail closed.

```
source_sha, test_elf_sha256, worker_sha256, worker_version, mgba_version,
rom_hook_protocol, wire_protocol, save_format, state_schema, decode_schema,
execution_mode ("normal" | "logic-only"),
critical_policy ("normal" | "item-ability-only"),
planner_identity (reserved, "none" until phase 5)
```

`calc_service.snapshot_key` identity must be extended to this tuple; generated ELF/ROM binaries stay out
of Git. Rebuilds update and verify both pin locations (`tools/rom_oracle/pinned_source.json` and
`rom_bridge.py`) before any downstream work uses the new artifact.

## 2. DecisionState (the node)

```jsonc
{
  "state_schema": "sidedex-state-v1",
  "identity": { /* TransitionIdentity */ },
  "token": {                      // OPAQUE. Never interpreted outside the ROM/worker.
    "handle": "<hex>",            // worker-local or content hash for portable blobs
    "owner_worker": "<stable worker id>",
    "portable": true,
    "bytes": 123456,
    "sha256": "<hex>"
  },
  "boundary": "player-choice" | "forced-switch-player" | "terminal",
  "turn": 3,
  "decoded": { /* projection, see 2.2 */ },
  "legal_player_actions": [
    {"kind": "move", "move_id": 57, "pp": 12, "target": 1},
    {"kind": "switch", "slot": 2}
  ],
  "terminal": null | {"result": "win" | "loss" | "draw", "reason": "<string>"},
  "rng": {"main": [a, b, c, ctr], "secondary": [a, b, c, ctr]},
  "metrics": {"frames": 812, "elapsed_ms": 41.2, "save_bytes": 123456}
}
```

Rules:

1. **The token is authoritative and opaque.** `decoded` is a *projection* for display, action legality,
   assertions, and cache keys only. A projection MUST NOT be used to reconstruct a deeper node. Nodes are
   created only by restoring a token or handing a token off. Consumers that need a different field must
   get it from the ROM exporter, not from a Python battle-mechanic substitute.
2. **Immutable.** Restoring a token must not mutate it, and a query against a token must leave the
   decoded projection, the RNG pair, and the state bytes byte-for-byte unchanged. Re-exporting a restored
   token must produce an identical projection and identical RNG (determinism check).
3. **`decoded` must carry the complete phase-1 field set** (see 2.2). Unrepresentable fields are rejected
   with `unrepresentable-state`, never silently defaulted.
4. `legal_player_actions` is derived from the ROM at the boundary, and must include only mechanically legal
   actions. Empty list on a non-terminal boundary is a bug (`no-legal-actions`).
5. `boundary == "terminal"` means the battle has ended: `terminal` is set, `legal_player_actions` is empty,
   and no transition may be requested from the node (`terminal-state`).

### 2.2 Required decoded fields (each with its C source recorded in the exporter)

Both parties (all slots): species, level, HP, max HP, PP per move, status1/status2, mutable ability and
form, held item, battle data (IVs/EVs/nature as needed for restore). Active: active indices, stat stages
(7), volatiles, disable data, protect data, special statuses, substitute/encore/taunt class flags. Field:
weather, terrain, trick room and their timers, screens, side conditions (hazards, side timers). Battle:
turn counters, trainer AI flags, trainer item inventory, both RNG streams, battle result. Any field that
cannot be exported is listed as unrepresentable in the exporter and fails closed instead of defaulting.

## 3. Decision boundary (normative)

A boundary exists at the point where all of the following hold:

1. entry effects and end-of-turn effects have settled;
2. any compulsory faint replacement has been resolved;
3. no outstanding presentation/controller obligation remains except accepting the next player choice.

That point is immediately before the next player choice. Forced switches that require player input are
**their own boundary** (`boundary == "forced-switch-player"`, legal actions = switches only) and are
transitioned like any other node. If the battle has ended at that point, the ROM returns a terminal
result instead of a node.

## 4. TransitionResult

```jsonc
{
  "transition_schema": "sidedex-transition-v1",
  "identity": { /* TransitionIdentity */ },
  "input_token_sha256": "<hex>",
  "player_action": {"kind": "move", "move_id": 57},
  "trainer_action": {"kind": "item", "item_id": 22},   // or move/switch; null only if none occurred
  "next": { /* DecisionState */ } | null,
  "terminal": {"result": "win", "reason": "all-fainted"} | null,
  "events": [{"kind": "damage", "battler": 1, "hp": {"from": 120, "to": 40}}],
  "rng": {"main_before": [..], "main_after": [..], "secondary_before": [..], "secondary_after": [..]},
  "frames": 915, "elapsed_ms": 44.8,
  "mode": "normal", "critical_policy": "normal"
}
```

`next` is null if and only if `terminal` is set. Switch-in effects, faint transitions, trainer item
execution, and terminal states are explicit cases: the resolved turn must execute the real trainer item
decision, not an item-agnostic rerun. No Python battle-mechanic substitute may fill a gap.

## 5. Execution modes and critical policy

- `normal`: real headless emulation with full presentation. Parity baseline and the future default planner.
- `logic-only`: test build only, gated by `SIDEDEX_LOGIC_ONLY` and a runtime mode switch at the fixture
  boundary. Rules: every controller command and frame wait is audited as *pure presentation*,
  *stateful presentation*, or *game logic*; only proven pure-presentation requests are acknowledged
  instantly; any stateful part executes synchronously before acknowledgement. A bounded script pump runs
  successive synchronous commands until a genuine asynchronous mechanical dependency, an unskippable
  controller command, a boundary, a terminal, or an instruction budget. Budget exhaustion surfaces
  `instruction-budget-exceeded`; it never spins to the frame cap.
- Critical policy: `normal` (real crits — required for parity and the default planner) and
  `item-ability-only` (existing alternate, explicitly labeled). Mode and policy ride request, result, and
  cache identity. Cards keep their explicit policy label; nothing is relabeled `engine-exact` when it is
  not.

## 6. RNG rules

1. A node carries the RNG pair observed at its boundary (after setup, after injection).
2. A transition restores the token first, then injects the pair, then runs. Injection MUST happen after
   restore and before any AI or choice evaluation.
3. Setup RNG must never contaminate paired action comparisons: the same root pair is used for every
   candidate action of a comparison, and a sample counts as paired only when every required action
   completed. Partial work is returned as completed work; zero outcomes are never invented.
4. Both streams are captured before and after every transition and reported in `TransitionResult.rng`.

## 7. Operations

| Op | Input | Output | Invariants |
| --- | --- | --- | --- |
| `SIMULATE_TURN` | token, player_action, rng pair, mode, policy | `TransitionResult` | Advances the battle; source token immutable. |
| `QUERY_DAMAGE` | token, player move (or move id) | damage distribution/range, accuracy, crit behavior, immediate KO probability, explicit unsupported reasons | No turn resolution, no RNG consumption, source token immutable. Mechanic-dependent composition (multi-hit weights, Sash/Sturdy, accuracy) lives in the ROM layer or the ROM enumerates complete outcome weights; only generic integer aggregation may remain in Python. |
| `QUERY_AI_ACTION` | token, rng pair | trainer action, labeled `deterministic` (fixed seed) or `distribution` (sampled) | Runs the real AI once without resolving a turn; never conflated with a probability distribution; item decisions are neither consumed nor repeated by a later transition from the same token. |
| `SAVE_CHECKPOINT` | working state | named immutable node | Worker-level; bounded count. |
| `DROP_CHECKPOINT` | handle | ok | Frees memory; later use is `stale-handle`. |

Every operation returns a versioned result. Queries must not resolve a turn and must not change the source
token. Unsupported mechanics fail closed with an explicit reason.

## 8. Node ownership, transfer, limits, cancellation

- Each token records `owner_worker` (stable id: worker version + pid + start time + ROM/ELF identity).
- A node is `portable: true` when its bytes can be restored by any worker of identical identity. The
  scheduler MAY route a node to its owner instead; the cost of moving/evicting a node is documented, not
  assumed. Default plan: portable blobs; if measurement shows transfer dominates, route to owner.
- Bounds (all enforced, all failing closed): nodes per worker **3**, queued jobs per pool **64**, node
  bytes per worker **64 MiB**, frames per job **24,000**, instructions per job **2,000,000**, wall deadline
  per request, output bytes per job **1 MiB**, batch size **8** (may be revised upward only with a measured
  reason).
- `stale-handle` is returned for a token whose owner is gone, whose handle was dropped, or whose identity
  no longer matches. A stale handle is never silently re-created.
- Cancellation and deadline expiry return completed partial work. Retries are allowed only for idempotent
  jobs after worker failure, and must not duplicate observation counts.

## 9. Canonical failures (fail closed)

`unsupported-mechanic`, `unrepresentable-state`, `incomplete-state`, `stale-handle`, `identity-mismatch`,
`instruction-budget-exceeded`, `deadline-exceeded`, `worker-failed`, `no-legal-actions`, `terminal-state`,
`artifact-mismatch`, `source-mismatch`, `invalid-id`, `timeout`. Existing UI reason strings are preserved;
new failures extend, never relabel.

## 10. Compatibility and freeze

- The four UI cards stay: `player_moves`, `enemy_moves`, `ai_actions`, `recommendation`. Exact query cards
  still publish when transition evaluation fails.
- Frozen until phase 5: `turns_to_ko`, speed order, survival, follow-up, and risk-first ranking code. Only
  a small correctness fix needed to keep them usable is permitted, and it must be called out in the PR
  description.
- `test_rom_eval.py::PinnedEngineEvaluation` regressions (four-card known-seed fill, first-turn KO, trainer
  items never `incomplete-state`, low-HP item prediction) and the first-turn faint/`ready` path stay green.

## 11. Required evidence per gate

- Phase 1: latency (median/p95), frames, save/restore time, state size for a deterministic root plus at
  least one deeper non-terminal node branching repeatedly; terminal/faint/switch/item cases finishing
  without the 24,000-frame fallback; complete state + both RNG streams returned; `normal` mode executable.
- Phase 2: same state + seed ⇒ stable query results; query leaves decoded state/RNG unchanged; query card
  availability independent of rollout completion; supported single/multi-hit fixtures match normal engine
  outcomes or documented exact enumeration; unsupported fail closed; p50/p95 query latency recorded
  separately.
- Phase 3: three workers used for one request; identical paired results vs one-worker execution; survival
  of one worker exit without duplicate observations; deadlines respected; improved verified transitions per
  second; per-worker and pool throughput plus IPC bytes reported.
- Phase 4: zero unexplained normal-vs-logic-only differences across the fixture matrix; AI leakage test
  passes; restore determinism and identity rejection pass; release-ROM comparison evidence, or an explicit
  `compiled-engine` label with phase 4 left open.
