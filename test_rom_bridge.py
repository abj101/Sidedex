"""Tests for strict ROM identity checks and the low-level mGBA JSONL client."""

import base64
import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.rom_oracle import make_sdr2_vectors as ref

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

# The fake workers below read their status and slot identity from the environment
# so the values are built from the active pin and a coordinated identity flip
# cannot leave stale literals behind.
FAKE_STATUS = json.dumps({
    "protocol": rom_bridge.PROTOCOL_VERSION, "ok": True, "platform": "gba",
    "rom_title": "TEST", "rom_code": "TEST", "frame_count": 0,
    "worker": rom_bridge.WORKER_VERSION, "mgba": rom_bridge.MGBA_VERSION,
    "supported_wire_protocols": [rom_bridge.PROTOCOL_VERSION],
    "state_schema": rom_bridge.STATE_SCHEMA,
    "transition_schema": rom_bridge.TRANSITION_SCHEMA_V2,
    "decode_schema": rom_bridge.DECODE_SCHEMA,
})
FAKE_SLOT_IDENTITY = json.dumps({
    "worker_version": rom_bridge.WORKER_VERSION, "mgba_version": rom_bridge.MGBA_VERSION,
    "wire_protocol": rom_bridge.PROTOCOL_VERSION, "save_format": rom_bridge.SAVE_FORMAT_ID,
})
FAKE_ENV = {"FAKE_STATUS": FAKE_STATUS, "FAKE_SLOT_IDENTITY": FAKE_SLOT_IDENTITY}


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

    @staticmethod
    def _sdr2(request_id, *, boundary=1, status=0):
        """A CRC-correct SDR2 buffer, built with the reference encoder."""
        if status:
            records = [ref.typed(11, [ref.entry(ref.UNSUPPORTED, reason=status, source=1)])]
            return ref.build(records, request_id=request_id, status=status, boundary=0)
        if boundary == 3:
            records = (ref.common(None, 3, terminal=1)
                       + [ref.typed(10, [ref.entry(ref.TERMINAL, result=1, reason=0)])])
            return ref.build(records, request_id=request_id, turn=3, boundary=3)
        return ref.build(ref.common(ref.CHOICE, 2), request_id=request_id, turn=2, boundary=1)

    def _v3_client(self):
        client = object.__new__(rom_bridge.RomWorkerClient)
        client.rom_sha256 = rom_bridge.ROM_SHA256
        client.worker_sha256 = rom_bridge.WORKER_SHA256
        client.wire_protocol = rom_bridge.PROTOCOL_VERSION
        client.worker_version = rom_bridge.WORKER_VERSION
        client._worker_id = "worker-v3|pid=1|start=1|artifact=" + rom_bridge.ROM_SHA256.lower()
        client._timeout = 5.0
        client._has_checkpoint = True
        return client

    def _v3_reply(self, client, request_id, *, boundary=1, status=0, child=True, **turn_overrides):
        token = int.from_bytes(hashlib.sha256(request_id.encode("ascii")).digest()[:2], "little")
        turn = {"frames": 50, "elapsed_ms": 12.5, "save_ms": 1.0, "restore_ms": 0.2,
                "result": base64.b64encode(self._sdr2(token, boundary=boundary, status=status)).decode("ascii")}
        if child:
            turn["child"] = {"handle": "a" * 32, "bytes": 512, "sha256": "b" * 64,
                             "identity": {"worker_version": rom_bridge.WORKER_VERSION,
                                          "mgba_version": rom_bridge.MGBA_VERSION,
                                          "artifact_sha256": rom_bridge.ROM_SHA256,
                                          "wire_protocol": rom_bridge.PROTOCOL_VERSION,
                                          "save_format": rom_bridge.SAVE_FORMAT_ID}}
        turn.update(turn_overrides)
        sent = {}

        def fake_request(op, _timeout=None, **fields):
            sent.update(fields)
            return {"request_id": fields["request_id"], "source_sha": fields["source_sha"],
                    "engine_elf_sha256": fields["engine_elf_sha256"],
                    "worker_sha256": fields["worker_sha256"], "_wire_bytes": 256, "turns": [turn]}

        client._request = fake_request
        return client, sent

    def _run_v3(self, client, request_id="v3-case"):
        return client.batch_turns_v3(
            0x02000100, 0x02001000, 8192, [b"x" * 876], mailbox_bytes=876,
            engine_elf_sha256=rom_bridge.ROM_SHA256,
            deadline_ms=int(time.monotonic() * 1000) + 5000, request_id=request_id)

    def test_v9_ready_parser_uses_announced_buffers(self):
        ready = rom_bridge.parse_v9_ready([
            "ROMTRACE|ready|33554560|876|9|3|33556000|8192",
        ])
        self.assertEqual(ready["address"], 0x02000080)
        self.assertEqual(ready["result_address"], 0x02000620)
        self.assertEqual(ready["result_capacity"], 8192)
        with self.assertRaises(rom_bridge.WorkerProtocolError):
            rom_bridge.parse_v9_ready(["ROMTRACE|ready|33554560|876|9|3|33554500|8192"])
        with self.assertRaises(rom_bridge.WorkerProtocolError):
            rom_bridge.parse_v9_ready(["ROMTRACE|ready|33554560|876|9|3|33556000|8191"])

    def test_v3_batch_returns_verified_children_and_ipc_metrics(self):
        client, sent = self._v3_reply(self._v3_client(), "v3-case")
        response = self._run_v3(client)
        self.assertEqual(sent["mailbox_count"], 1)
        self.assertEqual(response["turns"][0]["child"]["owner_worker"], client._worker_id)
        self.assertTrue(response["turns"][0]["child"]["portable"])
        self.assertEqual(response["ipc_response_bytes"], 256)
        self.assertEqual(response["turn_p95_ms"], 12.5)
        records = response["turns"][0]["binary"]["records"]
        self.assertEqual(len(records["party"]), 12)
        self.assertEqual(records["legal"][0],
                         {"kind": 0, "id": 33, "pp_or_species": 25, "target_or_slot": 1})

    def test_v3_batch_rejects_a_bad_sdr2_result_with_the_contract_code(self):
        client, _ = self._v3_reply(self._v3_client(), "v3-case")
        client._request = lambda *args, **kwargs: {
            "request_id": kwargs["request_id"], "source_sha": kwargs["source_sha"],
            "engine_elf_sha256": kwargs["engine_elf_sha256"], "worker_sha256": kwargs["worker_sha256"],
            "_wire_bytes": 64,
            "turns": [{"frames": 1, "elapsed_ms": 1.0, "save_ms": 0.1, "restore_ms": 0.1,
                       "result": base64.b64encode(b"not sdr2").decode("ascii")}]}
        with self.assertRaises(rom_bridge.WorkerResponseError) as raised:
            self._run_v3(client)
        self.assertEqual(raised.exception.code, "sdr2-length")

    def test_v3_batch_surfaces_the_tag11_status_code(self):
        # status 1 / reason 1 / source 1 is the pinned incomplete-state failure.
        client, _ = self._v3_reply(self._v3_client(), "v3-case", status=1, child=False)
        with self.assertRaises(rom_bridge.WorkerResponseError) as raised:
            self._run_v3(client)
        self.assertEqual(raised.exception.code, "incomplete-state")

    def test_v3_batch_terminal_turn_has_no_child(self):
        client, _ = self._v3_reply(self._v3_client(), "v3-case", boundary=3, child=False)
        response = self._run_v3(client)
        self.assertIsNone(response["turns"][0]["child"])
        self.assertEqual(response["turns"][0]["binary"]["header"]["boundary"], 3)

    def test_v3_batch_nonterminal_turn_requires_a_saved_child(self):
        client, _ = self._v3_reply(self._v3_client(), "v3-case", boundary=1, child=False)
        with self.assertRaises(rom_bridge.WorkerProtocolError):
            self._run_v3(client)

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
            rom_bridge._parse_reply(json.dumps({"protocol": rom_bridge.PROTOCOL_VERSION,
                                                "ok": False, "error": "invalid-request"}))
        self.assertEqual(raised.exception.code, "invalid-request")
        for code in ("stale-handle", "identity-mismatch", "deadline-exceeded", "worker-failed"):
            with self.assertRaises(rom_bridge.WorkerResponseError) as raised:
                rom_bridge._parse_reply(json.dumps(
                    {"protocol": rom_bridge.PROTOCOL_VERSION, "ok": False, "error": code}))
            self.assertEqual(raised.exception.code, code)

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

    def test_write_memory_validates_bytes_and_region_before_write(self):
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
            (0x03000000, b"x" * 2049),
            (0x03007FFF, b"xy"),
            (0x03008000, b"x"),
            (0x01FFFFFF, b"x"),
            (True, b"x"),
        )
        for address, data in invalid:
            with self.subTest(address=address, data_type=type(data).__name__), self.assertRaises(ValueError):
                client.write_memory(address, data)
        self.assertEqual(requests, [])

        client.write_memory(0x02000010, bytes((0, 15, 160, 255)))
        client.write_memory(0x03000000, bytes((1, 2)))
        self.assertEqual(
            requests,
            [
                (('write_memory',), {"address": 0x02000010, "data": "000fa0ff"}),
                (('write_memory',), {"address": 0x03000000, "data": "0102"}),
            ],
        )

    def test_read_memory_block_bounds_are_checked_before_write(self):
        client = object.__new__(rom_bridge.RomWorkerClient)
        requests = []
        client._request = lambda *args, **kwargs: requests.append((args, kwargs)) or {
            "address": kwargs["address"], "length": kwargs["length"],
            "data": "ab" * kwargs["length"],
        }
        for address, length in (
            (0x0203FFF0, 32),
            (0x02000000, rom_bridge.MAX_MEMORY_BLOCK_BYTES + 1),
            (0x03007FF0, 32),
            (0x01FFFFFF, 4),
            (0x02000000, 0),
            (True, 4),
        ):
            with self.subTest(address=address, length=length), self.assertRaises(ValueError):
                client.read_memory_block(address, length)
        self.assertEqual(requests, [])
        self.assertEqual(client.read_memory_block(0x02000000, 4), b"\xab" * 4)
        self.assertEqual(client.read_memory_block(0x03000000, 2), b"\xab" * 2)

    def test_set_keys_and_run_frames_keys_validation(self):
        client = object.__new__(rom_bridge.RomWorkerClient)
        requests = []
        client._request = lambda op, **kwargs: requests.append((op, kwargs)) or (
            {"keys": kwargs["keys"]} if op == "set_keys" else {"trace_events": [], "trace_overflow": False})
        for keys in (-1, rom_bridge.MAX_KEYS_MASK + 1, True, "0x1"):
            with self.subTest(keys=keys), self.assertRaises(ValueError):
                client.set_keys(keys)
            with self.subTest(keys=keys, op="run_frames"), self.assertRaises(ValueError):
                client.run_frames(1, keys=keys)
        self.assertEqual(requests, [])
        self.assertEqual(client.set_keys(0x11)["keys"], 0x11)
        client.run_frames(2, keys=0x8)
        client.run_frames(2)
        self.assertEqual(
            requests,
            [("set_keys", {"keys": 0x11}), ("run_frames", {"frames": 2, "keys": 0x8}),
             ("run_frames", {"frames": 2})],
        )

    def test_blob_text_and_state_name_validation(self):
        client = object.__new__(rom_bridge.RomWorkerClient)
        for blob in ("", "!!!", "A" * (rom_bridge.MAX_BLOB_BASE64_BYTES + 1), b"AAAA"):
            with self.subTest(blob=blob[:8]), self.assertRaises(ValueError):
                client._validate_blob_text(blob)
        self.assertEqual(client._validate_blob_text("QUJD"), "QUJD")
        with self.assertRaises(ValueError):
            client._validate_state_name("bad name")
        with self.assertRaises(ValueError):
            client._validate_state_name("x" * 65)
        self.assertEqual(client._validate_state_name("alpha-1"), "alpha-1")

    def test_batch_reply_parses_boundary_and_terminal_stops(self):
        def turn(stop, marker):
            return {"complete": True, "stop": stop, "stop_marker": marker,
                    "frames": 12, "events": [], "trace_overflow": False}

        reply = {"turns": [
            turn("boundary", "ROMTRACE|boundary|player-choice|3|4"),
            turn("terminal", "ROMTRACE|terminal|win|all-fainted"),
            turn("turn-complete", "ROMTRACE|complete|1"),
        ]}
        turns = rom_bridge.RomWorkerClient._validate_batch_reply(reply, 3, 24_000)
        self.assertEqual([item["stop"] for item in turns], ["boundary", "terminal", "turn-complete"])
        self.assertTrue(turns[0]["stop_marker"].startswith("ROMTRACE|boundary|"))

        bad = {"turns": [turn("ready", "ROMTRACE|ready|0|856")]}
        with self.assertRaises(rom_bridge.WorkerProtocolError):
            rom_bridge.RomWorkerClient._validate_batch_reply(bad, 1, 24_000)
        orphan = {"turns": [{"complete": True, "stop": None, "stop_marker": "ROMTRACE|x",
                             "frames": 1, "events": [], "trace_overflow": False}]}
        with self.assertRaises(rom_bridge.WorkerProtocolError):
            rom_bridge.RomWorkerClient._validate_batch_reply(orphan, 1, 24_000)

    def test_slot_reply_identity_covers_the_new_identity_fields(self):
        client = object.__new__(rom_bridge.RomWorkerClient)
        client.rom_sha256 = rom_bridge.ROM_SHA256
        base = {
            "handle": "a" * 32, "name": "alpha", "bytes": 528448, "sha256": "b" * 64,
            "identity": {"worker_version": rom_bridge.WORKER_VERSION, "mgba_version": rom_bridge.MGBA_VERSION,
                         "artifact_sha256": rom_bridge.ROM_SHA256.lower(),
                         "wire_protocol": rom_bridge.PROTOCOL_VERSION,
                         "save_format": rom_bridge.SAVE_FORMAT_ID},
        }
        self.assertEqual(client._validate_slot_reply({"slot": base})["handle"], "a" * 32)
        for field, value in (("wire_protocol", 1), ("save_format", "other"),
                             ("worker_version", "sidedex-mgba-worker/0.1.4"),
                             ("artifact_sha256", "0" * 64)):
            broken = {**base, "identity": {**base["identity"], field: value}}
            with self.subTest(field=field), self.assertRaises(rom_bridge.WorkerProtocolError):
                client._validate_slot_reply({"slot": broken})

    def test_slot_lifecycle_round_trips_through_the_client(self):
        client = object.__new__(rom_bridge.RomWorkerClient)
        client.rom_sha256 = rom_bridge.ROM_SHA256
        calls = []

        def fake_request(op, **kwargs):
            calls.append((op, kwargs))
            if op == "save_state":
                return {"slot": {"handle": "c" * 32, "name": kwargs.get("name", "default"),
                                 "bytes": 10, "sha256": "d" * 64,
                                 "identity": {"worker_version": rom_bridge.WORKER_VERSION,
                                              "mgba_version": rom_bridge.MGBA_VERSION,
                                              "artifact_sha256": rom_bridge.ROM_SHA256.lower(),
                                              "wire_protocol": rom_bridge.PROTOCOL_VERSION,
                                              "save_format": rom_bridge.SAVE_FORMAT_ID}}}
            if op == "load_state":
                return {"frame_count": 60}
            if op == "drop_state":
                return {"dropped": True}
            if op == "list_states":
                return {"states": [{"handle": "c" * 32, "name": "alpha", "bytes": 10}],
                        "slot_limit": rom_bridge.MAX_STATE_SLOTS, "memory_limit": rom_bridge.MAX_STATE_TOTAL_BYTES}
            raise AssertionError(op)

        client._request = fake_request
        saved = client.save_checkpoint("alpha")
        self.assertEqual((saved["handle"], saved["name"], saved["bytes"]), ("c" * 32, "alpha", 10))
        self.assertEqual(client.restore_state("c" * 32)["frame_count"], 60)
        self.assertTrue(client.drop_state("c" * 32)["dropped"])
        self.assertEqual(len(client.list_states()["states"]), 1)
        ops = [op for op, _ in calls]
        self.assertEqual(ops, ["save_state", "load_state", "drop_state", "list_states"])
        self.assertEqual(calls[0][1]["artifact_sha256"], rom_bridge.ROM_SHA256.lower())
        self.assertEqual(calls[1][1]["handle"], "c" * 32)

        with self.assertRaises(ValueError):
            client.restore_state("not-a-handle")

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
status = json.loads(os.environ["FAKE_STATUS"])
status.update({"rom_bytes": os.path.getsize(rom), "frame_count": 7})
print(json.dumps(status), flush=True)
request = json.loads(sys.stdin.readline())
print(json.dumps({**status, "op": request["op"], "address": request["address"], "data": request["data"]}), flush=True)
sys.stdin.readline()
print(json.dumps({"protocol": status["protocol"], "ok": True}), flush=True)
"""
            with patch.dict(os.environ, FAKE_ENV), patch.object(rom_bridge, "ROM_SHA256", digest):
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
import json, os, sys, time
rom = sys.argv[-1]
status = json.loads(os.environ["FAKE_STATUS"])
status["rom_bytes"] = os.path.getsize(rom)
print(json.dumps(status), flush=True)
sys.stdin.readline()
time.sleep(3)
"""
            with patch.dict(os.environ, FAKE_ENV), patch.object(rom_bridge, "ROM_SHA256", digest):
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
status = json.loads(os.environ["FAKE_STATUS"])
status["rom_bytes"] = os.path.getsize(rom)
protocol = status["protocol"]
print(json.dumps(status), flush=True)
for line in sys.stdin:
    req = json.loads(line)
    if req["op"] == "save_state":
        identity = json.loads(os.environ["FAKE_SLOT_IDENTITY"])
        identity["artifact_sha256"] = req["artifact_sha256"]
        slot = {"handle":"a"*32,"name":req.get("name","default"),"bytes":123,"sha256":"b"*64,
                "identity":identity}
        out = {"protocol":protocol,"ok":True,"slot":slot}
    elif req["op"] == "batch_turns":
        out = {"protocol":protocol,"ok":True,"request_id":req["request_id"],"source_sha":req["source_sha"],"engine_elf_sha256":req["engine_elf_sha256"],"worker_sha256":req["worker_sha256"],"turns":[{"complete":True,"stop":"boundary","stop_marker":"ROMTRACE|boundary|player-choice|1|4","frames":17,"events":["ROMTRACE|boundary|player-choice|1|4"],"trace_overflow":False} for _ in req["payloads"].split("|")]}
    else:
        out = {"protocol":protocol,"ok":True}
    print(json.dumps(out), flush=True)
    if req["op"] == "quit": break
