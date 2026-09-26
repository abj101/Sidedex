import tempfile
import unittest
import sqlite3
import json
from pathlib import Path
from unittest.mock import patch

from app import (
    apply_party_slots,
    connect,
    delete_team_config,
    list_team_configs,
    save_entry,
    save_team_config,
)
from planner_ui import calc_load_config


class StorageTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.sqlite3"
        self.db = connect(self.db_path)

    def tearDown(self):
        self.db.close()
        self.temp_dir.cleanup()

    def add(self, name, in_party=False, box_no=1):
        return save_entry(self.db, {
            "species": name, "level": 10, "ability": None, "item": None,
            "nature": None, "moves": [], "ivs": {}, "evs": {},
            "status": "alive", "in_party": in_party, "owner": None,
            "pair_id": None, "box_no": box_no,
        })

    def test_party_reorder_preserves_gaps_and_moves_outgoing_to_first_open_box(self):
        first = self.add("FIRST", in_party=True)
        second = self.add("SECOND", in_party=True)
        boxed = self.add("BOXED", box_no=2)

        apply_party_slots(self.db, [boxed, None, second, None, None, None])

        rows = {row["id"]: row for row in self.db.execute("SELECT * FROM owned")}
        self.assertEqual((rows[boxed]["in_party"], rows[boxed]["party_slot"]), (1, 0))
        self.assertEqual((rows[second]["in_party"], rows[second]["party_slot"]), (1, 2))
        self.assertEqual((rows[first]["in_party"], rows[first]["party_slot"]), (0, None))
        self.assertEqual((rows[first]["box_no"], rows[first]["box_slot"]), (1, 0))

    def test_invalid_or_unavailable_party_choices_leave_state_unchanged(self):
        member = self.add("MEMBER", in_party=True)
        unavailable = self.add("UNAVAILABLE")
        self.db.execute("UPDATE owned SET status='dead' WHERE id=?", (unavailable,))
        self.db.commit()

        with self.assertRaises(ValueError):
            apply_party_slots(self.db, [member, member, None, None, None, None])
        with self.assertRaises(ValueError):
            apply_party_slots(self.db, [unavailable, None, None, None, None, None])
        row = self.db.execute("SELECT in_party,party_slot FROM owned WHERE id=?", (member,)).fetchone()
        self.assertEqual(tuple(row), (1, 0))

    def test_invalid_config_does_not_change_party(self):
        member = self.add("MEMBER", in_party=True)
        record = {"settings": '{"party_ids":["999"],"trainer_id":"TRAINER_TEST",'
                              '"enemy_order":[],"level_cap":100,"difficulty":"Normal"}'}
        trainers = [{"trainer": {"id": "TRAINER_TEST", "party": []}, "group": "Test"}]
        with self.assertRaisesRegex(ValueError, "Saved party slots are invalid"):
            calc_load_config(self.db, record, {}, apply_party_slots, trainers)
        row = self.db.execute("SELECT in_party,party_slot FROM owned WHERE id=?", (member,)).fetchone()
        self.assertEqual(tuple(row), (1, 0))

    def test_config_load_skips_missing_member_and_keeps_gap(self):
        old = self.add("OLD", in_party=True)
        available = self.add("AVAILABLE")
        record = {"settings": json.dumps({
            "party_ids": [available, 999, None, None, None, None],
            "party_names": {"999": "Missing Pokémon"}, "trainer_id": "TRAINER_TEST",
            "enemy_order": [], "level_cap": 100, "difficulty": "Normal", "item_flags": {},
        })}
        trainers = [{"trainer": {"id": "TRAINER_TEST", "party": []}, "group": "Test"}]
        with patch("planner_ui.st.session_state", {}):
            notice = calc_load_config(self.db, record, {}, apply_party_slots, trainers)
        self.assertIn("Missing Pokémon", notice)
        rows = {row["id"]: row for row in self.db.execute("SELECT * FROM owned")}
        self.assertEqual((rows[available]["in_party"], rows[available]["party_slot"]), (1, 0))
        self.assertEqual((rows[old]["in_party"], rows[old]["party_slot"]), (0, None))

    def test_full_boxes_roll_back_party_change(self):
        party_ids = [self.add(f"PARTY{i}", in_party=True) for i in range(6)]
        for box_no in range(1, 15):
            for box_slot in range(30):
                self.db.execute(
                    "INSERT INTO owned(species, status, in_party, box_no, box_slot) VALUES(?, 'alive', 0, ?, ?)",
                    (f"BOX{box_no}_{box_slot}", box_no, box_slot),
                )
        self.db.commit()

        with self.assertRaisesRegex(ValueError, "Every Box is full"):
            apply_party_slots(self.db, [party_ids[0], party_ids[1], party_ids[2], party_ids[3], None, None])

        actual_party = self.db.execute(
            "SELECT id,party_slot FROM owned WHERE in_party=1 ORDER BY party_slot"
        ).fetchall()
        self.assertEqual([(row["id"], row["party_slot"]) for row in actual_party],
                         list(zip(party_ids, range(6))))

    def test_party_and_named_configs_persist_across_reopen(self):
        member = self.add("MEMBER", in_party=True)
        apply_party_slots(self.db, [None, member, None, None, None, None])
        config_id = save_team_config(self.db, "Boss prep", {"party": [member], "notes": "test"})
        save_team_config(self.db, "Boss prep", {"party": [member, 99]}, config_id)
        self.db.close()

        self.db = connect(self.db_path)
        row = self.db.execute("SELECT in_party,party_slot FROM owned WHERE id=?", (member,)).fetchone()
        self.assertEqual(tuple(row), (1, 1))
        configs = list_team_configs(self.db)
        self.assertEqual(configs, [{"id": config_id, "name": "Boss prep", "settings": {"party": [member, 99]}}])
        self.assertTrue(delete_team_config(self.db, config_id))
        self.assertFalse(delete_team_config(self.db, config_id))
        self.assertEqual(list_team_configs(self.db), [])

    def test_legacy_schema_migrates_box_and_party_slots(self):
        self.db.close()
        self.db_path = Path(self.temp_dir.name) / "legacy.sqlite3"
        legacy = sqlite3.connect(self.db_path)
        legacy.execute("""CREATE TABLE owned (
            id INTEGER PRIMARY KEY, species TEXT NOT NULL, status TEXT NOT NULL,
            in_party INTEGER NOT NULL, box_no INTEGER NOT NULL DEFAULT 1
        )""")
        legacy.executemany(
            "INSERT INTO owned(id,species,status,in_party,box_no) VALUES(?,?,?,?,?)",
            [(1, "PARTY_A", "alive", 1, 1), (2, "PARTY_B", "alive", 1, 1),
             (3, "BOX_A", "alive", 0, 2)],
        )
        legacy.commit()
        legacy.close()

        self.db = connect(self.db_path)

        migrated = {row["id"]: row for row in self.db.execute("SELECT * FROM owned")}
        self.assertEqual([migrated[id]["party_slot"] for id in (1, 2)], [0, 1])
        self.assertEqual((migrated[3]["box_no"], migrated[3]["box_slot"]), (2, 0))
        self.assertIn("settings", {row[1] for row in self.db.execute("PRAGMA table_info(team_configs)")})


if __name__ == "__main__":
    unittest.main()
