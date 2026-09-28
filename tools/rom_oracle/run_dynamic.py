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

from rom_bridge import RomWorkerClient, parse_v9_ready  # noqa: E402


MAGIC = 0x53445831  # SDX1
# Explicit operation field (contract section 7). v7 selects the query path with
# the legacy `secondPresent == 2` byte; v8 adds these op codes at offset 856.
OP_SIMULATE_TURN = 0
OP_QUERY_DAMAGE = 1
OP_QUERY_AI_ACTION = 2
MODE_NORMAL = 0
MODE_LOGIC_ONLY = 1
CRITICAL_POLICY_NORMAL = 0
CRITICAL_POLICY_ITEM_ABILITY_ONLY = 1
OPERATIONS = {"simulate-turn": OP_SIMULATE_TURN, "query-damage": OP_QUERY_DAMAGE,
              "query-ai-action": OP_QUERY_AI_ACTION}
QUERY_OPERATIONS = (OP_QUERY_DAMAGE, OP_QUERY_AI_ACTION)
CRITICAL_POLICIES = {"normal": CRITICAL_POLICY_NORMAL,
                     "item-ability-only": CRITICAL_POLICY_ITEM_ABILITY_ONLY}
# The trace echoes one mechanism label per policy; the ROM filters ordinary
# crits under item-ability-only but keeps the real crit rolls under `normal`.
CRITICAL_POLICY_MECHANISMS = {"normal": "real-crit-rolls",
                              "item-ability-only": "battle-script-filtered"}
V7_HEADER_BYTES = 856
V8_EXTENSION_BYTES = 8
# Contract Appendix B: v9 keeps the v8 864-byte prefix and appends the compact
# result buffer the ROM announced in its `ROMTRACE|ready|` line.
V9_EXTENSION_OFFSET = 864
V9_MAILBOX_BYTES = 876
V9_RESULT_VERSION = 1
V9_FLAG_COMPACT = 0x0001
# Contract C.6 flags: bit 0 compact result, bits 1-2 battle difficulty override,
# every other bit zero. The tree defines only easy and normal, so 3 is reserved.
V9_DIFFICULTY_SHIFT = 1
V9_DIFFICULTY_MASK = 0x0006
V9_DIFFICULTY_KEEP = 0
V9_DIFFICULTY_EASY = 1
V9_DIFFICULTY_NORMAL = 2
V9_DIFFICULTY_RESERVED = 3
V9_DIFFICULTY_LABELS = {"keep": V9_DIFFICULTY_KEEP, "easy": V9_DIFFICULTY_EASY,
                        "normal": V9_DIFFICULTY_NORMAL}
# Versions `encode_request` speaks. v9 shares the v8 body but adds a mandatory
# ROM-announced result buffer, so it has its own entry point (encode_v9_request).
SUPPORTED_PROTOCOL_VERSIONS = (7, 8)
SUBMITTED = 1
SECOND_ACTION_OFFSET = 836
SECOND_PRESENT_OFFSET = 842
V8_EXTENSION_OFFSET = 856
MAX_TRACE_RECORDS = 256
ID_MAP = json.loads((Path(__file__).with_name("numeric_id_map.json")).read_text(encoding="utf-8"))
PIN = json.loads((Path(__file__).with_name("pinned_source.json")).read_text(encoding="utf-8"))
if ID_MAP.get("source_sha") != PIN["source_commit"]:
    raise RuntimeError("artifact-mismatch: numeric ID map is for a different source commit")


def mailbox_bytes(protocol_version: int) -> int:
    """Contract Appendix A/B: v7 856, v8 864, v9 876 bytes."""
    if protocol_version == 7:
        return V7_HEADER_BYTES
    if protocol_version == 8:
        return V7_HEADER_BYTES + V8_EXTENSION_BYTES
    if protocol_version == 9:
        return V9_MAILBOX_BYTES
    raise ValueError(
        f"unsupported-protocol-version: {protocol_version!r} is not a known mailbox layout")


PINNED_PROTOCOL_VERSION = int(PIN["dynamic_oracle"]["protocol_version"])
# Backwards-compatible aliases: both follow the pin, so flipping the pin to 8
# switches the encoder to the v8 layout with no code change.
VERSION = PINNED_PROTOCOL_VERSION
MAILBOX_BYTES = mailbox_bytes(PINNED_PROTOCOL_VERSION)


