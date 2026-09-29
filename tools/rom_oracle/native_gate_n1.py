"""Gate N1: the native i386 engine returns SDR2 byte-identical to the mGBA oracle.

Both engines speak the worker JSON-lines protocol; this drives them with the
same bytes. P1 (p01_physical_neutral): announce -> save base -> root request ->
child at the player-choice boundary -> the tackle branch from that child. Both
SDR2 results must be byte-equal. Then, native only: timings over N branch runs
and a snapshot-determinism check (restore the child, run the branch twice:
identical SDR2 and identical child-state sha256, which hashes the whole
snapshot). Negative controls: a different RNG seed must change the native SDR2,
and one flipped byte must fail the comparison.

usage: python tools/rom_oracle/native_gate_n1.py [--runs 60]
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from rom_bridge import parse_v9_ready  # noqa: E402
from tools.rom_oracle import run_dynamic  # noqa: E402
from tools.rom_oracle import sdr2  # noqa: E402

ORACLE_WORKER = Path(r"C:\Users\Abj1\.codex\worktrees\rom-transition-orchestrator\Sidedex"
                     r"\native\rom_worker\build\mingw\rom_worker.exe")
ORACLE_ELF = Path(r"C:\Users\Abj1\.codex\artifacts\O\native-oracle\163395c52d\pokeemerald-test-headless.elf")
NATIVE_BIN = "/home/abj1/rom-n1/sidedex-native"
NATIVE_BIN_WIN = Path(r"\\wsl.localhost\Ubuntu") / NATIVE_BIN.lstrip("/")
FIXTURE = ROOT / "tools/rom_oracle/fixtures/p01_physical_neutral.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Engine:
    def __init__(self, command: list[str], artifact_sha: str):
        self.sha = artifact_sha
        self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     text=True, bufsize=1)
        self.status = self._read()

    def _read(self) -> dict:
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("engine exited")
        return json.loads(line)

    def op(self, op: str, **fields) -> dict:
        self.proc.stdin.write(json.dumps({"protocol": 3, "op": op, **fields}) + "\n")
        self.proc.stdin.flush()
        reply = self._read()
        if not reply.get("ok"):
            raise RuntimeError(f"{op}: {reply}")
        return reply

    def close(self) -> None:
        try:
            self.op("quit")
        finally:
            self.proc.wait(timeout=10)

    def announce(self) -> dict:
        events: list[str] = []
        for _ in range(400):
            events += self.op("run_frames", frames=60)["trace_events"]
            if any(e.startswith("ROMTRACE|ready|") for e in events):
                return parse_v9_ready(events)
        raise TimeoutError("no ready announcement")

    def save(self, name: str) -> str:
        return self.op("save_state", name=name, artifact_sha256=self.sha)["slot"]["handle"]

    def turn(self, handle: str, payload: bytes, ready: dict, request_id: str) -> dict:
        token = int.from_bytes(hashlib.sha256(request_id.encode()).digest()[:2], "little")
        wall = time.perf_counter()
        reply = self.op(
            "batch_turns_v3", handle=handle, address=ready["address"],
            result_address=ready["result_address"], result_capacity=ready["result_capacity"],
            mailbox_bytes=876, mailbox_count=1, mailboxes=base64.b64encode(payload).decode(),
            budget_ms=60_000, max_frames=24_000, request_id=request_id, request_token=token,
            source_sha="0" * 40, engine_elf_sha256=self.sha, worker_sha256="0" * 64,
            mode=0, critical_policy=0)
        turn = reply["turns"][0]
        turn["wall_ms"] = (time.perf_counter() - wall) * 1000
        turn["sdr2"] = base64.b64decode(turn["result"])
        sdr2.parse(turn["sdr2"], request_id=token, mode=0, critical_policy=0,
                   capacity=ready["result_capacity"])
        return turn


def p1_requests(seed_delta: int = 0) -> tuple[dict, dict]:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    root = json.loads(json.dumps(fixture["root"]))
    if seed_delta:
        root["rng_main"] = [(v + seed_delta) & 0xFFFFFFFF for v in root["rng_main"]]
    branch = json.loads(json.dumps(root))
    branch["candidate_action"] = fixture["actions"][0]["action"]
    return root, branch


def run_p1(engine: Engine, seed_delta: int = 0) -> tuple[bytes, bytes, str, dict]:
    ready = engine.announce()
    root, branch = p1_requests(seed_delta)
    base = engine.save("n1-base")
    first = engine.turn(base, run_dynamic.encode_v9_request(root, ready), ready, "n1-p1-root")
    child = first["child"]["handle"]
    second = engine.turn(child, run_dynamic.encode_v9_request(branch, ready), ready, "n1-p1-branch")
    if second["child"]:
        engine.op("drop_state", handle=second["child"]["handle"])
    return first["sdr2"], second["sdr2"], child, ready


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=60)
    args = parser.parse_args()
    oracle_sha, native_sha = sha256(ORACLE_ELF), sha256(NATIVE_BIN_WIN)
    print(f"oracle ELF {oracle_sha}\nnative bin {native_sha}")

    oracle = Engine([str(ORACLE_WORKER), "--rom", str(ORACLE_ELF)], oracle_sha)
    try:
        o_root, o_branch, _, _ = run_p1(oracle)
    finally:
        oracle.close()

    native = Engine(["wsl.exe", "-e", NATIVE_BIN, "--rom", NATIVE_BIN], native_sha)
    try:
        n_root, n_branch, child, ready = run_p1(native)
        _, branch = p1_requests()
        payload = run_dynamic.encode_v9_request(branch, ready)
        turns = []
        for i in range(args.runs):
            turn = native.turn(child, payload, ready, "n1-p1-branch")
            assert turn["sdr2"] == n_branch, f"native run {i} diverged from its first run"
            turns.append(turn)
            native.op("drop_state", handle=turn["child"]["handle"])
        det = []
        for _ in range(2):  # the child slot name repeats per request id: drop before rerunning
            det.append(native.turn(child, payload, ready, "n1-p1-branch"))
            native.op("drop_state", handle=det[-1]["child"]["handle"])
        det_hashes = [t["child"]["sha256"] for t in det]
        snapshot_bytes = det[0]["child"]["bytes"]
    finally:
        native.close()

    native2 = Engine(["wsl.exe", "-e", NATIVE_BIN, "--rom", NATIVE_BIN], native_sha)
    try:
        _, seeded_branch, _, _ = run_p1(native2, seed_delta=1)
    finally:
        native2.close()

    for label, o, n in (("root", o_root, n_root), ("branch", o_branch, n_branch)):
        print(f"P1 {label}: oracle sha256 {hashlib.sha256(o).hexdigest()} ({len(o)} B)")
        print(f"P1 {label}: native sha256 {hashlib.sha256(n).hexdigest()} ({len(n)} B)")
    ms = sorted(t["elapsed_ms"] for t in turns)
    wall = sorted(t["wall_ms"] for t in turns)
    frames = sorted(t["frames"] for t in turns)
    restore_us = sorted(t["restore_ms"] * 1000 for t in turns)
    pct = lambda xs, q: xs[min(len(xs) - 1, int(q * len(xs)))]  # noqa: E731
    print(f"transition ms p50 {statistics.median(ms):.3f} p95 {pct(ms, .95):.3f} (n={len(ms)}); "
          f"IPC wall ms p50 {statistics.median(wall):.3f} p95 {pct(wall, .95):.3f}")
    save = sorted(t["save_ms"] for t in turns)
    print(f"  of which child save (snapshot copy + sha256) ms p50 {statistics.median(save):.3f}")
    print(f"frames p50 {statistics.median(frames)}; restore us p50 {statistics.median(restore_us):.0f} "
          f"p95 {pct(restore_us, .95):.0f}; snapshot bytes {snapshot_bytes}")
    print(f"determinism: sdr2 equal {det[0]['sdr2'] == det[1]['sdr2'] == n_branch}; "
          f"child sha256 {det_hashes[0]} / {det_hashes[1]}")

    flipped = bytearray(n_branch)
    flipped[len(flipped) // 2] ^= 1
    checks = {
        "root byte-equal": o_root == n_root,
        "branch byte-equal": o_branch == n_branch,
        "determinism sdr2": det[0]["sdr2"] == det[1]["sdr2"] == n_branch,
        "determinism memory hash": det_hashes[0] == det_hashes[1],
        "negative: flipped byte detected": bytes(flipped) != o_branch,
        "negative: other seed diverges": seeded_branch != o_branch,
    }
    for name, ok in checks.items():
        print(f"{'PASS' if ok else 'FAIL'} {name}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
