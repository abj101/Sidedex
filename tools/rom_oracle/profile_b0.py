#!/usr/bin/env python3
"""B0 profile: split P1 root construction and transition wall time into buckets.

Runs against the pinned v8c test ELF and the worker built from this branch. The
worker instrumentation is behind SIDEDEX_WORKER_PROFILE (default off). This
script turns it on, boots the ELF to its mailbox, builds the P1 root (the
fixture's root request run to the opening boundary), then measures >=30 warm
transitions from that root after warmup. Buckets, matching B0 task 1:

  (a) emulation core run loop (mTiming cycles + frames + runFrame wall time)
  (b) mLog / ROMTRACE I/O (time inside the log callback incl. formatting)
  (c) JSONL IPC (Python encode/write/decode + worker residual parse/serialize)
  (d) Python parse (mailbox encode + domain trace parse)
  (e) savestate save/restore

It also compares each headless lever in task 2 and keeps one only on full-state
parity (byte-identical trace/state records + identical savestate SHA-256) on P1
and on the P29 multi-turn branch. Usage:

    python tools/rom_oracle/profile_b0.py [--iterations 30] [--warmup 3] \
        [--out C:/Users/Abj1/.codex/artifacts/B/B0-profile.md]
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import rom_bridge  # noqa: E402
from tools.rom_oracle import run_dynamic  # noqa: E402

ELF = ROOT / "native" / "rom_worker" / "build" / "mingw" / "pokeemerald-test-headless.elf"
WORKER = ROOT / "native" / "rom_worker" / "build" / "mingw" / "rom_worker.exe"
FIXTURE_DIR = Path(__file__).with_name("fixtures")
PIN = json.loads(Path(__file__).with_name("pinned_source.json").read_text(encoding="utf-8"))
ELF_SHA = PIN["dynamic_oracle"]["headless_elf_sha256"].upper()
ROM_SHA = PIN["rom_sha256"]
PROTOCOL = int(PIN["dynamic_oracle"]["protocol_version"])
MAILBOX_BYTES = run_dynamic.mailbox_bytes(PROTOCOL)
MAX_FRAMES = 24_000


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _pct(values: list[float], p: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(len(ordered) * p + 0.999999) - 1))
    return ordered[index]


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def root_request(fixture: dict) -> dict:
    request = dict(fixture["root"])
    request["operation"] = "simulate-turn"
    request["mode"] = "normal"
    request["critical_policy"] = fixture.get("critical_policy", "normal")
    return request


def action_request(fixture: dict, action: dict, operation: str = "simulate-turn") -> dict:
    request = dict(fixture["root"])
    request["candidate_action"] = action
    request["operation"] = operation
    request["mode"] = "normal"
    request["critical_policy"] = fixture.get("critical_policy", "normal")
    return request


_ANN: dict = {}  # the ROM's v9 ready announcement (set by boot_to_ready)


def payload(request: dict) -> bytes:
    return run_dynamic.encode_request(request, protocol_version=PROTOCOL, result_buffer=_ANN)


def parse_turn(events: list[str]) -> dict:
    states = [line for line in events if "|state|" in line]
    boundary = any(line.startswith(("ROMTRACE|boundary|", "ROMTRACE|terminal|")) for line in events)
    return {"states": len(states), "boundary": boundary}


def boot_to_ready(client: rom_bridge.RomWorkerClient) -> dict:
    started = time.perf_counter()
    trace: list[str] = []
    address = None
    emu_ns = log_ns = cycles = 0
    calls = 0
    for _ in range(400):
        reply = client.run_frames(60)
        calls += 1
        profile = reply.get("profile") or {}
        emu_ns += profile.get("emulation_ns", 0)
        log_ns += profile.get("log_ns", 0)
        cycles += profile.get("cycles", 0)
        trace.extend(run_dynamic._trace(reply))
        ready = [line for line in trace if line.startswith("ROMTRACE|ready|")]
        if ready:
            _ANN.update(rom_bridge.parse_v9_ready(ready))
            address = _ANN["address"]
            break
    if address is None:
        raise TimeoutError("ELF never announced its mailbox")
    return {"address": address, "elapsed_ns": (time.perf_counter() - started) * 1e9,
            "frames": client.status["frame_count"], "emulation_ns": emu_ns,
            "log_ns": log_ns, "cycles": cycles, "calls": calls}


def measure_batch(client: rom_bridge.RomWorkerClient, address: int, request: dict,
                  *, handle: str | None, tag: str, request_id: int) -> dict:
    """One batch_turns with a single turn; returns wall buckets + worker profile.

    The mailbox requestId must change per turn: a restored boundary ROM only
    processes a request whose id differs from the one already in its mailbox.
    """
    mailbox_t0 = time.perf_counter()
    turn_payload = payload({**request, "request_id": request_id})
    mailbox_ns = (time.perf_counter() - mailbox_t0) * 1e9
    before = len(client.transport_samples)
    deadline_ms = int(time.monotonic() * 1000) + 30_000
    batch_t0 = time.perf_counter()
    result = client.batch_turns(
        address, [turn_payload], mailbox_bytes=MAILBOX_BYTES, engine_elf_sha256=ELF_SHA,
        deadline_ms=deadline_ms, request_id=tag, max_frames=MAX_FRAMES, handle=handle,
    )
    batch_ns = (time.perf_counter() - batch_t0) * 1e9
    turn = result["turns"][0]
    events = turn["events"]
    parse_t0 = time.perf_counter()
    parsed = parse_turn(events)
    parse_ns = (time.perf_counter() - parse_t0) * 1e9
    transport = next((s for s in client.transport_samples[before:] if s["op"] == "batch_turns"), None)
    profile = turn.get("profile") or {}
    emu = profile.get("emulation_ns", 0)
    log = profile.get("log_ns", 0)
    restore = profile.get("restore_ns", 0)
    read_ns = transport["read_ns"] if transport else 0.0
    worker_ipc = max(0.0, read_ns - emu - log - restore)
    return {
        "mailbox_ns": mailbox_ns, "batch_ns": batch_ns, "parse_ns": parse_ns,
        "emulation_ns": emu, "log_ns": log, "restore_ns": restore,
        "cycles": profile.get("cycles", 0), "frames": turn["frames"],
        "worker_ipc_ns": worker_ipc,
        "encode_ns": transport["encode_ns"] if transport else 0.0,
        "write_ns": transport["write_ns"] if transport else 0.0,
        "decode_ns": transport["decode_ns"] if transport else 0.0,
        "stop": turn.get("stop"), "states": parsed["states"], "boundary": parsed["boundary"],
        "events": events,
    }


def run_multiturn(client: rom_bridge.RomWorkerClient, address: int, boot_handle: str,
                  fixture: dict, rid) -> dict:
    """P29-style multi-turn branch: build one root, then run each action from it."""
    measured = measure_batch(client, address, root_request(fixture), handle=boot_handle,
                             tag="b0-multi-root", request_id=next(rid))
    root_slot = client.save_checkpoint(name="b0-multi-root")
    client.drop_state(boot_handle)
    turns = [{"label": "root", "events": measured["events"], "stop": measured["stop"],
              "sha256": root_slot["sha256"], "frames": measured["frames"]}]
    for a in fixture["actions"]:
        label = a.get("label", "action")
        branch = measure_batch(client, address, action_request(fixture, a["action"]),
                               handle=root_slot["handle"], tag=f"b0-multi-{label}",
                               request_id=next(rid))
        slot = client.save_checkpoint(name=f"b0-multi-{label}")
        client.drop_state(slot["handle"])
        turns.append({"label": label, "events": branch["events"], "stop": branch["stop"],
                      "sha256": slot["sha256"], "frames": branch["frames"]})
    client.drop_state(root_slot["handle"])
    return {"turns": turns}


def run_session(iterations: int, warmup: int, min_log: bool, fixture: dict,
                multi_fixture: dict, *, worker: Path = None, elf: Path = None,
                extra_env: dict[str, str] | None = None) -> dict:
    worker = worker or WORKER
    elf = elf or ELF
    os.environ["SIDEDEX_WORKER_PROFILE"] = "1"
    os.environ["SIDEDEX_WORKER_MIN_LOG"] = "1" if min_log else "0"
    for key, value in (extra_env or {}).items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    rom_bridge.PROFILE_TRANSPORT = True
    client = rom_bridge.RomWorkerClient(
        rom_path=elf, worker_path=worker, expected_artifact_sha256=ELF_SHA,
        expected_worker_sha256=sha256_file(worker), timeout=120.0)
    try:
        boot = boot_to_ready(client)
        boot_slot = client.save_checkpoint(name="b0-boot")
        rid = itertools.count(1)

        root_req = root_request(fixture)
        roots = [measure_batch(client, boot["address"], root_req,
                               handle=boot_slot["handle"], tag=f"b0-root-{i}",
                               request_id=next(rid))
                 for i in range(iterations)]
        save_t0 = time.perf_counter()
        root_slot = client.save_checkpoint(name="b0-root")
        root_save_ns = (time.perf_counter() - save_t0) * 1e9

        action_req = action_request(fixture, fixture["actions"][0]["action"])
        for _ in range(warmup):
            measure_batch(client, boot["address"], action_req,
                          handle=root_slot["handle"], tag="b0-warmup", request_id=next(rid))
        transitions = [measure_batch(client, boot["address"], action_req,
                                     handle=root_slot["handle"], tag=f"b0-turn-{i}",
                                     request_id=next(rid))
                       for i in range(iterations)]

        query = None
        try:
            query = measure_batch(client, boot["address"],
                                  action_request(fixture, fixture["actions"][0]["action"],
                                                 operation="query-damage"),
                                  handle=root_slot["handle"], tag="b0-query",
                                  request_id=next(rid))
        except Exception as exc:  # noqa: BLE001 - query support is not a B0 requirement
            query = {"error": f"{type(exc).__name__}: {exc}"}

        client.drop_state(root_slot["handle"])
        multi = run_multiturn(client, boot["address"], boot_slot["handle"], multi_fixture, rid)
        return {
            "boot": boot, "root_slots": roots, "root_slot_sha256": root_slot["sha256"],
            "root_slot_bytes": root_slot["bytes"], "root_save_ns": root_save_ns,
            "transitions": transitions,
            "query": query, "multi": multi,
        }
    finally:
        client.close()
        rom_bridge.PROFILE_TRANSPORT = False
        for key in (extra_env or {}):
            os.environ.pop(key, None)


def _row(label: str, ns_values: list[float], unit: str = "ms", digits: int = 3) -> str:
    p50 = _pct(ns_values, 0.50) / 1e6
    p95 = _pct(ns_values, 0.95) / 1e6
    return f"| {label} | {p50:.{digits}f} {unit} | {p95:.{digits}f} {unit} |"


def summarize(samples: list[dict]) -> dict:
    return {key: [s[key] for s in samples] for key in
            ("batch_ns", "emulation_ns", "log_ns", "restore_ns", "worker_ipc_ns",
             "encode_ns", "write_ns", "decode_ns", "mailbox_ns", "parse_ns",
             "cycles", "frames")}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=30)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--out", type=str,
                        default=r"C:/Users/Abj1/.codex/artifacts/B/B0-profile.md")
    parser.add_argument("--worker", type=Path, default=WORKER,
                        help="worker binary to profile (default: the pinned-layout build)")
    parser.add_argument("--elf", type=Path, default=ELF, help="v8c test ELF")
    parser.add_argument("--extra-env", type=str, default="",
                        help="comma-separated KEY=VAL worker env overrides for a B1a variant")
    args = parser.parse_args()
    extra_env = dict(item.split("=", 1) for item in args.extra_env.split(",") if item)

    if not args.elf.is_file() or not args.worker.is_file():
        raise SystemExit("pinned ELF or worker binary is missing; build first")
    assert sha256_file(args.elf) == ELF_SHA, "ELF SHA-256 does not match the v8c pin"
    worker_sha = sha256_file(args.worker)
    fixture = load_fixture("p01_physical_neutral.json")
    multi_fixture = load_fixture("p29_multiturn_branch.json")

    baseline = run_session(args.iterations, args.warmup, False, fixture, multi_fixture,
                           worker=args.worker, elf=args.elf, extra_env=extra_env)
    headless = run_session(args.iterations, args.warmup, True, fixture, multi_fixture,
                           worker=args.worker, elf=args.elf, extra_env=extra_env)

    parity_ok = (
        baseline["root_slot_sha256"] == headless["root_slot_sha256"]
        and baseline["multi"] == headless["multi"]
        and all(r["events"] == h["events"] for r, h in zip(baseline["root_slots"], headless["root_slots"]))
        and all(r["events"] == h["events"] for r, h in zip(baseline["transitions"], headless["transitions"]))
    )

    b_root = summarize(baseline["root_slots"])
    b_tr = summarize(baseline["transitions"])
    h_tr = summarize(headless["transitions"])
    boot = baseline["boot"]

    lines: list[str] = []
    lines.append("# B0 profile: P1 root construction and transition")
    lines.append("")
    lines.append(f"- Fixture: P1 (physical damage, neutral); {args.iterations} iterations after "
                 f"{args.warmup} warmup; mode normal")
    lines.append(f"- ELF: `{ELF_SHA}` (protocol {PROTOCOL})")
    lines.append(f"- Release ROM: `{ROM_SHA}`")
    lines.append(f"- Worker: `{worker_sha}` (built from `codex/rom-worker-b`)")
    lines.append("")
    lines.append("## Boot to mailbox ready (one-time worker setup)")
    lines.append("")
    lines.append("| bucket | value |")
    lines.append("| --- | --- |")
    lines.append(f"| wall time | {boot['elapsed_ns'] / 1e6:.3f} ms |")
    lines.append(f"| frames | {boot['frames']} |")
    lines.append(f"| cycles | {boot['cycles']} |")
    lines.append(f"| emulation (runFrame) | {boot['emulation_ns'] / 1e6:.3f} ms |")
    lines.append(f"| mLog / ROMTRACE I/O | {boot['log_ns'] / 1e6:.3f} ms |")
    lines.append(f"| run_frames JSONL calls | {boot['calls']} |")
    lines.append("")
    lines.append("## Root construction (fixture root request run to the opening boundary)")
    lines.append("")
    lines.append("| bucket | p50 | p95 |")
    lines.append("| --- | --- | --- |")
    lines.append(_row("total (Python batch_turns)", b_root["batch_ns"]))
    lines.append(_row("(a) emulation run loop", b_root["emulation_ns"]))
    lines.append(_row("(b) mLog / ROMTRACE I/O", b_root["log_ns"]))
    lines.append(_row("(c) JSONL IPC - Python encode", b_root["encode_ns"], "us", 2))
    lines.append(_row("(c) JSONL IPC - Python write", b_root["write_ns"], "us", 2))
    lines.append(_row("(c) JSONL IPC - Python decode", b_root["decode_ns"], "us", 2))
    lines.append(_row("(c) JSONL IPC - worker residual", b_root["worker_ipc_ns"]))
    lines.append(_row("(d) Python parse - mailbox encode", b_root["mailbox_ns"], "us", 2))
    lines.append(_row("(d) Python parse - domain parse", b_root["parse_ns"], "us", 2))
    lines.append(_row("(e) savestate restore", b_root["restore_ns"]))
    lines.append(f"- cycles p50: {_pct(b_root['cycles'], 0.50):.0f}; frames p50: {_pct(b_root['frames'], 0.50):.0f}")
    lines.append(f"- boundary savestate after root: {baseline['root_slot_bytes']} bytes, "
                 f"sha256 `{baseline['root_slot_sha256'][:12]}...`; save checkpoint round trip "
                 f"{baseline['root_save_ns'] / 1e6:.3f} ms")
    lines.append("")
    lines.append("## Warm transition (restore root boundary + run fixture action, P1)")
    lines.append("")
    lines.append("| bucket | p50 | p95 |")
    lines.append("| --- | --- | --- |")
    lines.append(_row("total (Python batch_turns)", b_tr["batch_ns"]))
    lines.append(_row("(a) emulation run loop", b_tr["emulation_ns"]))
    lines.append(_row("(b) mLog / ROMTRACE I/O", b_tr["log_ns"]))
    lines.append(_row("(c) JSONL IPC - Python encode", b_tr["encode_ns"], "us", 2))
    lines.append(_row("(c) JSONL IPC - Python write", b_tr["write_ns"], "us", 2))
    lines.append(_row("(c) JSONL IPC - Python decode", b_tr["decode_ns"], "us", 2))
    lines.append(_row("(c) JSONL IPC - worker residual", b_tr["worker_ipc_ns"]))
    lines.append(_row("(d) Python parse - mailbox encode", b_tr["mailbox_ns"], "us", 2))
    lines.append(_row("(d) Python parse - domain parse", b_tr["parse_ns"], "us", 2))
    lines.append(_row("(e) savestate restore", b_tr["restore_ns"]))
    lines.append(f"- cycles p50: {_pct(b_tr['cycles'], 0.50):.0f}; frames p50: {_pct(b_tr['frames'], 0.50):.0f}; "
                 f"stop p50: {baseline['transitions'][0]['stop']}")
    if baseline["query"] and "error" not in baseline["query"]:
        lines.append(f"- query-damage (bonus): {baseline['query']['batch_ns'] / 1e6:.3f} ms, "
                     f"{baseline['query']['frames']} frames")
    lines.append("")
    lines.append("## Headless settings")
    lines.append("")
    lines.append("| setting | state | wall-time delta vs baseline (transition p50) | parity |")
    lines.append("| --- | --- | --- | --- |")
    total_delta = (_pct(h_tr["batch_ns"], 0.50) - _pct(b_tr["batch_ns"], 0.50)) / 1e6
    emu_delta = (_pct(h_tr["emulation_ns"], 0.50) - _pct(b_tr["emulation_ns"], 0.50)) / 1e6
    log_delta = (_pct(h_tr["log_ns"], 0.50) - _pct(b_tr["log_ns"], 0.50)) / 1e6
    parity = "pass" if parity_ok else "FAIL"
    lines.append(f"| minimal log level (mLogFilter drops non-GBA-Debug) | on "
                 f"(SIDEDEX_WORKER_MIN_LOG=1) | {total_delta:+.3f} ms total "
                 f"(emulation {emu_delta:+.3f} ms, log I/O {log_delta:+.3f} ms) | {parity} |")
    lines.append("| no frame sync / throttle | already off - the worker never attaches an "
                 "mCoreSync, so mCoreSyncPostFrame is a no-op | 0 | n/a |")
    lines.append("| no video rendering | already off - no frontend video buffer is attached; GBA "
                 "PPU emulation is intrinsic to the run loop | 0 | n/a |")
    lines.append("| audio off / no sample generation | already off at output - no frontend pulls "
                 "samples; the blip feed is intrinsic to the core | 0 | n/a |")
    lines.append("")
    if parity_ok:
        lines.append("Parity holds on P1 and the P29 multi-turn branch: identical root savestate "
                     "SHA-256 and byte-identical trace/state records at every boundary. The "
                     "minimal-log filter only suppresses host-side callbacks for non-ROMTRACE "
                     "categories, so it cannot change emulated state.")
    else:
        lines.append("PARITY FAILURE - do not keep the minimal-log setting.")
    lines.append("")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0 if parity_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
