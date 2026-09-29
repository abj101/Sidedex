# ROM transition contract v1 (frozen); v2 execution amendment below

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

## Appendix A: mailbox v8 layout (package A)

Appended by package A. The canonical `SidedexDynamicRequest` lives in
`include/test/sidedex_dynamic.h`. `SIDEDEX_DYNAMIC_MAGIC` is unchanged
(`0x53445831`); `SIDEDEX_DYNAMIC_VERSION` is **8**; the struct is **864** bytes.
All multi-byte fields are little-endian ARM. v7 byte offsets are preserved; the
v8 fields are appended at 856.

### Request header and fixed fields

| Offset | Size | Type | Field | Notes |
| --- | --- | --- | --- | --- |
| 0 | 4 | u32 | `magic` | `0x53445831` |
| 4 | 2 | u16 | `version` | must be 8 |
| 6 | 2 | u16 | `status` | 0 WAITING, 1 SUBMITTED, 2 RUNNING, 3 COMPLETE, 4 INVALID |
| 8 | 4 | u32 | `aiFlags` | source-mapped trainer AI flags |
| 12 | 2 | u16 | `candidateKind` | 0 MOVE, 1 SWITCH, 2 AI (Appendix C.7) |
| 14 | 2 | u16 | `playerMove` | move id for a MOVE candidate |
| 16 | 2 | u16 | `playerSlot` | party slot for a SWITCH candidate |
| 18 | 2 | u16 | `replayCount` | 0 or 1 (root construction only) |
| 20 | 16 | 4x u32 | `mainRng` | injected main RNG (a, b, c, ctr) |
| 36 | 16 | 4x u32 | `secondaryRng` | injected secondary RNG (a, b, c, ctr) |
| 52 | 2 | u16 | `playerCount` | 1..6 |
| 54 | 2 | u16 | `opponentCount` | 1..6 |
| 56 | 384 | 6x 64 | `player[6]` | see `SidedexDynamicMon` |
| 440 | 384 | 6x 64 | `opponent[6]` | see `SidedexDynamicMon` |
| 824 | 12 | 6x u16 | `replay` | see `SidedexDynamicTurn` |
| 836 | 2 | u16 | `secondKind` | v7 compat |
| 838 | 2 | u16 | `secondMove` | v7 compat |
| 840 | 2 | u16 | `secondSlot` | v7 compat |
| 842 | 2 | u16 | `secondPresent` | 2 still selects the v7 query path |
| 844 | 8 | 4x u16 | `trainerItems` | item ids, query turn only |
| 852 | 2 | u16 | `trainerItemCount` | 0..4 |
| 854 | 2 | u16 | `trainerReserved` | 0 |
| 856 | 2 | u16 | `op` | 0 SIMULATE_TURN, 1 QUERY_DAMAGE, 2 QUERY_AI_ACTION |
| 858 | 2 | u16 | `mode` | 0 normal, 1 logic-only |
| 860 | 2 | u16 | `criticalPolicy` | 0 normal (real crits), 1 item-ability-only |
| 862 | 2 | u16 | `requestId` | boundary-hold release token |

### `SidedexDynamicMon` (64 bytes, at offsets 56 and 440, stride 64)

| Offset | Size | Type | Field |
| --- | --- | --- | --- |
| 0 | 2 | u16 | `species` |
| 2 | 2 | u16 | `level` |
| 4 | 2 | u16 | `ability` |
| 6 | 2 | u16 | `item` |
| 8 | 2 | u16 | `nature` |
| 10 | 2 | u16 | `hp` |
| 12 | 2 | u16 | `maxHp` |
| 14 | 2 | u16 | `reserved` |
| 16 | 4 | u32 | `status1` |
| 20 | 8 | 4x u16 | `moves[4]` |
| 28 | 4 | 4x u8 | `pp[4]` |
| 32 | 12 | 6x u16 | `ivs[6]` |
| 44 | 12 | 6x u16 | `evs[6]` |
| 56 | 1 | u8 | `heldItemEnabled` |
| 57 | 7 | 7x u8 | `statStages[7]` |

`statStages` order: Attack, Defense, Speed, Special Attack, Special Defense,
accuracy, evasion; each 0..12, neutral 6.