"""
            with patch.dict(os.environ, FAKE_ENV), patch.object(rom_bridge, "ROM_SHA256", digest):
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
                    self.assertEqual(result["protocol_version"], rom_bridge.PROTOCOL_VERSION)
                    self.assertEqual(len(result["turns"]), 2)
                    self.assertEqual([turn["frames"] for turn in result["turns"]], [17, 17])
                    self.assertEqual([turn["stop"] for turn in result["turns"]], ["boundary", "boundary"])
                    self.assertTrue(result["turns"][0]["stop_marker"].startswith("ROMTRACE|boundary|"))
                    self.assertGreaterEqual(result["elapsed_ms"], 0)
                    with self.assertRaises(ValueError):
                        client.batch_turns(
                            0x02000100, [b"abc"], mailbox_bytes=4,
                            engine_elf_sha256=digest, deadline_ms=0, request_id="bad",
                        )

    def test_worker_id_is_stable_for_a_client_and_distinct_between_clients(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            rom = temp / "test.gba"
            rom.write_bytes(b"placeholder ROM for worker id test")
            digest = rom_bridge.sha256_file(rom)
            lock = temp / "source_lock.json"
            lock.write_text(json.dumps({"commit": rom_bridge.SOURCE_SHA}), encoding="utf-8")
            fake_worker = r"""
