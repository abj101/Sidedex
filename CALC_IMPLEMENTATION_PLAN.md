# ROM-accurate head-to-head Calc: orchestrated implementation plan

## Mandate and decisions

Implement the four existing Calc result cards for Emerald Imperium trainer **singles**:
player move KO probabilities, **Recommended action**, the AI action distribution,
and enemy move KO probabilities. The full-battle calculation button, whole-battle
win probability, doubles, and player bag actions are later work. Do not build a
turn-by-turn battle playback.

Use the pinned source in `data/source_lock.json`, commit
`2576d0f3f4c861c760f0c55198e37e6d7b91683e`. The sibling checkout
`../pokeemerald-expansion-ref` was at that commit during planning; verify it
again before using it. Runtime must be standalone Python plus the app's existing
Streamlit dependency. A development-only ROM build or emulator may serve as a
verification oracle. The local reference checkout has no `test/battle` directory
and no GBA build tools were found on PATH, so do not assume cited upstream test
files or a ready oracle exist.

Behavior already chosen with the user:

- Keep “Recommended action” as the card title. It may recommend a legal move or
  switch to a living party member. Label its basis **two-turn lookahead**; it is
  not a claim of whole-battle optimal play.
- Automatically recalculate after a relevant edit. Keep the page responsive,
  reuse results for identical states, discard stale results, and give each
  updated state a **two-second wall-clock budget**. Display an independently
  completed exact card even if another card is unavailable. Never display a
  sampled or approximate percentage as exact.
- Exact probabilities use the ROM RNG, including correlations between draws,
  over uniformly possible starting seeds consistent with the observed turn
  history. The ROM uses seeded SFC32 streams; a single in-game decision is not
  equivalent to a probability over every possible seed. If exact aggregation
  cannot finish in budget, show `Unavailable` and a specific reason.
- Preserve separate **focused** and **active** Pokémon selections. The focused
  cards are for browsing; active selections determine battle calculations.
  Include an explicit planned player move or switch for enemy move KO odds.
- Include trainer AI moves, party switches, and bag item use. The inspected
  singles AI predicts from prior move/battle history, not the current planned
  player action, so its action distribution is for the entered battle state.
- User and enemy move cards give probabilities **conditional on that move being
  selected**, not the probability of selecting and landing it. Count a KO of
  the Pokémon active at turn start caused by that move and its immediate
  effects during this turn. A switch prevents a KO of that turn-start target;
  unrelated residual damage does not count as the move's KO. Include move
  failure, accuracy, critical hits, damage rolls, multi-hit behavior, and action
  order when relevant. Enemy move odds also condition on the planned player
  action. Show the AI's action-choice probabilities separately.
- A missing player level, IV, EV, nature, ability, item, or other consequential
  value is unknown, not zero. Prefill only values verified from the ROM and the
  recorded build. “Use Item” off simulates no held item.
- Replace the free numeric level cap with a selected ROM progression milestone
  from `src/caps.c`. Simulate the ROM's raise-to-cap routine on a copy of the
  party, assuming all members are unmarked; never alter saved Box levels.
  Resolve `200+n` trainer levels from the highest **simulated** party level.
  Apply Easy-mode boss-level changes and other source conditions. Offer only
  Easy and Normal: the pinned ROM has no Hard mode. An old config saved as Hard
  requires an explicit mode correction before exact calculations.
- Save the manual turn state and observed turn log with each named Calc config.
  Conditional trainer parties, post-Champion variants, and other required
  progression flags must be resolved from entered context or reported unknown.

## Orchestration in Herdr

The user requested Pi agents under Herdr with one primary orchestrator. The
orchestrator must run **inside a Herdr-managed pane** and first verify
`HERDR_ENV=1`. If absent, stop Herdr control and report that prerequisite;
never operate another client's focused session from outside Herdr. In a valid
pane, inspect `herdr --help`, `herdr agent`, the current pane, and existing
agents. Start available `pi` agents in sibling panes in the current tab and
working directory, using `--no-focus`; keep the user's focus in the controller
pane. Read IDs from command responses. Do not create worktrees, tabs, or a new
workspace merely for these roles. Give each agent the relevant section of this
file and ask for short status reports with changed files, verification, and
blocking evidence. If Pi is not an installed Herdr kind, report that limitation
and ask before substituting another kind.

