"""Pure checks for the Calc battle-state UI: snapshot building, configs, cards."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import apply_party_slots, connect, resolve_trainer_level, save_entry
from planner_ui import (STAT_KEYS, build_calc_snapshot, calc_default_max_hp, calc_load_config, calc_result_card,
                        calc_seed_hp, calc_state_for_focus, calc_state_for_trainer, card_result,
                        fraction_text, history_from_log, level_cap_choices, level_cap_id_for,
                        parse_flag_text, parse_rng_words, percent_text, probability_text,
                        simulate_highest_level)


SHA = "2576d0f3f4c861c760f0c55198e37e6d7b91683e"
DATA = {
    "source": {"repo": "pokeemerald-expansion", "commit": SHA},
    "species": {"SPECIES_TEST": {"name": "Testmon", "types": ["TYPE_NORMAL"],
                                 "abilities": ["ABILITY_TEST"],
                                 "learnable_moves": ["MOVE_TACKLE", "MOVE_GROWL"],
                                 "stats": {"hp": 50, "attack": 50, "defense": 50,
                                           "sp_attack": 50, "sp_defense": 50, "speed": 50}}},
    "moves": {"MOVE_TACKLE": {"name": "Tackle", "pp": 35, "type": "TYPE_NORMAL"},
              "MOVE_GROWL": {"name": "Growl", "pp": 40, "type": "TYPE_NORMAL"}},
    "abilities": {"ABILITY_TEST": "A test ability."},
    "trainers": [],
    "level_caps": [{"id": "CAP_1", "flag": "FLAG_BADGE01_GET", "level": 15, "label": "Stone Badge"},
                   {"id": "CAP_MAX", "flag": None, "level": 100, "label": "Level 100"}],
}


def player_row(entry_id=7, *, moves=("MOVE_TACKLE",), ivs=None, evs=None):
    return {"id": entry_id, "species": "SPECIES_TEST", "level": 12, "ability": "ABILITY_TEST",
            "item": "ITEM_ORAN", "nature": "NATURE_HARDY", "moves": json.dumps(list(moves)),
            "ivs": json.dumps(ivs if ivs is not None else {}),
            "evs": json.dumps(evs if evs is not None else {}),
            "status": "alive", "in_party": 1, "box_no": 1, "box_slot": None, "party_slot": 0}


def enemy_member():
    return {"species": "SPECIES_TEST", "level": 10, "moves": ["MOVE_TACKLE", "MOVE_GROWL"],
            "ability": "ABILITY_TEST", "nature": "NATURE_HARDY", "item": "ITEM_ORAN",
            "ivs": {"hp": 31}, "evs": {"attack": 8}}


class SnapshotTest(unittest.TestCase):
    def test_build_snapshot_carries_the_entered_battle_state(self):
        state = {"player_active_slot": 0, "enemy_active_index": 0,
                 "planned_action": {"kind": "move", "move_id": "MOVE_TACKLE", "target_index": None},
                 "player": {"7": {"hp": 20, "max_hp": 25, "pp": {"MOVE_TACKLE": 30},
                                  "status": "STATUS1_BURN", "stat_stages": {"attack": 1}}},
                 "enemy": {"0": {"hp": 18, "max_hp": 22, "pp": {"MOVE_TACKLE": 10}}},
                 "rng": [1, 2, 3, 4],
                 "field": {"weather": "WEATHER_RAIN", "player_screens": ["SCREEN_REFLECT"]},
                 "history": {"turns": [{"player_action": None, "enemy_action": None, "events": []}]},
                 "progress_flags": {"FLAG_BADGE01_GET": True}}
        snapshot = build_calc_snapshot(
            DATA, [player_row(ivs={"hp": 31, "attack": 5}, evs={"speed": 4})], [enemy_member()], state,
            trainer_id="TRAINER_TEST", difficulty="Normal", level_cap_id="CAP_1",
            player_item_flags={"calc_item_player_7": False},
            enemy_item_flags={"calc_item_enemy_TRAINER_TEST_0": True},
            enemy_items={"ITEM_POTION": 2}, ai_flags=["AI_FLAG_SMART"], enemy_levels=[10])

        self.assertEqual(snapshot["schema_version"], 1)
        self.assertEqual(snapshot["source_sha"], SHA)
        self.assertEqual(snapshot["difficulty"], "Normal")
        self.assertEqual(snapshot["progress"], {"milestone_id": "CAP_1",
                                                "flags": {"FLAG_BADGE01_GET": True}})
        self.assertEqual(snapshot["player"]["active_index"], 0)
        player = snapshot["player"]["party"][0]
        self.assertEqual((player["hp"], player["max_hp"]), (20, 25))
        self.assertEqual(player["pp"], {"MOVE_TACKLE": 30})
        self.assertEqual(player["status"], "STATUS1_BURN")
        self.assertEqual(player["stat_stages"], {"attack": 1})
        self.assertEqual(player["ivs"], {key: {"hp": 31, "attack": 5}.get(key, 0) for key in STAT_KEYS})
        self.assertEqual(player["evs"], {key: {"speed": 4}.get(key, 0) for key in STAT_KEYS})
        self.assertEqual(player["moves"], ["MOVE_TACKLE"])
        self.assertIsNone(player["item"])
        self.assertFalse(player["use_item"])
        enemy = snapshot["enemy"]["party"][0]
        self.assertEqual((enemy["slot_id"], enemy["level"]), (None, 10))
        self.assertEqual(enemy["item"], "ITEM_ORAN")
        self.assertTrue(enemy["use_item"])
        self.assertEqual(enemy["hp"], 18)
        self.assertEqual(enemy["pp"], {"MOVE_TACKLE": 10})
        self.assertEqual(snapshot["enemy"]["items"], {"ITEM_POTION": 2})
        self.assertEqual(snapshot["enemy"]["ai_flags"], ["AI_FLAG_SMART"])
        self.assertEqual(snapshot["field"], {"weather": "WEATHER_RAIN", "terrain": None,
                                             "trick_room": None,
                                             "player_screens": ["SCREEN_REFLECT"],
                                             "enemy_screens": None, "player_hazards": None,
                                             "enemy_hazards": None})
        self.assertEqual(snapshot["rng"], {"state": [1, 2, 3, 4]})
        self.assertEqual(len(snapshot["history"]["turns"]), 1)
        self.assertEqual(snapshot["planned_action"],
                         {"kind": "move", "move_id": "MOVE_TACKLE", "target_index": None})

    def test_bench_member_state_reaches_the_snapshot_and_unknown_stays_none(self):
        party = [player_row(entry_id=7), dict(player_row(entry_id=9), party_slot=1)]
        enemies = [enemy_member(), dict(enemy_member(), level=11)]
        state = {"player_active_slot": 0, "enemy_active_index": 0,
                 "player": {"7": {"hp": 20, "max_hp": 25},
                            "9": {"hp": 11, "max_hp": 30, "status": "none"}},
                 "enemy": {"1": {"hp": 14, "max_hp": 19, "status": "STATUS1_PARALYSIS"}}}
        snapshot = build_calc_snapshot(DATA, party, enemies, state, trainer_id="TRAINER_TEST",
                                      difficulty="Easy", enemy_levels=[10, 11])
        bench_player = snapshot["player"]["party"][1]
        self.assertEqual((bench_player["hp"], bench_player["max_hp"]), (11, 30))
        self.assertEqual(bench_player["status"], "none")
        bench_enemy = snapshot["enemy"]["party"][1]
        self.assertEqual((bench_enemy["hp"], bench_enemy["max_hp"]), (14, 19))
        self.assertEqual(bench_enemy["status"], "STATUS1_PARALYSIS")

        blank = build_calc_snapshot(DATA, party, enemies,
                                    {"player_active_slot": 0, "enemy_active_index": 0},
                                    trainer_id="TRAINER_TEST", difficulty="Easy",
                                    enemy_levels=[10, 11])
        self.assertIsNone(blank["player"]["party"][1]["hp"])
        self.assertIsNone(blank["player"]["party"][1]["max_hp"])
        self.assertEqual(blank["player"]["party"][1]["status"], "none")
        self.assertIsNone(blank["enemy"]["party"][1]["hp"])
        self.assertEqual(blank["enemy"]["party"][1]["status"], "none")

    def test_unknown_hp_stays_none_and_form_stat_defaults_are_zero(self):
        state = {"player_active_slot": 0, "enemy_active_index": 0}
        snapshot = build_calc_snapshot(DATA, [player_row()], [enemy_member()], state,
                                      trainer_id="TRAINER_TEST", difficulty="Normal")
        player = snapshot["player"]["party"][0]
        self.assertIsNone(player["hp"])
        self.assertIsNone(player["max_hp"])
        self.assertEqual(player["pp"], {})
        self.assertEqual(player["ivs"], dict.fromkeys(STAT_KEYS, 0))
        self.assertEqual(player["evs"], dict.fromkeys(STAT_KEYS, 0))
        self.assertEqual(player["stat_stages"], {})
        self.assertEqual(player["status"], "none")
        self.assertNotEqual(player["hp"], 0)
        enemy = snapshot["enemy"]["party"][0]
        self.assertIsNone(enemy["hp"])
        self.assertEqual(enemy["evs"], {"attack": 8})
        self.assertEqual(enemy["pp"], {})
        self.assertIsNone(snapshot["rng"]["state"])
        self.assertEqual(snapshot["history"]["turns"], [])
        self.assertIsNone(snapshot["planned_action"])

    def test_snapshot_needs_an_active_pair_and_a_supported_difficulty(self):
        self.assertIsNone(build_calc_snapshot(DATA, [player_row()], [enemy_member()], {},
                                              trainer_id="TRAINER_TEST", difficulty="Normal"))
        self.assertIsNone(build_calc_snapshot(DATA, [player_row()], [enemy_member()],
                                              {"player_active_slot": 0, "enemy_active_index": 5},
                                              trainer_id="TRAINER_TEST", difficulty="Normal"))
        self.assertIsNone(build_calc_snapshot(DATA, [player_row()], [enemy_member()],
                                              {"player_active_slot": 0, "enemy_active_index": 0},
                                              trainer_id="TRAINER_TEST", difficulty="Hard"))

    def test_level_cap_simulates_player_levels_without_mutating_box(self):
        row = player_row()
        snap = build_calc_snapshot(
            DATA, [row], [enemy_member()],
            {"player_active_slot": 0, "enemy_active_index": 0},
            trainer_id="TRAINER_TEST", difficulty="Normal", level_cap_id="CAP_1")
        self.assertEqual(snap["player"]["party"][0]["level"], 15)
        self.assertEqual(row["level"], 12)

    def test_trainer_switch_discards_stale_turn_state(self):
        state = {"_trainer_id": "TRAINER_A", "enemy": {"0": {"hp": 1}},
                 "history": {"turns": [{}]}, "rng": [1, 2, 3, 4]}
        self.assertIs(calc_state_for_trainer(state, "TRAINER_A"), state)
        self.assertEqual(calc_state_for_trainer(state, "TRAINER_B"),
                         {"_trainer_id": "TRAINER_B"})

    def test_focused_pair_becomes_active_and_clears_old_action(self):
        party = [player_row(7), None, player_row(9)]
        state = {"_trainer_id": "TRAINER_TEST", "player_active_slot": 0,
                 "enemy_active_index": 0,
                 "planned_action": {"kind": "move", "move_id": "MOVE_TACKLE"},
                 "player": {"9": {"hp": 12}}}
        calc_state_for_focus(state, party, 9, 1)
        self.assertEqual((state["player_active_slot"], state["enemy_active_index"]), (2, 1))
        self.assertIsNone(state["planned_action"])
        self.assertEqual(state["player"]["9"]["hp"], 12)
        snap = build_calc_snapshot(DATA, party, [enemy_member(), enemy_member()], state,
                                   trainer_id="TRAINER_TEST", difficulty="Normal", ai_flags=[])
        self.assertEqual((snap["player"]["active_index"], snap["enemy"]["active_index"]),
                         (1, 1))

    def test_unresolved_ai_flags_fail_closed(self):
        snap = build_calc_snapshot(
            DATA, [player_row()], [enemy_member()],
            {"player_active_slot": 0, "enemy_active_index": 0},
            trainer_id="TRAINER_TEST", difficulty="Normal", ai_flags=None)
        self.assertEqual(snap["enemy"]["ai_flags"], ["AI_FLAG_UNKNOWN"])

    def test_hp_uses_species_max_and_preserves_current_damage(self):
        state = {}
        maximum = calc_default_max_hp(DATA, "SPECIES_TEST", 15, {"hp": 31}, {"hp": 0})
        self.assertEqual(maximum, 44)
        entry = calc_seed_hp(state, "player", 7, maximum)
        self.assertEqual((entry["hp"], entry["max_hp"]), (44, 44))
        entry.update(hp=12, max_hp=57, _auto_hp=False)
        calc_seed_hp(state, "player", 7, 60)
        self.assertEqual((entry["hp"], entry["max_hp"]), (12, 60))
        calc_seed_hp(state, "player", 7, 10)
        self.assertEqual((entry["hp"], entry["max_hp"]), (10, 10))
        entry["hp"] = 0
        calc_seed_hp(state, "player", 7, 10)
        self.assertEqual(entry["hp"], 0)
        enemy = calc_seed_hp(state, "enemy", 0, maximum)
        self.assertEqual((enemy["hp"], enemy["max_hp"]), (44, 44))
        calc_seed_hp(state, "enemy", 0, 50)
        self.assertEqual((enemy["hp"], enemy["max_hp"]), (50, 50))


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.temp_dir.name) / "test.sqlite3")
        self.member = save_entry(self.db, {
            "species": "SPECIES_TEST", "level": 12, "ability": "ABILITY_TEST",
            "item": "ITEM_ORAN", "nature": "NATURE_HARDY", "moves": ["MOVE_TACKLE"],
            "ivs": {}, "evs": {}, "status": "alive", "in_party": True, "owner": None,
            "pair_id": None, "box_no": 1})
        self.trainers = [{"trainer": {"id": "TRAINER_TEST", "party": []}, "group": "Test"}]

    def tearDown(self):
        self.db.close()
        self.temp_dir.cleanup()

    def payload(self, **overrides):
        payload = {"party_ids": [self.member, None, None, None, None, None],
                   "trainer_id": "TRAINER_TEST", "enemy_order": [], "level_cap": 100,
                   "difficulty": "Normal", "item_flags": {},
                   "calc_state_version": 2,
                   "battle_state": {"player_active_slot": 0, "enemy_active_index": None,
                                    "player": {str(self.member): {"hp": 21, "max_hp": 30,
                                                                  "pp": {"MOVE_TACKLE": 12}}}}}
        payload.update(overrides)
        return {"settings": json.dumps(payload)}

    def test_hard_difficulty_config_is_rejected_for_correction(self):
        session = {}
        with patch("planner_ui.st.session_state", session):
            with self.assertRaisesRegex(ValueError, "Hard"):
                calc_load_config(self.db, self.payload(difficulty="Hard"), DATA,
                                 apply_party_slots, self.trainers)
        self.assertNotIn("calc_difficulty", session)
        self.assertNotIn("calc_battle_state", session)

    def test_easy_and_normal_configs_load_with_their_state(self):
        for difficulty in ("Easy", "Normal"):
            session = {}
            record = self.payload(difficulty=difficulty)
            with patch("planner_ui.st.session_state", session):
                notice = calc_load_config(self.db, record, DATA, apply_party_slots, self.trainers)
            self.assertIn("Loaded config", notice)
            self.assertEqual(session["calc_difficulty"], difficulty)
            self.assertEqual(session["calc_battle_state"]["player"][str(self.member)]["hp"], 21)
            self.assertEqual(session["calc_battle_state"]["player"][str(self.member)]["pp"],
                             {"MOVE_TACKLE": 12})

    def test_v1_config_loads_without_inventing_battle_values(self):
        session = {"calc_battle_state": {"player": {str(self.member): {"hp": 99, "ivs": {"hp": 31}}}},
                   "calc_state_hp_p_7": 99, "calc_state_status_p_7": "STATUS1_BURN"}
        record = {"settings": json.dumps({
            "party_ids": [self.member, None, None, None, None, None],
            "trainer_id": "TRAINER_TEST", "enemy_order": [], "level_cap": 47,
            "difficulty": "Normal", "item_flags": {}})}
        with patch("planner_ui.st.session_state", session):
            notice = calc_load_config(self.db, record, DATA, apply_party_slots, self.trainers)
        self.assertIn("no saved battle state", notice)
        self.assertEqual(session["calc_battle_state"], {})
        self.assertNotIn("calc_state_hp_p_7", session)
        self.assertNotIn("calc_state_status_p_7", session)
        self.assertIsNone(session["calc_battle_state"].get("history"))
        # An old config's level cap still drives the 200+n resolution identically.
        self.assertEqual(session["calc_level_cap"], 47)
        self.assertEqual(resolve_trainer_level(
            "202", simulate_highest_level([player_row()], session["calc_level_cap"], DATA)), "45")

    def test_malformed_battle_state_is_rejected(self):
        for state in ({"rng": [1, 2, 3]}, {"planned_action": {"kind": "switch", "slot": "x"}},
                      {"player": []}):
            with patch("planner_ui.st.session_state", {}):
                with self.assertRaisesRegex(ValueError, "invalid"):
                    calc_load_config(self.db, self.payload(battle_state=state), DATA,
                                     apply_party_slots, self.trainers)


class CardTest(unittest.TestCase):
    def result(self):
        return {
            "status": "exact", "source_sha": SHA,
            "player_moves": {"status": "exact", "source_sha": SHA,
                             "evidence": {"basis": "source-transcribed", "oracle_verified": False},
                             "method": "seed-independent",
                             "moves": [{"move_id": "MOVE_TACKLE", "move_name": "Tackle",
                                        "probability": {"num": 1, "den": 3},
                                        "method": "seed-independent",
                                        "ko": "damage >= remaining HP of enemy active",
                                        "notes": []}]},
            "ai_actions": {"status": "unavailable", "reason": "missing-input",
                           "detail": "The enemy active Pokémon's HP is unknown.",
                           "missing": ["enemy.party[0].hp"], "mechanics": []},
        }

    def test_exact_card_shows_percentage_fraction_and_basis(self):
        card = calc_result_card("Your moves · KO probability", "player_moves",
                                card_result(self.result(), "player_moves"))
        self.assertIn("33%", card)
        self.assertIn("1/3", card)
        self.assertIn("source-transcribed, not emulator-verified", card)
        self.assertIn("damage &gt;= remaining HP of enemy active", card)

    def test_simulated_outcome_is_not_shown_as_a_ko_probability(self):
        result = self.result()
        move = result["player_moves"]["moves"][0]
        move["method"] = "known-seed"
        move["probability"] = {"num": 1, "den": 1}
        card = calc_result_card("Your moves · KO result", "player_moves",
                                card_result(result, "player_moves"))
        self.assertIn("KO in this simulation", card)
        self.assertNotIn("100%", card)
        self.assertNotIn("known-seed", card)

    def test_unavailable_card_shows_reason_and_missing_inputs(self):
        card = calc_result_card("AI predicted action", "ai_actions",
                                card_result(self.result(), "ai_actions"))
        self.assertIn("missing-input", card)
        self.assertIn("enemy.party[0].hp", card)
        self.assertNotIn("source-transcribed", card)

    def test_cards_are_independent_and_whole_run_failures_reach_every_card(self):
        result = self.result()
        exact = calc_result_card("Your moves · KO probability", "player_moves",
                                 card_result(result, "player_moves"))
        missing = calc_result_card("Enemy moves · KO probability", "enemy_moves",
                                   card_result(result, "enemy_moves"), ["Tackle"])
        self.assertIn("33%", exact)
        self.assertIn("Not calculated", missing)
        self.assertIn("Tackle", missing)
        failure = {"status": "unavailable", "reason": "engine-error",
                   "detail": "battle engine unavailable", "missing": [], "mechanics": []}
        for kind, _title in (("player_moves", ""), ("recommendation", ""),
                             ("ai_actions", ""), ("enemy_moves", "")):
            self.assertIn("engine-error", calc_result_card("x", kind, card_result(failure, kind)))

    def test_move_entries_are_gated_independently(self):
        result = self.result()
        result["player_moves"]["moves"].append(
            {"move_id": "MOVE_GROWL", "move_name": "Growl", "status": "unavailable",
             "reason": "unsupported-move", "mechanics": ["sound-based stat drop"]})
        card = calc_result_card("Your moves · KO probability", "player_moves",
                                card_result(result, "player_moves"))
        self.assertIn("33%", card)
        self.assertIn("Tackle", card)
        self.assertIn("unsupported-move", card)
        self.assertIn("sound-based stat drop", card)
        self.assertIn("Growl cannot be evaluated in this state.", card)
        self.assertIn("the remaining entries are still exact", card)
        self.assertNotIn("sd-calc-result-card\"><strong>Your moves</strong>", card)

    def test_a_card_level_failure_still_hides_every_entry(self):
        result = self.result()
        result["player_moves"] = {"status": "unavailable", "reason": "timeout",
                                  "detail": "The search ran out of budget.",
                                  "missing": [], "mechanics": []}
        card = calc_result_card("Your moves · KO probability", "player_moves",
                                card_result(result, "player_moves"))
        self.assertIn("timeout", card)
        self.assertNotIn("33%", card)

    def test_pending_cards_never_show_a_stale_percentage(self):
        card = calc_result_card("Your moves · KO probability", "player_moves", None, ["Tackle"], True)
        self.assertIn("Calculating", card)
        self.assertNotIn("33%", card)


class PresentationTest(unittest.TestCase):
    def test_percentages_come_from_the_exact_fraction(self):
        self.assertEqual(percent_text({"num": 1, "den": 3}), "33%")
        self.assertEqual(percent_text({"num": 2, "den": 3}), "67%")
        self.assertEqual(percent_text({"num": 1, "den": 1}), "100%")
        self.assertEqual(fraction_text({"num": 1, "den": 3}), "1/3")
        self.assertEqual(probability_text({"num": 1, "den": 3}), "33% (1/3)")

    def test_scaled_trainer_levels_come_from_the_simulated_cap(self):
        try:
            import battle_engine  # noqa: F401  (the routine under test)
        except ImportError:  # pragma: no cover - engine is part of the project
            self.skipTest("battle_engine is not available")
        party = [{"id": 1, "species": "SPECIES_TEST", "level": 12},
                 {"id": 9, "species": "SPECIES_TEST", "level": 20}]
        highest = simulate_highest_level(party, 47, DATA)
        self.assertEqual(highest, 47)
        self.assertEqual((party[0]["level"], party[1]["level"]), (12, 20))
        self.assertEqual(resolve_trainer_level("202", highest), "45")   # 47 - (202 - 200)
        self.assertEqual(resolve_trainer_level("200", highest), "47")
        self.assertEqual(resolve_trainer_level("42", highest), "42")
        # A member the ROM routine never raises is read back, not echoed as the cap.
        egg = [{"id": 3, "species": "SPECIES_EGG", "level": 5}]
        self.assertEqual(simulate_highest_level(egg, 47, DATA), 5)

    def test_no_milestone_or_empty_party_falls_back_to_manual_context(self):
        party = [{"id": 1, "species": "SPECIES_TEST", "level": 12}]
        self.assertIsNone(simulate_highest_level([], 47, DATA))
        self.assertIsNone(simulate_highest_level(party, None, DATA))
        self.assertEqual(resolve_trainer_level("202", simulate_highest_level([], 47, DATA)), "?")

    def test_level_cap_selects_a_rom_milestone_and_keeps_a_saved_level(self):
        levels, labels = level_cap_choices(DATA, 15)
        self.assertEqual(levels, [15, 100])
        self.assertEqual(labels[15], "Stone Badge")
        self.assertEqual(level_cap_id_for(DATA, 15), "CAP_1")
        self.assertEqual(level_cap_id_for(DATA, 100), "CAP_MAX")
        self.assertIsNone(level_cap_id_for(DATA, 57))

        levels, labels = level_cap_choices(DATA, 57)
        self.assertEqual(levels, [15, 57, 100])
        self.assertIn("not a ROM milestone", labels[57])

        levels, labels = level_cap_choices(DATA, None)
        self.assertEqual(levels, [15, 100])
        self.assertIsNone(labels.get(57))
        self.assertEqual(level_cap_choices({"level_caps": None}, 100), (None, None))
        self.assertEqual(level_cap_choices({}, 100), (None, None))

    def test_rng_and_flag_text_parsing(self):
        self.assertEqual(parse_rng_words("1 2 3 4"), [1, 2, 3, 4])
        self.assertEqual(parse_rng_words("4294967295,0,7,9"), [4294967295, 0, 7, 9])
        self.assertIsNone(parse_rng_words("1 2 3"))
        self.assertIsNone(parse_rng_words(""))
        self.assertIsNone(parse_rng_words("1 2 3 4294967296"))
        self.assertIsNone(parse_rng_words("-1 2 3 4"))
        self.assertIsNone(parse_rng_words("1x 2 3 4"))
        self.assertEqual(parse_flag_text("FLAG_BADGE01_GET, FLAG_BADGE02_GET"),
                         {"FLAG_BADGE01_GET": True, "FLAG_BADGE02_GET": True})

    def test_turn_log_grouping_preserves_order_and_actions(self):
        turns = history_from_log([
            {"turn": 1, "side": "player", "kind": "move", "id": "MOVE_TACKLE", "value": None},
            {"turn": 1, "side": "enemy", "kind": "move", "id": "MOVE_GROWL", "value": None},
            {"turn": 1, "side": "enemy", "kind": "damage", "id": None, "value": 12},
            {"turn": 2, "side": "player", "kind": "switch", "id": None, "value": 1},
            {"turn": None, "side": "enemy", "kind": "miss", "id": "MOVE_GROWL", "value": None},
            {"turn": 3, "side": None, "kind": None, "id": "", "value": None},
        ])
        self.assertEqual([len(turn["events"]) for turn in turns], [1, 0])
        self.assertEqual(turns[0]["player_action"],
                         {"kind": "move", "move_id": "MOVE_TACKLE", "target_index": None})
        self.assertEqual(turns[0]["enemy_action"],
                         {"kind": "move", "move_id": "MOVE_GROWL", "target_index": None})
        self.assertEqual(turns[0]["events"], [{"kind": "damage", "side": "enemy", "value": 12}])
        self.assertEqual(turns[1]["player_action"], {"kind": "switch", "slot": 1})


if __name__ == "__main__":
    unittest.main()