### `SidedexDynamicTurn` (12 bytes, at offset 824)

`playerKind` (0), `playerMove` (2), `playerSlot` (4), `opponentKind` (6),
`opponentMove` (8), `opponentSlot` (10).

### Mailbox size and validation

`SIDEDEX_DYNAMIC_REQUEST_BYTES` is 864 and is what `ROMTRACE|ready|<addr>|<size>`
announces. `SidedexDynamicRequestValid` rejects a version other than 8, an
out-of-range `op`/`mode`/`criticalPolicy`, and the existing v7 field checks, and
prints `ROMTRACE|invalid|1`.

### v8 trace markers

| Marker | Meaning |
| --- | --- |
| `ROMTRACE|boundary|<kind>|<turn>|<legal-action-count>` | decision boundary; `kind` is `player-choice` or `forced-switch-player` |
| `ROMTRACE|terminal|<win\|loss\|draw>|<reason>` | battle ended; no boundary follows |
| `ROMTRACE|unrepresentable|<field>` | a contract 2.2 field the exporter cannot read |
| `ROMTRACE|instruction-budget-exceeded|<instructions>` | logic-only pump budget exhausted |
| `ROMTRACE|phase|<turn>|boundary\|terminal\|unrepresentable\|...` | compatibility mirror kept by the legacy slim trace filter |
| `ROMTRACE|phase|<turn>|state|<group>|...` | decoded state export (groups and field counts in `tools/rom_oracle/README.md`) |
| `ROMTRACE|rng|<turn>|a|b|c|ctr|a2|b2|c2|ctr2` | both RNG streams at the boundary (unchanged v7 marker) |

### Appendix A addendum (A2 revision)

Mailbox layout, `SIDEDEX_DYNAMIC_VERSION` (8) and every byte offset above are
**unchanged** by A2. The A2 artifact is
`native/rom_worker/build/mingw/pokeemerald-test-v8b.elf`.

New/changed trace spellings (all additive; canonical spellings unchanged):

| Marker | Meaning |
| --- | --- |
| `ROMTRACE|legal|move|<move_id>|<pp>|<target>` | ROM-derived legal player move at a decision point |
| `ROMTRACE|legal|switch|<slot>|<species>` | ROM-derived legal player switch at a decision point |
| `ROMTRACE|legal|item|<item_id>` | reserved; never emitted in this build (no player bag, and trainer battles outside easy mode forbid item actions) |
| `ROMTRACE|phase|<turn>|legal|move\|switch\|item|...` | slim-filter mirror of the above |
| `ROMTRACE|query|outcomes|<owner>|<move>|<flags>|<chunk>|<chunks>|<denom>|<branches>|<hitsMode>|<minHits>|<maxHits>|<accuracy>|<hp>|<weight>|...` | complete outcome-weight enumeration, <= 8 branches per record, `denom` 1000000, weights sum to `denom` |
| `ROMTRACE|unrepresentable|trainer-item-decision` | the AI's item decision was refused by the ROM ruleset inside a resolved turn; the transition is `unsupported-mechanic` |
| `ROMTRACE|phase|<turn>|item-refused|<battler>|<item_id>` | diagnostic detail for the refusal |

`query|outcomes` `flags` bits: 1 multi-hit, 2 Focus Sash folded in, 4 Sturdy
folded in, 8 accuracy < 100, 16 enumeration refused, 32 internal axis clamp.
`hitsMode`: 0 standard 2-5, 1 Loaded Dice, 2 Skill Link, 3 Battle Bond, 4 fixed.

**Wire-contract ordering rule (part of the wire protocol):** `ROMTRACE|boundary|`
and `ROMTRACE|terminal|` must be the LAST records of their emission. The worker's
`batch_turns` stop treats them as "the turn's evidence is complete" and ends the
frame loop, so a marker emitted before the state/legal/rng/export group truncates
it (measured: 4 of 37 records returned before the fix, 37 of 37 after).

`secondPresent` (offset 842) values: 0 plain candidate turn, 1 second scripted
turn, 2 pre-choice query, 3 forced-switch scenario (`secondSlot` at 840 is the
recorded compulsory-replacement party index; the ROM holds at the
`forced-switch-player` boundary and injects the real choice).

