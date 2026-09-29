# Sidedex Calc handoff — 2026-09-26

Uncommitted working tree. Do not commit unless asked. No PR was opened.

The original mission: the compiled-engine Calc path worked for small trainers (Haley, grunts) but every card timed out against smart-AI bosses (Matt). The plan was Phases 0–5. **Phase 0 is done and verified. Phases 1–5 are not.**

---

## Current product state

`streamlit run app.py` should show numbers without setting `SIDEDEX_ENGINE_ELF_PATH`.

- Default ELF: `native/rom_worker/build/mingw/pokeemerald-test-headless.elf`
  SHA-256 `639059D550AB0FE3FC616F9277F7B170053D685CBD9FA11D692C6A8FAA77A6F7`
  (copied from WSL `/home/abj1/rom-oracle-checkout/pokeemerald-test-headless.elf`; gitignored)
- Default worker: `native/rom_worker/build/mingw/rom_worker.exe`
  version `sidedex-mgba-worker/0.1.3`
  SHA-256 `1D2431A439C24577196E8F3ABA69024A85115855C792A5372BF89698CFDA7E9D`
- Wrong-hash leftover in the same folder: `pokeemerald-test-dynamic.elf` — do not use it.

Measured live (known seed, one Pikachu vs trainer, 10 s deadline):

| Trainer | Before | After |
| --- | --- | --- |
| Haley | 2.7–4.8 s (two-turn) | ~0.48 s, four cards `engine-fixed-seed` |
| Matt Seafloor Cavern | first 3-turn batch hit the 5 s worker timeout | **0.86 s**, four cards `engine-fixed-seed` |

Matt recommendation on that seed ranked Growl above Thunder Shock / Quick Attack because the latter two get the player KO’d.

---

## What shipped (Phase 0)

### 1. Default ELF + named setup

- `rom_eval._engine_session` uses the mingw ELF path when the env var is unset.
- Worker timeout is remaining deadline, not capped at 5 s.
- `app.py --self-check` still prints `party cap check passed`, plus `engine artifact ok` / `engine artifact missing: …` with the SHA and `build.ps1`.
- Streamlit `main()` shows that diagnostic. `calc_service.py` missing-artifact detail names the same setup step.

### 2. Drop two-turn lookahead

- Recommendation is **one-turn** (`scored_turns: 1`, label `one-turn lookahead`).
- Calc does not send `second_action`. The mailbox field may still exist in the hook.
- Setup moves (Dragon Dance, Swords Dance) will essentially never be recommended. README, CALC_CONTRACT v3, and the recommendation card say so.
- **Leave the legacy Python model in `battle_engine.py` / `test_battle_engine.py` / `test_calc_integration.py` as two-turn.** That is v1, not the compiled path.

### 3. Partial cards + sample floor

- One batch timeout does not blank cards that already have complete trajectories.
- Unknown RNG requires `MIN_ESTIMATE_SAMPLES = 3` before `estimated`. Below that: `insufficient-samples`, never a zero-width `[250, 250]` interval.
- Unknown-RNG requests at least 3 samples even if the time budget formula would pick 1.
- Timeout with some complete turns but fewer than 3 samples → `insufficient-samples`. Timeout with zero samples → `timeout`.

### 4. Edited start + assumed lead

- `state_origin: "manual-scenario"` is accepted when field/stat-stages/history still reconstruct as a start-of-battle (HP/status/PP edits). Result meta: `scenario: "edited-start"`.
- Focused Pokémon on each side is permuted to slot 0 for the mailbox (`assumed_lead: true`). Switch slots in `planned_action` and `history.turns` are remapped. Switch labels still use the **original** party index.
- UI Battle-state active selectboxes follow focus. Widget session keys are only pushed when the focused pair changes (overwriting them every rerun swallowed selectbox clicks).

### 5. Matt timeout — completion-hook bug, not slow AI

Probe: `work/probe_matt_timeout.py` (results in `work/probe_matt_timeout_results.json` before, `work/probe_matt_timeout_after.json` after).

A first-turn player KO calls `AfterLastTurn` while `battleTurnCounter` is still 0, then the test runner emits `ROMTRACE|ready` instead of `ROMTRACE|complete`. The worker used to spin to `max_frames=24000` (~5–7.5 s). Smart AI / Mega Machamp only made lethal first turns common.

