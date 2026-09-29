"""The query trace must preserve the exact fraction and reject partial traces."""

import json
import unittest
from fractions import Fraction
from itertools import product
from pathlib import Path

import rom_query
from tools.rom_oracle import run_dynamic


DATA = json.loads((Path(__file__).parent / "data" / "game_data.json").read_text(encoding="utf-8"))

NONCRIT = [20] * 16
CRIT = [30] * 16
WN, WC = 15, 1


def _hitroll(side, move, crit, values):
    return "ROMTRACE|query|hitroll|" + "|".join(
        str(cell) for cell in (side, move, crit, *values))


def _brute_probability(hp, acc, protection):
    """Independent enumeration of the KO fraction: every hit count times every
    per-hit roll sequence, instead of the closed-form convolution."""
    outcomes = {}
    for damage in NONCRIT:
        outcomes[damage] = outcomes.get(damage, 0) + WN
    for damage in CRIT:
        outcomes[damage] = outcomes.get(damage, 0) + WC
    outcomes = {damage: ways for damage, ways in outcomes.items() if ways}
    total = 16 * (WN + WC)
    weights = rom_query.MODE_WEIGHTS[0]
    weight_den = sum(weights.values())
    probability = Fraction(0)
    for count, weight in weights.items():
        if protection and count < 2:
            continue
        steps = count - 1 if protection else count
        reaching = 0
        for combo in product(outcomes.items(), repeat=steps):
            ways, damage_total = 1, 0
            for damage, ways_per in combo:
                ways *= ways_per
                damage_total += damage
            if damage_total >= hp:
                reaching += ways
        probability += Fraction(weight, weight_den) * Fraction(reaching, total ** steps)
    return Fraction(acc, 100) * probability