A2 also fixes a base-code bug in `src/recorded_battle.c`:
`RecordedBattle_ClearBattlerAction` decremented a `u16` cursor before testing it,
so a battler with no recorded actions (the live AI of a non-link recorded battle)
underflowed to 65535 and wrote `sBattleRecords[battler][65535]`, ~64 KiB out of
bounds. The guard now runs before the decrement.

## Appendix B: v2 execution amendment (2026-09-28; orchestrator frozen)

This amendment supersedes v1 only where specified here. The pinned v8/worker-v2
path stays readable for diagnostics. Implementation targets
`transition_contract=2`, `rom_hook_protocol=9`, `wire_protocol=3`,
`transition_schema=sidedex-transition-v2`, and `decode_schema=decode-v2`.
`state_schema` and `save_format` retain their v1 names because the underlying
mGBA savestate format is unchanged. The worker advertises supported versions in
`status`; the client selects v9/v3 explicitly for compact turns and rejects a
missing version. It never guesses a layout from a reply. The coordinated artifact
flip changes the ELF, worker, both pin locations, and cache identity together.

### Child capture and boundary

The v1 section 3 definition is normative. The hook completes its state, legal,
RNG, trainer-action and mechanical-event records, commits the binary result,
then emits the final `ROMTRACE|binary-ready|<address>|<length>` marker. The
worker reads and verifies the committed result, **saves the live emulator at
that boundary**, and only then restores the parent for the next batch entry.
Saving after a parent restore, at a trace marker emitted before export, or after
another emulated frame is invalid. A nonterminal turn lacking a saved child is
an error and cannot count as a verified transition. Terminal turns have no child.

Each successful nonterminal worker result contains `child` with a newly minted
immutable handle, savestate byte count and SHA-256, plus an exportable portable
blob (base64 in the bounded JSONL envelope, or a separately retrievable blob
whose handle is retained until export). The public `DecisionState.token`
contains that blob/digest and the full `TransitionIdentity`; the handle is an
optimization. The source parent slot is never replaced. Importing the blob on
an identical worker permits A→B→restore A→C and B→D. Terminal results contain
`child=null`. Exceeding the three-slot/64-MiB limits must fail explicitly;
the scheduler may export and drop an older handle before a batch.

### Complete projection and ROM legality

`decoded` is a named, typed object assembled solely from the v9 ROM export.
Required groups are: `party[2][1..6]` (species, level, current/max HP, all
four move IDs/PP, status1, held item, ability, form, nature, six IVs and six
EVs); `active[2]` (party index, species, HP, status1/status2/status3, mutable
ability/form, types, seven stat stages, moves/PP, volatile flags, disable,
protect, special status, substitute HP, encore/taunt timers);
`field` (weather and timer, terrain and timer, trick room and timer);
`side[2]` (screens, hazards and layers, side conditions and all side timers);
`battle` (turn, type flags, outcome, AI flags and trainer item inventory);
and `rng` (both four-word streams). Existing v8 party/active/field/side-timer/
battle/ai-flags/trainer-item records provide a subset; the v9 exporter must
add every missing value from its named C source. All array lengths and
presence bits are explicit. A missing required group/value, contradictory
active index, invalid count, or exporter `unrepresentable` record rejects
the result as `incomplete-state` or `unrepresentable-state`. Unreadable
mechanics are reported as unsupported with their source reason; zero is never
invented. The decoded projection is never used to rebuild a child savestate.

`legal_player_actions` is built from the ROM's v9 legal records at the
*resulting* boundary: move = kind/move_id/PP/target, switch = kind/slot/species,
item only if the ROM actually offers it. The announced action count must equal
the parsed count, actions must be unique and consistent with the decoded state,
and a nonterminal boundary needs at least one. The snapshot's candidate list
may filter card presentation but is not the node's legality source. At a
forced-switch boundary only switches are legal; at terminal there are none.

### v9 mailbox and compact result

