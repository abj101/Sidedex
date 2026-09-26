"""Unit tests for the ROM oracle trace decoder."""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).parent
SPEC = importlib.util.spec_from_file_location("rom_oracle_runner", ROOT / "tools/rom_oracle/run_trace.py")
RUNNER = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(RUNNER)


def test_pinned_trace_records_decode_to_committed_fixture():
    records = [
        "GBA Debug: ROMTRACE|ability|1|22",
        "GBA Debug: ROMTRACE|action|0|move|261|1",
        "GBA Debug: ROMTRACE|action|1|move|33|0",
        "GBA Debug: ROMTRACE|hp|0|105|88",
        "GBA Debug: ROMTRACE|action|1|switch|1",
        "GBA Debug: ROMTRACE|final|0|1|88|105|0|65|5|261|0|0|0|14|10|5|15",
        "GBA Debug: ROMTRACE|final|1|4|99|99|0|22|6|33|0|0|0|34|15|15|5",
        "GBA Debug: ROMTRACE|rng|-941908513|-1199513784|1513372196|6",
    ]
    expected = json.loads((ROOT / "tools/rom_oracle/expected_trace.json").read_text(encoding="utf-8-sig"))
    assert RUNNER.parse_trace_lines(records) == expected


def test_trace_decoder_rejects_missing_final_state():
    try:
        RUNNER.parse_trace_lines(["GBA Debug: ROMTRACE|action|1|switch|1"])
    except ValueError as error:
        assert "incomplete" in str(error)
    else:
        raise AssertionError("incomplete trace was accepted")