class QueryParsing(unittest.TestCase):
    def _multi_hit_events(self, player="MOVE_BULLET_SEED", flags=8 | 16):
        enemy = "MOVE_ABSORB"
        seed, absorb = run_dynamic.numeric_id("moves", player), run_dynamic.numeric_id("moves", enemy)
        return [
            f"ROMTRACE|query|damage|0|{seed}|0|0|{flags}|40|150|100|16",
            f"ROMTRACE|query|hits|0|{seed}|0|2|5|{WN}|{WC}",
            _hitroll(0, seed, 0, NONCRIT),
            _hitroll(0, seed, 1, CRIT),
            f"ROMTRACE|query|damage|1|{absorb}|0|1600|0|2|8|100|1",
            f"ROMTRACE|query|ai|move|{absorb}",
            "ROMTRACE|query|complete", "ROMTRACE|complete|0",
        ]

    def test_multi_hit_move_composes_the_exact_ko_fraction(self):
        snapshot = {"player": {"party": [{"moves": ["MOVE_BULLET_SEED"]}]},
                    "enemy": {"party": [{"moves": ["MOVE_ABSORB"], "hp": 90}]}}
        row = rom_query.parse(self._multi_hit_events(), snapshot, DATA)["player_moves"]["moves"][0]
        self.assertEqual(row["status"], "engine-exact")
        self.assertEqual(row["method"], "engine-enumerated-multi-hit")
        self.assertEqual(row["damage_range"], {"min": 40, "max": 150})
        self.assertEqual(row["crit_denominator"], WN + WC)
        self.assertEqual(row["probability"], {"num": 241453, "den": 1310720})
        self.assertEqual(row["probability"], {
            "num": _brute_probability(90, 100, False).numerator,
            "den": _brute_probability(90, 100, False).denominator})

    def test_full_hp_sash_needs_the_cumulative_before_the_last_hit(self):
        snapshot = {"player": {"party": [{"moves": ["MOVE_BULLET_SEED"]}]},
                    "enemy": {"party": [{"moves": ["MOVE_ABSORB"], "hp": 60}]}}
        card = rom_query.parse(self._multi_hit_events(flags=8 | 16 | 2), snapshot, DATA)["player_moves"]
        row = card["moves"][0]
        expected = _brute_probability(60, 100, True)
        self.assertEqual(row["status"], "engine-exact")
        self.assertEqual(row["probability"], {"num": expected.numerator, "den": expected.denominator})
        self.assertEqual(row["probability"], {"num": 1543, "den": 5120})
        self.assertIn("Focus Sash", row["notes"][0])

    def test_enumerated_multi_hit_without_rolls_is_an_engine_error(self):
        snapshot = {"player": {"party": [{"moves": ["MOVE_BULLET_SEED"]}]},
                    "enemy": {"party": [{"moves": ["MOVE_ABSORB"], "hp": 90}]}}
        events = [line for line in self._multi_hit_events()
                  if not line.startswith("ROMTRACE|query|hitroll")]
        with self.assertRaisesRegex(ValueError, "engine-error"):
            rom_query.parse(events, snapshot, DATA)

    def test_exact_fraction_sash_and_ai(self):
        player = "MOVE_THUNDER_SHOCK"
        enemy = "MOVE_ABSORB"
        snapshot = {"player": {"party": [{"moves": [player]}]},
                    "enemy": {"party": [{"moves": [enemy]}]}}
        events = [
            f"ROMTRACE|query|damage|0|{run_dynamic.numeric_id('moves', player)}|240|6400|0|30|45|75|4",
            f"ROMTRACE|query|damage|1|{run_dynamic.numeric_id('moves', enemy)}|0|1600|2|20|25|100|1",
            f"ROMTRACE|query|ai|move|{run_dynamic.numeric_id('moves', enemy)}",
            "ROMTRACE|query|complete", "ROMTRACE|complete|0",
        ]
        cards = rom_query.parse(events, snapshot, DATA)
        self.assertEqual(cards["player_moves"]["moves"][0]["probability"], {"num": 240, "den": 6400})
        self.assertEqual(cards["player_moves"]["moves"][0]["damage_range"], {"min": 30, "max": 45})
        self.assertEqual(cards["enemy_moves"]["moves"][0]["probability"], {"num": 0, "den": 1600})
        self.assertIn("Focus Sash", cards["enemy_moves"]["moves"][0]["notes"][0])
        self.assertEqual(cards["ai_actions"]["actions"][0]["id"], enemy)
        with self.assertRaisesRegex(ValueError, "did not complete"):
            rom_query.parse(events[:-1], snapshot, DATA)

    def test_ai_item_choice_is_labelled(self):
        player, enemy = "MOVE_THUNDER_SHOCK", "MOVE_ABSORB"
        item = run_dynamic.numeric_id("items", "ITEM_HYPER_POTION")
        snapshot = {"player": {"party": [{"moves": [player]}]},
                    "enemy": {"party": [{"moves": [enemy]}]}}
        events = [
            f"ROMTRACE|query|damage|0|{run_dynamic.numeric_id('moves', player)}|240|6400|0|30|45|75|4",
            f"ROMTRACE|query|damage|1|{run_dynamic.numeric_id('moves', enemy)}|0|1600|0|20|25|100|1",
            f"ROMTRACE|query|ai|item|{item}",
            "ROMTRACE|query|complete", "ROMTRACE|complete|0",
        ]
        card = rom_query.parse(events, snapshot, DATA)["ai_actions"]
        self.assertEqual(card["actions"][0]["kind"], "item")
        self.assertEqual(card["actions"][0]["id"], "ITEM_HYPER_POTION")
        self.assertEqual(card["actions"][0]["label"], rom_query.item_label(DATA, "ITEM_HYPER_POTION"))
        self.assertEqual(card["predicted_action"]["kind"], "item")

    def test_post_switch_context_active_records_policy_and_faints(self):
        snapshot = {"player": {"party": [{"moves": ["MOVE_ABSORB"], "hp": 75, "max_hp": 100}]},
                    "enemy": {"party": [{"moves": ["MOVE_BULLET_SEED"], "hp": 90, "max_hp": 120}]}}
        events = self._multi_hit_events()
        events[0:0] = [
            "ROMTRACE|critical-policy|item-ability-only|battle-script-filtered",
            "ROMTRACE|query|context|post-switch",
            f"ROMTRACE|query|active|0|1|{run_dynamic.numeric_id('species', 'SPECIES_CHARIZARD')}|75|138|6|6|8|6|6|6|6",
            f"ROMTRACE|query|active-move|0|0|{run_dynamic.numeric_id('moves', 'MOVE_BULLET_SEED')}",
            f"ROMTRACE|query|active|1|0|{run_dynamic.numeric_id('species', 'SPECIES_GYARADOS')}|90|155|6|6|6|6|6|6|6",
            f"ROMTRACE|query|active-move|1|0|{run_dynamic.numeric_id('moves', 'MOVE_ABSORB')}",
            f"ROMTRACE|faint|1|1|2|{run_dynamic.numeric_id('species', 'SPECIES_BLASTOISE')}",
        ]
        cards = rom_query.parse(events, snapshot, DATA)
        self.assertEqual(cards["critical_policy"], "item-ability-only")
        self.assertTrue(cards["critical_policy_verified"])
        self.assertEqual(cards["query_context"], "post-switch")
        self.assertEqual(cards["active"][0]["party_slot"], 1)
        self.assertEqual(cards["active"][0]["hp"], 75)
        self.assertEqual(cards["active"][0]["stat_stages"], [6, 6, 8, 6, 6, 6, 6])
        self.assertEqual(cards["faints"][0]["party_slot"], 2)

        current_state_events = [
            event.replace("ROMTRACE|query|context|post-switch",
                          "ROMTRACE|query|context|current-state")
            for event in events
        ]
        self.assertEqual(rom_query.parse(current_state_events, snapshot, DATA)["query_context"],
                         "current-state")

        # A wrapper claim about the ELF is not evidence that the engine's
        # critical-hit filter ran for this action.
        unverified = [event for event in events if not event.startswith("ROMTRACE|critical-policy|")]
        unverified.append("ROMTRACE|runner|critical-policy|item-ability-only|verified-elf:test")
        cards = rom_query.parse(unverified, snapshot, DATA)
        self.assertFalse(cards["critical_policy_verified"])