The v8 request's first 864 bytes keep their offsets. v9 appends little-endian
`resultAddress:u32` at 864, `resultCapacity:u32` at 868,
`resultVersion:u16=1` at 872, and `flags:u16` at 874 (bit 0 requests compact
result; remaining bits zero); total **876 bytes**. The ROM validates the
address/capacity as an EWRAM range disjoint from the request and its own active
battle state. The test runner reserves 8192 bytes from the existing EWRAM
`gHeap` immediately after `InitHeap` and retains the allocation through all
held transitions; allocation failure fails closed. It announces the actual
pointer/capacity in `ROMTRACE|ready|<request_addr>|876|9|3|<result_addr>|8192`.
The worker supplies exactly that address/capacity, and the ROM checks the echo;
neither side accepts an arbitrary caller address. The pinned source linker map
puts `gHeap` at `0x0201A6E8` and the mailbox at `0x0203FC3C`; the runtime
pointer, not this predicted address, is authoritative. This heap reservation
replaces the proposed static result array because adding 8192 bytes to
`.ewram.sbss` overflows the test ELF by 8104 bytes. A v8 request uses the old
trace path only.

Result bytes are little-endian. Header (64 bytes): magic `SDR2` (4), format
version `u16=1`, header bytes `u16=64`, total bytes `u32`, CRC32 of bytes
12..end with the CRC field zeroed `u32`, requestId `u16`, status `u16`,
boundary kind `u16` (0 none, 1 choice, 2 forced switch, 3 terminal),
mode `u16`, critical policy `u16`, record count `u16`, turn `u32`,
main RNG after `4×u32`, secondary RNG after `4×u32`.
Records follow as `tag:u16, payload_bytes:u16, payload`, padded to four
bytes. Total bytes cannot exceed the announced capacity or 1 MiB. The
worker rejects CRC, length, version, requestId, mode/policy, record-count or
identity mismatch before returning any child. A nonzero status carries a
versioned error record and no child.

Tags 1..7 are the required typed projection groups in the order listed above
(party, active, field, side, battle, RNG, legal). Tags 8 and 9 are trainer
action and ordered mechanical events; tag 10 is terminal result; tag 11 is
unsupported reason. Each projection payload begins with `entry_count:u16,
words_per_entry:u16`, then fixed-width `u32` words in the documented
`decode-v2` field order above. Variable events use
`event_kind:u16, payload_words:u16, payload:u32[]` subrecords. Unknown
required tags, duplicate singleton tags, bad counts, or absent legal/RNG
data fail closed. The hook keeps `ROMTRACE` for diagnostic and differential
runs; production `batch_turns_v3` sends base64-encoded contiguous binary
mailboxes in one JSONL control request and returns compact base64 result bytes,
child summaries/blobs, frames and IPC byte counts without parsing textual
state records. Query operations may retain trace transport until independently
ported, but must honor the same token identity and immutability rules.

## Appendix C: contract v2.1 (2026-09-28; orchestrator frozen)

Supersedes nothing in Appendix B; it pins the details Appendix B left open. Word
tables and vectors are executable: `tools/rom_oracle/make_sdr2_vectors.py`
(`TABLES`, `sdr2_vectors/tables.json`) is the reference *encoder*; package B's
`tools/rom_oracle/sdr2.py` is the only production *decoder*; package A1's ROM
code must emit bytes the decoder accepts. Golden vectors are synthetic wire
examples (they pin layout, CRC, padding, negatives), not ROM-derived state.

### C.1 SDR2 byte layout (little-endian)

Header, 64 bytes: `0 magic "SDR2"`, `4 u16 format=1`, `6 u16 header_bytes=64`,
`8 u32 total_bytes`, `12 u32 crc32`, `16 u16 request_id`, `18 u16 status`,
`20 u16 boundary`, `22 u16 mode`, `24 u16 critical_policy`, `26 u16 record_count`,
`28 u32 turn`, `32 u32[4] rng_main`, `48 u32[4] rng_secondary`. CRC is zlib CRC-32
of `buffer[12:total]` computed with the CRC field (bytes 12..15) zeroed. Each
record is `u16 tag, u16 payload_bytes, payload`, zero-padded to 4 bytes
(`payload_bytes` excludes padding, max 65535). Tags appear in ascending order and
are singletons. A decoder rejects: bad magic/format/header size, `total_bytes`
that differs from the buffer length or exceeds the announced capacity (8192 in
production) or 1 MiB, CRC mismatch, `record_count` mismatch, duplicate or
descending tags, unknown required tags, `entry_count*words_per_entry*4 + 4 !=
payload_bytes`, wrong `words_per_entry`, request id / mode / critical policy
differing from the request, and missing required tags.

