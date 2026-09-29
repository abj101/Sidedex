"""Focused checks for the dynamic oracle request and pinned source IDs."""
import importlib.util
import json
import struct
from pathlib import Path

ROOT = Path(__file__).parent
SPEC = importlib.util.spec_from_file_location(
    "rom_oracle_dynamic", ROOT / "tools/rom_oracle/run_dynamic.py"
)
RUNNER = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(RUNNER)


def test_source_ids_encode_to_the_pinned_mailbox():
    request = json.loads((ROOT / "tools/rom_oracle/dynamic_sample.json").read_text(encoding="utf-8"))
    payload = RUNNER.encode_request(request)
    assert len(payload) == RUNNER.MAILBOX_BYTES == 856
    assert RUNNER.numeric_id("species", "SPECIES_BULBASAUR") == 1
    assert RUNNER.numeric_id("moves", "MOVE_TACKLE") == 33


def test_battle_statuses_encode_as_status1_words():
    request = json.loads((ROOT / "tools/rom_oracle/dynamic_sample.json").read_text(encoding="utf-8"))
    expected = {
        "STATUS1_NONE": 0,
        "STATUS1_SLEEP": 7,
        "STATUS1_POISON": 8,
        "STATUS1_BURN": 16,
        "STATUS1_FREEZE": 32,
        "STATUS1_PARALYSIS": 64,
        "STATUS1_TOXIC_POISON": 128,
        "STATUS1_FROSTBITE": 4096,
    }
    for status, value in expected.items():
        request["player_party"][0]["status1"] = status
        payload = RUNNER.encode_request(request)
        assert struct.unpack_from("<I", payload, 56 + 16)[0] == value

    request["player_party"][0]["status1"] = "STATUS1_TOXIC_COUNTER"
    try:
        RUNNER.encode_request(request)
    except ValueError as error:
        assert "selectable battle status" in str(error)
    else:
        raise AssertionError("toxic counter mask was accepted as a standalone status")


def test_switch_and_one_replayed_turn_are_encoded():
    request = json.loads((ROOT / "tools/rom_oracle/dynamic_sample.json").read_text(encoding="utf-8"))
    request["candidate_action"] = {"kind": "switch", "slot": 1}
    request["replay_turns"] = [{
        "player_action": {"kind": "move", "move_id": "MOVE_TACKLE"},
        "enemy_action": {"kind": "move", "move_id": "MOVE_TACKLE"},
    }]
    payload = RUNNER.encode_request(request)
    assert len(payload) == RUNNER.MAILBOX_BYTES


def test_second_candidate_turn_is_encoded():
    request = json.loads((ROOT / "tools/rom_oracle/dynamic_sample.json").read_text(encoding="utf-8"))
    request["second_action"] = {"kind": "move", "move_id": "MOVE_TACKLE"}
    payload = RUNNER.encode_request(request)
    assert len(payload) == RUNNER.MAILBOX_BYTES
    kind, move, _slot, present = struct.unpack_from("<4H", payload, RUNNER.SECOND_ACTION_OFFSET)
    assert present == 1 and kind == 0 and move == RUNNER.numeric_id("moves", "MOVE_TACKLE")

    request["second_action"] = {"kind": "move", "move_id": "MOVE_NOT_IN_PINNED_SOURCE"}
    try:
        RUNNER.encode_request(request)
    except ValueError as error:
        assert "invalid-id" in str(error)
    else:
        raise AssertionError("unknown second move was accepted")


def test_item_enablement_stages_and_post_switch_query_encoding():
    request = json.loads((ROOT / "tools/rom_oracle/dynamic_sample.json").read_text(encoding="utf-8"))
    mon = request["player_party"][0]
    mon["held_item_enabled"] = False
    mon["stat_stages"] = [6, 7, 5, 8, 4, 9, 3]
    request["candidate_action"] = {"kind": "move", "move_id": "MOVE_TACKLE"}
    request["replay_turns"] = [{
        "player_action": {"kind": "switch", "slot": 1},
        "enemy_action": {"kind": "move", "move_id": "MOVE_TACKLE"},
    }]
    request["post_switch_query"] = True
    payload = RUNNER.encode_request(request)
    assert len(payload) == 856
    # Party slot 0 starts after the 56-byte request header; each encoded mon is 64 bytes.
    assert payload[56 + 56] == 0
    assert list(payload[56 + 57:56 + 64]) == [6, 7, 5, 8, 4, 9, 3]
    kind, _move, _slot, present = struct.unpack_from("<4H", payload, RUNNER.SECOND_ACTION_OFFSET)
    assert present == 2

    mon["held_item_enabled"] = 1
    try:
        RUNNER.encode_request(request)
    except ValueError as error:
        assert "boolean" in str(error)
    else:
        raise AssertionError("non-boolean held item flag was accepted")


def test_unknown_source_id_fails_closed():
    try:
        RUNNER.numeric_id("moves", "MOVE_NOT_IN_PINNED_SOURCE")
    except ValueError as error:
        assert "invalid-id" in str(error)
    else:
        raise AssertionError("unknown source ID was accepted")


def test_invalid_state_fails_closed():
    request = json.loads((ROOT / "tools/rom_oracle/dynamic_sample.json").read_text(encoding="utf-8"))
    request["player_party"][0]["evs"] = [252, 252, 252, 0, 0, 0]
    try:
        RUNNER.encode_request(request)
    except ValueError as error:
        assert "EV total" in str(error)
    else:
        raise AssertionError("invalid EV total was accepted")

    request = json.loads((ROOT / "tools/rom_oracle/dynamic_sample.json").read_text(encoding="utf-8"))
    request["opponent_active_index"] = 1
    try:
        RUNNER.encode_request(request)
    except ValueError as error:
        assert "active index" in str(error)
    else:
        raise AssertionError("unsupported active index was accepted")


def test_unknown_field_and_unsupported_history_fail_closed():
    request = json.loads((ROOT / "tools/rom_oracle/dynamic_sample.json").read_text(encoding="utf-8"))
    request["field"]["weather"] = "WEATHER_RAIN"
    try:
        RUNNER.encode_request(request)
    except ValueError as error:
        assert "field" in str(error)
    else:
        raise AssertionError("unsupported field was accepted")

    request = json.loads((ROOT / "tools/rom_oracle/dynamic_sample.json").read_text(encoding="utf-8"))
    request["replay_turns"] = [{"player_action": {"kind": "move", "move_id": "MOVE_TACKLE"}}]
    try:
        RUNNER.encode_request(request)
    except ValueError as error:
        assert "both recorded actions" in str(error)
    else:
        raise AssertionError("incomplete replay was accepted")