def _protocol_version(protocol_version: int | None) -> int:
    value = PINNED_PROTOCOL_VERSION if protocol_version is None else protocol_version
    if type(value) is not int or value not in SUPPORTED_PROTOCOL_VERSIONS:
        raise ValueError(f"unsupported-protocol-version: {value!r}")
    return value


def operation(request: dict) -> int:
    """Explicit op, defaulting the legacy `query`/`post_switch_query` flags to QUERY_DAMAGE."""
    name = request.get("operation")
    if name is None:
        if request.get("query") or request.get("post_switch_query"):
            return OP_QUERY_DAMAGE
        return OP_SIMULATE_TURN
    if name not in OPERATIONS:
        raise ValueError(f"invalid-id: unknown operation {name!r}")
    return OPERATIONS[name]


def is_query(request: dict) -> bool:
    return operation(request) in QUERY_OPERATIONS


def _mode(request: dict) -> int:
    mode = request.get("mode", "normal")
    if mode == "normal":
        return MODE_NORMAL
    if mode == "logic-only":
        return MODE_LOGIC_ONLY
    raise ValueError(f"unsupported-mechanic: unknown execution mode {mode!r}")


def _critical_policy(request: dict) -> int:
    policy = request.get("critical_policy", "item-ability-only")
    if policy not in CRITICAL_POLICIES:
        raise ValueError(f"unsupported-policy: unknown critical policy {policy!r}")
    return CRITICAL_POLICIES[policy]


def critical_policy_label(request: dict) -> str:
    """The exact label the trace is expected to echo for this request."""
    return request.get("critical_policy", "item-ability-only")


def _policy_marker(request: dict) -> str:
    label = critical_policy_label(request)
    return f"ROMTRACE|critical-policy|{label}|{CRITICAL_POLICY_MECHANISMS[label]}"


# TOXIC_COUNTER is a mask inside status1, not a selectable standalone status.
_STATUS1_VALUES = {0, *range(1, 8), 8, 16, 32, 64, 128, 4096}


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


def encode_request(request: dict, protocol_version: int | None = None) -> bytes:
    """Encode a six-slot trainer-single state and optional query controls.

    `protocol_version` defaults to the pinned protocol from `pinned_source.json`.
    Version 7 writes the 856-byte v7 mailbox (query via `secondPresent == 2`);
    version 8 appends op/mode/criticalPolicy/requestId at offset 856 for an
    864-byte request. Both layouts share the v7 field offsets. The 876-byte v9
    request is `encode_v9_request` because it needs the ROM's announcement.
    """
    version = _protocol_version(protocol_version)
    query = is_query(request)
    op = operation(request)
    mode = _mode(request)
    policy = _critical_policy(request)
    if version < 8 and policy != CRITICAL_POLICY_ITEM_ABILITY_ONLY:
        raise ValueError(
            "unsupported-policy: protocol 7 supports only item-ability-only critical policy")
    request_id = _ints([request.get("request_id", 0)], 1, 0xFFFF)[0]
    for side in ("player", "opponent"):
        active_index = request.get(f"{side}_active_index", 0)
        if type(active_index) is not int or active_index != 0:
            raise ValueError(f"incomplete-state: {side} active index must be slot 0")
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
    if request.get("post_switch_query") and (not replay or player_replay[0] != 1):
        raise ValueError("invalid-state: post_switch_query requires a replayed player switch")
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
                     + b"\0" * ((6 - len(party)) * 64))
    if kind == 1 and slot >= counts[0]:
        raise ValueError("no-legal-actions: candidate switch slot is outside the player party")
    active_player_slot = player_replay[2] if replay and player_replay[0] == 1 else 0
    if kind == 0 and move not in [numeric_id("moves", value)
                                  for value in request["player_party"][active_player_slot]["moves"]]:
        raise ValueError("no-legal-actions: candidate move is not in the active player moves")
    if player_replay[0] == 1 and player_replay[2] >= counts[0]:
        raise ValueError("invalid-state: replay player switch slot is outside the party")
    if opponent_replay[0] == 1 and opponent_replay[2] >= counts[1]:
        raise ValueError("invalid-state: replay enemy switch slot is outside the party")
    if player_replay[0] == 0 and replay and player_replay[1] not in [numeric_id("moves", value) for value in request["player_party"][0]["moves"]]:
        raise ValueError("unrepresentable-history: recorded player move is not in the active set")
    if opponent_replay[0] == 0 and replay and opponent_replay[1] not in [numeric_id("moves", value) for value in request["opponent_party"][0]["moves"]]:
        raise ValueError("unrepresentable-history: recorded enemy move is not in the active set")
    second = request.get("second_action")
    if second is None:
        second_kind, second_move, second_slot = 0, 0, 0
    else:
        second_kind, second_move, second_slot = _action(second)
        # After a first-turn switch the active monster is the switch target, so
        # the second-turn move is validated against that slot's moves.
        active = request["player_party"][slot if kind == 1 else 0]
        if second_kind == 1 and not 0 < second_slot < counts[0]:
            raise ValueError("no-legal-actions: second switch slot is outside the player party")
        if second_kind == 0 and second_move not in [
                numeric_id("moves", value) for value in active["moves"]]:
            raise ValueError("no-legal-actions: second move is not in the active player moves")
    header = struct.pack("<IHHI4H8I2H", MAGIC, version, SUBMITTED, ai_flags,
                         kind, move, slot, len(replay), *rng, *counts)
    payload = header + b"".join(sides)
    payload += struct.pack("<6H", *player_replay, *opponent_replay)
    payload += struct.pack("<4H", second_kind, second_move, second_slot,
                           2 if query else (0 if second is None else 1))
    item_ids = []
    for item_id, count in (request.get("trainer_items") or {}).items():
        if type(count) is not int or count < 0:
            raise ValueError("incomplete-state: trainer item counts must be non-negative integers")
        item_ids.extend([numeric_id("items", item_id)] * count)
    if len(item_ids) > 4:
        raise ValueError("incomplete-state: a trainer can use at most four item slots")
    item_ids += [0] * (4 - len(item_ids))
    payload += struct.pack("<6H", *item_ids, sum(1 for value in item_ids if value), 0)
    if version >= 8:
        payload += struct.pack("<4H", op, mode, policy, request_id)
    assert len(payload) == mailbox_bytes(version)
    return payload