All agents share one checkout. The orchestrator fixes the interface contract
below **before** parallel edits and owns integration. Use these nonoverlapping
file assignments:

| Role / Pi agent | Owned implementation | Required handoff |
| --- | --- | --- |
| `rom_source` | `tools/extract_game_data.py`, generated `data/game_data.json`, source-oracle fixtures and extractor checks | Source fields, active config, fixture provenance, unresolved conditions, and exact ROM outputs |
| `battle_core` | New standalone battle/RNG/AI engine module and its focused tests | Supported-mechanic registry, exact result examples, timing, and reasons for unsupported cases |
| `calc_ui` | `planner_ui.py`, `app.py`, Calc config/state UI, related CSS and UI tests | Turn-state editor, config migration behavior, card rendering, and stale-job handling |
| Orchestrator | Shared interface contract, cross-module wiring, integration/performance tests, docs and final review | One verified end-to-end singles scenario; no false exact claims |

Agents must not rewrite another role's files. Route interface changes to the
orchestrator, who broadcasts the revised contract before dependent edits.
Preserve existing uncommitted and untracked user work; this repo had many such
files at planning time. Do not reset, clean, or overwrite them. Work in waves:

1. **Preflight and contract** — orchestrator verifies the source SHA, records
   baseline checks, pins the state/result schema below, and dispatches the
   three agents. Agent `rom_source` establishes the oracle first; the others
   may build against the contract but must not assert ROM accuracy yet.
2. **Independent implementation** — source extraction and oracle, battle core,
   and state/UI proceed in parallel within their owned files. Each agent runs
   the smallest meaningful check for its logic and reports completion.
3. **Integration** — orchestrator joins extracted data, core, and UI; runs
   differential fixtures and app checks; fixes interface mismatches. Agents
   receive targeted follow-ups only in their owned files.
4. **Release gate** — enable an exact card only for states covered by source
   fields, supported mechanics, complete inputs, and game-backed checks. Show
   all remaining cards as unavailable with actionable reasons. Record the
   supported subset and measured latency in the final handoff.

## Interface contract and data flow

Use one versioned, serializable `BattleSnapshot` built from the current Calc
selection. It includes source SHA; trainer ID and resolved party variant; Easy
or Normal; progression milestone and relevant flags; simulated player and enemy
builds; active party indexes; each member's current/max HP and move PP; held
item enablement; statuses and stat stages; weather, terrain, hazards, screens,
trick room and other effects when used; remaining trainer items; observed turn
log; and planned player action. Record `unknown` distinctly from zero, empty,
or false. The log must preserve ordered observed moves, switches, item use,
hits, misses, damage, and other RNG-relevant outcomes that can constrain seeds.
If an event cannot be represented exactly, do not claim the posterior is exact.

Provide a pure battle entry point conceptually equivalent to:

`evaluate_head_to_head(snapshot, game_data, deadline) -> HeadToHeadResult`

Return separate `player_moves`, `enemy_moves`, `ai_actions`, and
`recommendation` results. Each is either `exact` with an exact rational
probability/score and source SHA, or `unavailable` with machine-readable reason
and readable missing-input/mechanic detail. Convert fractions to rounded
percentages only in the UI. Key cached results by normalized snapshot, source
SHA, and engine version. A deadline or cancellation affects only unfinished
results. A newer edit invalidates the old result so it can never render under
the new battle state.

The recommendation evaluates every legal first-turn player move and party
switch against the ROM AI. After each observable first-turn outcome, it chooses
the best legal second-turn move or switch based on that observed state, never a
hidden RNG seed. Stop early at a ROM terminal win/loss. At the end of the
second turn, score the state as:

`sum(player 100*HP/maxHP) - sum(enemy 100*HP/maxHP) + 100*(living player count - living enemy count)`

Maximize the exact expected score. This adds one full HP bar of value for each
surviving Pokémon. Status, boosts, and hazards contribute when they change a
ROM-resolved outcome within the two turns; do not invent extra terminal
weights. Show all tied top actions and a short score breakdown. If the
two-turn search is incomplete, the recommendation is unavailable even when
current-turn damage or AI cards are exact.

Use ROM arithmetic and RNG call order, not generic Pokémon formulas or the
current `calculate_stats` helper's missing-IV/EV-as-zero behavior. Extend the
existing snapshot only with source-proven fields needed by the supported
mechanics. No LLM, new API service, ORM, or runtime ROM dependency.

## Phase deliverables and verification gates

