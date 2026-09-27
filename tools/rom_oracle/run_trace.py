#!/usr/bin/env python3
"""Build and capture the pinned Sidedex smart-AI ROM test trace."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
PIN = json.loads((HERE / "pinned_source.json").read_text(encoding="utf-8-sig"))
TRACE_NAME = PIN["trace_test"]
TRACKED_OVERLAY = ("include/test_runner.h", "src/battle_script_commands.c", "test/test_runner_battle.c")
FIXTURE_PATH = "test/battle/ai/sidedex_trace.c"


def parse_trace_lines(lines: list[str]) -> dict[str, Any]:
    """Parse ROMTRACE debug messages into the pinned, replay-oriented schema."""
    result: dict[str, Any] = {
        "events": [], "phases": [], "fields": [], "party": [], "active": [],
        "blobs": [], "rng": [],
    }
    for raw in lines:
        marker = "ROMTRACE|"
        pos = raw.find(marker)
        if pos < 0:
            continue
        fields = raw[pos:].strip().split("|")
        kind = fields[1]
        if kind in {"ability", "hp", "status"}:
            values = [int(value) for value in fields[2:]]
            if kind == "ability" and len(values) == 3:
                result["events"].append({"type": kind, "turn": values[0], "battler": values[1], "ability": values[2]})
            elif kind == "hp" and len(values) == 4:
                result["events"].append({"type": kind, "turn": values[0], "battler": values[1], "old_hp": values[2], "new_hp": values[3]})
            elif kind == "status" and len(values) == 3:
                result["events"].append({"type": kind, "turn": values[0], "battler": values[1], "status1": values[2]})
            else:
                raise ValueError(f"malformed ROMTRACE {kind} record: {raw.rstrip()}")
        elif kind == "action":
            if len(fields) == 7 and fields[4] == "move":
                result["events"].append({"type": kind, "turn": int(fields[2]), "battler": int(fields[3]), "action": "move", "move": int(fields[5]), "target": int(fields[6])})
            elif len(fields) == 6 and fields[4] == "switch":
                result["events"].append({"type": kind, "turn": int(fields[2]), "battler": int(fields[3]), "action": "switch", "party_index": int(fields[5])})
            else:
                raise ValueError(f"malformed ROMTRACE action record: {raw.rstrip()}")
        elif kind == "switch-settled" and len(fields) == 6:
            turn, battler, party_index, species = map(int, fields[2:])
            result["events"].append({"type": kind, "turn": turn, "battler": battler, "party_index": party_index, "species": species})
        elif kind == "phase" and len(fields) == 4:
            result["phases"].append({"turn": int(fields[2]), "phase": fields[3]})
        elif kind == "field" and len(fields) == 5:
            result["fields"].append({"turn": int(fields[2]), "battle_type_flags": int(fields[3]), "weather": int(fields[4])})
        elif kind == "party" and len(fields) == 17:
            values = list(map(int, fields[2:]))
            result["party"].append({
                "turn": values[0], "side": values[1], "slot": values[2], "species": values[3],
                "hp": values[4], "status1": values[5], "item": values[6], "ability_num": values[7],
                "moves": values[8:12], "pp": values[12:16],
            })
        elif kind == "active" and len(fields) == 31:
            values = list(map(int, fields[2:]))
            result["active"].append({
                "turn": values[0], "battler": values[1], "party_index": values[2], "species": values[3],
                "hp": values[4], "max_hp": values[5], "status1": values[6], "status2": values[7],
                "status3": values[8], "ability": values[9], "item": values[10],
                "stat_stages": values[11:18], "moves": values[18:22], "pp": values[22:26],
                "types": values[26:29],
            })
        elif kind == "blob" and len(fields) == 8:
            result["blobs"].append({
                "turn": int(fields[2]), "owner": int(fields[3]), "index": int(fields[4]),
                "kind": fields[5], "chunk": int(fields[6]), "hex": fields[7],
            })
        elif kind == "rng" and len(fields) == 11:
            values = list(map(int, fields[2:]))
            result["rng"].append({"turn": values[0], "main": values[1:5], "secondary": values[5:9]})
        else:
            raise ValueError(f"unrecognized ROMTRACE record: {raw.rstrip()}")
    if not result["events"] or not result["phases"] or not result["active"] or not result["party"] or not result["rng"]:
        raise ValueError("trace is incomplete: expected events, phases, active state, full parties, and both RNG streams")
    if not any(event["type"] == "switch-settled" for event in result["events"]):
        raise ValueError("trace is incomplete: no post-switch settled-state marker")
    return result

def run(command: list[str], cwd: Path, *, capture: bool = False) -> str:
    completed = subprocess.run(command, cwd=cwd, text=True, stdout=subprocess.PIPE if capture else None,
                               stderr=subprocess.STDOUT if capture else None)
    output = completed.stdout or "" if capture else ""
    if completed.returncode:
        if capture:
            sys.stderr.write(output)
        raise SystemExit(completed.returncode)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="full pinned pokeemerald source checkout")
    parser.add_argument("--jobs", type=int, default=4)
    args = parser.parse_args()
    source = args.source.resolve()
    head = run(["git", "rev-parse", "HEAD"], source, capture=True).strip()
    if head != PIN["source_commit"]:
        raise SystemExit(f"source pin mismatch: expected {PIN['source_commit']}, got {head}")

    patch = HERE / "trace_hooks.patch"
    expected_patch = patch.read_bytes()
    current_patch = run(["git", "diff", "--unified=0", "--binary", "--", *TRACKED_OVERLAY], source, capture=True).encode()
    if current_patch not in (b"", expected_patch):
        raise SystemExit("pinned source has tracked changes outside the exact ROM oracle overlay")
    status = run(["git", "status", "--porcelain", "--untracked-files=all"], source, capture=True)
    allowed = set(TRACKED_OVERLAY) | {FIXTURE_PATH}
    dirty_paths = {line[3:].split(" -> ")[-1] for line in status.splitlines() if line}
    unexpected = dirty_paths - allowed
    if unexpected:
        raise SystemExit("pinned source has unrelated dirty paths: " + ", ".join(sorted(unexpected)))

    fixture_target = source / FIXTURE_PATH
    fixture_data = (HERE / "sidedex_trace.c").read_bytes()
    if fixture_target.exists() and fixture_target.read_bytes() != fixture_data:
        raise SystemExit(f"refusing to overwrite existing {fixture_target}")
    if current_patch == b"":
        run(["git", "apply", str(patch)], source)
    if not fixture_target.exists():
        fixture_target.write_bytes(fixture_data)
    verified_patch = run(["git", "diff", "--unified=0", "--binary", "--", *TRACKED_OVERLAY], source, capture=True).encode()
    if verified_patch != expected_patch:
        raise SystemExit("applied ROM oracle source diff does not match the committed overlay patch")

    output = run(["make", f"-j{max(1, args.jobs)}", "check", f"TESTS={TRACE_NAME}"], source, capture=True)
    sys.stdout.write(output)
    if "Tests \x1b[32mPASSED\x1b[0m:          1" not in output:
        raise SystemExit("targeted ROM test did not report exactly one passing fixture")

    test_rom = source / "pokeemerald-test.elf"
    headless_rom = source / "pokeemerald-test-headless.elf"
    for artifact, field in ((test_rom, "test_elf_sha256"), (headless_rom, "test_headless_elf_sha256")):
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        if digest != PIN[field]:
            raise SystemExit(f"{artifact.name} hash mismatch: expected {PIN[field]}, got {digest}")

    emulator = source / "tools/mgba/mgba-rom-test"
    emulator_output = run([str(emulator), "-l", "255", str(headless_rom)], source, capture=True)
    marker = "ROMTRACE|"
    raw_actual = [line[line.find(marker):].strip() for line in emulator_output.splitlines() if marker in line]
    raw_expected = (HERE / "expected_trace.raw.txt").read_text(encoding="utf-8-sig").splitlines()
    if raw_actual != raw_expected:
        raise SystemExit("raw mGBA trace lines differ from expected_trace.raw.txt")
    raw_digest = hashlib.sha256(("\n".join(raw_actual) + "\n").encode()).hexdigest()
    if raw_digest != PIN["raw_trace_sha256"]:
        raise SystemExit(f"raw trace hash mismatch: expected {PIN['raw_trace_sha256']}, got {raw_digest}")

    actual = parse_trace_lines(emulator_output.splitlines())
    expected = json.loads((HERE / "expected_trace.json").read_text(encoding="utf-8-sig"))
    if actual != expected:
        print(json.dumps({"expected": expected, "actual": actual}, indent=2), file=sys.stderr)
        raise SystemExit("captured ROM trace differs from expected_trace.json")
    print(json.dumps(actual, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