def v9_difficulty(value: int | str) -> int:
    """Contract C.6 battle difficulty override: 0 keep, 1 easy, 2 normal.

    include/constants/game_settings.h defines only the normal and easy modes, so
    the reserved value 3 is refused here rather than announced to the ROM.
    """
    if isinstance(value, str):
        if value not in V9_DIFFICULTY_LABELS:
            raise ValueError(f"invalid-request: unknown difficulty {value!r}")
        return V9_DIFFICULTY_LABELS[value]
    if type(value) is not int or not V9_DIFFICULTY_KEEP <= value <= V9_DIFFICULTY_RESERVED:
        raise ValueError(f"invalid-request: difficulty override {value!r} is not a C.6 value")
    if value == V9_DIFFICULTY_RESERVED:
        raise ValueError("unsupported-mechanic: difficulty 3 is reserved; the tree has no such mode")
    return value


def v9_flags(*, compact: bool = True, difficulty: int | str = V9_DIFFICULTY_KEEP) -> int:
    """Contract C.6 request flags: bit 0 compact result, bits 1-2 difficulty."""
    if type(compact) is not bool:
        raise ValueError("invalid-request: compact must be a boolean")
    return (V9_FLAG_COMPACT if compact else 0) | (v9_difficulty(difficulty) << V9_DIFFICULTY_SHIFT)


