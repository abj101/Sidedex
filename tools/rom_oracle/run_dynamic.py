#!/usr/bin/env python3
"""Run one requested battle turn in the pinned, instrumented ARM test ELF."""

from __future__ import annotations

import argparse
import json
import struct
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from rom_bridge import RomWorkerClient  # noqa: E402


MAGIC = 0x53445831  # SDX1
VERSION = 4
SUBMITTED = 1
MAILBOX_BYTES = 740
MAX_TRACE_RECORDS = 256
ID_MAP = json.loads((Path(__file__).with_name("numeric_id_map.json")).read_text(encoding="utf-8"))
PIN = json.loads((Path(__file__).with_name("pinned_source.json")).read_text(encoding="utf-8"))
if ID_MAP.get("source_sha") != PIN["source_commit"]:
    raise RuntimeError("artifact-mismatch: numeric ID map is for a different source commit")


def numeric_id(kind: str, source_id: str | int) -> int:
    """Resolve a pinned source ID; reject unknown names instead of guessing."""
    try:
        values = ID_MAP["ids"][kind]
    except KeyError:
        raise ValueError(f"invalid-id: unknown ID category {kind!r}") from None
    if type(source_id) is int:
        if source_id in values.values():
            return source_id
    elif isinstance(source_id, str) and source_id in values:
        return values[source_id]
    raise ValueError(f"invalid-id: unknown {kind} ID {source_id!r}")


def _ints(values, count: int, maximum: int) -> list[int]:
    if not isinstance(values, list) or len(values) != count or any(
        type(value) is not int or not 0 <= value <= maximum for value in values
    ):
        raise ValueError(f"expected {count} integers from 0 through {maximum}")
    return values


def encode_request(request: dict) -> bytes:
    """Encode a six-slot trainer-single state and optional one-turn replay."""
    for side in ("player", "opponent"):
        active_index = request.get(f"{side}_active_index", 0)
        if type(active_index) is not int or active_index != 0:
            raise ValueError(f"incomplete-state: {side} active index must be slot 0")
    if request.get("trainer_items"):
        raise ValueError("incomplete-state: trainer item usage is not represented by the test hook")
    ai_flags = request["ai_flags"]
    if isinstance(ai_flags, list):
        ai_flags = _ai_flags(ai_flags)
    ai_flags = _ints([ai_flags], 1, 0xFFFFFFFF)[0]
    _validate_clear_field(request.get("field"))
    player_action = request["candidate_action"]
    kind, move, slot = _action(player_action)
    replay = request.get("replay_turns", [])
    if not isinstance(replay, list) or len(replay) > 1:
        raise ValueError("incomplete-state: oracle supports at most one replayed turn")
    replay_action = replay[0] if replay else {}
    if replay and ("player_action" not in replay_action or "enemy_action" not in replay_action):
        raise ValueError("incomplete-state: replay requires both recorded actions")
    player_replay = _action(replay_action.get("player_action")) if replay else (0, 0, 0)
    opponent_replay = _action(replay_action.get("enemy_action")) if replay else (0, 0, 0)
    rng = _ints(request["rng_main"], 4, 0xFFFFFFFF) + _ints(
        request["rng_secondary"], 4, 0xFFFFFFFF
    )
    sides = []
    counts = []
    for side in ("player_party", "opponent_party"):
        party = request[side]
        if not isinstance(party, list) or not 1 <= len(party) <= 6:
            raise ValueError("incomplete-state: oracle supports one to six Pokémon per side")
        counts.append(len(party))
        sides.append(b"".join(_encode_mon(mon) for mon in party)
                     + b"\0" * ((6 - len(party)) * 56))
    if kind == 1 and slot >= counts[0]:
        raise ValueError("no-legal-actions: candidate switch slot is outside the player party")
    if kind == 0 and move not in [numeric_id("moves", value) for value in request["player_party"][0]["moves"]]:
        raise ValueError("no-legal-actions: candidate move is not in the active player moves")
    if player_replay[0] == 1 and player_replay[2] >= counts[0]:
        raise ValueError("invalid-state: replay player switch slot is outside the party")
    if opponent_replay[0] == 1 and opponent_replay[2] >= counts[1]:
        raise ValueError("invalid-state: replay enemy switch slot is outside the party")
    if player_replay[0] == 0 and replay and player_replay[1] not in [numeric_id("moves", value) for value in request["player_party"][0]["moves"]]:
        raise ValueError("unrepresentable-history: recorded player move is not in the active set")
    if opponent_replay[0] == 0 and replay and opponent_replay[1] not in [numeric_id("moves", value) for value in request["opponent_party"][0]["moves"]]:
        raise ValueError("unrepresentable-history: recorded enemy move is not in the active set")
    header = struct.pack("<IHHI4H8I2H", MAGIC, VERSION, SUBMITTED, ai_flags,
                         kind, move, slot, len(replay), *rng, *counts)
    payload = header + b"".join(sides)
    payload += struct.pack("<6H", *player_replay, *opponent_replay)
    assert len(payload) == MAILBOX_BYTES
    return payload


