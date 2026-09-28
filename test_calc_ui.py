"""Pure checks for the Calc battle-state UI: snapshot building, configs, cards."""

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import apply_party_slots, connect, move_party_to_box, resolve_trainer_level, save_entry
from planner_ui import (STAT_KEYS, build_calc_snapshot, calc_default_max_hp, calc_load_config, calc_progress_markup, calc_result_card,
                        calc_snapshot,
                        calc_cure_boxed_status,
                        calc_seed_hp, calc_start_battle_at_full_hp, calc_state_for_focus,
                        calc_state_for_focused_battle,
                        calc_focus_for_active_selection,
                        calc_state_for_trainer, card_result, fraction_text, history_from_log,
                        history_to_log_rows, level_cap_choices, level_cap_id_for,
                        parse_flag_text, parse_rng_words, percent_text, probability_text,
                        simulate_highest_level, stat_bars_markup)


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

        self.assertEqual(snapshot["schema_version"], 2)
        self.assertEqual(snapshot["state_origin"], "manual-scenario")
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
        self.assertEqual(snapshot["rng"], {"main": [1, 2, 3, 4], "secondary": None})
        self.assertEqual(len(snapshot["history"]["turns"]), 1)
        self.assertEqual(snapshot["planned_action"],
                         {"kind": "move", "move_id": "MOVE_TACKLE", "target_index": None})

    def test_v2_snapshot_keeps_both_rng_streams_and_state_origin(self):
        snapshot = build_calc_snapshot(
            DATA, [player_row()], [enemy_member()],
            {"player_active_slot": 0, "enemy_active_index": 0,
             "state_origin": "battle-start",
             "rng": {"main": [1, 2, 3, 4], "secondary": [5, 6, 7, 8]}},
            trainer_id="TRAINER_TEST", difficulty="Normal")
        self.assertEqual(snapshot["schema_version"], 2)
        self.assertEqual(snapshot["state_origin"], "battle-start")
        self.assertEqual(snapshot["rng"], {"main": [1, 2, 3, 4], "secondary": [5, 6, 7, 8]})

    def test_unknown_rng_streams_and_current_hp_are_never_fabricated(self):
        snapshot = build_calc_snapshot(
            DATA, [player_row()], [enemy_member()],
            {"player_active_slot": 0, "enemy_active_index": 0},
            trainer_id="TRAINER_TEST", difficulty="Normal")
        self.assertEqual(snapshot["rng"], {"main": None, "secondary": None})
        self.assertIsNone(snapshot["player"]["party"][0]["hp"])
        self.assertIsNone(snapshot["enemy"]["party"][0]["hp"])
        self.assertIsNone(snapshot["player"]["party"][0]["status"])

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
        self.assertIsNone(blank["player"]["party"][1]["status"])
        self.assertIsNone(blank["enemy"]["party"][1]["hp"])
        self.assertIsNone(blank["enemy"]["party"][1]["status"])

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
        self.assertIsNone(player["status"])
        self.assertNotEqual(player["hp"], 0)
        enemy = snapshot["enemy"]["party"][0]
        self.assertIsNone(enemy["hp"])
        self.assertEqual(enemy["evs"], {"attack": 8})
        self.assertEqual(enemy["pp"], {})
        self.assertEqual(snapshot["rng"], {"main": None, "secondary": None})
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

    def test_focus_selects_active_pair_and_clears_old_action(self):
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

    def test_active_widget_selection_maps_back_to_focus_pair(self):
        party = [player_row(7), None, player_row(9)]
        enemies = [enemy_member(), enemy_member()]
        self.assertEqual(calc_focus_for_active_selection(party, enemies, 2, 1), (9, 1))
        self.assertEqual(calc_focus_for_active_selection(party, enemies, 99, 1), (None, 1))

    def test_focus_accepts_live_sqlite_party_rows(self):
        import sqlite3
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        try:
            row = db.execute("SELECT 9 AS id").fetchone()
            state = {}
            calc_state_for_focus(state, [row], 9, 0)
            self.assertEqual(state["player_active_slot"], 0)
            self.assertEqual(calc_focus_for_active_selection([row], [enemy_member()], 0, 0), (9, 0))
        finally:
            db.close()

    def test_focused_battle_ignores_hidden_saved_advanced_inputs(self):
        state = {"player_active_slot": 0, "enemy_active_index": 0,
                 "planned_action": {"kind": "move", "move_id": "MOVE_TACKLE"},
                 "rng": {"main": [1, 2, 3, 4], "secondary": [5, 6, 7, 8]},
                 "history": {"turns": [{"events": []}]}, "field": {"weather": "WEATHER_RAIN"},
                 "progress_flags": {"some_flag": True},
                 "player": {"7": {"hp": 12, "max_hp": 44, "status": "STATUS1_BURN",
                                  "pp": {"MOVE_TACKLE": 1}, "stat_stages": {"attack": 2}}},
                 "enemy": {"0": {"hp": 20, "max_hp": 30, "status": "STATUS1_PARALYSIS"}}}
        calc_state_for_focused_battle(state)
        self.assertEqual((state["player_active_slot"], state["enemy_active_index"]), (0, 0))
        self.assertEqual((state["player"]["7"]["hp"], state["enemy"]["0"]["hp"]), (12, 20))
        self.assertEqual(state["player"]["7"], {"hp": 12, "max_hp": 44, "status": "STATUS1_BURN"})
        self.assertEqual(state["enemy"]["0"], {"hp": 20, "max_hp": 30, "status": "STATUS1_PARALYSIS"})
        self.assertNotIn("planned_action", state)
        self.assertNotIn("progress_flags", state)
        self.assertEqual(state["rng"], {"main": None, "secondary": None})
        self.assertEqual(state["history"], {"turns": []})
        self.assertEqual(state["field"], {})

    def test_boxing_cures_only_current_scenario_status(self):
        state = {"player": {"7": {"status": "STATUS1_BURN"},
                            "9": {"status": "STATUS1_POISON"}}}
        calc_cure_boxed_status(state, {"9"})
        self.assertEqual(state["player"]["7"]["status"], "none")
        self.assertEqual(state["player"]["9"]["status"], "STATUS1_POISON")

    def test_focus_change_replaces_stale_active_widget_pair(self):
        party = [player_row(7), None, player_row(9)]
        state = {"player_active_slot": 0, "enemy_active_index": 0,
                 "planned_action": {"kind": "move", "move_id": "MOVE_TACKLE"}}
        calc_state_for_focus(state, party, 9, 1)
        self.assertEqual((state["player_active_slot"], state["enemy_active_index"]), (2, 1))
        self.assertEqual(calc_focus_for_active_selection(party, [enemy_member(), enemy_member()], 2, 1),
                         (9, 1))
        self.assertIsNone(state["planned_action"])

    def test_unresolved_ai_flags_fail_closed(self):
        snap = build_calc_snapshot(
            DATA, [player_row()], [enemy_member()],
            {"player_active_slot": 0, "enemy_active_index": 0},
            trainer_id="TRAINER_TEST", difficulty="Normal", ai_flags=None)
        self.assertEqual(snap["enemy"]["ai_flags"], ["AI_FLAG_UNKNOWN"])

    def test_hp_uses_species_max_and_preserves_current_damage(self):
        state = {"state_origin": "battle-start"}
        maximum = calc_default_max_hp(DATA, "SPECIES_TEST", 15, {"hp": 31}, {"hp": 0})
        self.assertEqual(maximum, 44)
        entry = calc_seed_hp(state, "player", 7, maximum)
        self.assertEqual(entry.get("hp"), maximum)
        self.assertEqual(entry["max_hp"], 44)
        entry.update(hp=12, max_hp=57)
        calc_seed_hp(state, "player", 7, 60)
        self.assertEqual((entry["hp"], entry["max_hp"]), (12, 60))
        calc_seed_hp(state, "player", 7, 10)
        self.assertEqual((entry["hp"], entry["max_hp"]), (10, 10))
        entry["hp"] = 0
        calc_seed_hp(state, "player", 7, 10)
        self.assertEqual(entry["hp"], 0)
        enemy = calc_seed_hp(state, "enemy", 0, maximum)
        self.assertEqual(enemy.get("hp"), maximum)
        self.assertEqual(enemy["max_hp"], 44)
        enemy["hp"] = 44
        calc_seed_hp(state, "enemy", 0, 50)
        self.assertEqual((enemy["hp"], enemy["max_hp"]), (44, 50))

    def test_saved_unknown_hp_remains_unknown_while_max_hp_is_updated(self):
        state = {"_preserve_unknown_hp": True, "player": {"7": {"hp": None}}}
        entry = calc_seed_hp(state, "player", 7, 44)
        self.assertIsNone(entry["hp"])
        self.assertEqual(entry["max_hp"], 44)

    def test_manual_unknown_hp_stays_blank_and_save_omits_runtime_marker(self):
        state = {"state_origin": "manual-scenario", "_preserve_unknown_hp": True}
        self.assertNotIn("hp", calc_seed_hp(state, "player", 7, 44))
        with patch("planner_ui.st.session_state", {}):
            saved = calc_snapshot(DATA, [], "TRAINER_TEST", [], state)
        self.assertNotIn("_preserve_unknown_hp", saved["battle_state"])

    def test_explicit_battle_start_sets_full_hp_and_resets_history_rng(self):
        state = {"history": {"turns": [{"events": []}]},
                 "rng": {"main": [1, 2, 3, 4], "secondary": [5, 6, 7, 8]},
                 "player": {"7": {"hp": 3, "pp": {"MOVE_TACKLE": 2}}}}
        calc_start_battle_at_full_hp(state, [player_row()], [enemy_member()], DATA,
                                     [12], [10])
        self.assertEqual(state["state_origin"], "battle-start")
        self.assertEqual(state["history"], {"turns": []})
        self.assertEqual(state["rng"], {"main": None, "secondary": None})
        self.assertEqual(state["player"]["7"]["hp"], state["player"]["7"]["max_hp"])
        self.assertEqual(state["enemy"]["0"]["hp"], state["enemy"]["0"]["max_hp"])
        self.assertNotIn("pp", state["player"]["7"])


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

    def test_boxing_cures_current_status_but_saved_config_restores_it(self):
        status = "STATUS1_BURN"
        state = {"player": {str(self.member): {"status": status}}}
        record = self.payload(battle_state=state)
        move_party_to_box(self.db, self.member, 1, 5)
        calc_cure_boxed_status(state, set())
        self.assertEqual(state["player"][str(self.member)]["status"], "none")
        session = {}
        with patch("planner_ui.st.session_state", session):
            calc_load_config(self.db, record, DATA, apply_party_slots, self.trainers)
        self.assertEqual(session["calc_battle_state"]["player"][str(self.member)]["status"], status)

    def test_v2_config_preserves_unknown_battle_values(self):
        session = {}
        state = {"player_active_slot": 0, "enemy_active_index": 0,
                 "player": {str(self.member): {"max_hp": 30, "hp": None}},
                 "rng": None, "history": {"turns": []}}
        with patch("planner_ui.st.session_state", session):
            calc_load_config(self.db, self.payload(battle_state=state), DATA,
                             apply_party_slots, self.trainers)
        restored = session["calc_battle_state"]
        self.assertIsNone(restored["player"][str(self.member)]["hp"])
        self.assertIsNone(restored["rng"])
        self.assertEqual(restored["history"], {"turns": []})

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
        self.assertTrue(session["calc_battle_state"]["_preserve_unknown_hp"])
        self.assertNotIn("player", session["calc_battle_state"])
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

    def test_exact_card_shows_percent_without_explanation(self):
        card = calc_result_card("Your moves · KO probability", "player_moves",
                                card_result(self.result(), "player_moves"))
        self.assertIn("33%", card)
        self.assertNotIn("1/3", card)
        self.assertNotIn("source-transcribed", card)
        self.assertNotIn("damage &gt;= remaining HP", card)

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

    def test_unavailable_card_shows_useful_message_without_diagnostics(self):
        card = calc_result_card("AI predicted action", "ai_actions",
                                card_result(self.result(), "ai_actions"))
        self.assertIn("Enter the opponent&#x27;s current HP.", card)
        self.assertNotIn("missing-input", card)
        self.assertNotIn("enemy.party[0].hp", card)
        self.assertNotIn("source-transcribed", card)

    def test_cards_are_independent_and_whole_run_failures_reach_every_card(self):
        result = self.result()
        exact = calc_result_card("Your moves · KO probability", "player_moves",
                                 card_result(result, "player_moves"))
        missing = calc_result_card("Enemy moves · KO probability", "enemy_moves",
                                   card_result(result, "enemy_moves"), ["Tackle"])
        self.assertIn("33%", exact)
        self.assertIn("Tackle", missing)
        self.assertIn("Calculating", missing)
        self.assertIn("Tackle", missing)
        failure = {"status": "unavailable", "reason": "engine-error",
                   "detail": "battle engine unavailable", "missing": [], "mechanics": []}
        for kind, _title in (("player_moves", ""), ("recommendation", ""),
                             ("ai_actions", ""), ("enemy_moves", "")):
            self.assertIn("Prediction unavailable.", calc_result_card("x", kind, card_result(failure, kind)))

    def test_move_entries_are_gated_independently(self):
        result = self.result()
        result["player_moves"]["moves"].append(
            {"move_id": "MOVE_GROWL", "move_name": "Growl", "status": "unavailable",
             "reason": "unsupported-move", "mechanics": ["sound-based stat drop"]})
        card = calc_result_card("Your moves · KO probability", "player_moves",
                                card_result(result, "player_moves"))
        self.assertIn("33%", card)
        self.assertIn("Tackle", card)
        self.assertIn("Growl", card)
        self.assertIn("Unavailable", card)
        self.assertNotIn("unsupported-move", card)
        self.assertNotIn("sound-based stat drop", card)
        self.assertNotIn("A move marked Not calculated", card)
        self.assertNotIn("sd-calc-result-card\"><strong>Your moves</strong>", card)

    def test_estimate_card_shows_ko_chance_without_sampling_metadata(self):
        card = {"status": "estimated", "sampler_id": "paired-rom-sampler-v1",
                "num": 3, "den": 10, "effective_samples": 400,
                "interval_95": {"low": 0.02, "high": 0.25},
                "moves": [{"move_id": "MOVE_TACKLE", "move_name": "Tackle",
                           "status": "estimated", "num": 3, "den": 10,
                           "effective_samples": 400,
                           "interval_95": {"low": 0.02, "high": 0.25}}]}
        markup = calc_result_card("Your moves · KO result", "player_moves", card)
        self.assertIn("Tackle", markup)
        self.assertIn("30%", markup)
        self.assertNotIn("samples", markup)
        self.assertNotIn("interval", markup)
        self.assertNotIn("paired-rom-sampler-v1", markup)
        self.assertNotIn("30% (3/10)", markup)

    def test_move_rows_show_damage_range_without_sample_badge(self):
        exact = {"status": "engine-exact", "moves": [{"move_id": "MOVE_TACKLE", "move_name": "Tackle",
                 "ko_probability": {"num": 1, "den": 4}, "damage_range": {"min": 12, "max": 18}}]}
        markup = calc_result_card("Moves", "player_moves", exact)
        self.assertIn("25%", markup)
        self.assertIn("12–18 dmg", markup)
        self.assertNotIn("Damage range", markup)
        self.assertNotIn("Compiled game engine", markup)
        self.assertIn("12–18", markup)
        sampled = {"status": "estimated", "samples": 24,
                   "moves": [{"move_id": "MOVE_TACKLE", "move_name": "Tackle",
                              "ko_probability": {"num": 1, "den": 4}}]}
        sampled_markup = calc_result_card("Moves", "enemy_moves", sampled)
        self.assertIn("25%", sampled_markup)
        self.assertNotIn("Sampled", sampled_markup)

    def test_unavailable_move_can_show_its_damage_range(self):
        card = {"status": "exact", "moves": [{"move_id": "MOVE_DOUBLE_KICK", "move_name": "Double Kick",
                "status": "unavailable", "reason": "unsupported-multi-hit-ko",
                "damage_range": {"min": 20, "max": 34}}]}
        markup = calc_result_card("Moves", "player_moves", card)
        self.assertIn("Unavailable", markup)
        self.assertIn("20–34 dmg", markup)

    def test_status_move_omits_zero_damage_range(self):
        card = {"status": "engine-exact", "moves": [{"move_id": "MOVE_GROWL",
                "move_name": "Growl", "ko_probability": {"num": 0, "den": 1},
                "damage_range": {"min": 0, "max": 0}}]}
        markup = calc_result_card("Moves", "player_moves", card)
        self.assertIn("Growl", markup)
        self.assertIn("0%", markup)
        self.assertNotIn("0–0 dmg", markup)

    def test_ai_predictions_keep_tied_choices_without_sampling_metadata(self):
        ai = {"status": "exact", "predicted_action": {"id": "MOVE_TACKLE", "label": "Tackle"},
              "conditioned_on": "entered battle state",
              "actions": [{"id": "MOVE_TACKLE", "label": "Tackle", "probability": {"num": 1, "den": 2}},
                          {"id": "MOVE_GROWL", "label": "Growl", "probability": {"num": 1, "den": 2}}]}
        ai_markup = calc_result_card("AI", "ai_actions", ai)
        self.assertIn("Tackle", ai_markup)
        self.assertIn("Growl", ai_markup)
        self.assertEqual(ai_markup.count("50%"), 2)
        self.assertNotIn("Conditioned on", ai_markup)
        self.assertNotIn("1/2", ai_markup)
        preview = {**ai, "status": "engine-fixed-seed", "preview": True}
        preview_markup = calc_result_card("AI", "ai_actions", preview)
        self.assertIn("Trainer action preview", preview_markup)
        self.assertNotIn("50%", preview_markup)

    def test_recommendation_shows_only_actionable_leaders(self):
        recommendation = {"status": "estimated", "actions": [
            {"id": "MOVE_TACKLE", "kind": "move", "label": "Tackle", "top_option": True,
             "death_risk": {"num": 1, "den": 10, "interval_95": {"low": 0.02, "high": 0.2}},
             "first_turn_death_risk": {"num": 1, "den": 10, "interval_95": {"low": 0.02, "high": 0.2}},
             "followup_death_risk": {"estimate": 0.2, "basis": "measured damage before estimated KO",
                                     "range": {"low": 0.1, "high": 0.3}},
             "ranking_death_risk": {"estimate": 0.3, "basis": "sampled first turn plus modeled follow-up",
                                   "range": {"low": 0.12, "high": 0.5}},
             "ko_before_faint": {"num": 8, "den": 10}, "turns_to_ko": {"estimate": 1, "basis": "sampled"}},
            {"id": "MOVE_GROWL", "kind": "move", "label": "Growl", "top_option": True,
             "death_risk": {"num": 1, "den": 10, "interval_95": {"low": 0.02, "high": 0.2}},
             "ko_before_faint": {"num": 3, "den": 10}, "turns_to_ko": None,
             "matchup_estimate": "stable"},
            {"id": 1, "kind": "switch", "label": "Switch to party slot 2", "top_option": True,
             "followup_viability": 0, "matchup_estimate": "unavailable"},
            {"id": "MOVE_SPLASH", "kind": "move", "label": "Splash"}],
            "ranking_uncertain": True, "critical_policy": "item-ability-only",
            "effective_samples": 12,
            "ranking_basis": "first-turn samples plus modeled follow-up"}
        markup = calc_result_card("Recommendation", "recommendation", recommendation)
        self.assertNotIn("sd-calc-recommended", markup)
        self.assertIn("Tackle", markup)
        self.assertIn("70% est. survival", markup)
        self.assertIn("Growl", markup)
        self.assertNotIn("Choice uncertain", markup)
        self.assertIn("Player action options", markup)
        self.assertIn("Switch to party slot 2", markup)
        self.assertNotIn("Splash", markup)
        for fluff in ("interval", "samples", "death risk", "Ranking basis", "critical hits",
                      "Matchup", "KO before faint", "turns to KO"):
            self.assertNotIn(fluff, markup)
        extended = {"status": "estimated", "ranking_uncertain": True, "actions": [
            {"id": f"MOVE_{index}", "kind": "move", "label": f"Move {index}", "top_option": index < 5}
            for index in range(8)]}
        extended_markup = calc_result_card("Recommendation", "recommendation", extended)
        for index in range(5):
            self.assertIn(f"Move {index}", extended_markup)
        self.assertNotIn("Move 5", extended_markup)

    def test_seeded_recommendation_is_an_action_preview_without_probabilities(self):
        card = calc_result_card("Recommendation", "recommendation", {
            "status": "engine-fixed-seed", "actions": [{"id": "MOVE_TACKLE", "label": "Tackle",
                "death_risk": {"num": 0, "den": 1}, "ko_before_faint": {"num": 1, "den": 1}}]})
        self.assertIn("Player action preview", card)
        self.assertIn("Tackle", card)
        self.assertNotIn("%", card)

    def test_proven_switch_is_shown_as_an_action(self):
        markup = calc_result_card("Recommendation", "recommendation", {
            "status": "estimated", "recommended_action": 2,
            "actions": [{"id": 2, "kind": "switch", "label": "Switch to party slot 3",
                         "top_option": True, "uniquely_recommended": True,
                         "followup_viability": 0.8, "matchup_estimate": "estimated-stable"},
                        {"id": "MOVE_TACKLE", "kind": "move", "label": "Tackle"}]})
        self.assertIn("Switch to party slot 3", markup)
        self.assertNotIn("Tackle", markup)

    def test_recommendation_uses_first_turn_survival_when_followup_is_unknown(self):
        action = {"id": "MOVE_TACKLE", "kind": "move", "label": "Tackle",
                  "top_option": True, "first_turn_death_risk": {"num": 1, "den": 4},
                  "ranking_death_risk": {"estimate": None}}
        markup = calc_result_card("Recommendation", "recommendation", {
            "status": "estimated", "actions": [action]})
        self.assertIn("75% est. turn survival", markup)
        preview = calc_result_card("Recommendation", "recommendation", {
            "status": "engine-fixed-seed", "actions": [action]})
        self.assertNotIn("75%", preview)

    def test_trainer_bag_items_block_recommendation_with_short_warning(self):
        markup = calc_result_card("Recommendation", "recommendation", {
            "status": "unavailable", "reason": "unsupported-mechanic",
            "detail": "Trainer bag items can change the matchup; resolved recommendation turns do not simulate item use."})
        self.assertIn("Trainer may use a bag item. No recommendation.", markup)
        self.assertNotIn("resolved recommendation turns", markup)

    def test_critical_policy_metadata_never_clutters_move_card(self):
        card = {"status": "engine-exact", "moves": [{"move_id": "MOVE_TACKLE",
                "move_name": "Tackle", "probability": {"num": 1, "den": 2}}]}
        self.assertNotIn("ordinary critical hits excluded",
                         calc_result_card("Moves", "player_moves", card))
        card["critical_policy"] = "item-ability-only"
        self.assertNotIn("ordinary critical hits excluded",
                         calc_result_card("Moves", "player_moves", card))

    def test_refinement_progress_has_only_loading_indicator(self):
        markup = calc_progress_markup({"phase": "recommendation", "completed_samples": 3,
                                       "target_samples": 10, "refining": True})
        self.assertIn("Calculating", markup)
        self.assertNotIn("samples", markup)
        self.assertNotIn("recommendation", markup)

    def test_engine_fixed_seed_move_cards_omit_engine_banner(self):
        for kind in ("player_moves", "enemy_moves"):
            markup = calc_result_card("Result", kind, {"status": "engine-fixed-seed"})
            self.assertNotIn("Compiled game engine", markup)
        ai = calc_result_card("Result", "ai_actions", {"status": "engine-fixed-seed"})
        self.assertNotIn("Compiled game engine", ai)

    def test_recommendation_omits_setup_moves_warning(self):
        markup = calc_result_card(
            "Recommendation", "recommendation",
            {"status": "exact", "scored_turns": 1,
             "actions": [{"id": "MOVE_TACKLE", "kind": "move",
                          "score": {"num": 1, "den": 1}}]})
        self.assertNotIn("one-turn lookahead", markup)
        self.assertNotIn("only one turn is scored", markup)

    def test_only_focused_battlers_keep_stat_stages(self):
        state = {"player": {"7": {"stat_stages": {"attack": 2}},
                            "9": {"stat_stages": {"speed": 1}}},
                 "enemy": {"0": {"stat_stages": {"defense": -1}}},
                 "player_active_slot": 0, "enemy_active_index": 0}
        calc_state_for_focused_battle(state, 7, 0)
        self.assertEqual(state["player"]["7"]["stat_stages"], {"attack": 2})
        self.assertNotIn("stat_stages", state["player"]["9"])
        self.assertEqual(state["enemy"]["0"]["stat_stages"], {"defense": -1})

    def test_recommendation_unavailable_preserves_insufficient_samples_reason(self):
        markup = calc_result_card(
            "Recommendation", "recommendation",
            {"status": "unavailable", "reason": "insufficient-samples",
             "detail": "No complete trajectory was returned."})
        self.assertIn("No reliable prediction yet.", markup)
        self.assertNotIn("insufficient-samples", markup)

    def test_unavailable_v2_result_has_specific_reason(self):
        markup = calc_result_card("AI action", "ai_actions",
                                  {"status": "unavailable", "reason": "incomplete-state",
                                   "detail": "Observed history lacks a volatile effect.",
                                   "missing": [], "mechanics": []})
        self.assertIn("Complete the battle state to predict.", markup)
        self.assertNotIn("incomplete-state", markup)

    def test_a_card_level_failure_still_hides_every_entry(self):
        result = self.result()
        result["player_moves"] = {"status": "unavailable", "reason": "timeout",
                                  "detail": "The search ran out of budget.",
                                  "missing": [], "mechanics": []}
        card = calc_result_card("Your moves · KO probability", "player_moves",
                                card_result(result, "player_moves"))
        self.assertIn("Prediction timed out.", card)
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
        self.assertEqual(probability_text({"num": 1, "den": 3}), "33%")

    def test_status_adjusts_stats_and_uses_status_ability_overrides(self):
        base_stats = {key: 100 for key in STAT_KEYS}
        burned = stat_bars_markup(base_stats, 50, "NATURE_SERIOUS", status="STATUS1_BURN")
        self.assertIn('style="--status-color:#e66d39"', burned)
        self.assertIn('<div><i style="width:35%;background:#67b985"></i></div><b><span>52</span><small class="sd-stat-delta">-53</small>', burned)
        self.assertNotIn('sd-status-arrow', burned)
        self.assertIn("105", burned)  # Unaffected rows keep their ordinary stat value.
        ordinary = stat_bars_markup(base_stats, 50, "NATURE_SERIOUS")
        self.assertEqual(ordinary.split('class="sd-stat sd-stat-total"')[1].split('</div>')[0],
                         burned.split('class="sd-stat sd-stat-total"')[1].split('</div>')[0])

        guts = stat_bars_markup(base_stats, 50, "NATURE_SERIOUS", status="STATUS1_BURN",
                                ability="ABILITY_GUTS")
        self.assertIn('157</span><small class="sd-stat-delta">+52', guts)

        paralyzed = stat_bars_markup(base_stats, 50, "NATURE_SERIOUS", status="STATUS1_PARALYSIS")
        self.assertIn('52</span><small class="sd-stat-delta">-53', paralyzed)
        quick_feet = stat_bars_markup(base_stats, 50, "NATURE_SERIOUS", status="STATUS1_PARALYSIS",
                                     ability="ABILITY_QUICK_FEET")
        self.assertIn('210</span><small class="sd-stat-delta">+105', quick_feet)

        frostbitten = stat_bars_markup(base_stats, 50, "NATURE_SERIOUS", status="STATUS1_FROSTBITE")
        self.assertIn('52</span><small class="sd-stat-delta">-53', frostbitten)
        widths = lambda markup: re.findall(r'<i style="width:(\d+)%', markup)
        for markup in (burned, guts, paralyzed, quick_feet, frostbitten):
            self.assertEqual(widths(ordinary), widths(markup))

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
        self.assertEqual(history_from_log(history_to_log_rows(turns)), turns)


if __name__ == "__main__":
    unittest.main()
