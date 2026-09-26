"""Focused tests for battle_engine.py.  Plain unittest, synthetic game_data.

Run with ``python -m pytest test_battle_engine.py -q``.  Every test uses the
synthetic in-file game_data below; the single exception is
``TestAbilities.test_allowlist_ids_exist_in_game_data``, which validates the
engine's own ability allowlist against ``data/game_data.json`` (the file another
agent regenerates) and skips when that file is absent.
"""

import io
import json
import os
import unittest

import battle_engine as be


# ---------------------------------------------------------------------------
# synthetic game data (schema 2 shaped)
# ---------------------------------------------------------------------------

def _stats(hp, atk, dfn, spa, spd, spe):
    return {"hp": hp, "attack": atk, "defense": dfn, "sp_attack": spa,
            "sp_defense": spd, "speed": spe}


TYPES = ["TYPE_NORMAL", "TYPE_ELECTRIC", "TYPE_WATER", "TYPE_GROUND", "TYPE_FIGHTING"]


def _chart():
    chart = {atk: {dfn: 1.0 for dfn in TYPES} for atk in TYPES}
    chart["TYPE_ELECTRIC"]["TYPE_WATER"] = 2.0
    chart["TYPE_ELECTRIC"]["TYPE_GROUND"] = 0.0
    chart["TYPE_FIGHTING"]["TYPE_NORMAL"] = 2.0
    return chart


def _move(name, move_type, power, category, **kw):
    move = {"name": name, "type": move_type, "power": power, "accuracy": 100,
            "pp": 20, "category": category, "effect": "EFFECT_HIT",
            "priority": 0, "always_critical": False, "crit_ratio": 0,
            "strike_count": 1, "recoil": 0, "flags": [],
            "secondary_effects": []}
    move.update(kw)
    return move


def game_data():
    return {
        "schema_version": 2,
        "species": {
            "SPECIES_PIKACHU": {"name": "Pikachu", "types": ["TYPE_ELECTRIC"],
                                "stats": _stats(35, 55, 40, 50, 50, 90)},
            "SPECIES_SQUIRTLE": {"name": "Squirtle", "types": ["TYPE_WATER"],
                                 "stats": _stats(44, 48, 65, 50, 64, 43)},
            "SPECIES_SANDSHREW": {"name": "Sandshrew", "types": ["TYPE_GROUND"],
                                  "stats": _stats(50, 75, 85, 20, 30, 40)},
        },
        "type_chart": _chart(),
        "trainers": [{"id": "TRAINER_DEMO", "is_boss": False,
                      "conditional_party": False, "items": [], "party": []}],
        "moves": {
            "MOVE_THUNDERBOLT": _move("Thunderbolt", "TYPE_ELECTRIC", 90, "special"),
            "MOVE_QUICK_ATTACK": _move("Quick Attack", "TYPE_NORMAL", 40, "physical",
                                       priority=1),
            "MOVE_WATER_GUN": _move("Water Gun", "TYPE_WATER", 40, "special"),
            "MOVE_DOUBLE_KICK": _move("Double Kick", "TYPE_FIGHTING", 30, "physical",
                                      strike_count=2),
        },
        "oracle": {"available": False, "reason": "no local emulator oracle"},
    }


IVS = {stat: 31 for stat in be.STATS}
EVS = {stat: 0 for stat in be.STATS}


def card_ctx(snap, gd, bench=("skip", "score")):
    """(snap, ctx, gate) built the way a card builds it (per-card resolution)."""
    snap = be.normalize_snapshot(snap)
    shared, base = be._prepare(snap, gd)
    gate = base.copy()
    player_party, enemy_party = be._active_pair(snap, shared, gate, bench)
    return snap, be._ctx(shared, player_party, enemy_party), gate


def build(species, level, moves, hp, max_hp, **kw):
    out = {"slot_id": kw.get("slot_id"), "species": species, "level": level,
           "ability": kw.get("ability", be.NO_ABILITY),
           "item": kw.get("item", be.NO_ITEM),
           "nature": kw.get("nature", "NATURE_SERIOUS"),
           "ivs": dict(IVS), "evs": dict(EVS), "moves": list(moves),
           "pp": {m: kw.get("pp", 20) for m in moves},
           "max_hp": max_hp, "hp": hp, "status": "none", "stat_stages": {},
           "use_item": False}
    for key, value in kw.items():
        if key in out:
            out[key] = value
    return out


def snapshot(enemy_hp=60, seed=(0x12345678, 0x9ABCDEF0, 0x0BADF00D, 1),
             player_moves=("MOVE_THUNDERBOLT", "MOVE_QUICK_ATTACK"),
             enemy_moves=("MOVE_WATER_GUN",), enemy_items=None, **kw):
    return {
        "schema_version": 1,
        "source_sha": be.SOURCE_SHA,
        "difficulty": kw.get("difficulty", "Normal"),
        "progress": {"milestone_id": "CAP_1", "flags": {}},
        "player": {"party": [build("SPECIES_PIKACHU", 25, player_moves, 70, 70)],
                   "active_index": 0},
        "enemy": {"trainer_id": "TRAINER_DEMO",
                  "party": [build("SPECIES_SQUIRTLE", 22, enemy_moves, enemy_hp, 60)],
                  "active_index": 0,
                  "items": dict(enemy_items or {}),
                  "ai_flags": []},
        "field": {key: None for key in be._FIELD_KEYS},
        "rng": {"state": None if seed is None else list(seed)},
        "history": {"turns": []},
        "planned_action": {"kind": "move", "move_id": "MOVE_THUNDERBOLT",
                           "target_index": None},
    }


# ---------------------------------------------------------------------------
# RNG
# ---------------------------------------------------------------------------

