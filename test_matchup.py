import unittest

from matchup import matchup, recommend_swaps
from planner_ui import matchup_indicator_state


class MatchupTest(unittest.TestCase):
    def test_enemy_card_indicator_states_compare_known_move_coverage(self):
        data = {
            "species": {
                "PLAYER": {"types": ["FIRE"], "learnable_moves": ["ROCK", "NORMAL"]},
                "ENEMY": {"types": ["GRASS"], "learnable_moves": ["FIRE", "WATER", "TACKLE"]},
            },
            "moves": {
                "ROCK": {"type": "ROCK", "power": 40},
                "FIRE": {"type": "FIRE", "power": 40},
                "NORMAL": {"type": "NORMAL", "power": 40},
                "WATER": {"type": "WATER", "power": 40},
                "TACKLE": {"type": "NORMAL", "power": 40},
            },
            "type_chart": {
                "ROCK": {"GRASS": 0.5}, "FIRE": {"FIRE": 0.25, "GRASS": 1},
                "NORMAL": {"GRASS": 1, "FIRE": 1}, "WATER": {"FIRE": 2},
            },
        }
        player = {"species": "PLAYER", "moves": ["ROCK"]}
        enemy = {"species": "ENEMY", "moves": ["FIRE"]}
        self.assertEqual(matchup_indicator_state(player, enemy, data), "player")

        player["moves"] = ["NORMAL"]
        enemy["moves"] = ["WATER"]
        self.assertEqual(matchup_indicator_state(player, enemy, data), "enemy")

        player["moves"] = ["NORMAL"]
        enemy["moves"] = ["TACKLE"]
        self.assertEqual(matchup_indicator_state(player, enemy, data), "neutral")

        player["moves"] = []
        self.assertEqual(matchup_indicator_state(player, enemy, data), "unknown")

    def test_live_box_immunity_improves_baseline_and_equal_swaps_tie(self):
        data = {
            "species": {
                "FIRE": {"types": ["FIRE"]},
                "GHOST_A": {"types": ["GHOST"]},
                "GHOST_B": {"types": ["GHOST"]},
                "NORMAL_ENEMY": {"types": ["NORMAL"]},
                "BUG": {"types": ["BUG"]},
                "GRASS_ENEMY": {"types": ["GRASS"]},
            },
            "moves": {
                "EMBER": {"type": "FIRE", "power": 40},
                "SHADOW": {"type": "GHOST", "power": 40},
                "TACKLE": {"type": "NORMAL", "power": 40},
                "STATUS_BUG": {"type": "BUG", "power": 0},
            },
            "type_chart": {
                "FIRE": {"FIRE": 0.5, "GHOST": 1, "NORMAL": 1},
                "GHOST": {"FIRE": 1, "GHOST": 1, "NORMAL": 0},
                "NORMAL": {"FIRE": 1, "GHOST": 0, "NORMAL": 1},
                "BUG": {"GRASS": 2},
            },
        }
        party = [{"species": "FIRE", "moves": ["EMBER"]}]
        box = [
            {"species": "GHOST_A", "moves": ["SHADOW"]},
            {"species": "GHOST_B", "moves": ["SHADOW"]},
        ]
        enemy = {"species": "NORMAL_ENEMY", "moves": ["TACKLE"]}

        result = recommend_swaps(party, box, [enemy], data)

        self.assertEqual(result["baseline"], {"covered": 0, "total": 1, "uncovered": ["NORMAL_ENEMY"]})
        self.assertEqual([swap["score"] for swap in result["suggestions"]], [1, 1])
        self.assertTrue(all(swap["replaced"] is None for swap in result["suggestions"]))
        self.assertTrue(all("immune to TACKLE" in swap["reasons"][0]["reasons"][0] for swap in result["suggestions"]))
        self.assertEqual(result["label"], "type coverage only")
        self.assertEqual(matchup(box[0], enemy, data)["outgoing_moves"][0]["effectiveness"], 0)

        full_party = [box[0]] * 6
        harmful_only = recommend_swaps(full_party, [party[0]], [enemy], data)
        self.assertEqual(harmful_only["suggestions"], [])

        status_candidate = {"species": "BUG", "moves": ["STATUS_BUG"]}
        grass_enemy = {"species": "GRASS_ENEMY", "moves": []}
        self.assertEqual(matchup(status_candidate, grass_enemy, data)["outgoing_moves"], [])
        self.assertEqual(recommend_swaps([], [status_candidate], [grass_enemy], data)["suggestions"], [])


if __name__ == "__main__":
    unittest.main()