Required tags: nonterminal success (`status=0`, boundary 1 or 2) = 1..9;
terminal success (boundary 3) = 1..6, 8, 9, 10 (no tag 7); any nonzero status =
exactly tag 11 and no other record (no child). Tag 9 may be empty
(`payload_bytes=0`). Tag 7 must carry at least one action for boundary 1/2, and
at boundary 2 only switch actions.

### C.2 Word tables (`u32` words; typed payloads start `entry_count:u16,words_per_entry:u16`)

| Tag | Group | Entries x words | Field order |
| --- | --- | --- | --- |
| 1 | party | 12 x 32 (index = side*6+slot) | side, slot, species, level, hp, max_hp, status1, item, ability, form, nature, move1-4, pp1-4, iv hp/atk/def/spe/spa/spd, ev hp/atk/def/spe/spa/spd, present |
| 2 | active | 2 x 37 (singles) | battler, party_slot, species, hp, max_hp, status1, status2, status3, ability, item, form, type1-3, stages atk/def/spe/spa/spd/acc/eva, move1-4, pp1-4, volatile_flags, disabled_move, disable_timer, protect_flags, special_status_flags, substitute_hp, encore_timer, taunt_timer |
| 3 | field | 1 x 6 | weather, weather_timer, terrain, terrain_timer, trick_room, trick_room_timer |
| 4 | side | 2 x 12 | side, screens, hazards, hazard_layers, conditions, reflect, light_screen, aurora_veil, mist, safeguard, tailwind, follow_me timers |
| 5 | battle | 1 x 10 | turn, outcome, type_flags, battler_count, ai_flags, item_count, item1-4 |
| 6 | rng | 1 x 8 | main a,b,c,ctr, secondary a,b,c,ctr (must equal the header pair) |
| 7 | legal | N x 4 | kind (0 move, 1 switch, 2 item), id, pp_or_species, target_or_slot |
| 8 | trainer action | 1 x 3 | kind, id, target |
| 9 | events | variable | subrecords `event_kind:u16, payload_words:u16, payload:u32[]` |
| 10 | terminal | 1 x 2 | result, reason |
| 11 | unsupported/error | 1 x 2 | reason, source |

`active` is 37 words (Appendix B's draft said 33 with packed protect/special
fields; packed fields were unspecified, so they are split). Every flag word names
its C source in the exporter; unreadable values use tag 11, never zero-fill.
The exact bit meaning of `volatile_flags`, `protect_flags`, `special_status_flags`,
`screens`, `hazards`, `hazard_layers` and `conditions` is defined in the A1
README section "v9 flag words" (with `file:line` sources) and is checked by the
compact-vs-`ROMTRACE` equality test.

### C.3 Enum tables

- Boundary kind: 0 none, 1 choice, 2 forced switch, 3 terminal.
- Status: 0 ok, 1 incomplete-state, 2 unrepresentable-state, 3 unsupported-mechanic,
  4 instruction-budget-exceeded, 5 frame-cap-exceeded, 6 invalid-request,
  7 internal-error. A nonzero status carries only tag 11.
- Tag 11 `reason`: same numbering as status. `source`: 1 export coverage,
  2 controller exec flags, 3 AI scoring scratch, 4 preset field/side state in a
  request, 5 trainer item use, 6 non-singles battle, 7 form/mega/tera change,
  8 request validation, 9 other (must name a C source in the A1 README).
- Terminal `result`: 1 player win, 2 player loss, 3 draw, 4 escaped, 5 captured.
  `reason`: 0 normal, 1 forfeit, 2 turn cap.
- Event kinds (tag 9): 1 move used [battler, move, target]; 2 damage [battler,
  amount, hp_after]; 3 heal [battler, amount, hp_after]; 4 status change [battler,
  before, after]; 5 stat stage [battler, stat, delta]; 6 faint [battler];
  7 switch out [battler, slot]; 8 switch in [battler, slot, species];
  9 weather [before, after]; 10 terrain [before, after]; 11 ability activated
  [battler, ability]; 12 item used/consumed [battler, item]; 13 miss/fail
  [battler, move, reason]; 14 critical hit [battler]; 15 hazard/side change
  [side, kind, value]. An unknown event kind fails closed.