def _reference_sfc32(state):
    """Independent transcription of include/random.h:52 using a different
    formulation for the rotate (additive instead of shift-or)."""
    a, b, c, ctr = state
    result = (a + b + ctr) % 2 ** 32
    ctr = (ctr + 1) % 2 ** 32
    a = (b ^ (b >> 9)) % 2 ** 32
    b = (c * 9) % 2 ** 32
    rotated = ((c * 2 ** 21) + (c // 2 ** 11)) % 2 ** 32
    c = (result + rotated) % 2 ** 32
    return (a, b, c, ctr), result


class TestRng(unittest.TestCase):
    def test_first_outputs_match_hand_computed_header_values(self):
        # a=1,b=2,c=3,ctr=4 -> result = 1+2+4 = 7, then 2+27+5 = 34
        rng = be.Rng([1, 2, 3, 4])
        self.assertEqual(rng.next_u32(), 7)
        self.assertEqual(rng.next_u32(), 34)

    def test_stream_matches_independent_transcription(self):
        state = (0x12345678, 0x9ABCDEF0, 0x0BADF00D, 0x00000001)
        rng = be.Rng(list(state))
        for _ in range(64):
            state, expected = _reference_sfc32(state)
            self.assertEqual(rng.next_u32(), expected)

    def test_random_helper_domains_and_free_paths(self):
        rng = be.Rng([7, 11, 13, 17])
        before = rng.snapshot()
        self.assertFalse(be.random_percentage(rng, 0))
        self.assertTrue(be.random_percentage(rng, 100))
        self.assertEqual(rng.snapshot(), before)          # no draw when t<=0 / t>=100
        self.assertIn(be.random_uniform(rng, 0, 23), range(24))
        self.assertIn(be.random_weighted_array(rng, [0, 0, 7, 7, 3, 3]), (2, 3, 4, 5))
        self.assertTrue(be.random_chance(be.Rng([9, 9, 9, 9]), 1, 1))


# ---------------------------------------------------------------------------
# snapshot handling
# ---------------------------------------------------------------------------

class TestNormalizeSnapshot(unittest.TestCase):
    def test_game_data_revision_mismatch_fails_closed(self):
        gd = game_data()
        gd["source"] = {"commit": "different-revision"}
        result = be.evaluate_head_to_head(snapshot(), gd)
        for kind in ("player_moves", "enemy_moves", "ai_actions", "recommendation"):
            self.assertEqual(result[kind]["reason"], "source-mismatch")

    def test_history_without_seed_posterior_fails_closed(self):
        snap = snapshot()
        snap["history"]["turns"] = [{"player_action": None, "enemy_action": None,
                                      "events": [{"kind": "hit"}]}]
        result = be.evaluate_head_to_head(snap, game_data())
        for kind in ("player_moves", "enemy_moves", "ai_actions", "recommendation"):
            self.assertEqual(result[kind]["reason"], "unrepresentable-history")

    def test_idempotent(self):
        once = be.normalize_snapshot(snapshot())
        twice = be.normalize_snapshot(once)
        self.assertEqual(once, twice)

    def test_rejects_malformed(self):
        cases = []
        cases.append(None)
        cases.append([])
        bad = snapshot()
        bad.pop("player")
        cases.append(bad)
        bad = snapshot()
        bad["player"]["active_index"] = 9
        cases.append(bad)
        bad = snapshot()
        bad["player"]["party"][0]["moves"] = ["MOVE_A"] * 5
        cases.append(bad)
        bad = snapshot()
        bad["player"]["party"][0]["moves"] = ["MOVE_A", "MOVE_A"]
        cases.append(bad)
        bad = snapshot()
        bad["rng"] = {"state": [1, 2, 3]}
        cases.append(bad)
        bad = snapshot()
        bad["planned_action"] = {"kind": "dance"}
        cases.append(bad)
        bad = snapshot()
        bad["player"]["party"][0]["ivs"] = {"luck": 1}
        cases.append(bad)
        for case in cases:
            with self.assertRaises(ValueError):
                be.normalize_snapshot(case)

    def test_unknown_preserved_not_coerced(self):
        snap = snapshot()
        snap["player"]["party"][0]["pp"] = {}
        norm = be.normalize_snapshot(snap)
        # an absent PP key stays absent (unknown), it is never coerced to 0
        self.assertNotIn("MOVE_THUNDERBOLT", norm["player"]["party"][0]["pp"])
        self.assertEqual(len(norm["rng"]["state"]), 4)


# ---------------------------------------------------------------------------
# arithmetic
# ---------------------------------------------------------------------------

class TestArithmetic(unittest.TestCase):
    def test_stats_match_hand_computed_rom_values(self):
        gd = game_data()
        pikachu = be.calc_mon_stats(gd["species"]["SPECIES_PIKACHU"]["stats"],
                                    25, "NATURE_SERIOUS", IVS, EVS)
        self.assertEqual(pikachu["hp"], 60)
        self.assertEqual(pikachu["sp_attack"], ((2 * 50 + 31 + 0) * 25) // 100 + 5)
        squirtle = be.calc_mon_stats(gd["species"]["SPECIES_SQUIRTLE"]["stats"],
                                     22, "NATURE_SERIOUS", IVS, EVS)
        self.assertEqual(squirtle["sp_defense"], 39)
        # Adamant: attack *110/100, sp_attack *90/100 (src/pokemon.c:5486)
        adamant = be.calc_mon_stats(gd["species"]["SPECIES_PIKACHU"]["stats"],
                                    25, "NATURE_ADAMANT", IVS, EVS)
        self.assertEqual(adamant["attack"], 40 * 110 // 100)
        self.assertEqual(adamant["sp_attack"], 37 * 90 // 100)

    def test_damage_matches_hand_computed_rom_values(self):
        gd = game_data()
        ctx = {"chart": gd["type_chart"]}
        attacker = {"types": ["TYPE_ELECTRIC"], "level": 25, "id": "SPECIES_PIKACHU",
                    "stats": {"attack": 40, "sp_attack": 37}}
        defender = {"types": ["TYPE_WATER"], "level": 22, "id": "SPECIES_SQUIRTLE",
                    "stats": {"defense": 40, "sp_defense": 39}}
        bolt = dict(gd["moves"]["MOVE_THUNDERBOLT"])
        bolt["id"] = "MOVE_THUNDERBOLT"
        # base = 90*37*(2*25//5+2)//39//50+2 = 22, roll 0 -> 22, STAB 1.5 -> 33,
        # type 2.0 -> 66; roll 15 -> 18 -> 1.5 -> 27 -> 2.0 -> 54
        self.assertEqual(be._hit_damage(bolt, attacker, defender, ctx, False, 0), 66)
        self.assertEqual(be._hit_damage(bolt, attacker, defender, ctx, False, 15), 54)
        self.assertEqual(be._hit_damage(bolt, attacker, defender, ctx, True, 0), 98)
        # immunity short-circuit (Electric vs Ground)
        grounded = dict(defender)
        grounded["types"] = ["TYPE_GROUND"]
        self.assertEqual(be._hit_damage(bolt, attacker, grounded, ctx, False, 0), 0)

    def test_multi_hit_bounds_scale_with_strike_count(self):
        gd = game_data()
        ctx = {"chart": gd["type_chart"]}
        attacker = {"types": ["TYPE_NORMAL"], "level": 25, "id": "SPECIES_PIKACHU",
                    "stats": {"attack": 40, "sp_attack": 37}}
        defender = {"types": ["TYPE_WATER"], "level": 22, "id": "SPECIES_SQUIRTLE",
                    "stats": {"defense": 40, "sp_defense": 39}}
        kick = dict(gd["moves"]["MOVE_DOUBLE_KICK"])
        kick["id"] = "MOVE_DOUBLE_KICK"
        one = be._hit_damage(kick, attacker, defender, ctx, False, 0)
        lo, hi = be._min_max_damage(attacker, defender, kick, ctx)
        self.assertEqual(lo, 2 * be._hit_damage(kick, attacker, defender, ctx, False, 15))
        self.assertEqual(hi, 2 * be._hit_damage(kick, attacker, defender, ctx, True, 0))
        self.assertTrue(one > 0)


# ---------------------------------------------------------------------------
# KO cards
# ---------------------------------------------------------------------------

class TestKoCards(unittest.TestCase):
    def test_guaranteed_ko_is_seed_independent(self):
        # Thunderbolt min damage 54, enemy hp 54 -> certain KO, rng unknown.
        result = be.evaluate_head_to_head(snapshot(enemy_hp=54, seed=None), game_data())
        card = result["player_moves"]
        self.assertEqual(card["status"], "exact")
        bolt = [m for m in card["moves"] if m["move_id"] == "MOVE_THUNDERBOLT"][0]
        self.assertEqual(bolt["probability"], {"num": 1, "den": 1})
        self.assertEqual(bolt["method"], "seed-independent")

    def test_impossible_ko_is_seed_independent(self):
        result = be.evaluate_head_to_head(snapshot(enemy_hp=54, seed=None), game_data())
        card = result["player_moves"]
        self.assertEqual(card["status"], "exact")
        quick = [m for m in card["moves"] if m["move_id"] == "MOVE_QUICK_ATTACK"][0]
        self.assertEqual(quick["probability"], {"num": 0, "den": 1})
        self.assertEqual(quick["method"], "seed-independent")

    def test_roll_dependent_ko_without_seed_is_unknown_rng_seed(self):
        # hp 60: Thunderbolt min 54 < 60 <= max 66, so the roll decides; Quick
        # Attack can never KO.  Per-entry gating keeps the provable sibling exact
        # and marks only the roll-dependent entry unavailable.
        result = be.evaluate_head_to_head(snapshot(enemy_hp=60, seed=None), game_data())
        card = result["player_moves"]
        self.assertEqual(card["status"], "exact")
        entries = {e["move_id"]: e for e in card["moves"]}
        self.assertEqual(entries["MOVE_THUNDERBOLT"]["status"], "unavailable")
        self.assertEqual(entries["MOVE_THUNDERBOLT"]["reason"], "unknown-rng-seed")
        self.assertEqual(entries["MOVE_QUICK_ATTACK"]["status"], "exact")
        self.assertEqual(entries["MOVE_QUICK_ATTACK"]["probability"], {"num": 0, "den": 1})

    def test_roll_dependent_ko_is_known_seed(self):
        seed = (2, 0x9ABCDEF0, 0x0BADF00D, 1)
        result = be.evaluate_head_to_head(snapshot(enemy_hp=60, seed=seed), game_data())
        card = result["player_moves"]
        self.assertEqual(card["status"], "exact")
        bolt = [m for m in card["moves"] if m["move_id"] == "MOVE_THUNDERBOLT"][0]
        # the 2/16-ish roll KO is resolved by the entered seed, never as a fraction
        self.assertEqual(bolt["method"], "known-seed")
        self.assertEqual(bolt["probability"], {"num": 1, "den": 1})

    def test_seed_can_prevent_the_ko(self):
        result = be.evaluate_head_to_head(
            snapshot(enemy_hp=60, seed=(0x12345678, 0x9ABCDEF0, 0x0BADF00D, 1)),
            game_data())
        bolt = [m for m in result["player_moves"]["moves"]
                if m["move_id"] == "MOVE_THUNDERBOLT"][0]
        self.assertEqual(bolt["probability"], {"num": 0, "den": 1})

    def test_enemy_moves_condition_on_the_planned_action(self):
        result = be.evaluate_head_to_head(snapshot(seed=(5, 7, 9, 11)), game_data())
        card = result["enemy_moves"]
        self.assertEqual(card["status"], "exact")
        self.assertEqual(len(card["moves"]), 1)
        self.assertEqual(card["moves"][0]["probability"], {"num": 0, "den": 1})
        no_plan = snapshot(seed=(5, 7, 9, 11))
        no_plan["planned_action"] = None
        result = be.evaluate_head_to_head(no_plan, game_data())
        self.assertEqual(result["enemy_moves"]["status"], "unavailable")
        self.assertEqual(result["enemy_moves"]["reason"], "missing-input")
        self.assertIn("planned_action", result["enemy_moves"]["missing"])

    def test_player_switch_prevents_enemy_ko_of_turn_start_target(self):
        snap = snapshot(seed=None)
        snap["player"]["party"].append(
            build("SPECIES_SQUIRTLE", 22, ["MOVE_WATER_GUN"], 60, 60, slot_id=2))
        snap["planned_action"] = {"kind": "switch", "slot": 1}
        result = be.evaluate_head_to_head(snap, game_data())
        card = result["enemy_moves"]
        self.assertEqual(card["status"], "exact")
        self.assertEqual(card["moves"][0]["probability"], {"num": 0, "den": 1})
        self.assertEqual(card["moves"][0]["method"], "seed-independent")


# ---------------------------------------------------------------------------
# failure reasons
# ---------------------------------------------------------------------------

class TestReasons(unittest.TestCase):
    def test_missing_input_reports_dotted_snapshot_path(self):
        snap = snapshot()
        snap["enemy"]["party"][0]["level"] = None
        result = be.evaluate_head_to_head(snap, game_data())
        self.assertEqual(result["player_moves"]["status"], "unavailable")
        self.assertEqual(result["player_moves"]["reason"], "missing-input")
        self.assertIn("enemy.party[0].level", result["player_moves"]["missing"])

    def test_unsupported_move(self):
        gd = game_data()
        gd["moves"]["MOVE_WATER_GUN"]["effect"] = "EFFECT_POISON"
        result = be.evaluate_head_to_head(snapshot(), gd)
        self.assertEqual(result["ai_actions"]["reason"], "unsupported-move")
        self.assertIn("EFFECT_POISON", result["ai_actions"]["mechanics"])

    def test_unsupported_ability(self):
        snap = snapshot()
        snap["player"]["party"][0]["ability"] = "ABILITY_STATIC"
        result = be.evaluate_head_to_head(snap, game_data())
        self.assertEqual(result["player_moves"]["reason"], "unsupported-ability")
        self.assertIn("ABILITY_STATIC", result["player_moves"]["mechanics"])

    def test_unknown_ability_is_missing_not_inert(self):
        snap = snapshot()
        snap["player"]["party"][0]["ability"] = None
        result = be.evaluate_head_to_head(snap, game_data())
        self.assertEqual(result["player_moves"]["reason"], "missing-input")
        self.assertIn("player.party[0].ability", result["player_moves"]["missing"])

    def test_unsupported_item_and_field_effect(self):
        snap = snapshot(enemy_items={"ITEM_X_ATTACK": 1})
        result = be.evaluate_head_to_head(snap, game_data())
        self.assertEqual(result["ai_actions"]["reason"], "unsupported-item")
        self.assertIn("ITEM_X_ATTACK", result["ai_actions"]["mechanics"])
        snap = snapshot()
        snap["field"]["weather"] = "B_WEATHER_RAIN"
        result = be.evaluate_head_to_head(snap, game_data())
        self.assertEqual(result["player_moves"]["reason"], "unsupported-mechanic")
        self.assertIn("field.weather='B_WEATHER_RAIN'", result["player_moves"]["mechanics"])

    def test_source_mismatch(self):
        snap = snapshot()
        snap["source_sha"] = "deadbeef"
        result = be.evaluate_head_to_head(snap, game_data())
        self.assertEqual(result["status"], "unavailable")
        for key in ("player_moves", "enemy_moves", "ai_actions", "recommendation"):
            self.assertEqual(result[key]["reason"], "source-mismatch")

    def test_hard_difficulty_is_rejected(self):
        result = be.evaluate_head_to_head(snapshot(difficulty="Hard"), game_data())
        self.assertEqual(result["player_moves"]["reason"], "unsupported-difficulty")

    def test_unrepresentable_ai_flag(self):
        snap = snapshot()
        snap["enemy"]["ai_flags"] = ["AI_FLAG_TRY_TO_FAINT"]
        result = be.evaluate_head_to_head(snap, game_data())
        self.assertEqual(result["ai_actions"]["reason"], "unsupported-mechanic")
        self.assertIn("ai-flag:AI_FLAG_TRY_TO_FAINT", result["ai_actions"]["mechanics"])

    def test_trainer_items_with_entered_history_are_unrepresentable(self):
        snap = snapshot(seed=None, enemy_items={"ITEM_POTION": 1})
        snap["history"]["turns"] = [{"player_action": None, "enemy_action": None,
                                      "events": [{"kind": "damage"}]}]
        result = be.evaluate_head_to_head(snap, game_data())
        self.assertEqual(result["ai_actions"]["reason"], "unrepresentable-history")
        self.assertIn("trainer-item-ai-history", result["ai_actions"]["mechanics"])

    def test_easy_boss_level_adjustment_is_applied_before_stats(self):
        snap = snapshot(difficulty="Easy")
        gd = game_data()
        gd["trainers"][0]["is_boss"] = True
        _, ctx, gate = card_ctx(snap, gd)
        self.assertIsNone(gate.result())
        self.assertEqual(ctx["enemy_party"][0]["level"], 20)   # 22 - 2
        gd["trainers"][0]["is_boss"] = None
        card = be.evaluate_head_to_head(snap, gd)["player_moves"]
        self.assertEqual(card["reason"], "unsupported-difficulty")
        self.assertIn("Easy:unresolved-boss", card["mechanics"])

    def test_trick_room_reverses_action_order(self):
        seed = (0x12345678, 0x9ABCDEF0, 0x0BADF00D, 1)
        normal = be.evaluate_head_to_head(snapshot(seed=seed), game_data())
        room = snapshot(seed=seed)
        room["field"]["trick_room"] = True
        reversed_ = be.evaluate_head_to_head(room, game_data())
        self.assertEqual(normal["status"], "exact")
        self.assertEqual(reversed_["status"], "exact")
        self.assertNotEqual(normal["recommendation"]["actions"],
                            reversed_["recommendation"]["actions"])


# ---------------------------------------------------------------------------
# AI actions
# ---------------------------------------------------------------------------

class TestAiActions(unittest.TestCase):
    def test_probabilities_sum_to_one_with_known_seed(self):
        snap = snapshot(seed=(11, 22, 33, 44),
                        enemy_moves=("MOVE_WATER_GUN", "MOVE_QUICK_ATTACK"))
        card = be.evaluate_head_to_head(snap, game_data())["ai_actions"]
        self.assertEqual(card["status"], "exact")
        total = sum(a["probability"]["num"] for a in card["actions"])
        self.assertEqual(total, sum(a["probability"]["den"] for a in card["actions"]))
        self.assertTrue(any(a["top"] for a in card["actions"]))

    def test_single_action_is_seed_independent(self):
        card = be.evaluate_head_to_head(snapshot(seed=None), game_data())["ai_actions"]
        self.assertEqual(card["status"], "exact")
        self.assertEqual(card["method"], "seed-independent")
        self.assertEqual(card["actions"][0]["probability"], {"num": 1, "den": 1})
        self.assertEqual(card["actions"][0]["id"], "MOVE_WATER_GUN")

    def test_tie_break_without_seed_is_unknown(self):
        snap = snapshot(seed=None, enemy_moves=("MOVE_WATER_GUN", "MOVE_QUICK_ATTACK"))
        card = be.evaluate_head_to_head(snap, game_data())["ai_actions"]
        self.assertEqual(card["status"], "unavailable")
        self.assertEqual(card["reason"], "unknown-rng-seed")

    def test_trainer_item_is_selected_by_should_use_item(self):
        # Potion (20 HP): AI_ShouldHeal heals when hp < maxHP/4 and the player
        # cannot KO even after the heal (src/battle_ai_switch_items.c:2427).
        snap = snapshot(enemy_hp=10, seed=(3, 5, 7, 9), enemy_items={"ITEM_POTION": 1},
                        player_moves=("MOVE_QUICK_ATTACK",))
        snap["enemy"]["party"][0]["max_hp"] = 60
        card = be.evaluate_head_to_head(snap, game_data())["ai_actions"]
        self.assertEqual(card["status"], "exact")
        self.assertEqual(card["actions"][0]["kind"], "item")
        self.assertEqual(card["actions"][0]["id"], "ITEM_POTION")
        self.assertEqual(card["actions"][0]["probability"], {"num": 1, "den": 1})

    def test_item_is_not_used_when_the_player_still_kos_after_healing(self):
        snap = snapshot(enemy_hp=10, seed=(3, 5, 7, 9), enemy_items={"ITEM_POTION": 1})
        snap["enemy"]["party"][0]["max_hp"] = 60
        card = be.evaluate_head_to_head(snap, game_data())["ai_actions"]
        self.assertEqual(card["status"], "exact")
        self.assertEqual(card["actions"][0]["kind"], "move")


# ---------------------------------------------------------------------------
# recommendation
# ---------------------------------------------------------------------------

class TestRecommendation(unittest.TestCase):
    def test_returns_all_tied_maxima(self):
        snap = snapshot(seed=(0x12345678, 0x9ABCDEF0, 0x0BADF00D, 1))
        card = be.evaluate_head_to_head(snap, game_data())["recommendation"]
        self.assertEqual(card["status"], "exact")
        self.assertEqual(card["label"], "two-turn lookahead")
        self.assertEqual(card["scored_turns"], 2)
        self.assertEqual(len(card["actions"]), 2)       # both moves tie at 1280/7
        scores = {(a["score"]["num"], a["score"]["den"]) for a in card["actions"]}
        self.assertEqual(scores, {(1280, 7)})

    def test_switch_is_an_offered_action(self):
        snap = snapshot(seed=(2, 0x9ABCDEF0, 0x0BADF00D, 1))
        snap["player"]["party"].append(
            build("SPECIES_SQUIRTLE", 25, ["MOVE_WATER_GUN"], 60, 60, slot_id=2))
        card = be.evaluate_head_to_head(snap, game_data())["recommendation"]
        self.assertEqual(card["status"], "exact")
        kinds = {a["kind"] for a in card["actions"]}
        labels = {a["label"] for a in card["actions"]}
        self.assertTrue(kinds <= {"move", "switch"})
        self.assertTrue(labels)
        self.assertIn("MOVE_THUNDERBOLT", labels)

    def test_unknown_seed_has_no_recommendation(self):
        card = be.evaluate_head_to_head(snapshot(seed=None), game_data())["recommendation"]
        self.assertEqual(card["status"], "unavailable")
        self.assertEqual(card["reason"], "unknown-rng-seed")


# ---------------------------------------------------------------------------
# deadline
# ---------------------------------------------------------------------------

class TestDeadline(unittest.TestCase):
    def test_deadline_in_the_past_is_timeout(self):
        result = be.evaluate_head_to_head(snapshot(), game_data(), deadline=0.0)
        self.assertEqual(result["status"], "unavailable")
        for key in ("player_moves", "enemy_moves", "ai_actions", "recommendation"):
            self.assertEqual(result[key]["reason"], "timeout")
            self.assertNotIn("probability", result[key])

    def test_future_deadline_does_not_change_results(self):
        import math
        far = 10 ** 9
        a = be.evaluate_head_to_head(snapshot(), game_data())
        b = be.evaluate_head_to_head(snapshot(), game_data(), deadline=far)
        self.assertEqual(a, b)
        self.assertTrue(math.isfinite(far))


# ---------------------------------------------------------------------------
# legal actions / level cap / serialisability
# ---------------------------------------------------------------------------

class TestHelpers(unittest.TestCase):
    def test_legal_player_actions(self):
        snap = snapshot()
        snap["player"]["party"].append(
            build("SPECIES_SQUIRTLE", 22, ["MOVE_WATER_GUN"], 60, 60, slot_id=2))
        out = be.legal_player_actions(snap, game_data())
        self.assertEqual([m["move_id"] for m in out["moves"]],
                         ["MOVE_THUNDERBOLT", "MOVE_QUICK_ATTACK"])
        self.assertEqual([s["slot"] for s in out["switches"]], [1])
        snap["player"]["party"][0]["pp"] = {"MOVE_THUNDERBOLT": 0}
        out = be.legal_player_actions(snap, game_data())
        self.assertEqual([m["move_id"] for m in out["moves"]], ["MOVE_QUICK_ATTACK"])

    def test_simulate_party_level_cap_copies(self):
        party = [build("SPECIES_PIKACHU", 5, ["MOVE_THUNDERBOLT"], 20, 20),
                 {"species": "SPECIES_NONE", "level": 5},
                 build("SPECIES_SQUIRTLE", 5, ["MOVE_WATER_GUN"], 20, 20)]
        out = be.simulate_party_level_cap(party, 15, game_data())
        self.assertEqual(out[0]["level"], 15)
        self.assertEqual(out[2]["level"], 15)
        self.assertEqual(out[1]["level"], 5)
        self.assertEqual(party[0]["level"], 5)          # input untouched

    def test_results_are_json_serialisable(self):
        result = be.evaluate_head_to_head(snapshot(), game_data())
        blob = json.dumps(result)
        self.assertIn("source-transcribed", blob)

    def test_engine_version_and_reasons(self):
        self.assertEqual(be.ENGINE_VERSION, "1.3.3")
        self.assertEqual(be.SOURCE_SHA, "2576d0f3f4c861c760f0c55198e37e6d7b91683e")
        self.assertIn("unknown-rng-seed", be.REASONS)
        self.assertIn("timeout", be.REASONS)


# ---------------------------------------------------------------------------
# inert-ability allowlist
# ---------------------------------------------------------------------------

# One representative per justification group; each must be indistinguishable
# from ABILITY_NONE on the whole result.
SAMPLE_INERT = ("ABILITY_FRISK", "ABILITY_IMMUNITY", "ABILITY_CHLOROPHYLL",
                "ABILITY_UNBURDEN", "ABILITY_POWER_SPOT", "ABILITY_UNAWARE",
                "ABILITY_GUTS", "ABILITY_STEADFAST", "ABILITY_ROCK_HEAD",
                "ABILITY_PICKUP")


class TestAbilities(unittest.TestCase):
    def test_allowlisted_abilities_match_no_ability(self):
        baseline = be.evaluate_head_to_head(snapshot(), game_data())
        for ability in SAMPLE_INERT:
            snap = snapshot()
            snap["player"]["party"][0]["ability"] = ability
            snap["enemy"]["party"][0]["ability"] = ability
            self.assertEqual(be.evaluate_head_to_head(snap, game_data()), baseline,
                             "%s changed the result" % ability)

    def test_non_inert_abilities_are_rejected(self):
        # one type-immunity ability and one damage-modifier ability
        for ability in ("ABILITY_LEVITATE", "ABILITY_TECHNICIAN"):
            snap = snapshot()
            snap["enemy"]["party"][0]["ability"] = ability
            result = be.evaluate_head_to_head(snap, game_data())
            self.assertEqual(result["player_moves"]["reason"], "unsupported-ability")
            self.assertIn(ability, result["player_moves"]["mechanics"])

    def test_unknown_ability_is_still_missing_input(self):
        snap = snapshot()
        snap["enemy"]["party"][0]["ability"] = None
        result = be.evaluate_head_to_head(snap, game_data())
        self.assertEqual(result["player_moves"]["reason"], "missing-input")
        self.assertIn("enemy.party[0].ability", result["player_moves"]["missing"])

    def test_allowlist_entries_carry_a_citation_and_reason(self):
        for ability, entry in be.INERT_ABILITIES.items():
            self.assertIsInstance(entry, tuple, ability)
            citation, reason = entry
            self.assertTrue(citation.strip(), ability)
            self.assertGreater(len(reason.split()), 4, ability)
            self.assertTrue(citation.startswith(("src/", "data/")), ability)

    def test_allowlist_ids_exist_in_game_data(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "data", "game_data.json")
        if not os.path.exists(path):
            self.skipTest("data/game_data.json is not present")
        with io.open(path, encoding="utf-8") as handle:
            abilities = json.load(handle).get("abilities") or {}
        missing = sorted(set(be.INERT_ABILITIES) - set(abilities))
        self.assertEqual(missing, [], "allowlisted ids absent from game_data.json")

# ---------------------------------------------------------------------------
# AI flags: AI_FLAG_CHECK_BAD_MOVE
# ---------------------------------------------------------------------------

class TestAiFlags(unittest.TestCase):
    def test_check_bad_move_is_supported(self):
        snap = snapshot()
        snap["enemy"]["ai_flags"] = ["AI_FLAG_CHECK_BAD_MOVE"]
        result = be.evaluate_head_to_head(snap, game_data())
        self.assertEqual(result["status"], "exact")
        card = result["ai_actions"]
        self.assertEqual(card["status"], "exact")
        self.assertEqual(sum(a["probability"]["num"] for a in card["actions"]),
                         sum(a["probability"]["den"] for a in card["actions"]))

    def test_unported_flags_are_rejected_by_name(self):
        snap = snapshot()
        snap["enemy"]["ai_flags"] = ["AI_FLAG_TRY_TO_FAINT"]
        result = be.evaluate_head_to_head(snap, game_data())
        self.assertEqual(result["ai_actions"]["reason"], "unsupported-mechanic")
        self.assertIn("ai-flag:AI_FLAG_TRY_TO_FAINT", result["ai_actions"]["mechanics"])

    def test_mixed_flag_set_names_only_the_unported_flag(self):
        snap = snapshot()
        snap["enemy"]["ai_flags"] = ["AI_FLAG_CHECK_BAD_MOVE",
                                      "AI_FLAG_TRY_TO_FAINT",
                                      "AI_FLAG_CHECK_VIABILITY"]
        result = be.evaluate_head_to_head(snap, game_data())
        self.assertEqual(result["ai_actions"]["reason"], "unsupported-mechanic")
        self.assertIn("ai-flag:AI_FLAG_TRY_TO_FAINT", result["ai_actions"]["mechanics"])
        self.assertIn("ai-flag:AI_FLAG_CHECK_VIABILITY", result["ai_actions"]["mechanics"])
        self.assertNotIn("ai-flag:AI_FLAG_CHECK_BAD_MOVE",
                         result["ai_actions"]["mechanics"])

    def test_check_bad_move_penalises_an_immune_move(self):
        """src/battle_ai_main.c:778 -- a type-immune move scores 100 - 20 = 80."""
        snap = snapshot(enemy_moves=("MOVE_THUNDERBOLT", "MOVE_WATER_GUN"))
        snap["enemy"]["ai_flags"] = ["AI_FLAG_CHECK_BAD_MOVE"]
        snap["player"]["party"][0] = build("SPECIES_SANDSHREW", 25, [], 70, 70)
        _, ctx, _gate = card_ctx(snap, game_data())
        enemy = ctx["enemy_party"][0]
        player = ctx["player_party"][0]
        gun = [m for m in enemy["moves"] if m["id"] == "MOVE_WATER_GUN"][0]
        bolt = [m for m in enemy["moves"] if m["id"] == "MOVE_THUNDERBOLT"][0]
        self.assertEqual(be._ai_check_bad_move(enemy, player, gun, ctx, 100), 100)
        self.assertEqual(be._ai_check_bad_move(enemy, player, bolt, ctx, 100), 80)
        tied = be._ai_max_score_moves(enemy, player, ctx)
        self.assertEqual([m["id"] for m, _ in tied], ["MOVE_WATER_GUN"])

    def test_negative_priority_effect_hit_keeps_the_default_score(self):
        """src/battle_ai_main.c:986 is unreachable: a negative-priority move is
        always slower than the priority-0 MOVE_NONE passed to AI_IsFaster."""
        snap = snapshot(
            enemy_moves=("MOVE_WATER_GUN",),
            player_moves=("MOVE_THUNDERBOLT",))
        gd = game_data()
        gd["moves"]["MOVE_SLOW_SLAM"] = _move("Slow Slam", "TYPE_WATER", 40,
                                              "special", priority=-6)
        snap["enemy"]["party"][0]["moves"] = ["MOVE_SLOW_SLAM"]
        snap["enemy"]["party"][0]["pp"] = {"MOVE_SLOW_SLAM": 20}
        snap["enemy"]["party"][0]["hp"] = 10           # well under 40%
        _, ctx, _gate = card_ctx(snap, gd)
        enemy = ctx["enemy_party"][0]
        player = ctx["player_party"][0]
        slow = enemy["moves"][0]
        self.assertEqual(be._ai_check_bad_move(enemy, player, slow, ctx, 100), 100)

    def test_check_bad_move_makes_a_two_move_choice_seed_independent(self):
        snap = snapshot(enemy_moves=("MOVE_THUNDERBOLT", "MOVE_WATER_GUN"), seed=None)
        snap["player"]["party"][0] = build("SPECIES_SANDSHREW", 25, [], 70, 70)
        # the empty flag set never calls AI_CheckBadMove, so the immune move keeps
        # AI_SCORE_DEFAULT and the choice needs the tie-break draw
        plain = be.evaluate_head_to_head(snap, game_data())["ai_actions"]
        self.assertEqual(plain["reason"], "unknown-rng-seed")
        # with the flag the immune move scores 80, leaving a single best move
        snap["enemy"]["ai_flags"] = ["AI_FLAG_CHECK_BAD_MOVE"]
        card = be.evaluate_head_to_head(snap, game_data())["ai_actions"]
        self.assertEqual(card["status"], "exact")
        self.assertEqual([a["id"] for a in card["actions"]], ["MOVE_WATER_GUN"])
        self.assertEqual(card["actions"][0]["probability"], {"num": 1, "den": 1})

    def test_two_equally_scored_moves_still_need_a_seed(self):
        snap = snapshot(enemy_moves=("MOVE_WATER_GUN", "MOVE_DOUBLE_KICK"), seed=None)
        snap["enemy"]["ai_flags"] = ["AI_FLAG_CHECK_BAD_MOVE"]
        card = be.evaluate_head_to_head(snap, game_data())["ai_actions"]
        self.assertEqual(card["reason"], "unknown-rng-seed")


class TestEmptySlots(unittest.TestCase):
    """MOVE_NONE slots and 0-PP slots are unusable and cannot block a card."""

    def test_move_none_is_an_empty_slot(self):
        snap = snapshot(enemy_moves=("MOVE_WATER_GUN", "MOVE_NONE"))
        result = be.evaluate_head_to_head(snap, game_data())
        self.assertEqual(result["status"], "exact")
        self.assertEqual([m["move_id"] for m in result["enemy_moves"]["moves"]],
                         ["MOVE_WATER_GUN"])

    def test_move_none_is_not_offered_as_a_player_action(self):
        snap = snapshot(player_moves=("MOVE_THUNDERBOLT", "MOVE_NONE"))
        out = be.legal_player_actions(snap, game_data())
        self.assertEqual([m["move_id"] for m in out["moves"]], ["MOVE_THUNDERBOLT"])

    def test_unsupported_move_in_a_zero_pp_slot_is_ignored(self):
        """CheckMoveLimitations zeroes the slot (src/battle_util.c:1372), so the AI
        never scores or selects it and it is never used."""
        gd = game_data()
        snap = snapshot(enemy_moves=("MOVE_WATER_GUN", "MOVE_CALM_MIND"))
        snap["enemy"]["party"][0]["pp"] = {"MOVE_WATER_GUN": 20, "MOVE_CALM_MIND": 0}
        result = be.evaluate_head_to_head(snap, gd)
        self.assertEqual(result["status"], "exact")
        # a known non-zero PP on the same slot must stay unsupported
        snap["enemy"]["party"][0]["pp"]["MOVE_CALM_MIND"] = 20
        result = be.evaluate_head_to_head(snap, gd)
        self.assertEqual(result["player_moves"]["reason"], "unsupported-move")


class TestImmuneMoves(unittest.TestCase):
    """A legal move that cannot damage the target is still an exact 0 entry.

    Type immunity is decided before any RNG is drawn (``DoMoveDamageCalc``
    src/battle_util.c:10411 returns 0 for a UQ_4_12(0.0) multiplier), and
    ``IsMoveUnusable`` (include/battle_ai_util.h:60) -- the function that makes a
    slot unselectable -- tests only MOVE_NONE/MOVE_UNAVAILABLE and the limitation
    bits (``CheckMoveLimitations`` src/battle_util.c:1372).  Effectiveness is not
    part of selectability, so the card must state the 0 chance instead of
    reporting no-legal-actions.
    """

    def immune_player(self, seed=None, **kw):
        """Pikachu's Thunderbolt against a Ground enemy: 0x, seed unknown."""
        snap = snapshot(player_moves=("MOVE_THUNDERBOLT",), seed=seed, **kw)
        snap["enemy"]["party"][0] = build("SPECIES_SANDSHREW", 22,
                                           ("MOVE_WATER_GUN",), 60, 60)
        return snap

    def test_an_immune_move_is_an_exact_zero_entry(self):
        card = be.evaluate_head_to_head(self.immune_player(), game_data())["player_moves"]
        self.assertEqual(card["status"], "exact", card)
        self.assertEqual(card["method"], "seed-independent")
        entries = [m for m in card["moves"] if m["move_id"] == "MOVE_THUNDERBOLT"]
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["status"], "exact")
        self.assertEqual(entry["probability"], {"num": 0, "den": 1})
        self.assertIn("immune", entry["ko"])
        self.assertTrue(any("immune" in note for note in entry["notes"]), entry)

    def test_immune_enemy_move_gets_an_exact_zero_entry(self):
        snap = self.immune_player()
        snap["player"]["party"][0] = build("SPECIES_SANDSHREW", 25,
                                            ("MOVE_QUICK_ATTACK",), 70, 70)
        snap["enemy"]["party"][0] = build("SPECIES_PIKACHU", 22,
                                           ("MOVE_THUNDERBOLT",), 60, 60)
        snap["planned_action"] = {"kind": "move", "move_id": "MOVE_QUICK_ATTACK",
                                  "target_index": 0}
        card = be.evaluate_head_to_head(snap, game_data())["enemy_moves"]
        self.assertEqual(card["status"], "exact", card)
        self.assertEqual(card["moves"][0]["probability"], {"num": 0, "den": 1})
        self.assertIn("immune", card["moves"][0]["ko"])

    def test_no_legal_actions_needs_nothing_selectable(self):
        # a legal but immune move is selectable
        snap = self.immune_player()
        immune = be.evaluate_head_to_head(snap, game_data())["player_moves"]
        self.assertEqual(immune["status"], "exact")
        # nothing with PP and no filled slot -> nothing can be selected
        snap["player"]["party"][0]["pp"] = {"MOVE_THUNDERBOLT": 0}
        card = be.evaluate_head_to_head(snap, game_data())["player_moves"]
        self.assertEqual(card["status"], "unavailable")
        self.assertEqual(card["reason"], "no-legal-actions")
        snap["player"]["party"][0]["moves"] = ["MOVE_NONE"]
        snap["player"]["party"][0]["pp"] = {}
        card = be.evaluate_head_to_head(snap, game_data())["player_moves"]
        self.assertEqual(card["reason"], "no-legal-actions")

    def test_ai_still_selects_an_immune_move(self):
        """IsDamageMoveUnusable feeds only the damage estimate (src/battle_ai_util.c:757),
        so the choice still covers the immune move -- 80 > 0 with CHECK_BAD_MOVE
        (RETURN_SCORE_MINUS(20) src/battle_ai_main.c:779) and 100 without it."""
        for flags in ((), ("AI_FLAG_CHECK_BAD_MOVE",)):
            snap = snapshot(player_moves=("MOVE_QUICK_ATTACK",), enemy_moves=("MOVE_THUNDERBOLT",),
                            seed=None)
            snap["player"]["party"][0] = build("SPECIES_SANDSHREW", 25,
                                                ("MOVE_QUICK_ATTACK",), 70, 70)
            snap["enemy"]["ai_flags"] = list(flags)
            card = be.evaluate_head_to_head(snap, game_data())["ai_actions"]
            self.assertEqual(card["status"], "exact", card)
            self.assertEqual([a["id"] for a in card["actions"]], ["MOVE_THUNDERBOLT"])
            self.assertEqual(card["actions"][0]["kind"], "move")

    def test_the_recommendation_evaluates_a_move_that_cannot_damage(self):
        """Every legal first-turn player move is a path, immune or not; the entry
        it cannot win is only a lower score, never a skipped candidate."""
        seed = (0x12345678, 0x9ABCDEF0, 0x0BADF00D, 1)
        snap = self.immune_player(seed=seed)
        bolt = be.evaluate_head_to_head(snap, game_data())["recommendation"]
        self.assertEqual(bolt["status"], "exact", bolt)
        self.assertEqual([a["label"] for a in bolt["actions"]], ["MOVE_THUNDERBOLT"])
        self.assertEqual(bolt["scored_turns"], 2)
        snap["player"]["party"][0]["moves"] = ["MOVE_QUICK_ATTACK"]
        snap["player"]["party"][0]["pp"] = {"MOVE_QUICK_ATTACK": 20}
        quick = be.evaluate_head_to_head(snap, game_data())["recommendation"]
        self.assertEqual([a["label"] for a in quick["actions"]], ["MOVE_QUICK_ATTACK"])
        bolt_score = bolt["actions"][0]["score"]
        quick_score = quick["actions"][0]["score"]
        self.assertLess(bolt_score["num"] * quick_score["den"],
                        quick_score["num"] * bolt_score["den"])


class TestBenchSwitching(unittest.TestCase):
    """ShouldSwitch src/battle_ai_switch_items.c:1018 for [] / [CHECK_BAD_MOVE]."""

    def with_bench(self, bench_moves=("MOVE_WATER_GUN",), enemy_moves=("MOVE_WATER_GUN",),
                   bench_species="SPECIES_SQUIRTLE", bench_ability=be.NO_ABILITY):
        snap = snapshot(enemy_moves=enemy_moves)
        bench = build(bench_species, 22, list(bench_moves), 60, 60, ability=bench_ability)
        del bench["ivs"], bench["evs"]          # inactive slots need no stat data
        snap["enemy"]["party"].append(bench)
        return snap

    def test_inactive_enemy_slots_do_not_block_the_cards(self):
        snap = self.with_bench(bench_moves=("MOVE_CALM_MIND", "MOVE_ABSORB"))
        result = be.evaluate_head_to_head(snap, game_data())
        self.assertEqual(result["status"], "exact")

    def test_inert_inactive_ability_is_accepted(self):
        snap = self.with_bench(bench_ability="ABILITY_CHLOROPHYLL")
        self.assertEqual(be.evaluate_head_to_head(snap, game_data())["status"], "exact")

    def test_non_inert_inactive_ability_is_not_read_when_no_switch_is_reachable(self):
        """With no reachable switch the bench is never walked, so an unported bench
        ability cannot change any card (contract section 3: gating is per card)."""
        snap = self.with_bench(bench_ability="ABILITY_REGENERATOR")
        self.assertEqual(be.evaluate_head_to_head(snap, game_data())["status"], "exact")

    def test_a_non_inert_active_ability_still_fails_closed(self):
        snap = snapshot()
        snap["enemy"]["party"][0]["ability"] = "ABILITY_REGENERATOR"
        card = be.evaluate_head_to_head(snap, game_data())["ai_actions"]
        self.assertEqual(card["reason"], "unsupported-ability")
        self.assertIn("ABILITY_REGENERATOR", card["mechanics"])

    def test_a_reachable_switch_requires_exactly_the_bench_hp(self):
        """Every active slot is type-immune, so ShouldSwitchIfAllMovesBad can fire
        (:388) and the candidate's validity (IsValidForBattle) is needed."""
        snap = self.with_bench(enemy_moves=("MOVE_THUNDERBOLT",))
        snap["player"]["party"][0] = build("SPECIES_SANDSHREW", 25, [], 70, 70)
        snap["enemy"]["party"][1]["hp"] = None
        snap["enemy"]["party"][1]["max_hp"] = None
        card = be.evaluate_head_to_head(snap, game_data())["ai_actions"]
        self.assertEqual(card["reason"], "missing-input")
        self.assertEqual(card["missing"], ["enemy.party[1].hp"])
        snap["enemy"]["party"][1]["hp"] = 0            # known fainted -> no candidate
        self.assertEqual(be.evaluate_head_to_head(snap, game_data())["ai_actions"]["status"],
                         "exact")
        snap["enemy"]["party"][1]["hp"] = 60
        card = be.evaluate_head_to_head(snap, game_data())["ai_actions"]
        self.assertEqual(card["reason"], "unsupported-mechanic")
        self.assertIn("ai-switching:ShouldSwitchIfAllMovesBad", card["mechanics"])

    def test_three_possible_candidates_consume_a_switch_in_draw(self):
        """GetBestMonBatonPass draws Random()%3 whenever aliveCount > 2
        (src/battle_ai_switch_items.c:1341), which this engine cannot model."""
        snap = self.with_bench()
        for hp in (60, 60):
            snap["enemy"]["party"].append(
                build("SPECIES_SQUIRTLE", 22, ("MOVE_WATER_GUN",), hp, 60))
        card = be.evaluate_head_to_head(snap, game_data())["ai_actions"]
        self.assertEqual(card["reason"], "unsupported-mechanic")
        self.assertIn("ai-switching:switch-in-rng", card["mechanics"])

    def test_two_candidates_with_unknown_hp_need_no_bench_input(self):
        """aliveCount can be at most 2 with two bench slots, so the Random()%3 draw
        cannot happen and the bench is irrelevant (no HP/status needed)."""
        snap = self.with_bench()
        snap["enemy"]["party"].append(
            build("SPECIES_SQUIRTLE", 22, ("MOVE_WATER_GUN",), 60, 60))
        for member in snap["enemy"]["party"][1:]:
            member["hp"] = member["max_hp"] = None
            member["status"] = None
        result = be.evaluate_head_to_head(snap, game_data())
        self.assertEqual(result["status"], "exact")

    def test_three_unknown_hp_candidates_are_named_in_missing(self):
        snap = self.with_bench()
        for hp in (60, 60):
            snap["enemy"]["party"].append(
                build("SPECIES_SQUIRTLE", 22, ("MOVE_WATER_GUN",), hp, 60))
        for member in snap["enemy"]["party"][1:]:
            member["hp"] = member["max_hp"] = None
        card = be.evaluate_head_to_head(snap, game_data())["player_moves"]
        self.assertEqual(card["reason"], "missing-input")
        self.assertEqual(card["missing"], ["enemy.party[1].hp", "enemy.party[2].hp", "enemy.party[3].hp"])

    def test_a_revive_can_add_a_candidate_and_is_named(self):
        """A Revive adds a bench member, and therefore the Random()%3 draw on the
        following turn, so two live + one fainted candidate already fail closed."""
        snap = self.with_bench()
        for hp in (60, 0):
            snap["enemy"]["party"].append(
                build("SPECIES_SQUIRTLE", 22, ("MOVE_WATER_GUN",), hp, 60))
        snap["enemy"]["items"] = {"ITEM_REVIVE": 1}
        card = be.evaluate_head_to_head(snap, game_data())["ai_actions"]
        self.assertEqual(card["reason"], "unsupported-mechanic")
        self.assertIn("ai-switching:switch-in-rng", card["mechanics"][0])

    def test_a_baton_pass_holder_with_unknown_hp_is_named(self):
        snap = self.with_bench(bench_moves=("MOVE_BATON_PASS",))
        snap["enemy"]["party"][1]["hp"] = snap["enemy"]["party"][1]["max_hp"] = None
        card = be.evaluate_head_to_head(snap, game_data())["player_moves"]
        self.assertEqual(card["reason"], "missing-input")
        self.assertEqual(card["missing"], ["enemy.party[1].hp"])

    def test_all_moves_bad_is_named(self):
        # Electric vs Ground is a type immunity, so every slot is "bad" and the
        # ROM would take the ShouldSwitchIfAllMovesBad branch instead.
        snap = self.with_bench(enemy_moves=("MOVE_THUNDERBOLT",))
        snap["player"]["party"][0] = build("SPECIES_SANDSHREW", 25, [], 70, 70)
        card = be.evaluate_head_to_head(snap, game_data())["ai_actions"]
        self.assertEqual(card["reason"], "unsupported-mechanic")
        self.assertIn("ai-switching:ShouldSwitchIfAllMovesBad", card["mechanics"])

    def test_conditioned_enemy_move_keeps_seed_independent_result(self):
        snap = self.with_bench(enemy_moves=("MOVE_THUNDERBOLT",))
        snap["player"]["party"][0] = build("SPECIES_SANDSHREW", 25,
                                           ("MOVE_QUICK_ATTACK",), 70, 70)
        snap["planned_action"] = {"kind": "move", "move_id": "MOVE_QUICK_ATTACK",
                                  "target_index": 0}
        snap["rng"]["state"] = None
        result = be.evaluate_head_to_head(snap, game_data())
        self.assertEqual(result["ai_actions"]["reason"], "unsupported-mechanic")
        card = result["enemy_moves"]
        self.assertEqual(card["status"], "exact", card)
        self.assertEqual(card["moves"][0]["probability"], {"num": 0, "den": 1})

        snap["rng"]["state"] = [1, 2, 3, 4]
        card = be.evaluate_head_to_head(snap, game_data())["enemy_moves"]
        self.assertEqual(card["reason"], "unsupported-mechanic")

    def test_baton_pass_in_the_bench_is_named(self):
        # GetBestMonBatonPass draws Random()%3 / Random()%n when a valid candidate
        # knows Baton Pass (src/battle_ai_switch_items.c:1341).
        snap = self.with_bench(bench_moves=("MOVE_BATON_PASS",))
        card = be.evaluate_head_to_head(snap, game_data())["ai_actions"]
        self.assertEqual(card["reason"], "unsupported-mechanic")
        self.assertIn("ai-switching:baton-pass-switch-in", card["mechanics"])

    def test_an_allowed_live_bench_consumes_no_extra_rng(self):
        """The AI's draw sequence must not depend on the bench when no switch can
        happen: the same seed must pick the same move either way."""
        live = self.with_bench(bench_moves=("MOVE_CALM_MIND",))
        dead = self.with_bench(bench_moves=("MOVE_CALM_MIND",))
        dead["enemy"]["party"][1]["hp"] = 0
        for seed in (0x12345678, 7, 0xFFFFFFFF):
            snap_live = json.loads(json.dumps(live))
            snap_dead = json.loads(json.dumps(dead))
            snap_live["rng"]["state"] = [seed, 0x9ABCDEF0, 0x0BADF00D, 1]
            snap_dead["rng"]["state"] = [seed, 0x9ABCDEF0, 0x0BADF00D, 1]
            got_live = be.evaluate_head_to_head(snap_live, game_data())
            got_dead = be.evaluate_head_to_head(snap_dead, game_data())
            self.assertEqual(got_live["ai_actions"], got_dead["ai_actions"])
            self.assertEqual(got_live["enemy_moves"], got_dead["enemy_moves"])
            self.assertEqual(got_live["player_moves"], got_dead["player_moves"])


class TestPerCardGating(unittest.TestCase):
    """Contract section 3: gating is per result and per entry, never per snapshot."""

    def test_missing_planned_action_blocks_only_enemy_moves(self):
        snap = snapshot()
        snap["planned_action"] = None
        result = be.evaluate_head_to_head(snap, game_data())
        self.assertEqual(result["enemy_moves"]["reason"], "missing-input")
        self.assertIn("planned_action", result["enemy_moves"]["missing"])
        for kind in ("player_moves", "ai_actions", "recommendation"):
            self.assertEqual(result[kind]["status"], "exact", result[kind])

    def test_unreachable_enemy_bench_ability_and_moves_are_never_read(self):
        snap = snapshot()
        snap["enemy"]["party"].append(
            build("SPECIES_SQUIRTLE", 22, ("MOVE_CALM_MIND",), 60, 60,
                  ability="ABILITY_REGENERATOR"))
        for member in snap["enemy"]["party"][1:]:
            member["hp"] = member["max_hp"] = None
            member["status"] = None
        result = be.evaluate_head_to_head(snap, game_data())
        self.assertEqual(result["status"], "exact")

    def test_an_unported_player_bench_member_blocks_only_the_recommendation(self):
        snap = snapshot()
        snap["player"]["party"].append(
            build("SPECIES_SQUIRTLE", 22, ("MOVE_WATER_GUN",), 60, 60,
                  ability="ABILITY_REGENERATOR"))
        result = be.evaluate_head_to_head(snap, game_data())
        self.assertEqual(result["recommendation"]["reason"], "incomplete-search")
        self.assertIn("switch:1", result["recommendation"]["mechanics"])
        for kind in ("player_moves", "enemy_moves", "ai_actions"):
            self.assertEqual(result[kind]["status"], "exact", result[kind])

    def test_an_unported_player_move_entry_does_not_blank_the_other_entries(self):
        gd = game_data()
        gd["moves"]["MOVE_TAKE_DOWN"] = _move("Take Down", "TYPE_NORMAL", 90,
                                              "physical", recoil=25)
        snap = snapshot(player_moves=("MOVE_TAKE_DOWN", "MOVE_QUICK_ATTACK"))
        card = be.evaluate_head_to_head(snap, gd)["player_moves"]
        self.assertEqual(card["status"], "exact")
        entries = {e["move_id"]: e for e in card["moves"]}
        self.assertEqual(entries["MOVE_TAKE_DOWN"]["status"], "unavailable")
        self.assertEqual(entries["MOVE_TAKE_DOWN"]["reason"], "unsupported-move")
        self.assertTrue(any("recoil" in m for m in entries["MOVE_TAKE_DOWN"]["mechanics"]))
        self.assertEqual(entries["MOVE_QUICK_ATTACK"]["status"], "exact")

    def test_a_card_without_any_exact_entry_reports_the_entry_reason(self):
        gd = game_data()
        gd["moves"]["MOVE_REST"] = _move("Rest", "TYPE_NORMAL", 0, "status")
        snap = snapshot(player_moves=("MOVE_REST",))
        card = be.evaluate_head_to_head(snap, gd)["player_moves"]
        self.assertEqual(card["reason"], "unsupported-move")
        self.assertEqual(card["moves"][0]["status"], "unavailable")

    def test_an_unmodelled_move_flag_fails_closed(self):
        gd = game_data()
        gd["moves"]["MOVE_QUICK_ATTACK"]["flags"] = ["alwaysCriticalHit"]
        card = be.evaluate_head_to_head(snapshot(), gd)["player_moves"]
        entries = {e["move_id"]: e for e in card["moves"]}
        self.assertEqual(entries["MOVE_QUICK_ATTACK"]["status"], "unavailable")
        self.assertEqual(entries["MOVE_QUICK_ATTACK"]["reason"], "unsupported-move")
        self.assertIn("flag:alwaysCriticalHit", entries["MOVE_QUICK_ATTACK"]["mechanics"])

    def test_a_listed_move_flag_is_accepted(self):
        gd = game_data()
        gd["moves"]["MOVE_QUICK_ATTACK"]["flags"] = ["makesContact", "slicingMove"]
        self.assertEqual(be.evaluate_head_to_head(snapshot(), gd)["status"], "exact")

    def test_item_none_with_use_item_false_means_no_held_item(self):
        snap = snapshot()
        snap["player"]["party"][0]["item"] = None
        snap["player"]["party"][0]["use_item"] = False
        self.assertEqual(be.evaluate_head_to_head(snap, game_data())["status"], "exact")

    def test_item_none_with_use_item_true_is_missing_input(self):
        snap = snapshot()
        snap["player"]["party"][0]["item"] = None
        snap["player"]["party"][0]["use_item"] = True
        card = be.evaluate_head_to_head(snap, game_data())["player_moves"]
        self.assertEqual(card["reason"], "missing-input")
        self.assertIn("player.party[0].item", card["missing"])

    def test_exact_result_carries_evidence(self):
        result = be.evaluate_head_to_head(snapshot(), game_data())
        self.assertEqual(result["evidence"],
                         {"basis": "source-transcribed", "oracle_verified": False})
        self.assertEqual(result["status"], "exact")


if __name__ == "__main__":
    unittest.main()
