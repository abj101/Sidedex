"""Tests for strict ROM identity checks and the low-level mGBA JSONL client."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import rom_bridge


ROOT = Path(__file__).resolve().parent
PINNED_ROM = Path(os.environ.get("SIDEDEX_ROM_PATH", ""))
PINNED_WORKER = Path(os.environ.get(
    "SIDEDEX_ROM_WORKER_EXE",
    ROOT / "native" / "rom_worker" / "build" / "mingw" / "rom_worker.exe",
))
TRACE_ELF = Path(os.environ.get("SIDEDEX_TRACE_ELF_PATH", ""))
TRACE_ELF_SHA256 = "5B14FD4BE4E14EF443D6288D3A5AC6AD8DD5D7524FD34ADAE1EAE2DA4A16E002"
EXPECTED_TRACE = [
    "ROMTRACE|ability|1|22",
    "ROMTRACE|action|0|move|261|1",
    "ROMTRACE|action|1|move|33|0",
    "ROMTRACE|hp|0|105|88",
    "ROMTRACE|action|1|switch|1",
    "ROMTRACE|final|0|1|88|105|0|65|5|261|0|0|0|14|10|5|15",
    "ROMTRACE|final|1|4|99|99|0|22|6|33|0|0|0|34|15|15|5",
    "ROMTRACE|rng|-941908513|-1199513784|1513372196|6",
]


class IdentityTests(unittest.TestCase):
    def test_rejects_missing_rom_before_worker_launch(self):
        with self.assertRaises(rom_bridge.RomIdentityError):
            rom_bridge.RomWorkerClient("missing-worker.exe", "missing-rom.gba")

    def test_rejects_rom_hash_mismatch_before_worker_launch(self):
        with tempfile.TemporaryDirectory() as temp:
            rom = Path(temp) / "test.gba"
            rom.write_bytes(b"not the pinned ROM")
            with self.assertRaisesRegex(rom_bridge.RomIdentityError, "SHA-256"):
                rom_bridge.RomWorkerClient("missing-worker.exe", rom)

    def test_rejects_source_lock_mismatch_before_worker_launch(self):
        with tempfile.TemporaryDirectory() as temp:
            rom = Path(temp) / "test.gba"
            rom.write_bytes(b"rom placeholder")
            lock = Path(temp) / "source_lock.json"
            lock.write_text(json.dumps({"commit": "0" * 40}), encoding="utf-8")
            with patch.object(rom_bridge, "sha256_file", return_value=rom_bridge.ROM_SHA256):
                with self.assertRaisesRegex(rom_bridge.RomIdentityError, "source lock commit"):
                    rom_bridge.RomWorkerClient("missing-worker.exe", rom, source_lock_path=lock)

    def test_local_source_lock_matches_module_pin(self):
        lock = json.loads((ROOT / "data" / "source_lock.json").read_text(encoding="utf-8"))
        self.assertEqual(lock["commit"], rom_bridge.SOURCE_SHA)


class ProtocolTests(unittest.TestCase):
    def test_reply_parser_rejects_duplicate_keys_and_wrong_types(self):
        with self.assertRaises(rom_bridge.WorkerProtocolError):
            rom_bridge._parse_reply('{"protocol":1,"protocol":1,"ok":true}')
        with self.assertRaises(rom_bridge.WorkerProtocolError):
            rom_bridge._parse_reply('{"protocol":1,"ok":1}')
        with self.assertRaises(rom_bridge.WorkerProtocolError):
            rom_bridge._parse_reply('[]')

    def test_worker_error_is_a_typed_error(self):
        with self.assertRaises(rom_bridge.WorkerResponseError) as raised:
            rom_bridge._parse_reply('{"protocol":1,"ok":false,"error":"invalid_request"}')
        self.assertEqual(raised.exception.code, "invalid_request")

    def test_memory_and_frame_arguments_are_validated_before_write(self):
        client = object.__new__(rom_bridge.RomWorkerClient)
        with self.assertRaises(ValueError):
            client.run_frames(True)
        with self.assertRaises(ValueError):
            client.run_frames(rom_bridge.MAX_FRAMES_PER_REQUEST + 1)
        with self.assertRaises(ValueError):
            client.read_memory(-1)
        with self.assertRaises(ValueError):
            client.read_memory(0x02000000, 3)

    def test_request_timeout_terminates_the_worker(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            rom = temp / "test.gba"
            rom.write_bytes(b"placeholder ROM for protocol test")
            digest = rom_bridge.sha256_file(rom)
            lock = temp / "source_lock.json"
            lock.write_text(json.dumps({"commit": rom_bridge.SOURCE_SHA}), encoding="utf-8")
            fake_worker = r"""
