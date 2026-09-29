"""Gate N1: the native i386 engine returns SDR2 byte-identical to the mGBA oracle.

Both engines speak the worker JSON-lines protocol; this drives them with the
same bytes. P1 (p01_physical_neutral): announce -> save base -> root request ->
child at the player-choice boundary -> the tackle branch from that child. Both
SDR2 results must be byte-equal. Then, native only: timings over N branch runs
and a snapshot-determinism check (restore the child, run the branch twice:
identical SDR2 and identical child-state sha256, which hashes the whole
snapshot). Negative controls: a different RNG seed must change the native SDR2,
and one flipped byte must fail the comparison. Also native only: a child saved
without its sha256 hashes lazily on export_state (same digest as an eager save),
and an exported blob imported into a fresh worker replays the branch byte-equal.
`--rollout N` adds the rollout throughput bench (N rollouts per configuration).

usage: python tools/rom_oracle/native_gate_n1.py [--runs 60] [--oracle-elf PATH] [--rollout N]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import rom_native  # noqa: E402
from rom_native import Engine  # noqa: E402
from tools.rom_oracle import run_dynamic  # noqa: E402

ORACLE_WORKER = ROOT / "native/rom_worker/build/mingw/rom_worker.exe"
ORACLE_ELF = ROOT / "native/rom_worker/build/mingw/pokeemerald-test-headless.elf"  # the active pin
NATIVE_BIN, NATIVE_BIN_WIN = rom_native.NATIVE_BIN, Path(rom_native.NATIVE_BIN_WIN)
FIXTURE = ROOT / "tools/rom_oracle/fixtures/p01_physical_neutral.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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


def bench_rollout(engine: Engine, root: str, ready: dict, count: int) -> None:
    """Single-worker rollout throughput from the P1 root (ROM AI plays every turn)."""
    def seeded(s: int, max_turns: int) -> dict:
        return {"rng1": [s * 7919 + 1, s * 104729 + 3, s * 13 + 5, 1], "rng2": [1, 2, s + 3, 29],
                "prefix": [], "max_turns": max_turns}
    for max_turns in (8, 100):
        start = time.perf_counter()
        out = []
        for lo in range(0, count, 50):
            out += engine.rollout(root, [seeded(s, max_turns) for s in range(lo, min(count, lo + 50))],
                                  "n1-bench", ready=ready, max_frames=2000)
        wall = time.perf_counter() - start
        turns = sum(r["turns"] for r in out)
        frames = sum(r["frames"] for r in out)
        outcomes = {o: sum(r["outcome"] == o for r in out) for o in sorted({r["outcome"] for r in out})}
        print(f"rollout max_turns={max_turns}: {count / wall:.0f} rollouts/s, {turns / count:.2f} turns/rollout, "
              f"{turns / wall:.0f} turns/s, {frames / max(turns, 1):.0f} frames/turn, outcomes {outcomes}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=60)
    parser.add_argument("--oracle-elf", type=Path, default=ORACLE_ELF)
    parser.add_argument("--rollout", type=int, default=0, help="also bench N rollouts per configuration")
    args = parser.parse_args()
    oracle_elf = args.oracle_elf
    oracle_sha, native_sha = sha256(oracle_elf), sha256(NATIVE_BIN_WIN)
    print(f"oracle ELF {oracle_sha}\nnative bin {native_sha}")

    oracle = Engine([str(ORACLE_WORKER), "--rom", str(oracle_elf)], oracle_sha)
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
            turn = native.turn(child, payload, ready, "n1-p1-branch", hash_children=False)
            assert turn["sdr2"] == n_branch, f"native run {i} diverged from its first run"
            turns.append(turn)
            native.op("drop_state", handle=turn["child"]["handle"])
        det = []
        for _ in range(2):  # the child slot name repeats per request id: drop before rerunning
            det.append(native.turn(child, payload, ready, "n1-p1-branch"))
            native.op("drop_state", handle=det[-1]["child"]["handle"])
        det_hashes = [t["child"]["sha256"] for t in det]
        snapshot_bytes = det[0]["child"]["bytes"]
        lazy = native.turn(child, payload, ready, "n1-p1-branch", hash_children=False)
        lazy_null = lazy["child"]["sha256"] is None
        lazy_sha = native.op("export_state", handle=lazy["child"]["handle"])["sha256"]
        native.op("drop_state", handle=lazy["child"]["handle"])
        exported = native.op("export_state", handle=child, artifact_sha256=native_sha)
        if args.rollout:
            bench_rollout(native, child, ready, args.rollout)
    finally:
        native.close()

    native3 = Engine(["wsl.exe", "-e", NATIVE_BIN, "--rom", NATIVE_BIN], native_sha)
    try:
        ready3 = native3.announce()
        imported = native3.op("import_state", blob=exported["blob"], artifact_sha256=native_sha,
                              name="imported")["slot"]
        imported_branch = native3.turn(imported["handle"], payload, ready3, "n1-p1-branch")["sdr2"]
    finally:
        native3.close()

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
    print(f"  of which child save (snapshot copy, sha256 skipped) ms p50 {statistics.median(save):.3f}")
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
        "lazy child hash == eager hash": lazy_null and lazy_sha == det_hashes[0],
        "export_state -> import_state replays the branch": imported_branch == n_branch,
        "snapshot bytes are process independent (sha256)": imported["sha256"] == exported["sha256"],
        "negative: flipped byte detected": bytes(flipped) != o_branch,
        "negative: other seed diverges": seeded_branch != o_branch,
    }
    for name, ok in checks.items():
        print(f"{'PASS' if ok else 'FAIL'} {name}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
