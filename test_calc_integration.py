"""Cross-module integration checks for the Calc head-to-head pipeline.

Owned by the orchestrator. Exercises planner_ui's snapshot builder, battle_engine
and calc_service against the generated data/game_data.json.
"""

import io
import json
import time
import unittest
from fractions import Fraction
from pathlib import Path

import battle_engine as be
import calc_service
import planner_ui

ROOT = Path(__file__).parent
DATA = json.loads((ROOT / "data" / "game_data.json").read_text(encoding="utf-8"))
STAT_KEYS = ("hp", "attack", "defense", "sp_attack", "sp_defense", "speed")
BUDGET = 2.0


def trainer(trainer_id):
    for record in DATA["trainers"]:
        if record["id"] == trainer_id:
            return record
    raise AssertionError(f"trainer {trainer_id} missing from game_data")


def player_row(row_id=1, species="SPECIES_PIKACHU", level=50,
               moves=("MOVE_QUICK_ATTACK", "MOVE_TACKLE")):
    return {"id": row_id, "species": species, "level": level, "ability": "ABILITY_NONE",
            "item": "ITEM_NONE", "nature": "NATURE_HARDY",
            "ivs": json.dumps({key: 31 for key in STAT_KEYS}),
            "evs": json.dumps({key: 0 for key in STAT_KEYS}),
            "moves": json.dumps(list(moves))}


def rom_max_hp(species, level, nature, ivs, evs):
    return be.calc_mon_stats(DATA["species"][species]["stats"], level, nature, ivs, evs)["hp"]


def snapshot(trainer_id, enemy_index=0, *, hp=120, enemy_hp=40, rng=(0x12345678, 0x9ABCDEF0, 0x0BADF00D, 1),
             ai_flags=(), move="MOVE_QUICK_ATTACK", trainer_items=None):
    """Build a BattleSnapshot through the UI builder, not by hand."""
    record = trainer(trainer_id)
    party = record["party"]
    player_max = rom_max_hp("SPECIES_PIKACHU", 100, "NATURE_HARDY",
                            {key: 31 for key in STAT_KEYS}, {key: 0 for key in STAT_KEYS})
    enemy_max = rom_max_hp(party[enemy_index]["species"], party[enemy_index]["level"],
                           party[enemy_index]["nature"], party[enemy_index]["ivs"],
                           party[enemy_index]["evs"])
    state = {
        "player_active_slot": 0,
        "enemy_active_index": enemy_index,
        "progress_flags": {},
        "rng": list(rng) if rng else None,
        "planned_action": {"kind": "move", "move_id": move, "target_index": enemy_index},
        "field": {},
        "history": {"turns": []},
        "player": {"1": {"hp": min(hp, player_max), "max_hp": player_max, "status": "none",
                         "pp": {mid: 20 for mid in ("MOVE_QUICK_ATTACK", "MOVE_TACKLE")}}},
        "enemy": {str(index): {"hp": min(enemy_hp, enemy_max) if index == enemy_index else None,
                               "max_hp": enemy_max if index == enemy_index else None,
                               "status": "none" if index == enemy_index else None, "pp": {}}
                  for index in range(len(party))},
    }
    return planner_ui.build_calc_snapshot(
        DATA, [player_row()], party, state, trainer_id=trainer_id, difficulty="Normal",
        level_cap_id="CAP_MAX", enemy_items=trainer_items,
        ai_flags=record.get("ai_flags") or list(ai_flags),
        enemy_levels=[member.get("level") for member in party])


def evaluate(snap, deadline=BUDGET):
    started = time.monotonic()
    result = be.evaluate_head_to_head(snap, DATA, deadline=time.monotonic() + deadline)
    return result, time.monotonic() - started


def exact(card):
    assert card["status"] == "exact", card
    return card


def unavailable(card, reason=None):
    assert card["status"] == "unavailable", card
    if reason is not None:
        assert card["reason"] == reason, card
    return card


def fraction(value):
    return Fraction(int(value["num"]), int(value["den"]))


def fraction_from_text(text):
    """'3590/9' or '398' -> Fraction."""
    text = str(text).strip()
    if "/" in text:
        num, den = text.split("/")
        return Fraction(int(num), int(den))
    return Fraction(int(text), 1)


