#!/usr/bin/env python3
"""Source-extraction regression tests for `data/game_data.json` and `data/rom_fixtures.json`."""

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_PATH = ROOT / "data" / "game_data.json"
FIXTURE_PATH = ROOT / "data" / "rom_fixtures.json"
LOCK = json.loads((ROOT / "data" / "source_lock.json").read_text(encoding="utf-8"))
SOURCE = (ROOT.parent / "pokeemerald-expansion-ref").resolve()
sys.path.insert(0, str(ROOT / "tools"))
import extract_game_data as extractor  # noqa: E402

GAME = json.loads(DATA_PATH.read_text(encoding="utf-8"))
FIXTURES = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
MOVE_CATEGORIES = {"physical", "special", "status"}
FIXTURE_KINDS = {"damage", "stats", "accuracy", "rng", "ai-score"}


def source_available():
    if not (SOURCE / "src/data/moves_info.h").is_file():
        return False
    try:
        head = subprocess.check_output(["git", "-C", str(SOURCE), "rev-parse", "HEAD"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return False
    return head == LOCK["commit"]


SOURCE_OK = source_available()


def uq4_12(value):
    return int(value * 4096 + 0.5)


def apply_modifier(modifier, value):
    """uq4_12_multiply_by_int_half_down  include/fpmath.h:70

    ((modifier * value) + UQ_4_12_ROUND - 1) >> 12 with UQ_4_12_ROUND = 2048.
    """
    return ((modifier * value) + 2047) >> 12


def multiply_modifiers(a, b):
    return ((a * b) + 2048) >> 12


class SchemaTest(unittest.TestCase):
    def test_move_descriptions_and_values_are_imported(self):
        for move_id, move in GAME["moves"].items():
            if move_id != "MOVE_NONE":
                self.assertTrue(move["description"], move_id)
            for field in ("power", "accuracy", "pp"):
                self.assertIsInstance(move[field], int, f"{move_id} {field}")

    def test_schema_version_is_2(self):
        self.assertEqual(GAME["schema_version"], 2)
        self.assertEqual(FIXTURES["source"]["commit"], LOCK["commit"])

    def test_oracle_is_honest(self):
        oracle = GAME["oracle"]
        self.assertIsInstance(oracle["available"], bool)
        self.assertTrue(oracle["reason"])
        self.assertTrue(oracle["detail"])
        self.assertIsInstance(oracle["toolchain"], dict)
        self.assertFalse(oracle["toolchain"]["rom_products_copied_into_sidedex"])
        # No oracle was established: it is only ever `available` after a real build/emulator run.
        blocked = (not any(oracle["toolchain"]["path_tools"].values())
                   and not oracle["toolchain"]["install_dir_hits"]
                   and not oracle["toolchain"]["test_dir"])
        if blocked:
            self.assertFalse(oracle["available"])
            self.assertIn("no C/ARM toolchain", oracle["reason"])

    def test_unresolved_move_fields_are_null_not_defaults(self):
        for move_id, move in GAME["moves"].items():
            if move["category"] is not None:
                self.assertIn(move["category"], MOVE_CATEGORIES, move_id)
            if move["effect"] is not None:
                self.assertTrue(move["effect"].startswith(("EFFECT_", "Z_EFFECT_")), move_id)
            if move["target"] is not None:
                self.assertTrue(move["target"].startswith("MOVE_TARGET_"), move_id)
            if move["priority"] is not None:
                self.assertIsInstance(move["priority"], int)
            if move["flags"] is not None:
                self.assertTrue(all(isinstance(flag, str) and flag for flag in move["flags"]), move_id)
            if move["crit_ratio"] is not None:
                self.assertIsInstance(move["crit_ratio"], int)
            if move["strike_count"] is not None:
                self.assertIsInstance(move["strike_count"], int)
            # include/pokemon.h:511 recoil is a percentage; an omitted table entry is 0.
            self.assertIsInstance(move["recoil"], int, move_id)
            self.assertGreaterEqual(move["recoil"], 0, move_id)
            self.assertIsInstance(move["always_critical"], bool)
            if move["secondary_effects"] is not None:
                for effect in move["secondary_effects"]:
                    self.assertIsInstance(effect["chance"], int)
                    if effect["effect"] is not None:
                        self.assertTrue(effect["effect"].startswith(("MOVE_EFFECT_", "Z_EFFECT_")))
            if move["effect_argument"] is not None:
                self.assertIsInstance(move["effect_argument"]["value"], int)
            for key in ("power", "accuracy", "pp"):
                self.assertNotEqual(move[key], "", move_id)

    def test_recoil_percentage_from_table(self):
        # MOVE_TAKE_DOWN sets .recoil = 25 (src/data/moves_info.h, EFFECT_HIT); the field is a
        # percentage of damage dealt (src/battle_script_commands.c:6012). Moves that omit it are 0.
        self.assertEqual(GAME["moves"]["MOVE_TAKE_DOWN"]["recoil"], 25)
        self.assertEqual(GAME["moves"]["MOVE_QUICK_ATTACK"]["recoil"], 0)
        self.assertEqual(GAME["moves"]["MOVE_TACKLE"]["recoil"], 0)

    def test_trainer_ai_flags_and_fixed_ivs(self):
        stats = ("hp", "attack", "defense", "sp_attack", "sp_defense", "speed")
        for trainer in GAME["trainers"]:
            self.assertIn("flags", trainer)
            if trainer["ai_flags"] is not None:
                self.assertTrue(all(flag.startswith("AI_FLAG_") for flag in trainer["ai_flags"]), trainer["id"])
            self.assertIn("party_size", trainer)
            for member in trainer["party"]:
                self.assertIn("ivs_encoding", member)
                self.assertIn("evs_encoding", member)
                # src/battle_main.c:2100 applies the packed .iv unconditionally and only applies EVs
                # when .ev != NULL, so every party member carries fixed values; nothing is rolled.
                for table, encoding, source in (("ivs", "ivs_encoding", "ivs_source"),
                                                ("evs", "evs_encoding", "evs_source")):
                    self.assertEqual(set(member[table]), set(stats), f"{trainer['id']} {table}")
                    for stat, value in member[table].items():
                        self.assertIsInstance(value, int, f"{trainer['id']} {table} {stat}")
                    self.assertIn(member[source], ("table-encoding", "table-default-0"))
                    if member[encoding] is None:
                        self.assertEqual(member[source], "table-default-0", trainer["id"])
                        self.assertTrue(all(value == 0 for value in member[table].values()), trainer["id"])
                    else:
                        self.assertEqual(member[source], "table-encoding", trainer["id"])

    def test_level_caps_ordered_and_terminated_by_max(self):
        caps = GAME["level_caps"]
        self.assertIsInstance(caps, list)
        self.assertEqual(caps[0], {"id": "CAP_1", "flag": "FLAG_BADGE01_GET", "level": 15, "label": "Badge01 Get"})
        self.assertEqual(caps[-1]["id"], "CAP_MAX")
        self.assertIsNone(caps[-1]["flag"])
        self.assertEqual(caps[-1]["level"], 100)
        self.assertEqual(GAME["level_cap_type"], "LEVEL_CAP_FLAG_LIST")
        self.assertEqual(GAME["difficulty_boss_levels"]["Easy"]["boss_level_delta"], -2)
        self.assertEqual(GAME["difficulty_boss_levels"]["Normal"]["boss_level_delta"], 0)
        self.assertIsNone(GAME["difficulty_boss_levels"]["Hard"])

    def test_sawyer_active_table_not_inactive_party_syntax(self):
        trainers = {trainer["id"]: trainer for trainer in GAME["trainers"]}
        sawyer = trainers["TRAINER_SAWYER_1"]
        self.assertEqual(sawyer["party_size"], 1)
        self.assertEqual(sawyer["party"][0]["species"], "SPECIES_GRAVELER_ALOLA")
        self.assertEqual(sawyer["party"][0]["level"], 40)
        # .iv and .ev are omitted in the table: fixed 0, with the encoding left null for provenance.
        self.assertIsNone(sawyer["party"][0]["ivs_encoding"])
        self.assertEqual(sawyer["party"][0]["ivs_source"], "table-default-0")
        self.assertTrue(all(value == 0 for value in sawyer["party"][0]["ivs"].values()))
        self.assertIsNone(sawyer["party"][0]["evs_encoding"])
        self.assertEqual(sawyer["party"][0]["evs_source"], "table-default-0")
        self.assertTrue(all(value == 0 for value in sawyer["party"][0]["evs"].values()))
        if not SOURCE_OK:
            return
        inactive = (SOURCE / "src/data/trainers.party").read_text(encoding="utf-8")
        block = re.search(r"===\s*TRAINER_SAWYER_1\s*===(.*?)(?=\n===|\Z)", inactive, re.S)
        self.assertTrue(block and re.search(r"Geodude", block.group(1), re.I) and re.search(r"Level:\s*21\b", block.group(1)))


class SourceAgreementTest(unittest.TestCase):
    def setUp(self):
        if not SOURCE_OK:
            self.skipTest(f"pinned checkout unavailable at {SOURCE}")

    def test_move_category_matches_source_table(self):
        text = extractor.strip_comments((SOURCE / "src/data/moves_info.h").read_text(encoding="utf-8"))
        sources = {}
        for key, body in extractor.blocks(text, r"\[\s*(MOVE_\w+)\s*\]\s*=\s*\{"):
            match = re.search(r"\.category\s*=\s*([^,\n]+)", body)
            found = re.search(r"DAMAGE_CATEGORY_\w+", match.group(1)) if match else None
            if found:
                sources[key] = found.group(0)
        mapping = {"DAMAGE_CATEGORY_PHYSICAL": "physical", "DAMAGE_CATEGORY_SPECIAL": "special",
                   "DAMAGE_CATEGORY_STATUS": "status"}
        checked = 0
        for move_id, move in GAME["moves"].items():
            source = sources.get(move_id) or sources.get(move.get("alias_of", ""))
            if source is None or move["category"] is None:
                continue
            checked += 1
            self.assertEqual(move["category"], mapping[source], move_id)
        self.assertGreater(checked, 800)

    def test_fixture_provenance_resolves_to_cited_token(self):
        skipped = []
        for fixture in FIXTURES["fixtures"]:
            path, line = fixture["provenance"].rsplit(":", 1)
            source_file = SOURCE / path
            if not source_file.is_file():
                skipped.append((fixture["id"], f"{path} missing"))
                continue
            lines = source_file.read_text(encoding="utf-8").splitlines()
            line = int(line)
            window = "\n".join(lines[max(0, line - 3):line + 2])
            self.assertIn(fixture["provenance_token"], window,
                          f"{fixture['id']}: {fixture['provenance']} does not contain {fixture['provenance_token']!r}")
        self.assertEqual(skipped, [], f"unresolved provenance: {skipped}")

    def test_unresolved_move_fields_have_warnings(self):
        warned = {warning["move"] for warning in GAME["warnings"] if "move" in warning}
        for move_id, move in GAME["moves"].items():
            if move.get("alias_of"):
                continue
            if move["target"] is None or move["flags"] is None:
                self.assertIn(move_id, warned, f"{move_id} has an unresolved field but no warning")
        for warning in GAME["warnings"]:
            if warning.get("field") == "effect_argument":
                self.assertIsNone(GAME["moves"][warning["move"]]["effect_argument"])
        self.assertIsNone(GAME["moves"]["MOVE_ACUPRESSURE"]["target"])
        self.assertIn("windMove", GAME["moves"]["MOVE_GUST"]["flags"])
        self.assertNotIn("ignoresKingsRock", GAME["moves"]["MOVE_GUST"]["flags"])
        self.assertEqual(GAME["moves"]["MOVE_ACUPRESSURE"]["priority"], 0)


class FixtureArithmeticTest(unittest.TestCase):
    def test_fixture_file_is_well_formed(self):
        self.assertGreaterEqual(len(FIXTURES["fixtures"]), 10)
        self.assertLessEqual(len(FIXTURES["fixtures"]), 25)
        ids = [fixture["id"] for fixture in FIXTURES["fixtures"]]
        self.assertEqual(len(ids), len(set(ids)))
        for fixture in FIXTURES["fixtures"]:
            self.assertIn(fixture["kind"], FIXTURE_KINDS, fixture["id"])
            self.assertTrue(fixture["derivation"], fixture["id"])
            self.assertTrue(fixture["provenance_token"], fixture["id"])

    def test_sfc32_fixtures(self):
        mask = 0xFFFFFFFF

        def next_state(a, b, c, ctr, stream=1):
            result = (a + b + ctr) & mask
            ctr = (ctr + stream) & mask
            a = (b ^ (b >> 9)) & mask
            b = (c * 9) & mask
            c = (result + (((c << 21) | (c >> 11)) & mask)) & mask
            return result, a, b, c, ctr

        for fixture in FIXTURES["fixtures"]:
            if fixture["kind"] != "rng":
                continue
            inputs = fixture["inputs"]
            if fixture["id"] == "rng-seedrng-first-random32":
                a = b = 0
                c, ctr = inputs["seed"], inputs["stream"]
                for _ in range(16):
                    _, a, b, c, ctr = next_state(a, b, c, ctr, inputs["stream"])
                seeded = {"a": a, "b": b, "c": c, "ctr": ctr}
                result, a, b, c, ctr = next_state(a, b, c, ctr, inputs["stream"])
                self.assertEqual(result, fixture["expected"]["random32"])
                self.assertEqual(result >> 16, fixture["expected"]["random"])
                self.assertEqual(seeded, fixture["expected"]["state_after_seed"])
            elif fixture["id"] == "rng-uniform-damage-roll":
                lo, hi, random = inputs["lo"], inputs["hi"], inputs["random"]
                self.assertEqual(lo + ((((hi - lo + 1) * random) >> 16)), fixture["expected"]["value"])
            elif fixture["id"].startswith("rng-crit-chance"):
                target = (24 * inputs["random"]) >> 16
                self.assertEqual(bool(target >= 23), fixture["expected"]["is_critical_hit"])
            else:
                state = inputs["state"]
                a, b, c, ctr = state["a"], state["b"], state["c"], state["ctr"]
                outputs = []
                for _ in range(inputs["steps"]):
                    result, a, b, c, ctr = next_state(a, b, c, ctr, inputs.get("stream", 1))
                    outputs.append(result)
                self.assertEqual(outputs, fixture["expected"]["outputs"])

    def test_accuracy_fixtures(self):
        for fixture in FIXTURES["fixtures"]:
            if fixture["kind"] != "accuracy":
                continue
            accuracy = fixture["inputs"]["accuracy_percent"]
            random = fixture["inputs"]["random"]
            target = (100 * random) >> 16
            self.assertEqual(bool(target >= 100 - accuracy), fixture["expected"]["hits"])

    def test_damage_fixtures(self):
        for fixture in FIXTURES["fixtures"]:
            if fixture["kind"] != "damage":
                continue
            inputs = fixture["inputs"]
            base = inputs["power"] * inputs["attack"] * (2 * inputs["level"] // 5 + 2) // inputs["defense"] // 50 + 2
            self.assertEqual(base, fixture["expected"]["base_damage"], fixture["id"])
            damage = base
            if inputs["is_crit"]:
                damage = apply_modifier(uq4_12(inputs["crit_multiplier"]), damage)
            damage = damage * (100 - inputs["roll_index"]) // 100
            damage = apply_modifier(uq4_12(inputs["stab"]), damage)
            if inputs["type_effectiveness"] == 0.0:
                damage = 0
            else:
                modifier = inputs.get("type_effectiveness_uq4_12")
                if modifier is None:
                    modifier = multiply_modifiers(uq4_12(inputs["type_effectiveness"]), 4096)
                damage = apply_modifier(modifier, damage)
            if inputs["burned_physical"]:
                damage = apply_modifier(2048, damage)
            self.assertEqual(damage, fixture["expected"]["final_damage"], fixture["id"])

    def test_stats_fixtures(self):
        modifiers = {"NATURE_HARDY": {}, "NATURE_ADAMANT": {"attack": 110, "sp_attack": 90}}
        for fixture in FIXTURES["fixtures"]:
            if fixture["kind"] != "stats":
                continue
            inputs = fixture["inputs"]
            base_stats, ivs, evs = inputs["base_stats"], inputs["ivs"], inputs["evs"]
            level = inputs["level"]
            expected = fixture["expected"]
            hp = ((2 * base_stats["hp"] + ivs["hp"] + evs["hp"] // 4) * level) // 100 + level + 10
            self.assertEqual(hp, expected["hp"], fixture["id"])
            for stat in ("attack", "defense", "sp_attack", "sp_defense", "speed"):
                value = ((2 * base_stats[stat] + ivs[stat] + evs[stat] // 4) * level) // 100 + 5
                value = value * modifiers[inputs["nature"]].get(stat, 100) // 100
                self.assertEqual(value, expected[stat], f"{fixture['id']} {stat}")

    def test_ai_score_fixtures(self):
        for fixture in FIXTURES["fixtures"]:
            if fixture["kind"] != "ai-score":
                continue
            inputs = fixture["inputs"]
            deltas = {"ai-prefer-strongest-ohko": 4,
                      "ai-hp-aware-high-hp-explosion": -2,
                      "ai-hp-aware-mid-hp-stat-raise": -2}
            self.assertEqual(inputs["score_before"] + deltas[fixture["id"]], fixture["expected"]["score_after"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
