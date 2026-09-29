# Phase-4 differential fixture matrix (acceptance spec)

Orchestrator-owned. Package E implements this; every row must end up either **green** (compared, equal, with
evidence) or **explicitly unsupported** (fail-closed reason recorded), never silently skipped. A row moving
to green requires the evidence named in the comparison contract below.

Fixtures are frozen inputs: a decision-state token (or a reproducible root recipe), both RNG streams, the
player action, the mode (`normal` / `logic-only`), and the critical policy (`normal`, unless the row says
otherwise). Fixtures never read the wall clock and never reseed from RNG.

## Comparison contract (applies to every row)

For each fixture run the **normal** and **logic-only** test ELF from the same decision checkpoint and compare:

1. trainer action (kind + id + target);
2. every decoded state field named in contract §2.2 (both parties, actives, stat stages, volatiles,
   weather/terrain/timers, side conditions, turn counters, trainer AI/item inventory, battle result);
3. boundary kind and terminal result;
4. ordered mechanical events;
5. final values of both RNG streams (`main_after`, `secondary_after`);
6. raw/opaque state comparison where the serializer metadata is deterministic;
7. frames and elapsed ms (recorded, not asserted equal).

A mismatch is a failure. Benign presentation-byte differences must be named in an explicit exclude list with
a reason; anything not on that list fails. Missing data never counts as equal.

## Matrix

| ID | Scenario | Mechanics exercised | Notes |
| --- | --- | --- | --- |
| P1 | physical damage, neutral | physical formula, contact | baseline row |
| P2 | special damage, neutral | special formula | |
| P3 | physical damage, resisted | type effectiveness | |
| P4 | special damage, super effective | type effectiveness | |
| P5 | accuracy miss | accuracy roll, RNG stream 1 | deterministic miss seed |
| P6 | normal critical | real crit roll, `critical_policy=normal` | required parity row |
| P7 | forced critical (Scope Lens class) | crit stage from held item | must differ from P6 only by crit |
| P8 | priority move | priority bracket | |
| P9 | speed tie | speed comparison, tie roll | both orders must be exercised |
| P10 | status: paralysis full-turn skip | status1, RNG | |
| P11 | status: sleep wake | status1, counter, RNG | |
| P12 | volatile: confusion self-hit | volatile state | |
| P13 | volatile: disable / protect data | disable + protect structs | exercises blob export |
| P14 | ability: damage-modifying ability | mutable ability | |
| P15 | ability: immunity ability | ability gate | |
| P16 | held item (damage modifier) | item | |
| P17 | held item: Focus Sash at full HP | Sash + multi-hit composition | |
| P18 | Sturdy at full HP | Sturdy | |
| P19 | trainer item decision inside a resolved turn | real ROM AI item usage | must execute, not rerun item-agnostically |
| P20 | weather set / expiry | weather + timer | |
| P21 | terrain | terrain + timer | |
| P22 | hazards (Stealth Rock class) | side conditions | |
| P23 | screens | side conditions + timer | |
| P24 | switch-in effect (Intimidate class) | switch-in effects | |
| P25 | player switch, opponent attack | switch boundary, `forced-switch-player` if required | |
| P26 | faint and forced replacement | faint transition + replacement boundary | |
| P27 | terminal win (all opposing fainted) | `terminal` result | no boundary after |
| P28 | terminal loss | `terminal` result | |
| P29 | multi-turn branch (two successive transitions from one root) | node re-use, restore determinism | branch = two different first actions |
| P30 | smart-AI trainer (boss `ai_flags`) | real AI with flags | |
| P31 | first-turn KO regression (`TRAINER_MATT_SEAFLOOR_CAVERN`) | existing regression, faint/`ready` path | must not regress |
| P32 | multi-hit move enumeration vs normal engine | QUERY_DAMAGE weights vs normal-mode outcome | phase-2/4 boundary row |

Rows whose mechanics the ROM cannot yet represent must be marked unsupported with the exact reason
(`unsupported-mechanic` and the ROM record naming the field) — not deleted, not silently skipped.

## Non-fixture checks (also required for the gate)

- **AI leakage**: with the same pre-choice token and identical RNG, run at least three different player
  actions; where game rules say the trainer decision must be independent of the player's unrevealed action,
  the trainer's pre-reveal choice must be identical. A difference is a failure.
- **Restore determinism**: restore the same token twice and re-export; decoded projection + RNG + state bytes
  identical. Token restored after another branch must reproduce the same bytes.
- **State-token identity rejection**: tokens minted under a different ELF / worker / wire protocol / mode /
  critical policy must be rejected with `identity-mismatch`, and a token whose owner is gone must return
  `stale-handle` — never silently re-created.
- **Query immutability**: `QUERY_DAMAGE` and `QUERY_AI_ACTION` leave the source token's decoded projection,
  RNG, and state bytes byte-for-byte unchanged, and do not resolve a turn.
- **Query independence**: exact query cards publish even when transition evaluation fails, and query
  availability does not depend on rollout completion.
- **Paired RNG**: the same root pair is used for every candidate action; a sample counts as paired only when
  every required action completed; partial work returns completed work, never invented zeros.
- **Pool**: one request demonstrably uses all three workers; the same paired results as one-worker execution;
  one worker exit survives without duplicate observations; deadlines respected.

## Evidence format

Each green row records: fixture id, token/root recipe, both RNG pairs, mode pair, player action, the compared
projection digest, the event list, gate parameters (source sha, ELF sha, worker sha, protocol, state schema,
mode, policy), and the exact command that regenerates it. Evidence lives under `tools/rom_oracle/` as a
fixture file plus a test that regenerates and compares it; generated binaries stay out of Git.