def _encode_mon(mon: dict) -> bytes:
    core = [numeric_id("species", mon["species"]), mon["level"],
            numeric_id("abilities", mon["ability"]), numeric_id("items", mon["item"]),
            numeric_id("natures", mon["nature"]), mon["hp"], mon["max_hp"], 0]
    if type(mon["hp"]) is not int or type(mon["max_hp"]) is not int or not 0 < mon["hp"] <= mon["max_hp"] <= 0xFFFF:
        raise ValueError("incomplete-state: HP and MaxHP must be valid positive values")
    status = numeric_id("statuses", mon["status1"])
    moves = _ints([numeric_id("moves", move_id) for move_id in mon["moves"]], 4, 0xFFFF)
    pp = _ints(mon["pp"], 4, 255)
    ivs = _ints(mon["ivs"], 6, 31)
    evs = _ints(mon["evs"], 6, 252)
    if sum(evs) > 510:
        raise ValueError("invalid-state: EV total exceeds 510")
    return struct.pack("<8HI4H4B12H", *core, status, *moves, *pp, *ivs, *evs)


def _validate_clear_field(field: dict | None) -> None:
    expected = {"weather": "WEATHER_NONE", "terrain": "TERRAIN_NONE",
                "trick_room": False, "player_screens": 0, "enemy_screens": 0,
                "player_hazards": 0, "enemy_hazards": 0}
    if field != expected:
        raise ValueError("incomplete-state: only a verified clear battle-start field is supported")


def _ai_flags(names: list[str]) -> int:
    if any(not isinstance(name, str) for name in names):
        raise ValueError("invalid-id: AI flags must be source names")
    mapping = ID_MAP["ids"]["ai_flags"]
    if any(name not in mapping for name in names):
        unknown = next(name for name in names if name not in mapping)
        raise ValueError(f"invalid-id: unknown AI flag {unknown!r}")
    result = 0
    for name in names:
        result |= mapping[name]
    return result


def _action(action: dict | None) -> tuple[int, int, int]:
    if not isinstance(action, dict):
        raise ValueError("invalid-state: expected an action object")
    if action.get("kind") == "move":
        return 0, numeric_id("moves", action["move_id"]), 0
    if action.get("kind") == "switch":
        slot = action.get("slot")
        _ints([slot], 1, 5)
        return 1, 0, slot
    raise ValueError("invalid-state: action must be a move or switch")


def _trace(reply: dict) -> list[str]:
    marker = "ROMTRACE|"
    trace = [line[line.index(marker):].strip() for line in reply["trace_events"] if marker in line]
    if len(trace) > MAX_TRACE_RECORDS:
        raise RuntimeError("engine-error: bounded oracle trace exceeded 256 records")
    return trace


def run_turn(elf: Path, sha256: str, request: dict) -> list[str]:
    payload = encode_request(request)
    with RomWorkerClient(rom_path=elf, expected_artifact_sha256=sha256,
                         timeout=15.0) as worker:
        trace = []
        address = None
        for _ in range(400):
            trace.extend(_trace(worker.run_frames(60)))
            ready = [line for line in trace if line.startswith("ROMTRACE|ready|")]
            if ready:
                fields = ready[-1].split("|")
                if len(fields) != 4 or int(fields[3]) != MAILBOX_BYTES:
                    raise RuntimeError("invalid battle mailbox announcement")
                address = int(fields[2])
                break
        if address is None:
            raise TimeoutError("battle harness did not announce its mailbox")
        worker.save_state()
        result = worker.batch_turns(
            address, [payload], mailbox_bytes=MAILBOX_BYTES,
            engine_elf_sha256=sha256, deadline_ms=int(time.monotonic() * 1000) + 15_000,
            request_id="dynamic-oracle", max_frames=24_000,
        )
        turn = result["turns"][0]["events"]
        complete = [line for line in turn if line.startswith("ROMTRACE|complete|")]
        if len(complete) != 1:
            raise RuntimeError("incomplete-state: expected exactly one completed turn marker")
        fields = complete[0].split("|")
        if len(fields) != 3:
            raise RuntimeError("invalid completion marker")
        turn_number = int(fields[2])
        required = (f"ROMTRACE|phase|{turn_number}|after-turn",
                    f"ROMTRACE|party|{turn_number}|",
                    f"ROMTRACE|active|{turn_number}|",
                    f"ROMTRACE|rng|{turn_number}|")
        if not any(record.startswith("ROMTRACE|action|") for record in turn) or not all(
            any(record.startswith(prefix) for record in turn) for prefix in required
        ):
            raise RuntimeError("incomplete-state: oracle returned an incomplete turn trace")
        return turn


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--elf", required=True, type=Path)
    parser.add_argument("--sha256", required=True, help="expected SHA-256 of this test ELF")
    parser.add_argument("--request", required=True, type=Path, help="JSON battle request")
    args = parser.parse_args()
    request = json.loads(args.request.read_text(encoding="utf-8"))
    expected_elf = PIN["dynamic_oracle"]["headless_elf_sha256"]
    if args.sha256.upper() != expected_elf.upper():
        raise SystemExit(f"artifact-mismatch: expected test ELF {expected_elf}, got {args.sha256.upper()}")
    print(json.dumps({"test_elf_sha256": args.sha256.upper(),
                      "trace": run_turn(args.elf, args.sha256, request)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