class GameDataIntegration(unittest.TestCase):
    def test_generated_data_matches_the_contract(self):
        self.assertEqual(DATA["schema_version"], 2)
        self.assertEqual(DATA["source"]["commit"], be.SOURCE_SHA)
        self.assertTrue(DATA["level_caps"])
        self.assertIsInstance(DATA["oracle"]["available"], bool)
        move = DATA["moves"]["MOVE_QUICK_ATTACK"]
        for field in ("category", "effect", "priority", "target", "secondary_effects",
                      "flags", "always_critical", "crit_ratio", "strike_count", "recoil"):
            self.assertIn(field, move)
        # Sawyer regression from the extractor must survive regeneration.
        sawyer = trainer("TRAINER_SAWYER_1")["party"][0]
        self.assertEqual(sawyer["species"], "SPECIES_GRAVELER_ALOLA")
        self.assertEqual(sawyer["level"], 40)

    def test_trainer_bag_items_are_absent_in_the_shipped_table(self):
        # The ported AI item path (ShouldUseItem) is unreachable with this data:
        # every trainer entry in the pinned table uses ITEM_NONE.
        self.assertTrue(all(item == "ITEM_NONE"
                            for record in DATA["trainers"]
                            for item in (record.get("items") or [])))

    def test_trainer_ivs_are_source_resolved_never_rolled(self):
        for member in trainer("TRAINER_LAO_1")["party"]:
            self.assertEqual(sorted(member["ivs"]), sorted(STAT_KEYS))
            self.assertEqual(sorted(member["evs"]), sorted(STAT_KEYS))


class SnapshotIntegration(unittest.TestCase):
    def test_bench_only_unknowns_do_not_blank_the_cards(self):
        """The enemy bench has no HP/status entered; the four cards must still work."""
        snap = snapshot("TRAINER_HALEY_1", enemy_index=2)
        active = snap["enemy"]["active_index"]
        for index, member in enumerate(snap["enemy"]["party"]):
            if index == active:
                continue
            self.assertIsNone(member["hp"])
            self.assertEqual(member["status"], "none")
        result, _ = evaluate(snap)
        for kind in ("player_moves", "enemy_moves", "ai_actions"):
            self.assertEqual(result[kind]["status"], "exact", result[kind])

    def test_ui_snapshot_validates_through_the_engine(self):
        snap = snapshot("TRAINER_HALEY_1")
        self.assertEqual(be.normalize_snapshot(snap), snap)
        self.assertEqual(snap["source_sha"], be.SOURCE_SHA)
        self.assertEqual(len(snap["player"]["party"]), 1)
        self.assertEqual(len(snap["enemy"]["party"]), 3)

    def test_unknown_values_stay_none(self):
        record = trainer("TRAINER_HALEY_1")
        state = {"player_active_slot": 0, "enemy_active_index": 0, "planned_action": None,
                 "field": {}, "history": {"turns": []}, "player": {"1": {}}, "enemy": {}}
        snap = planner_ui.build_calc_snapshot(
            DATA, [player_row()], record["party"], state, trainer_id="TRAINER_HALEY_1",
            difficulty="Normal", level_cap_id="CAP_MAX",
            ai_flags=record.get("ai_flags"), enemy_levels=[m.get("level") for m in record["party"]])
        self.assertIsNone(snap["player"]["party"][0]["hp"])
        self.assertIsNone(snap["player"]["party"][0]["max_hp"])
        self.assertIsNone(snap["rng"]["state"])