Fixes (both kept):

1. **Current ELF (hash unchanged 6390…):** `worker.c` 0.1.3 sets complete when it sees `ROMTRACE|ready` after `ROMTRACE|phase|*|after-turn`. Rebuild via `native/rom_worker/build.ps1`; pin is `rom_bridge.WORKER_SHA256`.
2. **Parser:** `rom_eval._parse_turn` accepts that terminal snapshot (actions + party/active/rng at turn 0, then ready).
3. **Patch (not yet in the installed ELF):** `dynamic_request.patch` `AfterLastTurn` also completes when `replayCount == 0 && battleTurnCounter == 0`. Needs an ELF rebuild to take effect. Worker workaround makes that rebuild optional for bosses.

Live regression: `PinnedEngineEvaluation.test_matt_first_turn_ko_fills_all_four_cards` in `test_rom_eval.py`.

---

## Files changed (repo)

```
CALC_CONTRACT.md
README.md
app.py
calc_service.py
native/rom_worker/CMakeLists.txt   # 0.1.2 → 0.1.3
native/rom_worker/worker.c
planner_ui.py
rom_bridge.py                      # WORKER_VERSION + WORKER_SHA256
rom_eval.py
test_calc_ui.py
test_rom_bridge.py
test_rom_eval.py
test_rom_worker_smoke.py
tools/rom_oracle/README.md
tools/rom_oracle/dynamic_request.patch
```

Untracked (gitignore `native/rom_worker/build/` already covers ELF/exe):

```
work/probe_matt_timeout.py
work/probe_matt_timeout_results.json
work/probe_matt_timeout_after.json
work/apply_phase1_query.py          # incomplete Phase 1 helper; see below
handoff.md
```

`work/probe_rng.py` and `work/dynamic_trace.json` were already there.

---

## Tests run

- `python -m pytest test_calc_ui.py -q` → 39 passed
- `python -m pytest test_rom_eval.py test_rom_bridge.py -q` → 28 passed, 2 skipped (live skips in bridge)
- Live Matt `rom_eval.evaluate` → four cards, 0.86 s
- Probe A/B/D/F after worker fix: all `outcome: complete`, ~0.28–0.81 s
- `python app.py --self-check` should still print `party cap check passed`

Full suite and a Streamlit walkthrough were **not** re-run at wrap-up.

---

## Dirty WSL checkout — read this before rebuilding the ELF

Phase 1 was started then **stopped**. It did **not** update `tools/rom_oracle/dynamic_request.patch` or any Python in the repo.

It **did** mutate `/home/abj1/rom-oracle-checkout/` in place via `work/apply_phase1_query.py`:

- `include/test_runner.h`
- `src/battle_main.c`
- `test/battle/ai/sidedex_dynamic.c`
- `test/test_runner.c`
- `test/test_runner_battle.c`

ELF rebuild was **not** started. The installed Windows ELF is still the Phase 0 pin (`6390…`).

Treat that WSL tree as dirty. Either:

- `git -C /home/abj1/rom-oracle-checkout checkout -- .` and re-apply `tools/rom_oracle/dynamic_request.patch` from this repo (includes the KO `complete` fix), or
- continue from those query-mode edits, then regenerate the repo patch from `git diff` so Sidedex stays the source of truth.

Do not copy a new ELF to mingw until its SHA is written into `rom_bridge.ENGINE_ELF_SHA256` and `tools/rom_oracle/pinned_source.json`.

---

## Incomplete Phase 1 sketch (in WSL + `work/apply_phase1_query.py`)

Intent: `secondPresent == 2` means query-only. At opponent choice, `TestRunner_Battle_RunQuery` runs `CalculateMoveDamage` for each side’s moves (16 rolls, crit and no-crit), tags Focus Sash / Sturdy, emits `ROMTRACE|query|damage|…`, runs `AI_TrySwitchOrUseItem` without resolving the turn, emits `ROMTRACE|query|ai|…`, then `ROMTRACE|complete|0`.

Known gaps in that sketch:

