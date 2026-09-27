"""Focused checks for the dynamic oracle request and pinned source IDs."""
import importlib.util
import json
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
    assert len(payload) == RUNNER.MAILBOX_BYTES == 740
    assert RUNNER.numeric_id("species", "SPECIES_BULBASAUR") == 1
    assert RUNNER.numeric_id("moves", "MOVE_TACKLE") == 33


def test_switch_and_one_replayed_turn_are_encoded():
    request = json.loads((ROOT / "tools/rom_oracle/dynamic_sample.json").read_text(encoding="utf-8"))
    request["candidate_action"] = {"kind": "switch", "slot": 1}
    request["replay_turns"] = [{
        "player_action": {"kind": "move", "move_id": "MOVE_TACKLE"},
        "enemy_action": {"kind": "move", "move_id": "MOVE_TACKLE"},
    }]
    payload = RUNNER.encode_request(request)
    assert len(payload) == 740


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