class RomFixtureDifferential(unittest.TestCase):
    """Drive battle_engine with the extractor's hand-derived ROM fixtures.

    rom_source transcribed the expectations from src/... by hand, battle_core
    transcribed the code; agreement here is independent double-entry evidence,
    not emulator verification.
    """

    @classmethod
    def setUpClass(cls):
        cls.fixtures = json.loads(
            (ROOT / "data" / "rom_fixtures.json").read_text(encoding="utf-8"))["fixtures"]
        cls.by_kind = {}
        for fixture in cls.fixtures:
            cls.by_kind.setdefault(fixture["kind"], []).append(fixture)

    @staticmethod
    def _rng_with_random16(value):
        """A state whose next Random() (u16) is exactly `value`."""
        return be.Rng([value << 16, 0, 0, 0])

    def test_sfc32_fixtures_reproduce(self):
        for fixture in self.by_kind.get("rng", []):
            inputs = fixture["inputs"]
            if "outputs" not in fixture["expected"] or inputs.get("stream", 1) != 1:
                continue  # the stream-29 variant is not part of the supported slice
            state = inputs["state"]
            rng = be.Rng([state["a"], state["b"], state["c"], state["ctr"]])
            got = [rng.next_u32() for _ in range(inputs["steps"])]
            self.assertEqual(got, fixture["expected"]["outputs"], fixture["id"])

    def test_uniform_and_accuracy_fixtures_reproduce(self):
        for fixture in self.by_kind.get("rng", []):
            if fixture["id"] != "rng-uniform-damage-roll":
                continue
            inputs = fixture["inputs"]
            rng = self._rng_with_random16(inputs["random"])
            self.assertEqual(be.random_uniform(rng, inputs["lo"], inputs["hi"]),
                             fixture["expected"]["value"], fixture["id"])
        for fixture in self.by_kind.get("accuracy", []):
            inputs = fixture["inputs"]
            rng = self._rng_with_random16(inputs["random"])
            self.assertEqual(be.random_percentage(rng, inputs["accuracy_percent"]),
                             fixture["expected"]["hits"], fixture["id"])

    def test_crit_chance_fixture_reproduces(self):
        for fixture in self.by_kind.get("rng", []):
            if fixture["id"] != "rng-crit-chance-stage0-hit":
                continue
            inputs = fixture["inputs"]
            rng = self._rng_with_random16(inputs["random"])
            self.assertEqual(be.random_chance(rng, inputs["successes"], inputs["total"]),
                             fixture["expected"]["is_critical_hit"], fixture["id"])

    def test_stats_fixtures_reproduce(self):
        for fixture in self.by_kind.get("stats", []):
            inputs = fixture["inputs"]
            stats = be.calc_mon_stats(inputs["base_stats"], inputs["level"],
                                      inputs["nature"], inputs["ivs"], inputs["evs"])
            self.assertEqual(stats, fixture["expected"], fixture["id"])

    def test_damage_fixtures_reproduce(self):
        """Burn is excluded: the engine rejects status instead of modelling it."""
        for fixture in self.by_kind.get("damage", []):
            inputs = fixture["inputs"]
            if inputs.get("burned_physical"):
                continue
            move_type = "TYPE_NORMAL"
            move = {"id": "MOVE_FIXTURE", "type": move_type, "power": inputs["power"],
                    "category": "physical"}
            attacker = {"level": inputs["level"], "types": ["TYPE_WATER"],
                        "stats": {"attack": inputs["attack"], "sp_attack": inputs["attack"]}}
            defender = {"types": ["TYPE_GRASS"],
                        "stats": {"defense": inputs["defense"], "sp_defense": inputs["defense"]}}
            if inputs["stab"] != 1.0:
                attacker["types"] = [move_type]
            effectiveness = inputs["type_effectiveness"]
            chart = {move_type: {"TYPE_GRASS": effectiveness}}
            ctx = {"chart": chart}
            self.assertEqual(be._base_damage(inputs["power"], inputs["attack"], inputs["level"],
                                            inputs["defense"]),
                             fixture["expected"]["base_damage"], fixture["id"])
            damage = be._hit_damage(move, attacker, defender, ctx, inputs["is_crit"],
                                    inputs["roll_index"])
            self.assertEqual(damage, fixture["expected"]["final_damage"], fixture["id"])


class ExactCardIntegration(unittest.TestCase):
    """One complete real head-to-head state must fill all four cards."""

    def setUp(self):
        self.snap = snapshot("TRAINER_HALEY_1", enemy_index=2)  # Rookidee: Wing Attack + Tackle
        self.result, self.elapsed = evaluate(self.snap)

    def test_all_four_cards_are_exact_inside_the_budget(self):
        for kind in ("player_moves", "enemy_moves", "ai_actions", "recommendation"):
            exact(self.result[kind])
        self.assertLess(self.elapsed, BUDGET)
        self.assertTrue(self.result["evidence"]["oracle_verified"]
                        or self.result["evidence"]["basis"] == "source-transcribed")

    def test_exact_fractions_are_in_range(self):
        for kind in ("player_moves", "enemy_moves"):
            for entry in self.result[kind]["moves"]:
                value = fraction(entry["probability"])
                self.assertLessEqual(0, value)
                self.assertLessEqual(value, 1)
                self.assertLessEqual(entry["probability"]["den"], 10 ** 12)

    def test_ai_actions_sum_to_exactly_one(self):
        actions = self.result["ai_actions"]["actions"]
        self.assertTrue(actions)
        self.assertEqual(sum(fraction(action["probability"]) for action in actions), 1)

    def test_recommendation_uses_the_two_turn_score(self):
        card = exact(self.result["recommendation"])
        self.assertEqual(card["label"], "two-turn lookahead")
        self.assertEqual(card["scored_turns"], 2)
        self.assertTrue(card["actions"])
        best = max(fraction(action["score"]) for action in card["actions"])
        for action in card["actions"]:
            self.assertEqual(fraction(action["score"]), best)  # only tied maxima are listed

    def test_repeat_of_the_same_state_is_stable(self):
        again, _ = evaluate(self.snap)
        self.assertEqual(again["player_moves"], self.result["player_moves"])
        self.assertEqual(again["recommendation"], self.result["recommendation"])