import json, sys, time
rom = sys.argv[-1]
print(json.dumps({
    "protocol": 1, "ok": True,
    "worker": "sidedex-mgba-worker/0.1.0",
    "mgba": "0.10.5+26b7884", "platform": "gba",
    "rom_title": "TEST", "rom_code": "TEST",
    "rom_bytes": __import__("os").path.getsize(rom), "frame_count": 0
}), flush=True)
sys.stdin.readline()
time.sleep(3)
"""
            with patch.object(rom_bridge, "ROM_SHA256", digest):
                client = rom_bridge.RomWorkerClient(
                    sys.executable,
                    rom,
                    source_lock_path=lock,
                    timeout=0.2,
                    expected_artifact_sha256=digest,
                    worker_args=("-c", fake_worker),
                )
                with self.assertRaises(rom_bridge.WorkerTimeoutError):
                    client.status_request()
                self.assertFalse(client.is_running)

    def test_persistent_worker_client_against_actual_rom_when_available(self):
        if not PINNED_WORKER.is_file() or not PINNED_ROM.is_file():
            self.skipTest("build the mGBA worker and set SIDEDEX_ROM_PATH for the real-ROM client smoke")
        with rom_bridge.RomWorkerClient(PINNED_WORKER, PINNED_ROM) as client:
            first = client.status
            self.assertEqual(first["worker"], rom_bridge.WORKER_VERSION)
            self.assertEqual(first["mgba"], rom_bridge.MGBA_VERSION)
            self.assertEqual(first["source_sha"], rom_bridge.SOURCE_SHA)
            self.assertEqual(first["rom_sha256"], rom_bridge.ROM_SHA256)
            self.assertEqual(client.rom_sha256, rom_bridge.ROM_SHA256)
            before = first["frame_count"]
            self.assertEqual(client.run_frames(2)["frame_count"], before + 2)
            saved_bytes = client.save_state()
            self.assertGreater(saved_bytes, 0)
            after_run = client.run_frames(4)["frame_count"]
            self.assertEqual(client.load_state()["frame_count"], before + 2)
            self.assertGreaterEqual(after_run, before + 6)
            self.assertIsInstance(client.read_memory(0x02000000, 4), int)
            self.assertEqual(client.reset()["platform"], "gba")

    def test_persistent_worker_captures_pinned_test_elf_trace_when_available(self):
        if not TRACE_ELF.is_file() or not PINNED_WORKER.is_file():
            self.skipTest("set SIDEDEX_TRACE_ELF_PATH and build the mGBA worker for the test-ELF trace check")
        with rom_bridge.RomWorkerClient(
            PINNED_WORKER, TRACE_ELF, expected_artifact_sha256=TRACE_ELF_SHA256, timeout=10
        ) as client:
            self.assertEqual(client.rom_sha256, TRACE_ELF_SHA256)
            events = []
            for _ in range(20):
                events.extend(client.run_frames(60)["trace_events"])
                if events and events[-1].startswith("ROMTRACE|rng|"):
                    break
            self.assertEqual(events, EXPECTED_TRACE)


if __name__ == "__main__":
    unittest.main()
