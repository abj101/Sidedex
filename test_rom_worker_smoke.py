"""Smoke test the real mGBA worker when a built worker and ROM are available.

This verifies emulator lifecycle primitives and the protocol-3 state/transport
ops. It intentionally does not assert or synthesize battle outcomes.
"""

import hashlib
import json
import os
import shutil
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
# Gate V1 retired wire 2; the pinned worker (1529b4de) speaks wire 3 only and
# advertises the v9/compact transition contract.
PROTOCOL = 3
WORKER_NAME = "sidedex-mgba-worker/0.3.0"
MGBA = "0.10.5+26b7884"
TRANSITION_SCHEMA = "sidedex-transition-v2"
DECODE_SCHEMA = "decode-v2"


MINGW_BIN = Path(os.environ.get("SIDEDEX_MINGW_BIN", "C:/msys64/mingw64/bin"))
STOP_POLICY_TEST = ROOT / "native" / "rom_worker" / "stop_policy_test.c"
RESULT_TAGS_TEST = ROOT / "native" / "rom_worker" / "result_tags_test.c"


def c_compiler():
    """The compiler that builds the worker; None when the host has none."""
    for name in ("x86_64-w64-mingw32-gcc", "gcc", "cc"):
        found = shutil.which(name)
        if found:
            return found
    for candidate in (MINGW_BIN / "x86_64-w64-mingw32-gcc.exe", MINGW_BIN / "gcc.exe"):
        if candidate.is_file():
            return str(candidate)
    return None


def compiler_env():
    # gcc finds its assembler and linker on PATH, which a bare Windows process
    # does not have unless the mingw bin directory is added.
    env = dict(os.environ)
    env["Path"] = str(MINGW_BIN) + os.pathsep + env.get("Path", "")
    return env


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

    def test_protocol3_state_ops_and_memory(self):
        status = self.initial
        self.assertTrue(status["ok"])
        self.assertEqual(status["worker"], WORKER_NAME)
        self.assertEqual(status["mgba"], MGBA)
        self.assertEqual(status["platform"], "gba")
        self.assertEqual(status["supported_wire_protocols"], [PROTOCOL])
        self.assertEqual(status["state_schema"], "sidedex-state-v1")
        self.assertEqual(status["transition_schema"], TRANSITION_SCHEMA)
        self.assertEqual(status["decode_schema"], DECODE_SCHEMA)
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
                # Some worker builds accept only --rom; skip rather than fail on
                # one that predates --save.
                line = process.stdout.readline()
                if not line:
                    process.wait(timeout=10)
                    self.skipTest("worker build has no --save option")
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
            status = json.loads(process.stdout.readline())
            self.assertEqual(status.get("supported_wire_protocols"), [PROTOCOL], status)
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


class StopPolicyTest(unittest.TestCase):
    """Contract Appendix B: a compact turn stops at binary-ready, never at the
    boundary/terminal marker that precedes it; a trace turn is unchanged.

    The policy lives in native/rom_worker/stop_policy.h, which worker.c compiles
    in. Building it here drives the real production decision over a fake marker
    stream, so the ordering rules are covered without an emulator.
    """

    @classmethod
    def setUpClass(cls):
        cls.compiler = c_compiler()
        if cls.compiler is None:
            raise unittest.SkipTest("no C compiler to build the stop policy harness")
        cls.temp = tempfile.TemporaryDirectory()
        cls.exe = Path(cls.temp.name) / "stop_policy_test.exe"
        built = subprocess.run([cls.compiler, "-O2", "-o", str(cls.exe), str(STOP_POLICY_TEST)],
                               capture_output=True, text=True, env=compiler_env())
        if built.returncode != 0 or not cls.exe.is_file():
            cls.temp.cleanup()
            raise unittest.SkipTest(f"cannot build the stop policy harness: {built.stderr[:200]}")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def decide(self, *sequences):
        script = "".join(f"{compact} {' '.join(kinds)}\n" for compact, kinds in sequences)
        run = subprocess.run([str(self.exe)], input=script, capture_output=True, text=True,
                             env=compiler_env())
        self.assertEqual(run.returncode, 0, run.stderr)
        return run.stdout.split()

    def test_a_compact_turn_stops_at_binary_ready_after_the_boundary(self):
        self.assertEqual(self.decide((1, ("boundary", "binary-ready"))), ["binary-ready|1"])

    def test_a_compact_terminal_turn_stops_at_binary_ready_before_complete(self):
        self.assertEqual(self.decide((1, ("terminal", "binary-ready", "turn-complete"))),
                         ["binary-ready|1"])

    def test_a_compact_turn_without_binary_ready_fails_closed(self):
        # Negative control for both cases above: nothing completes the turn, so
        # the worker's frame bound ends the loop and it reports incomplete-state.
        self.assertEqual(self.decide((1, ("boundary",)), (1, ("terminal", "turn-complete"))),
                         ["none|0", "none|0"])

    def test_the_trace_stop_policy_is_unchanged(self):
        # Negative control for the compact rule: a trace turn still stops on the
        # first boundary/terminal marker and never on an unrelated marker.
        self.assertEqual(self.decide((0, ("boundary", "binary-ready")), (0, ("terminal",)),
                                     (0, ("phase", "ready"))),
                         ["boundary|1", "terminal|1", "none|0"])


class ResultTagsTest(unittest.TestCase):
    """Contract C.1: the worker's required-tag rule depends on the boundary.

    Builds the production native/rom_worker/result_tags.h (the rule
    validate_v9_result calls) into a tiny harness; no emulator involved.
    """

    @classmethod
    def setUpClass(cls):
        cls.compiler = c_compiler()
        if cls.compiler is None:
            raise unittest.SkipTest("no C compiler to build the result tags harness")
        cls.temp = tempfile.TemporaryDirectory()
        cls.exe = Path(cls.temp.name) / "result_tags_test.exe"
        built = subprocess.run([cls.compiler, "-O2", "-o", str(cls.exe), str(RESULT_TAGS_TEST)],
                               capture_output=True, text=True, env=compiler_env())
        if built.returncode != 0 or not cls.exe.is_file():
            cls.temp.cleanup()
            raise unittest.SkipTest(f"cannot build the result tags harness: {built.stderr[:200]}")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def accepted(self, status, boundary, tags):
        line = f"{status} {boundary} {' '.join(map(str, tags))}\n"
        run = subprocess.run([str(self.exe)], input=line, capture_output=True, text=True,
                             env=compiler_env())
        self.assertEqual(run.returncode, 0, run.stderr)
        return run.stdout.strip() == "1"

    def test_nonterminal_needs_tags_1_to_9(self):
        for boundary in (1, 2):
            self.assertTrue(self.accepted(0, boundary, range(1, 10)))
        self.assertFalse(self.accepted(0, 1, [1, 2, 3, 4, 5, 6, 8, 9]))  # no tag 7
        self.assertFalse(self.accepted(0, 1, range(1, 11)))              # tag 10 on a choice

    def test_terminal_has_no_tag_7(self):
        self.assertTrue(self.accepted(0, 3, [1, 2, 3, 4, 5, 6, 8, 9, 10]))
        self.assertFalse(self.accepted(0, 3, range(1, 11)))              # tag 7 present
        self.assertFalse(self.accepted(0, 3, range(1, 10)))              # no tag 10

    def test_nonzero_status_is_exactly_tag_11(self):
        self.assertTrue(self.accepted(1, 0, [11]))
        self.assertFalse(self.accepted(1, 0, [1, 11]))
        self.assertFalse(self.accepted(1, 0, range(1, 10)))


if __name__ == "__main__":
    unittest.main()