class FailClosedIntegration(unittest.TestCase):
    def test_guaranteed_ko_is_seed_independent(self):
        snap = snapshot("TRAINER_HALEY_1", enemy_index=2, enemy_hp=1, rng=None)
        card = evaluate(snap)[0]["player_moves"]
        entry = exact(card)["moves"][0]
        self.assertEqual(fraction(entry["probability"]), 1)
        self.assertEqual(entry["method"], "seed-independent")

    def test_no_exact_result_without_a_seed_unless_seed_independent(self):
        snap = snapshot("TRAINER_HALEY_1", enemy_index=2, rng=None, enemy_hp=35)
        result, _ = evaluate(snap)
        for kind in ("player_moves", "enemy_moves", "ai_actions", "recommendation"):
            card = result[kind]
            if card["status"] == "exact":
                method = card.get("method")
                if method is None:  # a card whose entries carry their own method
                    for entry in card.get("moves") or card.get("actions") or []:
                        self.assertEqual(entry.get("method"), "seed-independent", entry)
                else:
                    self.assertEqual(method, "seed-independent", card)

    def test_unsupported_move_is_named(self):
        snap = snapshot("TRAINER_HALEY_1", move="MOVE_THUNDERBOLT")
        snap["player"]["party"][0]["moves"] = ["MOVE_THUNDERBOLT"]
        result, _ = evaluate(snap)
        card = result["player_moves"]
        if card["status"] == "unavailable":
            self.assertEqual(card["reason"], "unsupported-move")
            self.assertTrue(any("THUNDERBOLT" in str(item) for item in card["mechanics"]))
        else:
            entries = {entry["move_id"]: entry for entry in card["moves"]}
            self.assertEqual(entries["MOVE_THUNDERBOLT"]["status"], "unavailable")

    def test_recoil_move_is_not_simulated_as_plain_damage(self):
        snap = snapshot("TRAINER_HALEY_1", move="MOVE_TAKE_DOWN")
        snap["player"]["party"][0]["moves"] = ["MOVE_TAKE_DOWN", "MOVE_QUICK_ATTACK"]
        result, _ = evaluate(snap)
        card = result["player_moves"]
        if card["status"] == "unavailable":
            self.assertEqual(card["reason"], "unsupported-move")
        else:
            entries = {entry["move_id"]: entry for entry in card["moves"]}
            self.assertNotEqual(entries["MOVE_TAKE_DOWN"].get("status", "exact"), "exact")

    def test_missing_hp_is_reported_by_path(self):
        snap = snapshot("TRAINER_HALEY_1")
        snap["player"]["party"][0]["hp"] = None
        card = evaluate(snap)[0]["player_moves"]
        self.assertIn("player.party[0].hp", unavailable(card).get("missing", []))

    def test_impossible_max_hp_fails_closed(self):
        snap = snapshot("TRAINER_HALEY_1")
        snap["player"]["party"][0]["max_hp"] -= 1
        card = evaluate(snap)[0]["player_moves"]
        self.assertEqual(unavailable(card, "unsupported-mechanic")["status"], "unavailable")
        self.assertTrue(any("max_hp" in item for item in card["mechanics"]))

    def test_wrong_source_sha_is_rejected(self):
        snap = snapshot("TRAINER_HALEY_1")
        snap["source_sha"] = "0" * 40
        result, _ = evaluate(snap)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["player_moves"]["reason"], "source-mismatch")

    def test_expired_deadline_never_invents_a_number(self):
        snap = snapshot("TRAINER_HALEY_1")
        result = be.evaluate_head_to_head(snap, DATA, deadline=time.monotonic() - 1)
        for kind in ("player_moves", "enemy_moves", "ai_actions", "recommendation"):
            card = result[kind]
            if card["status"] == "unavailable":
                self.assertIn(card["reason"], {"timeout", "incomplete-search"})
            else:
                self.assertEqual(card.get("status"), "exact")