def encode_v9_request(request: dict, result_buffer: dict, *,
                      difficulty: int | str = V9_DIFFICULTY_KEEP, compact: bool = True) -> bytes:
    """Encode the 876-byte v9 request (contract Appendix B).

    The v9 request is the v8 request with those four little-endian fields
    appended: resultAddress:u32 at 864, resultCapacity:u32 at 868,
    resultVersion:u16 = 1 at 872 and flags:u16 at 874 (contract C.6: bit 0 = a
    compact result, bits 1-2 = the difficulty override, all other bits zero).

    `result_buffer` must be the `rom_bridge.parse_v9_ready` record for the ROM's
    own `ROMTRACE|ready|` announcement, so the address is never the caller's:
    a request carrying one is refused rather than encoded. `compact=False` asks
    for ROMTRACE records instead of an SDR2 result (encode_v9_trace_request).
    """
    if not isinstance(result_buffer, dict):
        raise ValueError(
            "invalid-state: the v9 result buffer must come from the ROM's ready announcement")
    for banned in ("result_address", "result_capacity", "result_version", "resultVersion"):
        if banned in request:
            raise ValueError(f"invalid-state: {banned} comes from the ROM announcement, not the caller")
    flags = v9_flags(compact=compact, difficulty=difficulty)
    payload = encode_request(request, protocol_version=8)
    # v9 shares the v8 body and extension; only the layout version in the header
    # (u16 at offset 4) and the appended result tail differ.
    payload = payload[:4] + struct.pack("<H", 9) + payload[6:]
    payload += struct.pack("<IIHH", result_buffer["result_address"],
                           result_buffer["result_capacity"], V9_RESULT_VERSION, flags)
    assert len(payload) == V9_MAILBOX_BYTES
    return payload


def encode_v9_trace_request(request: dict, result_buffer: dict, *,
                            difficulty: int | str = V9_DIFFICULTY_KEEP) -> bytes:
    """A v9 request with the compact bit clear (contract C.6 flags without bit 0).

    The hook answers with ROMTRACE records rather than an SDR2 result, which is
    how the verifier cross-checks a compact turn against the decoded projection
    and how package D drives queries through the same v9 mailbox. The announced
    result buffer is still required: the ROM validates that range on every v9
    request, compact or not.
    """
    return encode_v9_request(request, result_buffer, difficulty=difficulty, compact=False)


def _encode_mon(mon: dict) -> bytes:
    core = [numeric_id("species", mon["species"]), mon["level"],
            numeric_id("abilities", mon["ability"]), numeric_id("items", mon["item"]),
            numeric_id("natures", mon["nature"]), mon["hp"], mon["max_hp"], 0]
    if type(mon["hp"]) is not int or type(mon["max_hp"]) is not int or not 0 < mon["hp"] <= mon["max_hp"] <= 0xFFFF:
        raise ValueError("incomplete-state: HP and MaxHP must be valid positive values")
    # A raw int is a status1 word (1..7 = sleep turns left, which no name maps
    # to); names go through the pinned ID map.
    raw = mon["status1"]
    status = raw if type(raw) is int else numeric_id("statuses", raw)
    if status not in _STATUS1_VALUES:
        raise ValueError("invalid-id: status1 must be a selectable battle status")
    moves = _ints([numeric_id("moves", move_id) for move_id in mon["moves"]], 4, 0xFFFF)
    pp = _ints(mon["pp"], 4, 255)
    ivs = _ints(mon["ivs"], 6, 31)
    evs = _ints(mon["evs"], 6, 252)
    if sum(evs) > 510:
        raise ValueError("invalid-state: EV total exceeds 510")
    held_item_enabled = mon.get("held_item_enabled", True)
    if type(held_item_enabled) is not bool:
        raise ValueError("invalid-state: held_item_enabled must be a boolean")
    stages = _ints(mon.get("stat_stages", [6] * 7), 7, 12)
    return struct.pack("<8HI4H4B12HB7B", *core, status, *moves, *pp, *ivs, *evs,
                       int(held_item_enabled), *stages)


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


def run_v9_turn(elf: Path, sha256: str, request: dict, *, worker_path: Path | None = None,
                worker_sha256: str | None = None, request_id: str = "dynamic-oracle-v9",
                difficulty: int | str = V9_DIFFICULTY_KEEP) -> dict:
    """Drive one v9 compact turn and return the decoded SDR2 turns.

    Needs the A1 candidate ELF. The result address and capacity are read from the
    ROM's own `ROMTRACE|ready|` announcement; the request cannot supply them.
    """
    size = mailbox_bytes(9)
    kwargs: dict = {"expected_artifact_sha256": sha256, "wire_protocol": 3, "timeout": 15.0}
    if worker_path is not None:
        kwargs["worker_path"] = worker_path
    if worker_sha256 is not None:
        kwargs["expected_worker_sha256"] = worker_sha256
    trace: list[str] = []
    with RomWorkerClient(rom_path=elf, **kwargs) as worker:
        announced = None
        for _ in range(400):
            trace.extend(_trace(worker.run_frames(60)))
            ready = [line for line in trace if line.startswith("ROMTRACE|ready|")]
            if ready:
                announced = parse_v9_ready(ready)
                break
        if announced is None:
            raise TimeoutError("battle harness did not announce a v9 mailbox")
        payload = encode_v9_request(request, announced, difficulty=difficulty)
        worker.save_state()
        result = worker.batch_turns_v3(
            announced["address"], announced["result_address"], announced["result_capacity"],
            [payload], mailbox_bytes=size, engine_elf_sha256=sha256,
            deadline_ms=int(time.monotonic() * 1000) + 15_000, request_id=request_id)
    return {"announced": announced, "payload_bytes": len(payload), "turns": result["turns"]}


