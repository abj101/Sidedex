#!/usr/bin/env python3
"""Build and capture the pinned Sidedex smart-AI ROM test trace."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
PIN = json.loads((HERE / "pinned_source.json").read_text(encoding="utf-8-sig"))
TRACE_NAME = PIN["trace_test"]


def parse_trace_lines(lines: list[str]) -> dict[str, Any]:
    """Parse ROMTRACE debug messages into the committed deterministic schema."""
    result: dict[str, Any] = {"events": [], "final_state": [], "rng": None}
    for raw in lines:
        marker = "ROMTRACE|"
        pos = raw.find(marker)
        if pos < 0:
            continue
        fields = raw[pos:].strip().split("|")
        kind = fields[1]
        nums = [int(value) for value in fields[2:] if value.lstrip("-").isdigit()]
        if kind == "ability" and len(nums) == 2:
            result["events"].append({"type": kind, "battler": nums[0], "ability": nums[1]})
        elif kind == "action" and len(fields) == 6 and fields[3] == "move":
            result["events"].append({"type": kind, "battler": int(fields[2]), "action": "move", "move": int(fields[4]), "target": int(fields[5])})
        elif kind == "action" and len(fields) == 5 and fields[3] == "switch":
            result["events"].append({"type": kind, "battler": int(fields[2]), "action": "switch", "party_index": int(fields[4])})
        elif kind == "hp" and len(nums) == 3:
            result["events"].append({"type": kind, "battler": nums[0], "old_hp": nums[1], "new_hp": nums[2]})
        elif kind == "status" and len(nums) == 2:
            result["events"].append({"type": kind, "battler": nums[0], "status1": nums[1]})
        elif kind == "final" and len(nums) == 15:
            result["final_state"].append({
                "battler": nums[0], "species": nums[1], "hp": nums[2], "max_hp": nums[3],
                "status1": nums[4], "ability": nums[5], "attack_stage": nums[6],
                "moves": nums[7:11], "pp": nums[11:15],
            })
        elif kind == "rng" and len(nums) == 4:
            result["rng"] = nums
        else:
            raise ValueError(f"unrecognized ROMTRACE record: {raw.rstrip()}")
    if not result["events"] or len(result["final_state"]) != 2 or result["rng"] is None:
        raise ValueError("trace is incomplete: expected events, both active battlers, and RNG state")
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

    fixture_target = source / "test/battle/ai/sidedex_trace.c"
    fixture_data = (HERE / "sidedex_trace.c").read_bytes()
    if fixture_target.exists() and fixture_target.read_bytes() != fixture_data:
        raise SystemExit(f"refusing to overwrite existing {fixture_target}")
    if not fixture_target.exists():
        fixture_target.write_bytes(fixture_data)

    patch = HERE / "trace_hooks.patch"
    marker = "SidedexOracleTraceActive"
    target_runner = source / "test/test_runner_battle.c"
    if marker not in target_runner.read_text(encoding="utf-8"):
        run(["git", "apply", str(patch)], source)

    output = run(["make", f"-j{max(1, args.jobs)}", "check", f"TESTS={TRACE_NAME}"], source, capture=True)
    sys.stdout.write(output)
    if "Tests \x1b[32mPASSED\x1b[0m:          1" not in output:
        raise SystemExit("targeted ROM test did not report exactly one passing fixture")

    emulator = source / "tools/mgba/mgba-rom-test"
    headless_rom = source / "pokeemerald-test-headless.elf"
    emulator_output = run([str(emulator), "-l", "255", str(headless_rom)], source, capture=True)
    actual = parse_trace_lines(emulator_output.splitlines())
    expected = json.loads((HERE / "expected_trace.json").read_text(encoding="utf-8-sig"))
    if actual != expected:
        print(json.dumps({"expected": expected, "actual": actual}, indent=2), file=sys.stderr)
        raise SystemExit("captured ROM trace differs from expected_trace.json")
    print(json.dumps(actual, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())