class SwitchRecommendationIntegration(unittest.TestCase):
    """A position where switching out must outrank every move."""

    def test_player_switch_can_outrank_every_move(self):
        record = trainer("TRAINER_HALEY_1")
        party = record["party"]
        # Bonemerang is Ground, so it does 0x to the Flying Rookidee; Rock Throw is 2x.
        active = player_row(1, "SPECIES_CUBONE", 40, ("MOVE_BONEMERANG",))
        bench = player_row(2, "SPECIES_GRAVELER", 40, ("MOVE_ROCK_THROW",))
        state = {"player_active_slot": 0, "enemy_active_index": 2, "progress_flags": {},
                 "rng": [0x12345678, 0x9ABCDEF0, 0x0BADF00D, 1],
                 "planned_action": {"kind": "move", "move_id": "MOVE_BONEMERANG",
                                    "target_index": 2},
                 "field": {}, "history": {"turns": []},
                 "player": {"1": {"hp": 60, "max_hp": rom_max_hp("SPECIES_CUBONE", 100,
                     "NATURE_HARDY", {key: 31 for key in STAT_KEYS}, {key: 0 for key in STAT_KEYS}),
                                   "status": "none", "pp": {}},
                            "2": {"hp": 90, "max_hp": rom_max_hp("SPECIES_GRAVELER", 100,
                     "NATURE_HARDY", {key: 31 for key in STAT_KEYS}, {key: 0 for key in STAT_KEYS}),
                                  "status": "none", "pp": {}}},
                 "enemy": {"0": {}, "1": {},
                           "2": {"hp": 20, "max_hp": rom_max_hp(party[2]["species"],
                     party[2]["level"], party[2]["nature"], party[2]["ivs"], party[2]["evs"]),
                                 "status": "none", "pp": {}}}}
        snap = planner_ui.build_calc_snapshot(
            DATA, [active, bench], party, state, trainer_id="TRAINER_HALEY_1",
            difficulty="Normal", level_cap_id="CAP_MAX",
            ai_flags=record.get("ai_flags"),
            enemy_levels=[member.get("level") for member in party])
        self.assertTrue(snap["player"]["party"][0]["moves"],
                        "the UI builder dropped every move of the active Pokémon")
        card = be.evaluate_head_to_head(snap, DATA, deadline=time.monotonic() + BUDGET)["recommendation"]
        self.assertEqual(card["status"], "exact", card)
        best = max(fraction(action["score"]) for action in card["actions"])
        top = [action for action in card["actions"] if fraction(action["score"]) == best]
        self.assertTrue(any(action["kind"] == "switch" for action in top), card["actions"])
        self.assertTrue(all(action["kind"] == "switch" for action in top), card["actions"])
        # the immune move must still be an evaluated first-turn candidate, and lose
        scores = dict(entry.split(" -> ") for entry in card["breakdown"])
        self.assertIn("MOVE_BONEMERANG", scores)
        self.assertLess(fraction_from_text(scores["MOVE_BONEMERANG"]), best)


class CardRenderIntegration(unittest.TestCase):
    """The real renderer must show the engine's exact result, never a bare number."""

    def test_exact_card_renders_percentage_basis_and_kind(self):
        snap = snapshot("TRAINER_HALEY_1", enemy_index=2, enemy_hp=1, rng=None)
        result = be.evaluate_head_to_head(snap, DATA, deadline=time.monotonic() + BUDGET)
        markup = planner_ui.calc_result_card("Your moves · KO probability", "player_moves",
                                            card=result["player_moves"])
        self.assertIn("100%", markup)
        self.assertIn("1/1", markup)
        self.assertIn("source-transcribed", markup)
        self.assertIn("seed-independent", markup)
        self.assertNotIn("Not calculated", markup)

    def test_unavailable_card_renders_the_reason_and_missing_inputs(self):
        snap = snapshot("TRAINER_HALEY_1", enemy_index=2)
        snap["enemy"]["party"][2]["hp"] = None
        result = be.evaluate_head_to_head(snap, DATA, deadline=time.monotonic() + BUDGET)
        card = result["player_moves"]
        self.assertEqual(card["status"], "unavailable")
        markup = planner_ui.calc_result_card("Your moves · KO probability", "player_moves", card=card)
        self.assertIn("missing-input", markup)
        self.assertIn("enemy.party[2].hp", markup)
        self.assertNotIn("% (", markup)

    def test_percent_text_uses_the_exact_fraction(self):
        self.assertEqual(planner_ui.percent_text({"num": 1, "den": 3}), "33%")
        self.assertEqual(planner_ui.probability_text({"num": 2, "den": 4}), "50% (2/4)")
        self.assertEqual(planner_ui.percent_text(None), "Unknown")


