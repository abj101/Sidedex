# Sidedex

The implementation plan for ROM-backed predictions and move recommendations is
in [`CALC_IMPLEMENTATION_PLAN.md`](CALC_IMPLEMENTATION_PLAN.md). The battle
state editor is present, but the four Calc result cards remain unavailable for
schema-v2 requests until complete ROM turn replay is verified. The older Python
model is retained for legacy tests and is not presented as a ROM result.

Local Emerald Imperium Box and type-coverage planner with Pokedex and Calc tabs.

```powershell
python -m pip install -r requirements.txt
python app.py --self-check
streamlit run app.py
```

Browse trainers by gym or mapped location, inspect their teams and moves, and
manage your party and 14 visual PC Boxes on the same page. Each Box holds 30
Pokémon; use the game arrows to switch boxes and the Box field to move a Pokémon.
Party Pokémon use the
same card style as trainer teams, with a player trainer card for your profile.
Select a Pokémon to see its details in a full-width panel with stats on the left
and moves on the right. Enemy team cards show whether the selected Pokémon has
an advantage, the matchup is even, or the enemy has the advantage; unknown data
gets a question mark. The separate matchup view is gone.
Use **Add Pokémon** to create a record. Owned Pokémon are stored locally in
`sidedex.sqlite3`.

Trainer and matchup data comes from `data/game_data.json`. Location references
used by the trainer browser are in `data/trainer_locations.json`. Icon assets
are bundled locally; see `assets/README.md` for provenance and redistribution
notes.

The checked snapshot is generated from the pinned Emerald Imperium commit in
`data/source_lock.json`. To regenerate it from a local checkout at that exact
commit, run `python tools/extract_game_data.py PATH_TO_CHECKOUT`. The extractor
rejects other revisions and checks representative trainers and type data. It
resolves omitted trainer moves from the level-up learnset at battle level,
uses the pinned game's default nature and first ability, and reads trainer
portrait IDs, IVs, and EVs from the source tables. An omitted trainer IV/EV is
not unknown: the ROM applies the table's packed IV word unconditionally and
leaves EVs at zero, so the snapshot records those resolved values.
The species picker excludes source marker IDs and Shadow Lugia, whose numeric
ID has no species data initializer in the pinned build. Trainer levels encoded
as `200+n` are resolved from the highest simulated party level for the selected
progression milestone (see the level-cap note below); affected trainers carry
that rule in `context_required`.

Enemy advantage badges use extracted move power and type effectiveness only.
Unknown builds or types are marked unknown. Soul Link constraints are not
supported. The upstream fork has no
root license file at the pinned commit; confirm its terms before redistributing
the generated snapshot.

The **Calc** tab shows ordered player and trainer teams around two focused
Pokémon panels. Click a compact card to focus it; use its change icon to choose
another Pokémon. Player slot changes update the actual party,
while trainer slot changes only reorder that trainer's display. The Box retains
its hover preview, but clicking a Box Pokémon does not change Calc focus.
Named configs save the party lineup, trainer, focus choices, display order,
item switches, level cap milestone, difficulty, explicit battle actives,
entered battle state, and observed turn log. Focus changes do not change
actives. Current HP and both RNG streams stay unknown until entered; the
explicit start-at-full-HP action initializes a battle-start scenario. Older
configs load without inventing missing HP, IVs, EVs, or RNG. Hard difficulty
configs require an explicit Easy/Normal correction.

## Calc head-to-head results

The current UI sends schema-v2 snapshots to a separate ROM service path. It
shows specific unavailable reasons while the battle hook and full-turn oracle
comparison are unfinished. A built ROM or a running emulator alone does not
establish prediction accuracy. Unknown-seed results will be labelled
**model-based estimates** with sample count and uncertainty only after the ROM
worker supports replay and sampling. See `CALC_CONTRACT.md` for the result
contract and the implementation plan for the remaining gates.

### Legacy Python model

Legacy tests compute the four result cards from a narrow Python model
transcribed from the pinned ROM source in `data/source_lock.json` (commit
`2576d0f3f4c861c760f0c55198e37e6d7b91683e`). Returned fractions are
exact for that model, but the model has not been checked against a running ROM.
Nothing is sampled in that model. The descriptions below document the legacy
calculation and its limits; they are not claims about current v2 output.