import json, os, sys
rom = sys.argv[-1]
status = json.loads(os.environ["FAKE_STATUS"])
status["rom_bytes"] = os.path.getsize(rom)
print(json.dumps(status), flush=True)
for line in sys.stdin:
    req = json.loads(line)
    print(json.dumps({"protocol":status["protocol"],"ok":True,"op":req["op"]}), flush=True)
"""
            kwargs = dict(source_lock_path=lock, expected_artifact_sha256=digest,
                          expected_worker_sha256=rom_bridge.sha256_file(sys.executable),
                          worker_args=("-c", fake_worker))
            with patch.dict(os.environ, FAKE_ENV), patch.object(rom_bridge, "ROM_SHA256", digest):
                with rom_bridge.RomWorkerClient(sys.executable, rom, **kwargs) as first:
                    self.assertEqual(first.worker_id, first.worker_id)
                    self.assertIn(rom_bridge.WORKER_VERSION, first.worker_id)
                    self.assertIn(f"pid={first._process.pid}", first.worker_id)
                    self.assertIn(digest.lower(), first.worker_id)
                    first_id = first.worker_id
                with rom_bridge.RomWorkerClient(sys.executable, rom, **kwargs) as second:
                    self.assertNotEqual(first_id, second.worker_id)

    def test_batch_turns_restores_a_named_handle_or_the_default_slot(self):
        client = object.__new__(rom_bridge.RomWorkerClient)
        client.rom_sha256 = rom_bridge.ROM_SHA256
        client.worker_sha256 = rom_bridge.WORKER_SHA256
        client._has_checkpoint = False
        client._timeout = 5.0
        sent = {}

        def fake_request(op, _timeout=None, **fields):
            sent.clear()
            sent.update(fields)
            return {"ok": True, "request_id": fields["request_id"], "source_sha": fields["source_sha"],
                    "engine_elf_sha256": fields["engine_elf_sha256"], "worker_sha256": fields["worker_sha256"],
                    "_wire_bytes": 100, "_request_wire_bytes": 100,
                    "turns": [{"complete": True, "stop": "turn-complete", "stop_marker": "ROMTRACE|ready|1",
                               "frames": 5, "events": [], "trace_overflow": False}]}

        client._request = fake_request
        args = dict(mailbox_bytes=4, engine_elf_sha256=rom_bridge.ROM_SHA256,
                    deadline_ms=int(time.monotonic() * 1000) + 5000, request_id="batch-1")
        with self.assertRaises(ValueError):
            client.batch_turns(0x02000100, [b"abcd"], **args)
        client.batch_turns(0x02000100, [b"abcd"], handle="a" * 32, **args)
        self.assertEqual(sent["handle"], "a" * 32)
        client._has_checkpoint = True
        client.batch_turns(0x02000100, [b"abcd"], **args)
        self.assertNotIn("handle", sent)
        with self.assertRaises(ValueError):
            client.batch_turns(0x02000100, [b"abcd"], handle="nope", **args)

    def test_persistent_worker_client_against_actual_rom_when_available(self):
        if not PINNED_WORKER.is_file() or not PINNED_ROM.is_file():
            self.skipTest("build the mGBA worker and set SIDEDEX_ROM_PATH for the real-ROM client smoke")
        artifact_sha = rom_bridge.sha256_file(PINNED_ROM)
        with rom_bridge.RomWorkerClient(
            PINNED_WORKER, PINNED_ROM,
            expected_artifact_sha256=artifact_sha,
            expected_worker_sha256=rom_bridge.sha256_file(PINNED_WORKER),
        ) as client:
            first = client.status
            self.assertEqual(first["worker"], rom_bridge.WORKER_VERSION)
            self.assertEqual(first["mgba"], rom_bridge.MGBA_VERSION)
            self.assertEqual(first["source_sha"], rom_bridge.SOURCE_SHA)
            self.assertEqual(first["rom_sha256"], artifact_sha)
            self.assertEqual(client.rom_sha256, artifact_sha)
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


class Protocol2LiveTests(unittest.TestCase):
    """Live protocol-2 slot/blob coverage; set SIDEDEX_ROM_WORKER_EXE (v2) and
    SIDEDEX_ROM_PATH to a GBA artifact to run it."""

    WORKER = Path(os.environ.get("SIDEDEX_ROM_WORKER_V2_EXE", ""))
    ARTIFACT = Path(os.environ.get("SIDEDEX_V2_ARTIFACT_PATH", ""))

    @classmethod
    def _client(cls):
        return rom_bridge.RomWorkerClient(
            cls.WORKER, cls.ARTIFACT, timeout=30,
            expected_artifact_sha256=rom_bridge.sha256_file(cls.ARTIFACT),
            expected_worker_sha256=rom_bridge.sha256_file(cls.WORKER),
        )

    def setUp(self):
        if not self.WORKER.is_file() or not self.ARTIFACT.is_file():
            self.skipTest("set SIDEDEX_ROM_WORKER_V2_EXE and SIDEDEX_V2_ARTIFACT_PATH for the live protocol-2 test")

    def test_named_slots_portable_blob_and_error_taxonomy(self):
        try:
            first = self._client()
        except rom_bridge.WorkerProtocolError as exc:
            self.skipTest(f"worker is not protocol 2: {exc}")
        with first:
            first.run_frames(60)
            alpha = first.save_checkpoint("alpha")
            beta = first.save_checkpoint("beta")
            self.assertEqual(alpha["bytes"], beta["bytes"])
            self.assertGreater(alpha["bytes"], 0)
            first.run_frames(30)
            self.assertEqual(first.restore_state(alpha["handle"])["frame_count"], 60)
            self.assertEqual(first.restore_state(beta["handle"])["frame_count"], 60)
            self.assertEqual(len(first.list_states()["states"]), 2)
            exported = first.export_state(alpha["handle"])
            self.assertGreater(exported["blob_bytes"], alpha["bytes"])
            with self.assertRaises(rom_bridge.WorkerResponseError) as raised:
                first.restore_state("0" * rom_bridge.MAX_HANDLE_HEX)
            self.assertEqual(raised.exception.code, "stale-handle")
            with self.assertRaises(rom_bridge.WorkerResponseError) as raised:
                first._request("load_state", handle=alpha["handle"], artifact_sha256="0" * 64)
            self.assertEqual(raised.exception.code, "identity-mismatch")
            first.drop_state(beta["handle"])
            with self.assertRaises(rom_bridge.WorkerResponseError):
                first.restore_state(beta["handle"])
            with self.assertRaises(ValueError):
                first.read_memory_block(0x0203FFF0, 32)
            self.assertEqual(len(first.read_memory_block(0x02000000, 32)), 32)
            first.set_keys(0x10)
            self.assertEqual(first.run_frames(5, keys=0x8)["frame_count"], 65)
            blob = exported["blob"]
        with self._client() as second:
            imported = second.import_state(blob, name="ported")
            self.assertEqual(second.restore_state(imported["handle"])["frame_count"], 60)
            self.assertEqual(second.load_state_blob(blob)["frame_count"], 60)


class BoundaryLegalTraceLiveTests(unittest.TestCase):
    """Live guard for contract section 7: canonical ``ROMTRACE|legal|`` records
    emitted at choice boundaries must survive the worker's slim-trace filter.
    Set SIDEDEX_ROM_WORKER_V2_EXE (v3) and SIDEDEX_V8_ELF_PATH to a boundary
    ELF (protocol 8) to run it."""

    WORKER = Path(os.environ.get("SIDEDEX_ROM_WORKER_V2_EXE", ""))
    ELF = Path(os.environ.get("SIDEDEX_V8_ELF_PATH", ""))
    PROTOCOL = 8

    def test_batch_turns_keeps_canonical_legal_records(self):
        if not self.WORKER.is_file() or not self.ELF.is_file():
            self.skipTest("set SIDEDEX_ROM_WORKER_V2_EXE and SIDEDEX_V8_ELF_PATH for the boundary ELF")
        from tools.rom_oracle import run_dynamic

        digest = rom_bridge.sha256_file(self.ELF)
        size = run_dynamic.mailbox_bytes(self.PROTOCOL)
        request = json.loads((ROOT / "tools" / "rom_oracle" / "dynamic_sample.json").read_text(encoding="utf-8"))
        payload = run_dynamic.encode_request(request, protocol_version=self.PROTOCOL)
        with rom_bridge.RomWorkerClient(
            self.WORKER, self.ELF, timeout=20.0,
            expected_artifact_sha256=digest,
            expected_worker_sha256=rom_bridge.sha256_file(self.WORKER),
        ) as client:
            address = None
            for _ in range(400):
                events = client.run_frames(60)["trace_events"]
                ready = [line for line in events if line.startswith("ROMTRACE|ready|")]
                if ready:
                    address = int(ready[-1].split("|")[2])
                    break
            self.assertIsNotNone(address, "boundary ELF did not announce its mailbox")
            checkpoint = client.save_checkpoint("legal-trace")
            result = client.batch_turns(
                address, [payload], mailbox_bytes=size, engine_elf_sha256=digest,
                deadline_ms=int(time.monotonic() * 1000) + 20_000,
                request_id="legal-trace", max_frames=24_000, handle=checkpoint["handle"],
            )
        events = result["turns"][0]["events"]
        legal = [record for record in events if record.startswith("ROMTRACE|legal|")]
        self.assertTrue(any(record.startswith("ROMTRACE|legal|move|") for record in legal), events)
        self.assertTrue(any(record.startswith("ROMTRACE|legal|switch|") for record in legal), events)


class Status1EncodingTest(unittest.TestCase):
    """run_dynamic accepts named statuses and raw selectable status1 words
    (fixture P11 uses sleep counter 1) and still rejects everything else."""

    def encode(self, status1):
        from tools.rom_oracle import run_dynamic

        request = json.loads((ROOT / "tools" / "rom_oracle" / "fixtures" / "p11_sleep_wake.json")
                             .read_text(encoding="utf-8"))["root"]
        request["player_party"][0]["status1"] = status1
        return run_dynamic.encode_request(request, protocol_version=8)

    def test_numeric_and_named_statuses_encode_to_the_same_word(self):
        import struct

        for value in (0, 1, 7, 8, 64, 4096):
            self.assertEqual(struct.unpack_from("<I", self.encode(value), 72)[0], value)
        self.assertEqual(self.encode("STATUS1_SLEEP")[72:76], (7).to_bytes(4, "little"))

    def test_invalid_statuses_are_rejected(self):
        for bad in (9, 3840, -1, "STATUS1_TOXIC_COUNTER", "garbage", 1.0, True):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.encode(bad)


if __name__ == "__main__":
    unittest.main()
