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
TRACE_ELF_SHA256 = "44BDBE5AA7A071064A1DCA20C411B0E3AB56C8E4D8FCA00857F3B50D56C5BD93"
EXPECTED_TRACE_COUNT = 170
EXPECTED_TRACE_LINES = ROOT / "tools" / "rom_oracle" / "expected_trace.raw.txt"


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

    def test_reply_parser_accepts_bounded_large_trace_response(self):
        reply = {
            "protocol": rom_bridge.PROTOCOL_VERSION,
            "ok": True,
            "trace_events": ["ROMTRACE|" + "x" * 100 for _ in range(170)],
            "trace_overflow": False,
        }
        wire = json.dumps(reply, separators=(",", ":"))
        self.assertGreater(len(wire.encode("utf-8")), 4096)
        self.assertEqual(len(rom_bridge._parse_reply(wire)["trace_events"]), 170)

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

    def test_write_memory_validates_bytes_and_ewram_range_before_write(self):
        client = object.__new__(rom_bridge.RomWorkerClient)
        requests = []
        client._request = lambda *args, **kwargs: requests.append((args, kwargs)) or {"ok": True}
        invalid = (
            (-1, b"x"),
            (0x02000000, bytearray(b"x")),
            (0x02000000, b""),
            (0x02000000, b"x" * 2049),
            (0x0203FFFF, b"xy"),
            (0x02040000, b"x"),
            (True, b"x"),
        )
        for address, data in invalid:
            with self.subTest(address=address, data_type=type(data).__name__), self.assertRaises(ValueError):
                client.write_memory(address, data)
        self.assertEqual(requests, [])

        client.write_memory(0x02000010, bytes((0, 15, 160, 255)))
        self.assertEqual(
            requests,
            [(('write_memory',), {"address": 0x02000010, "data": "000fa0ff"})],
        )

    def test_write_memory_sends_hex_and_validates_status_reply(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            rom = temp / "test.gba"
            rom.write_bytes(b"placeholder ROM for protocol test")
            digest = rom_bridge.sha256_file(rom)
            lock = temp / "source_lock.json"
            lock.write_text(json.dumps({"commit": rom_bridge.SOURCE_SHA}), encoding="utf-8")
            fake_worker = r"""
import json, os, sys
rom = sys.argv[-1]
status = {
    "protocol": 1, "ok": True,
    "worker": "sidedex-mgba-worker/0.1.4", "mgba": "0.10.5+26b7884",
    "platform": "gba", "rom_title": "TEST", "rom_code": "TEST",
    "rom_bytes": os.path.getsize(rom), "frame_count": 7,
}
print(json.dumps(status), flush=True)
request = json.loads(sys.stdin.readline())
print(json.dumps({**status, "op": request["op"], "address": request["address"], "data": request["data"]}), flush=True)
sys.stdin.readline()
print(json.dumps({"protocol": 1, "ok": True}), flush=True)
"""
            with patch.object(rom_bridge, "ROM_SHA256", digest):
                with rom_bridge.RomWorkerClient(
                    sys.executable,
                    rom,
                    source_lock_path=lock,
                    expected_artifact_sha256=digest,
                    expected_worker_sha256=rom_bridge.sha256_file(sys.executable),
                    worker_args=("-c", fake_worker),
                ) as client:
                    reply = client.write_memory(0x02000010, bytes((0, 15, 160, 255)))
                    self.assertEqual(reply["op"], "write_memory")
                    self.assertEqual(reply["address"], 0x02000010)
                    self.assertEqual(reply["data"], "000fa0ff")
                    self.assertEqual(client.status["frame_count"], 7)

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
    "worker": "sidedex-mgba-worker/0.1.4",
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
                    expected_worker_sha256=rom_bridge.sha256_file(sys.executable),
                    worker_args=("-c", fake_worker),
                )
                with self.assertRaises(rom_bridge.WorkerTimeoutError):
                    client.status_request()
                self.assertFalse(client.is_running)

    def test_batch_turns_reuses_checkpoint_and_validates_reply_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            rom = temp / "test.gba"
            rom.write_bytes(b"placeholder ROM for batch test")
            digest = rom_bridge.sha256_file(rom)
            lock = temp / "source_lock.json"
            lock.write_text(json.dumps({"commit": rom_bridge.SOURCE_SHA}), encoding="utf-8")
            fake_worker = r"""
import json, os, sys
rom = sys.argv[-1]
status = {"protocol":1,"ok":True,"worker":"sidedex-mgba-worker/0.1.4","mgba":"0.10.5+26b7884","platform":"gba","rom_title":"TEST","rom_code":"TEST","rom_bytes":os.path.getsize(rom),"frame_count":0}
print(json.dumps(status), flush=True)
for line in sys.stdin:
    req = json.loads(line)
    if req["op"] == "save_state":
        out = {"protocol":1,"ok":True,"state_bytes":123}
    elif req["op"] == "batch_turns":
        out = {"protocol":1,"ok":True,"request_id":req["request_id"],"source_sha":req["source_sha"],"engine_elf_sha256":req["engine_elf_sha256"],"worker_sha256":req["worker_sha256"],"turns":[{"complete":True,"frames":17,"events":["ROMTRACE|complete|1"],"trace_overflow":False} for _ in req["payloads"].split("|")]}
    else:
        out = {"protocol":1,"ok":True}
    print(json.dumps(out), flush=True)
    if req["op"] == "quit": break
"""
            with patch.object(rom_bridge, "ROM_SHA256", digest):
                with rom_bridge.RomWorkerClient(
                    sys.executable, rom, source_lock_path=lock,
                    expected_artifact_sha256=digest,
                    expected_worker_sha256=rom_bridge.sha256_file(sys.executable),
                    worker_args=("-c", fake_worker),
                ) as client:
                    client.save_state()
                    result = client.batch_turns(
                        0x02000100, [b"abcd", b"efgh"], mailbox_bytes=4,
                        engine_elf_sha256=digest,
                        deadline_ms=int(__import__("time").monotonic() * 1000) + 5000,
                        request_id="batch-1",
                    )
                    self.assertEqual(result["protocol_version"], 3)
                    self.assertEqual(len(result["turns"]), 2)
                    self.assertEqual([turn["frames"] for turn in result["turns"]], [17, 17])
                    self.assertGreaterEqual(result["elapsed_ms"], 0)
                    with self.assertRaises(ValueError):
                        client.batch_turns(
                            0x02000100, [b"abc"], mailbox_bytes=4,
                            engine_elf_sha256=digest, deadline_ms=0, request_id="bad",
                        )

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
            for _ in range(200):
                response = client.run_frames(60)
                self.assertIs(response["trace_overflow"], False)
                events.extend(response["trace_events"])
                if events and events[-1].startswith("ROMTRACE|rng|2|"):
                    break
            self.assertEqual(len(events), EXPECTED_TRACE_COUNT)
            expected = EXPECTED_TRACE_LINES.read_bytes()
            self.assertEqual(("\n".join(events) + "\n").encode("ascii"), expected)


if __name__ == "__main__":
    unittest.main()
