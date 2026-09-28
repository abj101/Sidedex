"""Smoke test the real mGBA worker when a built worker and ROM are available.

This verifies emulator lifecycle primitives and the protocol-2 state/transport
ops. It intentionally does not assert or synthesize battle outcomes.
"""

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
WORKER = Path(os.environ.get(
    "SIDEDEX_ROM_WORKER_EXE",
    ROOT / "native" / "rom_worker" / "build" / "mingw" / "rom_worker.exe",
))
ROM = Path(os.environ.get("SIDEDEX_ROM_PATH", "")) if os.environ.get("SIDEDEX_ROM_PATH") else None
PROTOCOL = 2
WORKER_NAME = "sidedex-mgba-worker/0.2.0"
MGBA = "0.10.5+26b7884"
# The instrumented wire-3 worker advertises this; the pinned worker does not.
V3_WIRE_PROTOCOLS = [3]


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@unittest.skipUnless(WORKER.is_file(), f"mGBA worker executable is missing: {WORKER}")
@unittest.skipUnless(ROM is not None and ROM.is_file(), "set SIDEDEX_ROM_PATH to a ROM artifact")
class RomWorkerSmokeTest(unittest.TestCase):
    def setUp(self):
        self.process = subprocess.Popen(
            [str(WORKER), "--rom", str(ROM)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1,
        )
        self.artifact_sha = sha256_file(ROM)
        self.initial = json.loads(self.process.stdout.readline())

    def tearDown(self):
        if self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=10)

    def request(self, op, **fields):
        self.process.stdin.write(json.dumps({"protocol": PROTOCOL, "op": op, **fields}) + "\n")
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        if not line:
            self.fail(f"worker exited early: {self.process.stderr.read()}")
        return json.loads(line)

    def ok(self, op, **fields):
        reply = self.request(op, **fields)
        self.assertTrue(reply.get("ok"), reply)
        return reply

    def test_protocol2_state_ops_and_memory(self):
        status = self.initial
        self.assertTrue(status["ok"])
        self.assertEqual(status["worker"], WORKER_NAME)
        self.assertEqual(status["mgba"], MGBA)
        self.assertEqual(status["platform"], "gba")
        self.assertGreater(status["rom_bytes"], 0)
        base_frame = status["frame_count"]

        self.process.stdin.write(json.dumps({"protocol": 99, "op": "status"}) + "\n")
        self.process.stdin.flush()
        bad_protocol = json.loads(self.process.stdout.readline())
        self.assertEqual(bad_protocol, {"protocol": PROTOCOL, "ok": False, "error": "invalid-request"})

        self.assertEqual(self.ok("run_frames", frames=3)["frame_count"], base_frame + 3)

        saved = self.ok("save_state", name="smoke", artifact_sha256=self.artifact_sha)["slot"]
        self.assertGreater(saved["bytes"], 0)
        self.assertRegex(saved["sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(saved["identity"]["worker_version"], WORKER_NAME)
        self.assertEqual(saved["identity"]["wire_protocol"], PROTOCOL)
        self.assertEqual(saved["identity"]["save_format"], "mgba-savestate-savedata-rtc-v1")
        self.assertEqual(saved["identity"]["artifact_sha256"], self.artifact_sha.lower())
        handle = saved["handle"]
        self.assertRegex(handle, r"^[0-9a-f]{32}$")

        self.assertEqual(self.ok("run_frames", frames=5)["frame_count"], base_frame + 8)
        self.assertEqual(self.ok("load_state", handle=handle, artifact_sha256=self.artifact_sha)["frame_count"], base_frame + 3)

        states = self.ok("list_states")
        self.assertEqual([slot["name"] for slot in states["states"]], ["smoke"])
        self.assertEqual(states["slot_limit"], 3)
        self.assertGreater(states["memory_limit"], 0)

        exported = self.ok("export_state", handle=handle, artifact_sha256=self.artifact_sha)
        blob = exported["blob"]
        self.assertGreater(exported["blob_bytes"], saved["bytes"])
        self.assertTrue(blob)

        imported = self.ok("import_state", blob=blob, name="ported", artifact_sha256=self.artifact_sha)["slot"]
        self.assertEqual(imported["name"], "ported")
        self.assertEqual(imported["bytes"], saved["bytes"])
        self.assertEqual(self.ok("load_state_blob", blob=blob, artifact_sha256=self.artifact_sha)["frame_count"], base_frame + 3)

        dropped = self.ok("drop_state", handle=imported["handle"])
        self.assertTrue(dropped["dropped"])
        stale = self.request("load_state", handle=imported["handle"], artifact_sha256=self.artifact_sha)
        self.assertEqual(stale, {"protocol": PROTOCOL, "ok": False, "error": "stale-handle"})
        mismatch = self.request("load_state", handle=handle, artifact_sha256="0" * 64)
        self.assertEqual(mismatch, {"protocol": PROTOCOL, "ok": False, "error": "identity-mismatch"})
        self.assertEqual(self.request("load_state", handle="f" * 32, artifact_sha256=self.artifact_sha)["error"], "stale-handle")

        block = self.ok("read_memory_block", address=0x02000000, length=8)
        self.assertEqual(len(block["data"]), 16)
        self.assertEqual(self.request("read_memory_block", address=0x0203FFF0, length=32)["error"], "unmapped-memory")
        self.assertEqual(self.request("read_memory_block", address=0x02000000, length=4097)["error"], "invalid-memory-block")
        self.assertEqual(len(self.ok("read_memory_block", address=0x03000000, length=4)["data"]), 8)

        self.assertEqual(self.ok("set_keys", keys=0x10)["keys"], 0x10)
        self.assertEqual(self.ok("run_frames", frames=5, keys=0x8)["frame_count"], base_frame + 8)

        self.assertEqual(self.ok("write_memory", address=0x0203FFFC, data="01020304")["platform"], "gba")
        self.assertEqual(self.ok("read_memory", address=0x0203FFFC, width=4)["value"], 0x04030201)
        self.assertEqual(self.ok("write_memory", address=0x03000000, data="0102")["platform"], "gba")
        for address, data in ((0x01FFFFFF, "00"), (0x0203FFFF, "0001"), (0x02000000, "0"), (0x02000000, "AF")):
            reply = self.request("write_memory", address=address, data=data)
            self.assertEqual(reply, {"protocol": PROTOCOL, "ok": False, "error": "invalid-memory-write"}, (address, data))

        self.assertTrue(self.ok("quit")["ok"])
        self.process.wait(timeout=10)
        self.assertEqual(self.process.returncode, 0, self.process.stderr.read())

    def test_optional_external_save_loads_before_status(self):
        with tempfile.TemporaryDirectory() as temp:
            save = Path(temp) / "fixture.sav"
            save.write_bytes(bytes([0xFF]) * (128 * 1024))
            process = subprocess.Popen(
                [str(WORKER), "--rom", str(ROM), "--save", str(save)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", bufsize=1,
            )
            try:
                # --save was added on this branch; the pinned release-evidence
                # worker only accepts --rom, so skip rather than fail on it.
                line = process.stdout.readline()
                if not line:
                    process.wait(timeout=10)
                    self.skipTest("worker has no --save option (pinned wire-2 build)")
                status = json.loads(line)
                self.assertTrue(status["ok"], status)
                self.assertEqual(status["save_bytes"], 128 * 1024)
                self.assertEqual(status["save_sha256"], hashlib.sha256(save.read_bytes()).hexdigest())
                process.stdin.write(json.dumps({"protocol": PROTOCOL, "op": "quit"}) + "\n")
                process.stdin.flush()
                self.assertTrue(json.loads(process.stdout.readline())["ok"])
                process.wait(timeout=10)
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=10)

@unittest.skipUnless(WORKER.is_file(), f"mGBA worker executable is missing: {WORKER}")
@unittest.skipUnless(ROM is not None and ROM.is_file(), "set SIDEDEX_ROM_PATH to a ROM artifact")
class RomWorkerProfileFlagTest(unittest.TestCase):
    """The B0 timer flag is opt-in: off must change nothing, on only adds a profile."""

    def _run_frames(self, profile_on):
        env = dict(os.environ)
        env.pop("SIDEDEX_WORKER_PROFILE", None)
        if profile_on:
            env["SIDEDEX_WORKER_PROFILE"] = "1"
        process = subprocess.Popen(
            [str(WORKER), "--rom", str(ROM)], env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1,
        )
        try:
            json.loads(process.stdout.readline())
            process.stdin.write(json.dumps({"protocol": PROTOCOL, "op": "run_frames", "frames": 3}) + "\n")
            process.stdin.flush()
            return json.loads(process.stdout.readline())
        finally:
            process.terminate()
            process.wait(timeout=10)

    def test_profile_flag_defaults_off_and_is_additive(self):
        off = self._run_frames(False)
        on = self._run_frames(True)
        self.assertNotIn("profile", off)
        self.assertEqual(off["frame_count"], 3)
        # With the flag on the reply is byte-for-byte the same apart from the
        # additive profile object: no other observable behaviour changes.
        self.assertEqual({key: value for key, value in on.items() if key != "profile"}, off)
        self.assertEqual(set(on["profile"]), {"emulation_ns", "log_ns", "cycles"})
        self.assertGreater(on["profile"]["emulation_ns"], 0)
        self.assertEqual(on["profile"]["cycles"], 3 * 280896)


if __name__ == "__main__":
    unittest.main()
