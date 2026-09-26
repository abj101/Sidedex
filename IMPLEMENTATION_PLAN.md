# Emerald Imperium battle planner: audit and implementation plan

Source checked: `iriv24/pokeemerald-expansion`, `release` at `2576d0f3f4c861c760f0c55198e37e6d7b91683e` (2026-09-24). The local Sidedex repository currently contains only a README and LICENSE; there is no application code to reuse yet. The attached idea is a proposal, not an instruction to adopt its architecture.

## Audit verdict

Build a local planning UI, but change the proposed data source and defer the exact AI promise. The first useful product is **owned Box → trainer ID → two-way matchup grid → explainable party swaps**. Only show conclusions the available battle state can support.

1. **The proposed trainer importer points at inactive data.** [`include/config/general.h`](https://github.com/iriv24/pokeemerald-expansion/blob/2576d0f3f4c861c760f0c55198e37e6d7b91683e/include/config/general.h#L74) sets `COMPETITIVE_PARTY_SYNTAX` to `FALSE`; [`Makefile`](https://github.com/iriv24/pokeemerald-expansion/blob/2576d0f3f4c861c760f0c55198e37e6d7b91683e/Makefile#L346-L349) runs `trainerproc` on `.party` only when it is true. [`src/data.c`](https://github.com/iriv24/pokeemerald-expansion/blob/2576d0f3f4c861c760f0c55198e37e6d7b91683e/src/data.c#L229-L234) includes `trainer_parties.h` and `trainers.h`. Concrete mismatch: `TRAINER_SAWYER_1` is Geodude, level 21 in [`trainers.party`](https://github.com/iriv24/pokeemerald-expansion/blob/2576d0f3f4c861c760f0c55198e37e6d7b91683e/src/data/trainers.party#L9-L20), but Alolan Graveler, level 40 in [`trainer_parties.h`](https://github.com/iriv24/pokeemerald-expansion/blob/2576d0f3f4c861c760f0c55198e37e6d7b91683e/src/data/trainer_parties.h#L1-L7). Use the compiled C tables, keyed by trainer constant. Name alone is ambiguous across rematches.
2. **The AI source is a reference implementation, not a ready library.** [`battle_ai_main.c`](https://github.com/iriv24/pokeemerald-expansion/blob/2576d0f3f4c861c760f0c55198e37e6d7b91683e/src/battle_ai_main.c#L492-L552) reads live battle globals and randomly breaks equal scores. [`battle_ai_switch_items.c`](https://github.com/iriv24/pokeemerald-expansion/blob/2576d0f3f4c861c760f0c55198e37e6d7b91683e/src/battle_ai_switch_items.c#L1018-L1115) depends on party, hazards, flags, statuses, prior actions, and random switch chances. The source determines behavior **given complete state and RNG**, but species pair alone cannot yield an exact action. Porting three C files to Python would still require much of the battle engine.
3. **The displayed trainer build may depend on the save.** [`CreateNPCTrainerPartyFromTrainer`](https://github.com/iriv24/pokeemerald-expansion/blob/2576d0f3f4c861c760f0c55198e37e6d7b91683e/src/battle_main.c#L1975-L2189) can select an alternate party, treat a stored level of `200+n` as the player's highest party level minus `n`, lower boss levels in Easy mode, or raise Elite Four/Champion levels after becoming Champion. Missing moves inherit the game's generated level-up moves. Species and moves also have conditional C initializers; the type table is in [`src/data/types_info.h`](https://github.com/iriv24/pokeemerald-expansion/blob/2576d0f3f4c861c760f0c55198e37e6d7b91683e/src/data/types_info.h), which the proposal omitted. Keep raw values and mark unresolved context; do not present a static team as exact when it is conditional.
4. **The proposed Python architecture is much larger than the need.** Keep Streamlit as the one UI dependency. Use Python standard library `sqlite3` for owned Pokémon and a pinned, generated JSON snapshot for read-only game data. Skip SQLAlchemy, Pydantic, NumPy, pandas, an API service, a model, and a Python copy of the battle engine. Add a dependency only when a measured requirement demands it.

## Source-of-truth pipeline

```text
pinned Imperium commit + active config
  → narrow extractor for trainer IDs, trainer builds, species/types, moves used, type table
  → checked JSON snapshot with source SHA and extraction warnings
  → Streamlit UI + sqlite3 owned Box
  → two-way matchup grid + rule-based, explained swaps
  → optional verified damage/AI adapter after feasibility gates
```

Keep game data read-only and separate from user-owned data. Do not copy the entire upstream repository into Sidedex. Pin its commit in the extractor configuration; refresh deliberately and show the source SHA in the UI. If distributing copied source or data, check upstream and inherited license terms first; the fork has no root `LICENSE` file at this commit.

## Orchestrated delivery

| Step | Smallest deliverable | Done when | Depends on |
| --- | --- | --- | --- |
| 0. Source lock | Record commit and active config; choose one representative boss and one ordinary trainer. | Both trainer IDs resolve to the active C tables, and the Sawyer mismatch is captured as a regression check. | None |
| 1. Thin extractor | Read `trainers.h`, `trainer_parties.h`, species fields, move fields used, and `types_info.h`; emit one JSON file with warnings for unresolved macros/defaults. Preserve raw level values and alternate-party markers. Parse only fields needed for the first screen. | Sampled teams match C source for species, raw level, moves, item, nature, ability, and AI flags; unknowns remain explicitly unknown. | 0 |
| 2. Vertical slice | One Streamlit page: create/edit Box entries, party checkbox (maximum six), alive/dead/unavailable, trainer ID picker, two-way type/move matchup grid. Ask for difficulty/highest party level when the selected trainer needs them; flag unsupported alternate-party selectors. Store owned entries in one SQLite table; `pair_id` and owner can be nullable columns. | Restart preserves Box; dead/unavailable entries never appear as candidates; conditional teams are never labeled exact without required context. | 1 |
| 3. Minimal recommendation | Rank legal swaps by coverage of the selected team using only verified type interactions and known moves. Explain each suggestion and mark it "type coverage only." Include current party as baseline. | A fixture with an obvious missing immunity/resistance recommends the relevant live Box member; ties stay ties; no KO or safe-switch claims. | 2 |
| 4. Damage gate | Probe the existing [`battle tests`](https://github.com/iriv24/pokeemerald-expansion/blob/2576d0f3f4c861c760f0c55198e37e6d7b91683e/test/battle/damage_formula.c) as an oracle for a few fixed singles states. Use the game's C path if a repeatable runtime adapter is practical. | Candidate calculations match the game on damage rolls, items/abilities, and a hack-specific mechanic. Otherwise keep step 3 and label damage unsupported. | 3 |
| 5. AI gate | Probe the existing [`AI battle tests`](https://github.com/iriv24/pokeemerald-expansion/blob/2576d0f3f4c861c760f0c55198e37e6d7b91683e/test/battle/ai/ai_choice.c) with an explicit state and fixed RNG. Prefer a thin game/test-harness adapter over a Python port. | Repeated scenario returns the same move/switch/action set as the game, including tie and switch randomness. If the adapter needs a broad engine fork, stop and expose only AI flags plus uncertainty. | 4 |
| 6. Constraints and builds | Enforce Soul Link pair availability and test a specific obtainable move/item/nature change only when the user asks for it. | No dead pair is recommended; every build suggestion shows its legal source and the verified matchup delta. | 3; verified damage for quantitative build claims |

Steps 0–3 are the first release. Steps 4–6 are gates, not promises: implement only when they improve a real planning decision and have a game-backed check. Do not add a local decision model unless users find a concrete judgment the deterministic pipeline cannot express.

## UI contract for the first release

Select trainer by **ID plus display name**; show the source revision and whether the battle is singles or doubles. Enter owned Pokémon with species, level, ability, item, nature, moves, IVs/EVs if known, status, party membership, owner, and optional Soul Link pair. Ask for difficulty, highest party level, or alternate-party trigger only where that trainer requires it. The grid shows candidate versus each enemy: known moves, outgoing type effectiveness, incoming type effectiveness, and notes for unknown or conditional mechanics. Suggestions explain which uncovered enemy or immunity they address. Doubles can display teams, but battle advice stays off until doubles logic is verified.

## Decisions intentionally deferred

- Exact move choice, safe switches, KO odds, and optimal six: require real battle state, damage behavior, and game-backed validation.
- Whole-Dex relational import and learnset legality: start with fields used by the current Box and selected trainers; expand on demand.
- Automatic save-file ingestion, route progression mapping, multi-user sync, optimizer search, and LLM: add only after the manual flow proves useful.
