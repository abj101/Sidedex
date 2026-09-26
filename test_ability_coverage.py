"""Focused checks for the conservative, source-anchored ability audit."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import build_ability_coverage as coverage


PIN = "2576d0f3f4c861c760f0c55198e37e6d7b91683e"


def fake_source(root):
    contents = {}
    contents["src/battle_main.c"] = [
        "IsTrainerDoubleBattle(gTrainerBattleOpponent_A) ? BATTLE_TYPE_DOUBLE : 0"]
    for ability, rule in coverage.RULES.items():
        contents.setdefault(rule["file"], []).extend([rule["ref_anchor"], *rule["anchors"]])
        for extra_file, anchor in rule.get("extra", []):
            contents.setdefault(extra_file, []).append(anchor)
    for relative_path, lines in contents.items():
        source = root / relative_path
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("\n".join(lines) + "\n", encoding="utf-8")


class AbilityCoverageTest(unittest.TestCase):
    def test_only_pinned_source_rules_receive_reachability_labels(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp)
            fake_source(source)
            with patch.object(coverage, "source_commit", return_value=PIN):
                rows = coverage.build(source)
        by_id = {row["ability_id"]: row for row in rows}
        for ability, rule in coverage.RULES.items():
            self.assertEqual(by_id[ability]["singles_reachability"], rule["status"])
            self.assertTrue(by_id[ability]["source_ref"])
            self.assertIn(rule["reason"], by_id[ability]["reachability_reason"])
            self.assertEqual(by_id[ability]["rom_replay"], "unverified")
        self.assertEqual(by_id["ABILITY_ADAPTABILITY"]["singles_reachability"], "unreviewed")

    def test_missing_source_anchor_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp)
            file = source / "src" / "battle_util.c"
            file.parent.mkdir(parents=True)
            file.write_text("case ABILITY_BATTERY:\n", encoding="utf-8")
            self.assertIsNone(coverage.classify("ABILITY_BATTERY", source))

    def test_non_pinned_checkout_cannot_generate_reviewed_rows(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp)
            fake_source(source)
            with patch.object(coverage, "source_commit", return_value="different-commit"):
                rows = coverage.build(source)
        self.assertTrue(rows)
        self.assertTrue(all(row["singles_reachability"] == "unreviewed" for row in rows))
        self.assertTrue(all(row["rom_replay"] == "unverified" for row in rows))


if __name__ == "__main__":
    unittest.main()
