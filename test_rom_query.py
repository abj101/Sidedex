"""The query trace must preserve the damage ranges and reject partial traces."""

import json
import unittest
from pathlib import Path

import rom_query
from tools.rom_oracle import run_dynamic


DATA = json.loads((Path(__file__).parent / "data" / "game_data.json").read_text(encoding="utf-8"))

class QueryParsing(unittest.TestCase):
    def _multi_hit_events(self, player="MOVE_BULLET_SEED", flags=8):
        enemy = "MOVE_ABSORB"
        seed, absorb = run_dynamic.numeric_id("moves", player), run_dynamic.numeric_id("moves", enemy)
        return [
            f"ROMTRACE|query|damage|0|{seed}|0|0|{flags}|40|150|100|16",
            f"ROMTRACE|query|damage|1|{absorb}|0|0|0|2|8|100|1",
            f"ROMTRACE|query|ai|move|{absorb}",
            "ROMTRACE|query|complete", "ROMTRACE|complete|0",
        ]

    def test_multi_hit_total_range(self):
        # Each hit is summed in-engine, so only the range is reported.
        events = self._multi_hit_events()
        snapshot = {"player": {"party": [{"moves": ["MOVE_BULLET_SEED"]}]},
                    "enemy": {"party": [{"moves": ["MOVE_ABSORB"], "hp": 90}]}}
        row = rom_query.parse(events, snapshot, DATA)["player_moves"]["moves"][0]
        self.assertEqual(row["status"], "engine-exact")
        self.assertEqual(row["damage_range"], {"min": 40, "max": 150})
        self.assertEqual(row["crit_denominator"], 16)
        self.assertNotIn("probability", row)

    def test_damage_ranges_and_ai(self):
        player = "MOVE_THUNDER_SHOCK"
        enemy = "MOVE_ABSORB"
        snapshot = {"player": {"party": [{"moves": [player]}]},
                    "enemy": {"party": [{"moves": [enemy]}]}}
        events = [
            f"ROMTRACE|query|damage|0|{run_dynamic.numeric_id('moves', player)}|0|0|0|30|45|75|4",
            f"ROMTRACE|query|damage|1|{run_dynamic.numeric_id('moves', enemy)}|0|0|0|20|25|100|1",
            f"ROMTRACE|query|ai|move|{run_dynamic.numeric_id('moves', enemy)}",
            "ROMTRACE|query|complete", "ROMTRACE|complete|0",
        ]
        cards = rom_query.parse(events, snapshot, DATA)
        self.assertEqual(cards["player_moves"]["moves"][0]["damage_range"], {"min": 30, "max": 45})
        self.assertEqual(cards["enemy_moves"]["moves"][0]["damage_range"], {"min": 20, "max": 25})
        self.assertEqual(cards["ai_actions"]["actions"][0]["id"], enemy)
        with self.assertRaisesRegex(ValueError, "did not complete"):
            rom_query.parse(events[:-1], snapshot, DATA)

    def test_ai_item_choice_is_labelled(self):
        player, enemy = "MOVE_THUNDER_SHOCK", "MOVE_ABSORB"
        item = run_dynamic.numeric_id("items", "ITEM_HYPER_POTION")
        snapshot = {"player": {"party": [{"moves": [player]}]},
                    "enemy": {"party": [{"moves": [enemy]}]}}
        events = [
            f"ROMTRACE|query|damage|0|{run_dynamic.numeric_id('moves', player)}|0|0|0|30|45|75|4",
            f"ROMTRACE|query|damage|1|{run_dynamic.numeric_id('moves', enemy)}|0|0|0|20|25|100|1",
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
            f"ROMTRACE|query|damage|0|{seed}|0|0|0|30|45|75|4",
            f"ROMTRACE|query|damage|1|{absorb}|0|0|0|20|25|100|1",
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
        self.assertEqual(damage["player_moves"]["moves"][0]["damage_range"], {"min": 30, "max": 45})

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
            f"ROMTRACE|query|damage|1|{absorb}|0|0|0|20|25|100|1",
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