class StaleRenderIntegration(unittest.TestCase):
    """A newer edit must never render the previous state's result."""

    def setUp(self):
        calc_service.invalidate()

    def tearDown(self):
        calc_service.invalidate()

    def test_edit_to_another_state_does_not_show_the_old_result(self):
        before = snapshot("TRAINER_HALEY_1", enemy_index=2, enemy_hp=20)
        after = snapshot("TRAINER_HALEY_1", enemy_index=2, enemy_hp=10)  # relevant edit
        self.assertNotEqual(calc_service.snapshot_key(before), calc_service.snapshot_key(after))
        for _ in range(200):
            if calc_service.poll(before):
                break
            planner_ui.calc_service_fetch(before, DATA)
            time.sleep(0.05)
        self.assertIsNotNone(calc_service.cached(before))
        fetched = planner_ui.calc_service_fetch(after, DATA)
        self.assertIsNone(fetched)          # pending: nothing from `before` may render
        for _ in range(200):
            fetched = fetched or planner_ui.calc_service_fetch(after, DATA)
            if fetched:
                break
            time.sleep(0.05)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched["source_sha"], be.SOURCE_SHA)
        self.assertIsNotNone(calc_service.cached(before))  # old key still cached, never rendered


class ServiceIntegration(unittest.TestCase):
    class _FakeEngine:
        ENGINE_VERSION = "fake"
        SOURCE_SHA = "fake"
        calls = []

        @staticmethod
        def normalize_snapshot(snapshot):
            return snapshot

        @classmethod
        def evaluate_head_to_head(cls, snapshot, game_data, deadline=None):
            cls.calls.append((snapshot.get("tag"), deadline))
            return {"status": "exact", "tag": snapshot.get("tag")}

    def setUp(self):
        calc_service.invalidate()
        self._real_engine = calc_service._engine
        calc_service._engine = lambda: (self._FakeEngine, None)
        self._FakeEngine.calls = []

    def tearDown(self):
        calc_service._engine = self._real_engine
        calc_service.invalidate()

    def test_result_only_renders_for_its_own_snapshot(self):
        first = {"tag": "first", "schema_version": 1}
        second = {"tag": "second", "schema_version": 1}
        self.assertIsNone(calc_service.poll(first))
        calc_service.request(first, DATA)
        for _ in range(50):
            if calc_service.poll(first):
                break
            time.sleep(0.05)
        self.assertIsNotNone(calc_service.poll(first))
        self.assertIsNone(calc_service.poll(second))  # never a stale render
        self.assertTrue(calc_service.cached(first))

    def test_budget_is_two_seconds_and_reused_from_cache(self):
        snap = {"tag": "budget", "schema_version": 1}
        calc_service.request(snap, DATA)
        for _ in range(50):
            if calc_service.poll(snap):
                break
            time.sleep(0.05)
        tag, deadline = self._FakeEngine.calls[0]
        self.assertEqual(tag, "budget")
        self.assertLessEqual(deadline - time.monotonic(), calc_service.BUDGET_SECONDS)
        self.assertGreater(deadline - time.monotonic(), 0)
        calls = len(self._FakeEngine.calls)
        calc_service.request(snap, DATA)  # cache hit must not schedule work
        time.sleep(calc_service.DEBOUNCE_SECONDS + 0.3)
        self.assertEqual(len(self._FakeEngine.calls), calls)
        self.assertIsNotNone(calc_service.poll(snap))

    def test_engine_failure_is_an_unavailable_card_not_a_crash(self):
        def broken():
            raise RuntimeError("boom")
        calc_service._engine = broken
        snap = {"tag": "broken", "schema_version": 1}
        calc_service.request(snap, DATA)
        for _ in range(50):
            if calc_service.poll(snap):
                break
            time.sleep(0.05)
        self.assertIn("engine", str(calc_service.poll(snap)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