- Not folded back into `dynamic_request.patch`.
- KO numerator/denominator math is unfinished (accuracy × 16 rolls × crit odds; hit/miss not enumerated via `RNG_ACCURACY`; crit forcing via `RNG_CRITICAL_HIT` is not wired — only `RNG_DAMAGE_MODIFIER` is overridden).
- AI prediction is a single forced choice, not an enumeration of AI random tags.
- Query only fires when the opponent battler is choosing (`GetBattlerSide != B_SIDE_OPPONENT` returns false).
- Python `rom_eval` / `run_dynamic` know nothing about query records yet.
- Recommendation is still full-turn sampling (correct; Phase 1 should not block it).

---

## Remaining plan (as originally scoped)

**Phase 1 — Instant exact tier, target &lt; 0.5 s**

Query mode from the saved pre-choice checkpoint: exact single-hit KO % (16 damage rolls, crit/no-crit, hit/miss, `RNG_DAMAGE_MODIFIER` / `RNG_CRITICAL_HIT` / `RNG_ACCURACY`), Sash/Sturdy as full-HP checks, multi-hit called out. AI choice at the same state without resolving the turn.

**Phase 2 — One-turn recommendation ~3 s, refine to 10 s**

Dropping two-turn is **done**. Still needed: restore the pre-choice savestate, poke both RNG streams in memory, reuse the same seeds across candidates. Full-turn samples also refine the enemy KO card conditioned on the planned move and cross-check Phase 1.

**Phase 3 — Speed**

Long-lived worker pool (pre-warm at app start, 3–4 processes), checkpoint cache keyed by battle-start state, disable video/audio, slim traces (actions/HP/status/faint only), stream results (KO % + AI first, then recommendation n grows until intervals separate or 10 s), disk cache of the two leads, measure boot / checkpoint / query / turn / per-seed before picking process count.

**Phase 4 — Display**

KO % on each move in both focused panels (badge: exact damage range vs sampled n=…). Predicted trainer action on the enemy panel. Highlight the recommended player action; mark uncertain when intervals overlap. Progress while cards refine.

**Phase 5 — Verification**

~20 trainers including smart-AI bosses and Megas; each fills every card or names a reason, inside the latency targets. Phase 1 exact KO % must fall inside Phase 2 sampled intervals across many seeds. Then full suite, `app.py --self-check`, one manual walkthrough.

Matt’s remaining risk for Phase 2 sampling is **lethality**, not frame cost: lethal turns are ~1600 frames / ~270 ms. A 3–4 process pool should keep one-turn sampling near 3 s. Query mode is what makes the UI feel instant.

---

## How to resume

```powershell
cd C:\Users\Abj1\Documents\GitHub\Sidedex
python -m pytest test_rom_eval.py test_calc_ui.py test_rom_bridge.py -q
python -c "import time, rom_eval, test_rom_eval as t; s=t.snapshot_v2('TRAINER_MATT_SEAFLOOR_CAVERN', rng={'main': t.KNOWN_MAIN, 'secondary': t.KNOWN_SECONDARY}); t0=time.monotonic(); r=rom_eval.evaluate(s, t.DATA, deadline=t0+10); print(round(time.monotonic()-t0,3), r['status'], {k:r[k]['status'] for k in rom_eval._CARDS})"
python work\probe_matt_timeout.py --case A --case B --timeout 15
python app.py --self-check
```

Then `streamlit run app.py`, pick Matt, known RNG optional. Cards should fill, not time out.

Rebuild worker after `worker.c` edits: `./native/rom_worker/build.ps1` then `Get-FileHash native\rom_worker\build\mingw\rom_worker.exe -Algorithm SHA256` into `rom_bridge.WORKER_SHA256`.

ELF rebuild recipe: `tools/rom_oracle/pinned_source.json` → `dynamic_oracle.recipe`. Source pin `2576d0f3f4c861c760f0c55198e37e6d7b91683e`.

---

## Pointers

- Evaluator: `rom_eval.py` (`evaluate`, `_parse_turn`, `_prepare_snapshot`, `_engine_session`)
- Worker complete detection: `native/rom_worker/worker.c` `capture_log`
- Hook complete: `tools/rom_oracle/dynamic_request.patch` `TestRunner_Battle_AfterLastTurn`
- UI focus sync: `planner_ui.py` `calc_state_for_focus` / `calc_focus_for_active_selection` / Battle-state selectboxes
- Contract: `CALC_CONTRACT.md` v3 (one-turn, assumed lead, edited-start, 3-sample floor)
- Do not call codegraph on this repo; it is not indexed.