class SplitQueryApi(unittest.TestCase):
    """Contract section 7: separate versioned QUERY_DAMAGE / QUERY_AI_ACTION paths."""

    PLAYER = "MOVE_THUNDER_SHOCK"
    ENEMY = "MOVE_ABSORB"

    def _snapshot(self, player_hp=100, enemy_hp=90):
        return {"player": {"party": [{"moves": [self.PLAYER], "hp": player_hp,
                                        "max_hp": player_hp}]},
                "enemy": {"party": [{"moves": [self.ENEMY], "hp": enemy_hp,
                                       "max_hp": enemy_hp}]}}

    def _events(self):
        seed = run_dynamic.numeric_id("moves", self.PLAYER)
        absorb = run_dynamic.numeric_id("moves", self.ENEMY)
        return [
            f"ROMTRACE|query|damage|0|{seed}|240|6400|0|30|45|75|4",
            f"ROMTRACE|query|damage|1|{absorb}|0|1600|0|20|25|100|1",
            f"ROMTRACE|query|ai|move|{absorb}",
            "ROMTRACE|query|complete", "ROMTRACE|complete|0",
        ]

    def test_damage_and_ai_are_separate_versioned_paths(self):
        events = self._events()
        damage = rom_query.parse_damage(events, self._snapshot(), DATA)
        self.assertEqual(damage["query_schema"], "sidedex-query-damage-v1")
        self.assertEqual(damage["decode_schema"], "decode-v1")
        self.assertIn("player_moves", damage)
        self.assertNotIn("ai_actions", damage)
        self.assertEqual(damage["player_moves"]["moves"][0]["probability"], {"num": 240, "den": 6400})

        ai = rom_query.parse_ai_action(events, DATA)
        self.assertEqual(ai["query_schema"], "sidedex-query-ai-action-v1")
        self.assertNotIn("player_moves", ai)
        self.assertEqual(ai["ai_actions"]["label"], "deterministic")
        self.assertFalse(ai["ai_actions"]["distribution"])
        self.assertEqual(ai["ai_actions"]["predicted_action"]["id"], self.ENEMY)

    def test_critical_policy_label_is_carried_and_never_conflated(self):
        normal = self._events() + ["ROMTRACE|critical-policy|normal|real-crit-rolls"]
        result = rom_query.parse_damage(normal, self._snapshot(), DATA, expected_policy="normal")
        self.assertEqual(result["critical_policy"], "normal")
        self.assertTrue(result["critical_policy_verified"])
        self.assertEqual(result["enemy_moves"]["critical_policy"], "normal")
        # The legacy four-card wrapper reports the observed label as-is.
        self.assertEqual(rom_query.parse(normal, self._snapshot(), DATA)["critical_policy"],
                         "normal")
        # A requested policy that the trace does not echo fails closed.
        with self.assertRaisesRegex(ValueError, "critical policy mismatch"):
            rom_query.parse_damage(normal, self._snapshot(), DATA,
                                   expected_policy="item-ability-only")
        with self.assertRaisesRegex(ValueError, "unsupported critical policy"):
            rom_query.parse_damage(
                self._events() + ["ROMTRACE|critical-policy|always-crit|battle-script-filtered"],
                self._snapshot(), DATA)

    def test_rom_outcome_weights_are_consumed_and_fail_closed(self):
        seed = run_dynamic.numeric_id("moves", self.PLAYER)
        absorb = run_dynamic.numeric_id("moves", self.ENEMY)
        head = [
            f"ROMTRACE|query|damage|0|{seed}|0|38400|0|30|45|100|24",
            f"ROMTRACE|query|damage|1|{absorb}|0|1600|0|20|25|100|1",
        ]
        tail = [f"ROMTRACE|query|ai|move|{absorb}",
                "ROMTRACE|query|complete", "ROMTRACE|complete|0"]
        complete = head + [f"ROMTRACE|query|outcomes|0|{seed}|1|0|1|1000000|2|0|2|3|100|0|750000|5|250000"] + tail
        row = rom_query.parse_damage(complete, self._snapshot(enemy_hp=5), DATA)["player_moves"]["moves"][0]
        self.assertEqual(row["outcomes_source"], "rom")
        self.assertEqual(row["method"], "engine-enumerated-multi-hit")
        self.assertEqual(row["probability"], {"num": 3, "den": 4})
        self.assertEqual(row["distribution"][0],
                         {"remaining_hp": 0, "damage": 5, "weight": 750000})
        # A truncated enumeration (declared two branches, one present) fails closed.
        truncated = head + [f"ROMTRACE|query|outcomes|0|{seed}|1|0|1|1000000|2|0|2|3|100|0|750000"] + tail
        with self.assertRaisesRegex(ValueError, "lost a branch"):
            rom_query.parse_damage(truncated, self._snapshot(enemy_hp=5), DATA)
        # A refused enumeration is an explicit unsupported-mechanic row.
        refused = head + [f"ROMTRACE|query|outcomes|0|{seed}|17|0|0|1000000|0|0|1|1|0"] + tail
        row = rom_query.parse_damage(refused, self._snapshot(enemy_hp=5), DATA)["player_moves"]["moves"][0]
        self.assertEqual(row["status"], "unavailable")
        self.assertEqual(row["reason"], "unsupported-mechanic")

    def test_query_cards_publish_without_rollout_completion(self):
        # The trace carries no transition records (no `phase|N|after-turn`, no
        # `action|`, no `rng|`), so card availability cannot depend on a rollout.
        events = self._events()
        self.assertFalse(any("after-turn" in event or event.startswith("ROMTRACE|action|")
                             or event.startswith("ROMTRACE|rng|") for event in events))
        cards = rom_query.parse(events, self._snapshot(), DATA)
        self.assertEqual(cards["player_moves"]["status"], "engine-exact")
        self.assertEqual(cards["enemy_moves"]["status"], "engine-exact")
        self.assertEqual(cards["ai_actions"]["status"], "engine-fixed-seed")

    def test_unsupported_mechanic_fails_closed_with_a_reason(self):
        seed = run_dynamic.numeric_id("moves", self.PLAYER)
        absorb = run_dynamic.numeric_id("moves", self.ENEMY)
        events = [
            f"ROMTRACE|query|damage|0|{seed}|0|0|1|0|0|0|0",
            f"ROMTRACE|query|damage|1|{absorb}|0|1600|0|20|25|100|1",
            f"ROMTRACE|query|ai|move|{absorb}",
            "ROMTRACE|query|complete", "ROMTRACE|complete|0",
        ]
        damage = rom_query.parse_damage(events, self._snapshot(), DATA)
        row = damage["player_moves"]["moves"][0]
        self.assertEqual(row["status"], "unavailable")
        self.assertEqual(row["reason"], "unsupported-mechanic")
        self.assertTrue(row["detail"])
        self.assertNotIn("probability", row)
        self.assertEqual(damage["unsupported"][0]["move_id"], self.PLAYER)

    def test_token_is_frozen_and_identity_is_checked(self):
        import dataclasses

        token = rom_query.DecisionStateToken(
            handle="a" * 32, sha256="b" * 64, bytes=1234,
            identity={"test_elf_sha256": "C" * 64, "worker_sha256": "D" * 64,
                      "rom_hook_protocol": 8},
            rng={"main": [1, 2, 3, 4], "secondary": [5, 6, 7, 8]},
            request={})
        before = token.fingerprint()
        self.assertEqual(len(before), 64)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            token.turn = 3
        self.assertEqual(token.fingerprint(), before)

        class _Client:
            rom_sha256 = "C" * 64
            worker_sha256 = "D" * 64

        rom_query._assert_identity(_Client(), token, 8)
        with self.assertRaisesRegex(ValueError, "identity-mismatch"):
            rom_query._assert_identity(_Client(), token, 7)

    def test_latency_percentiles_are_nearest_rank(self):
        stats = rom_query.latency_percentiles([10.0, 20.0, 30.0, 40.0])
        self.assertEqual(stats["count"], 4)
        self.assertEqual(stats["p50"], 20.0)
        self.assertEqual(stats["p95"], 40.0)
        self.assertEqual(rom_query.latency_percentiles([])["p50"], None)


if __name__ == "__main__":
    unittest.main()