### A. Source data and oracle (`rom_source`)

- Expand the pinned extractor with move category, effect, priority, accuracy,
  targeting, secondary effects, relevant ability/item parameters, AI flags,
  trainer inventory, cap milestones, and conditional party inputs needed by the
  first supported states. Resolve active preprocessor branches; reject
  ambiguous values instead of guessing. Bump the generated data schema.
- Obtain game outputs from the pinned source using a development-only
  instrumented build/harness or emulator. Record the source SHA, battle-state
  inputs, RNG seed/state, and expected damage/action output in compact fixtures.
  If the local build toolchain is unavailable, establish it outside the app or
  report the concrete blocker. Do not copy a ROM or build products into Sidedex.
- Start with an ordinary trainer and a smart-switching trainer, then add
  Easy/Normal scaling, held item, status, AI tie, and trainer-item fixtures.
  Include Sawyer as an active-table regression: the inactive `.party` syntax
  must not supply the team.

### B. Exact battle core (`battle_core`)

- Port only the source paths reachable by declared supported singles cases:
  stats, battle damage/rounding, move resolution, action priority, seeded SFC32
  RNG, AI scoring and tie breaks, smart switching, and trainer-item selection.
  Register unsupported moves/effects/state features and fail closed before
  producing a number.
- Reproduce fixed-seed fixture outputs exactly. For unknown seed, aggregate
  only if the set of consistent seeds/outcomes can be covered exhaustively
  within the two-second budget. Use integer counts or `Fraction`; no sampling,
  independent-draw substitution, or floating-point probability accumulation.
- Expose legal player moves and switches, conditional current-turn KO
  probabilities, AI action probabilities, and the two-turn expected-score
  evaluator through the agreed result contract. Keep each result independently
  gated so an unsupported status move need not suppress a verified simple
  damage result unless it can affect that result.

### C. Calc state and presentation (`calc_ui`)

- Replace the four `Not calculated` result bodies in `planner_ui.py`. Show
  exact percentage, outcome context, source revision, and specific unavailable
  reasons. The AI card lists all nonzero move, switch-destination, and item
  probabilities and highlights all top ties. The recommendation card displays
  its two-turn label, chosen move or switch target, and breakdown.
- Add active selectors, planned action, milestone/difficulty, and a compact
  current-state/turn-log editor. Keep focus selections independent. Include
  only values needed by the supported state, with an expandable advanced area
  for field effects and history. Prefill verified opening values; unknowns
  remain visibly unknown.
- Version and validate named Calc configs. Persist the new battle state and
  history. Load old configs without inventing absent HP/IV/EV/history; require
  correction of Hard difficulty. Apply `Use Item` toggles to calculation rather
  than display only.
- Use a cached background calculation keyed by the normalized state. Debounce
  edits enough to avoid launching work for each keystroke, enforce the shared
  two-second deadline, cancel or ignore stale work, and keep the main UI usable.

### D. Orchestrator integration and acceptance

- Run existing `python app.py --self-check` and unit tests, then add focused
  cross-module tests for source extraction, exact damage, AI moves/ties,
  switching, items, KO conditioning, missing inputs, timeout, and config load.
  Test a smart-switching position in which a player switch outranks every move.
- Differentially compare every enabled mechanic with the ROM oracle, including
  fixed RNG seeds and action order. Verify probabilities sum to one over all
  legal AI actions for a fully supported state. Verify no percentage or
  recommendation appears after an edit until it belongs to the new state.
- Measure automatic recalculation on representative opening and midbattle
  singles states. Finish only when at least one complete, useful head-to-head
  state fills all four cards in under two seconds; otherwise report the
  concrete feasibility blocker and leave unverified cards unavailable.
- Update the README with the supported subset, required inputs, meaning of
  percentages and two-turn score, source SHA, and unavailable behavior. The
  full-battle button remains unimplemented in this delivery.

## Stop conditions and handoff

Do not turn a partial port, guessed default, generic damage formula, sampled
RNG result, or type-coverage suggestion into an “exact” battle result. If the
oracle cannot be established, or if exact unknown-seed aggregation cannot
meet the two-second gate for even one complete representative state, stop that
path with evidence and report what remains unavailable. Ship independently
verified pieces only when their displayed meaning is still correct. The next
agent's final report should list changed files, commands/tests run, supported
mechanics, measured latency, and every material unsupported case.
