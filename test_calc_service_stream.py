import unittest
import tempfile
from pathlib import Path
from unittest import mock

import calc_service
import rom_eval


class PartialResultTest(unittest.TestCase):
    def test_invalidate_fences_in_flight_result(self):
        snapshot = {"schema_version": 2, "source_sha": "source"}
        game_data = {"source": {"commit": "source"}}
        key = calc_service.snapshot_key(snapshot)
        final = {"status": "engine-fixed-seed", **{
            kind: {"status": "engine-fixed-seed"} for kind in rom_eval._CARDS}}

        def evaluate(*_args, **_kwargs):
            calc_service.invalidate()
            return final

        calc_service.invalidate()
        with mock.patch.object(rom_eval, "evaluate", evaluate):
            self.assertIs(calc_service._run(key, snapshot, game_data), final)
        self.assertIsNone(calc_service.cached(snapshot))
        old_generation = calc_service._GENERATION["value"]
        calc_service.invalidate()
        with mock.patch.object(rom_eval, "evaluate", lambda *_args, **_kwargs: final):
            self.assertIs(calc_service._run(key, snapshot, game_data, old_generation), final)
        self.assertIsNone(calc_service.cached(snapshot))

    def test_cancelled_timeout_is_not_cached(self):
        snapshot = {"schema_version": 2, "source_sha": "source"}
        game_data = {"source": {"commit": "source"}}
        key = calc_service.snapshot_key(snapshot)
        timeout = {"status": "unavailable", "reason": "timeout"}
        result = {kind: timeout for kind in rom_eval._CARDS}
        calc_service.invalidate()
        calc_service._CURRENT["key"] = None  # navigated away mid-run
        with mock.patch.object(rom_eval, "evaluate", lambda *_a, **_k: result):
            calc_service._run(key, snapshot, game_data)
        self.assertIsNone(calc_service.cached(snapshot))

    def test_two_result_disk_cache_survives_memory_reset(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            calc_service, "_DISK_PATH", Path(directory) / "cache.json"
        ):
            calc_service.invalidate()
            snapshots = [{"schema_version": 2, "source_sha": str(index)} for index in range(3)]
            for index, snapshot in enumerate(snapshots):
                calc_service._save_disk(calc_service.snapshot_key(snapshot), {"value": index})
            calc_service._CACHE.clear()
            calc_service._DISK_CACHE.clear()
            calc_service._DISK_LOADED = False
            self.assertIsNone(calc_service.cached(snapshots[0]))
            self.assertEqual(calc_service.cached(snapshots[1])["value"], 1)
            self.assertEqual(calc_service.cached(snapshots[2])["value"], 2)
            calc_service.invalidate()

    def test_rom_key_changes_when_evaluator_source_changes(self):
        snapshot = {"schema_version": 2, "source_sha": "source"}
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "rom_eval.py"
            source.write_text("old = 1\n", encoding="utf-8")
            with mock.patch.object(calc_service, "_ROM_CODE_FILES", (source,)), \
                    mock.patch.object(calc_service, "_ROM_CODE_STAMP", {}):
                before = calc_service.snapshot_key(snapshot)
                self.assertEqual(calc_service.snapshot_key(snapshot), before)
                source.write_text("new = 22\n", encoding="utf-8")
                self.assertNotEqual(calc_service.snapshot_key(snapshot), before)

    def test_poll_sees_refinement_then_final_result(self):
        snapshot = {"schema_version": 2, "source_sha": "source"}
        game_data = {"source": {"commit": "source"}}
        key = calc_service.snapshot_key(snapshot)
        partial = {"status": "refining", "_meta": {"completed_samples": 1}}
        final = {"status": "engine-fixed-seed", **{
            kind: {"status": "engine-fixed-seed"} for kind in rom_eval._CARDS}}

        def evaluate(_snapshot, _data, *, publish, **_kwargs):
            publish(partial)
            self.assertIs(calc_service.poll(snapshot), partial)
            return final

        calc_service.invalidate()
        try:
            calc_service._CURRENT["key"] = key
            with mock.patch.object(rom_eval, "evaluate", evaluate):
                self.assertIs(calc_service._run(key, snapshot, game_data), final)
            self.assertIs(calc_service.poll(snapshot), final)
            self.assertEqual(calc_service.status()["refining"], 0)
        finally:
            calc_service.invalidate()


if __name__ == "__main__":
    unittest.main()