def run_turn(elf: Path, sha256: str, request: dict, *, protocol_version: int | None = None) -> list[str]:
    version = _protocol_version(protocol_version)
    if version >= 9:
        raise RuntimeError("unsupported-protocol-version: v9 returns SDR2 bytes, use run_v9_turn")
    size = mailbox_bytes(version)
    if version == PINNED_PROTOCOL_VERSION:
        expected_elf = PIN["dynamic_oracle"]["headless_elf_sha256"]
        if sha256.upper() != expected_elf.upper():
            raise RuntimeError(f"artifact-mismatch: expected test ELF {expected_elf}, got {sha256.upper()}")
    query = is_query(request)
    payload = encode_request(request, protocol_version=version)
    with RomWorkerClient(rom_path=elf, expected_artifact_sha256=sha256,
                         timeout=15.0) as worker:
        trace = []
        address = None
        for _ in range(400):
            trace.extend(_trace(worker.run_frames(60)))
            ready = [line for line in trace if line.startswith("ROMTRACE|ready|")]
            if ready:
                fields = ready[-1].split("|")
                if len(fields) != 4 or int(fields[3]) != size:
                    raise RuntimeError("invalid battle mailbox announcement")
                address = int(fields[2])
                break
        if address is None:
            raise TimeoutError("battle harness did not announce its mailbox")
        worker.save_state()
        result = worker.batch_turns(
            address, [payload], mailbox_bytes=size,
            engine_elf_sha256=sha256, deadline_ms=int(time.monotonic() * 1000) + 15_000,
            request_id="dynamic-oracle", max_frames=24_000,
        )
        turn = result["turns"][0]["events"]
        if query:
            if _policy_marker(request) not in turn:
                raise RuntimeError("engine-error: pinned engine trace did not confirm the critical-hit policy")
            if not any(record.startswith("ROMTRACE|query|complete") for record in turn):
                raise RuntimeError("incomplete-state: dynamic query did not complete at its choice checkpoint")
            required_query = ("ROMTRACE|query|context|",
                             "ROMTRACE|query|active|")
            if not all(any(record.startswith(prefix) for record in turn) for prefix in required_query):
                raise RuntimeError("incomplete-state: query lacks active-state records")
            if request.get("post_switch_query") and not any(
                record == "ROMTRACE|query|context|post-switch" for record in turn
            ):
                raise RuntimeError("incomplete-state: post-switch query did not reach the switched state")
            return turn
        if version >= 8:
            # v8 SIMULATE_TURN stops at the next decision boundary or terminal and
            # emits the complete state export instead of a v7 after-turn trace.
            # v8b emits the `ROMTRACE|boundary|` marker after the state export, so
            # this batch reply carries the whole boundary state. The original v8
            # build emitted the marker first and the worker's batch stop truncated
            # the export (4 of 34 records observed); that artifact is superseded.
            if not any(record.startswith(("ROMTRACE|boundary|", "ROMTRACE|terminal|"))
                       for record in turn):
                raise RuntimeError("incomplete-state: v8 transition did not reach a boundary")
            return turn
        if _policy_marker(request) not in turn:
            raise RuntimeError("engine-error: pinned engine trace did not confirm the critical-hit policy")
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
    parser.add_argument("--protocol-version", type=int, default=None,
                        help="mailbox layout override (default: the pin); use to drive a staged ELF")
    args = parser.parse_args()
    request = json.loads(args.request.read_text(encoding="utf-8"))
    print(json.dumps({"test_elf_sha256": args.sha256.upper(),
                      "protocol_version": _protocol_version(args.protocol_version),
                      "trace": run_turn(args.elf, args.sha256, request,
                                        protocol_version=args.protocol_version)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
