"""Unit tests for the ROM oracle trace decoder."""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).parent
SPEC = importlib.util.spec_from_file_location("rom_oracle_runner", ROOT / "tools/rom_oracle/run_trace.py")
RUNNER = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(RUNNER)


def test_trace_decoder_preserves_turn_and_settled_switch_state():
    lines = [
        "GBA Debug: ROMTRACE|ability|0|1|22",
        "GBA Debug: ROMTRACE|action|0|0|move|261|1",
        "GBA Debug: ROMTRACE|action|1|1|switch|1",
        "GBA Debug: ROMTRACE|switch-settled|1|1|0|485",
        "GBA Debug: ROMTRACE|phase|1|switch-settled",
        "GBA Debug: ROMTRACE|field|1|16777228|0",
        "GBA Debug: ROMTRACE|party|1|1|1|485|151|0|0|0|33|0|0|0|35|5|5",
        "GBA Debug: ROMTRACE|active|1|1|0|485|151|151|0|0|0|18|0|6|6|6|6|6|6|6|33|0|0|0|35|5|5|5|11|9|10",
        "GBA Debug: ROMTRACE|blob|1|1|0|disable|0|00000001",
        "GBA Debug: ROMTRACE|rng|1|1|2|3|4|5|6|7|8",
    ]
    trace = RUNNER.parse_trace_lines(lines)
    assert trace["events"][-1] == {"type": "switch-settled", "turn": 1, "battler": 1, "party_index": 0, "species": 485}
    assert trace["phases"] == [{"turn": 1, "phase": "switch-settled"}]
    assert trace["party"][0]["moves"] == [33, 0, 0, 0]
    assert trace["active"][0]["status3"] == 0
    assert trace["blobs"][0]["hex"] == "00000001"
    assert trace["rng"][0] == {"turn": 1, "main": [1, 2, 3, 4], "secondary": [5, 6, 7, 8]}


def test_trace_decoder_rejects_missing_settled_switch_marker():
    records = [
        "ROMTRACE|ability|0|1|22",
        "ROMTRACE|phase|1|after-turn",
        "ROMTRACE|party|1|1|1|485|151|0|0|0|33|0|0|0|35|5|5",
        "ROMTRACE|active|1|1|0|485|151|151|0|0|0|18|0|6|6|6|6|6|6|6|33|0|0|0|35|5|5|5|11|9|10",
        "ROMTRACE|rng|1|1|2|3|4|5|6|7|8",
    ]
    try:
        RUNNER.parse_trace_lines(records)
    except ValueError as error:
        assert "settled-state marker" in str(error)
    else:
        raise AssertionError("trace without settled switch was accepted")


def test_expected_trace_covers_full_six_slot_parties_and_settled_heatran():
    expected = json.loads((ROOT / "tools/rom_oracle/expected_trace.json").read_text(encoding="utf-8-sig"))
    assert len(expected["party"]) == 24  # both six-slot parties at both snapshots
    assert any(event == {"type": "switch-settled", "turn": 1, "battler": 1, "party_index": 0, "species": 485}
               for event in expected["events"])
    assert [phase["phase"] for phase in expected["phases"]] == ["switch-settled", "after-turn"]
    assert len(expected["rng"]) == 2 and all(len(row["secondary"]) == 4 for row in expected["rng"])

def test_committed_raw_trace_parses_to_expected_state():
    raw = (ROOT / "tools/rom_oracle/expected_trace.raw.txt").read_text(encoding="utf-8-sig").splitlines()
    expected = json.loads((ROOT / "tools/rom_oracle/expected_trace.json").read_text(encoding="utf-8-sig"))
    assert len(raw) == 170
    assert RUNNER.parse_trace_lines(raw) == expected