- Error codes surfaced by the client: `sdr2-crc`, `sdr2-length`, `sdr2-version`,
  `sdr2-request-id`, `sdr2-mode`, `sdr2-record-count`, `sdr2-duplicate-tag`,
  `sdr2-missing-tag`, `sdr2-oversize`, `sdr2-identity`, `sdr2-status` (tag 11).

### C.4 Child transport decision

The worker result's inline child summary is `{handle, bytes, sha256}`; the
portable blob is retrieved with `export_state` on demand (already built in B).
A public token (`DecisionState.token`) always carries the blob or a still-live
exportable handle before it leaves the worker's request scope; the host adapter
(D) exports before a handle can be evicted. The three-slot / 64 MiB bounds and
explicit failure on exceeding them stand.

### C.5 Performance decision rule and frozen benchmark set

The rule in ROM_TRANSITION_FINISH_PLAN.md section 6 is frozen. Logic-only
becomes the planner default only if it gives at least 20 % more 3-worker pool
throughput than optimized normal mode on the frozen fixtures and passes full
parity on every supported row; otherwise it is a documented test-only flag.
Frozen benchmark fixtures: P1, P29, P25, P26, P19, P27, P28, each with the
fixed paired dual-RNG seeds of `tools/rom_oracle/fixtures/`, in both modes, on
parity-passing transitions only. Warm end-to-end card latency target: p50 <= 40 ms.

### C.6 Request flags and worker stop rule (2026-09-28 amendment)

v9 request `flags:u16` at offset 874: bit 0 = compact result, bits 1-2 = battle
difficulty override (0 keep the test default, 1 easy, 2 normal, 3 reserved and refused: the tree defines only GAME_SETTING_DIFFICULTY_NORMAL_MODE and _EASY_MODE in include/constants/game_settings.h; mapped by
the hook to the ROM's DIFFICULTY_* values, with the source cited in the A1/A2
README). All other bits must be zero and the ROM rejects a request that sets
them. Difficulty is part of the canonical request hash and of cache identity.
Worker stop rule for compact turns: the hook emits `ROMTRACE|binary-ready|addr|len`
LAST (after the boundary or terminal marker and after the header and CRC are
committed); the worker stops and saves the child at binary-ready, never at the
earlier boundary/terminal marker, and a missing binary-ready within the frame
bound is a fail-closed error. Wire protocol 2 is retired: worker 0.3.0 speaks
wire 3 only.

### C.7 Player-action code `SIDEDEX_DYNAMIC_AI` (2026-09-28, package H)

`candidateKind` (offset 12) value **2** (`SIDEDEX_DYNAMIC_AI`, `include/test/sidedex_dynamic.h`)
released at any player boundary (choice or forced switch) lets the pokeemerald-expansion
trainer AI choose the PLAYER battler's action instead of an injected move/switch:
move and target, a voluntary switch (`AI_TrySwitchOrUseItem`), or the KO / forced
replacement (`GetMostSuitableMonToSwitchInto`, same decision tree as
`OpponentHandleChoosePokemon`). `playerMove` and `playerSlot` are ignored.
AI flags for the player battler = the opponent's AI flags | `AI_FLAG_SMART_SWITCHING`
| `AI_FLAG_SMART_MON_CHOICES`. `B_FLAG_AI_VS_AI_BATTLE` stays 0 and is not used: the player
counts as an AI battler (`BattlerHasAi`) only inside its own AI computation, so the
opponent's AI sees the player exactly as for an injected action. The player has no
item action (the opponent's trainer items are hidden from the player AI) and no
gimmick channel. The AI consumes RNG in the released turn like any other decision, so
determinism still comes from the injected RNG pair. `candidateKind` 2 is rejected
with `secondPresent == 1` and is not valid in replay turns. The request layout, size
and SDR2 result are unchanged (TESTING only; the TEST=0 release ROM is unaffected).
Python: `run_dynamic.encode_request` accepts `{"kind": "ai"}` as `candidate_action`.