- **Your moves · KO probability / Enemy moves · KO probability** give the
  probability that the move knocks out the Pokémon that was active at the start
  of the turn, conditional on that move being selected (and, for the enemy
  card, on the player action you planned). Move failure, accuracy, critical
  hits, damage rolls and multi-hit counts are included. A move is gated on its
  own: an unsupported move is listed as unavailable with its reason while its
  siblings still show exact odds.
- **AI predicted action** is the distribution over the enemy trainer's legal
  actions (moves, switch destinations, bag items) for the entered battle state,
  summing to exactly 1. It is not conditioned on your planned move, because the
  ROM AI predicts from prior battle history.
- **Recommended action** is a **two-turn lookahead** over every legal first-turn;
  after each observable outcome it picks the best legal second-turn action,
  scoring `sum(100*HP/maxHP)` over your team minus the enemy's, plus 100 for
each surviving Pokémon you have more than the enemy. Ties are all listed with
their score breakdown. It is not a claim of whole-battle optimal play.

Percentages are `round(100 * num / den)` of an exact rational. Each result says
how it was obtained:

- **known-seed** — simulated from a saved four-word SFC32 RNG state;
- **seed-independent** — proven identical for every possible RNG state (for
example a minimum damage roll already KOs);
- otherwise the card is **Unavailable** with a machine-readable reason such as
  `unknown-rng-seed`, `missing-input`, `unsupported-move`, `unsupported-ability`,
  `unsupported-item`, `unsupported-mechanic`, `unsupported-difficulty`,
  `conditional-party`, `unrepresentable-history`, `no-legal-actions`,
  `incomplete-search`, `timeout`, `source-mismatch` or `engine-error`, plus the
  missing input paths or the specific unsupported id.

### Level cap and difficulty

The free level-cap number is replaced by the ROM progression milestones from
`src/caps.c`. Selecting a milestone simulates the ROM's raise-to-cap routine
(`LevelBoxesToCap`) on a copy of the party, assuming every member is unmarked;
saved Box levels are never altered. The highest simulated party level is what
resolves the ROM's `200+n` scaled trainer levels. Only Easy and Normal are
offered, because the pinned ROM has no Hard mode; a config saved as Hard must be
corrected explicitly before it can produce exact results.

### Required inputs

Unknown is never treated as zero. A card is exact only when every value it
actually reads is known: level, IVs, EVs, nature, ability, held item (or
`Use Item` off for no item), current and maximum HP, PP, status and stat stages
for the active pair, the active selections, the planned player action, the
progression milestone (from `src/caps.c`) and Easy/Normal difficulty. The
optional four-word RNG state is what turns roll-dependent results exact for the
model. Nonempty saved turn logs currently make the results unavailable because
the engine does not implement seed conditioning from history. Bench HP/status
fields are needed only when a switch is actually reachable. For pinned ROM data,
an entered maximum HP that differs from the computed stat is rejected.

### Supported subset and verification

The legacy model's supported slice is singles with one active Pokémon per side,
physical/special damaging moves with a known effect and no recoil, secondary
effects, status moves or held items, abilities proven inert for the computed
paths, and the ordinary `AI_FLAG_CHECK_BAD_MOVE` trainer logic plus trainers
with no AI flags. Reachable AI switching remains unsupported.

One valid Haley/Rookidee fixture fills all four cards in 0.42 ms on this
machine. This is a model check, not a ROM oracle or broad performance benchmark.

The pinned trainer table carries no bag items (every entry is `ITEM_NONE`), so
the ported trainer-item path cannot be reached with the shipped data.

The pinned source has a battle test suite. A disposable full checkout now
builds its ROM under WSL; complete-turn replay and comparison are still in
progress. The legacy Python output is labelled *source-transcribed, not
emulator-verified*.
The arithmetic is transcribed from cited `src/...:line` locations, hand-derived
expectations live in `data/rom_fixtures.json` with their provenance, and
`test_battle_engine.py`, `test_rom_source.py`, `test_calc_ui.py` and
`test_calc_integration.py` check the ports against those fixtures. Treat the
numbers as exact for the implemented model, not as emulator-verified.

The whole-battle calculation button, whole-battle win probability, doubles and
player bag actions remain unimplemented and are not claimed.
