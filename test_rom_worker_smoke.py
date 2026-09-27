"""Smoke test the real mGBA worker when a built worker and ROM are available.

This verifies emulator lifecycle primitives only. It intentionally does not
assert or synthesize battle outcomes.
"""

import json
import os
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
WORKER = Path(os.environ.get(
    "SIDEDEX_ROM_WORKER_EXE",
    ROOT / "native" / "rom_worker" / "build" / "mingw" / "rom_worker.exe",
))
ROM = Path(os.environ.get("SIDEDEX_ROM_PATH", "")) if os.environ.get("SIDEDEX_ROM_PATH") else None


@unittest.skipUnless(WORKER.is_file(), f"mGBA worker executable is missing: {WORKER}")
@unittest.skipUnless(ROM is not None and ROM.is_file(), "set SIDEDEX_ROM_PATH to a ROM artifact")
class RomWorkerSmokeTest(unittest.TestCase):
    def test_persistent_core_run_reset_save_load_and_memory(self):
        commands = [
            {"protocol": 1, "op": "status"},
            {"protocol": 99, "op": "status"},
            {"protocol": 1, "op": "read_memory", "address": 0x02000000, "width": 4},
            {"protocol": 1, "op": "run_frames", "frames": 3},
            {"protocol": 1, "op": "save_state"},
            {"protocol": 1, "op": "run_frames", "frames": 5},
            {"protocol": 1, "op": "load_state"},
            {"protocol": 1, "op": "reset"},
            {"protocol": 1, "op": "write_memory", "address": 0x0203FFFC, "data": "01020304"},
            {"protocol": 1, "op": "read_memory", "address": 0x0203FFFC, "width": 4},
            {"protocol": 1, "op": "write_memory", "address": 0x01FFFFFF, "data": "00"},
            {"protocol": 1, "op": "write_memory", "address": 0x0203FFFF, "data": "0001"},
            {"protocol": 1, "op": "write_memory", "address": 0x02000000, "data": "0"},
            {"protocol": 1, "op": "write_memory", "address": 0x02000000, "data": "AF"},
            {"protocol": 1, "op": "write_memory", "address": 0x02000000, "data": "00" * 2049},
            {"protocol": 1, "op": "quit"},
        ]
        process = subprocess.run(
            [str(WORKER), "--rom", str(ROM)],
            input="".join(json.dumps(command) + "\n" for command in commands),
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        replies = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual(len(replies), len(commands) + 1)  # initial status plus one per request
        self.assertTrue(all(reply.get("ok") for reply in replies[:2] + replies[3:11] + replies[16:]), replies)
        self.assertEqual(replies[2], {"protocol": 1, "ok": False, "error": "invalid_request"})
        self.assertTrue(all(reply == {"protocol": 1, "ok": False, "error": "invalid_memory_write"}
                            for reply in replies[11:16]), replies)

        (initial, status, rejected, memory, after_run, saved, after_more,
         restored, reset, write, written_memory,
         out_of_range, crosses_boundary, odd_hex, uppercase_hex, oversized, quit_reply) = replies
        self.assertEqual(initial["worker"], "sidedex-mgba-worker/0.1.2")
        self.assertEqual(initial["mgba"], "0.10.5+26b7884")
        self.assertEqual(initial["platform"], "gba")
        self.assertGreater(initial["rom_bytes"], 0)
        self.assertEqual(status["frame_count"], initial["frame_count"])
        self.assertEqual(memory["width"], 4)
        self.assertIsInstance(memory["value"], int)
        self.assertEqual(after_run["frame_count"], status["frame_count"] + 3)
        self.assertGreater(saved["state_bytes"], 0)
        self.assertEqual(after_more["frame_count"], after_run["frame_count"] + 5)
        self.assertEqual(restored["frame_count"], after_run["frame_count"])
        self.assertEqual(reset["platform"], "gba")
        self.assertEqual(write["platform"], "gba")
        self.assertEqual(written_memory["value"], 0x04030201)
        self.assertTrue(quit_reply["ok"])


if __name__ == "__main__":
    unittest.main()
