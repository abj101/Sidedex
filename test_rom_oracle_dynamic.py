"""Focused checks for the dynamic oracle request and pinned source IDs."""
import importlib.util
import json
import struct
from pathlib import Path

import pytest

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
    assert len(payload) == RUNNER.MAILBOX_BYTES == RUNNER.mailbox_bytes(RUNNER.PINNED_PROTOCOL_VERSION)
    assert RUNNER.numeric_id("species", "SPECIES_BULBASAUR") == 1
    assert RUNNER.numeric_id("moves", "MOVE_TACKLE") == 33


def test_v7_and_v8_layouts_are_selected_by_protocol_version():
    request = json.loads((ROOT / "tools/rom_oracle/dynamic_sample.json").read_text(encoding="utf-8"))
    plain = RUNNER.encode_request(request, protocol_version=7)
    assert len(plain) == 856 and struct.unpack_from("<H", plain, 4)[0] == 7

    request["operation"] = "query-ai-action"
    request["mode"] = "logic-only"
    request["request_id"] = 7
    v7 = RUNNER.encode_request(request, protocol_version=7)
    v8 = RUNNER.encode_request(request, protocol_version=8)
    assert len(v7) == 856 and struct.unpack_from("<H", v7, 4)[0] == 7
    assert len(v8) == 864 and struct.unpack_from("<H", v8, 4)[0] == 8
    op, mode, policy, request_id = struct.unpack_from("<4H", v8, RUNNER.V8_EXTENSION_OFFSET)
    assert (op, mode, policy, request_id) == (
        RUNNER.OP_QUERY_AI_ACTION, RUNNER.MODE_LOGIC_ONLY,
        RUNNER.CRITICAL_POLICY_ITEM_ABILITY_ONLY, 7)
    # v7 offsets are preserved: the only header difference is the version field.
    assert v8[:4] == v7[:4] and v8[6:856] == v7[6:856]
    try:
        RUNNER.encode_request(request, protocol_version=9)
    except ValueError as error:
        assert "unsupported-protocol-version" in str(error)
    else:
        raise AssertionError("an unknown mailbox layout was accepted")


def test_query_operations_set_the_explicit_op_field():
    request = json.loads((ROOT / "tools/rom_oracle/dynamic_sample.json").read_text(encoding="utf-8"))
    for name, expected in (("query-damage", RUNNER.OP_QUERY_DAMAGE),
                           ("query-ai-action", RUNNER.OP_QUERY_AI_ACTION),
                           ("simulate-turn", RUNNER.OP_SIMULATE_TURN)):
        request["operation"] = name
        payload = RUNNER.encode_request(request, protocol_version=8)
        assert struct.unpack_from("<H", payload, RUNNER.V8_EXTENSION_OFFSET)[0] == expected
    # The legacy query flag still selects the query path on both layouts.
    request.pop("operation")
    request["query"] = True
    assert RUNNER.is_query(request)
    assert struct.unpack_from("<H", RUNNER.encode_request(request), RUNNER.SECOND_PRESENT_OFFSET)[0] == 2
    assert struct.unpack_from(
        "<H", RUNNER.encode_request(request, protocol_version=8), RUNNER.V8_EXTENSION_OFFSET)[0] == RUNNER.OP_QUERY_DAMAGE


def test_critical_policy_is_selectable_and_labelled():
    request = json.loads((ROOT / "tools/rom_oracle/dynamic_sample.json").read_text(encoding="utf-8"))
    request["operation"] = "query-damage"
    for label, expected in (("normal", RUNNER.CRITICAL_POLICY_NORMAL),
                            ("item-ability-only", RUNNER.CRITICAL_POLICY_ITEM_ABILITY_ONLY)):
        request["critical_policy"] = label
        payload = RUNNER.encode_request(request, protocol_version=8)
        assert struct.unpack_from("<H", payload, RUNNER.V8_EXTENSION_OFFSET + 4)[0] == expected
        assert RUNNER.critical_policy_label(request) == label
    # Protocol 7 has no policy field; the v7 ROM only implements item-ability-only.
    request["critical_policy"] = "normal"
    with pytest.raises(ValueError, match="unsupported-policy"):
        RUNNER.encode_request(request, protocol_version=7)
    request["critical_policy"] = "always-crit"
    with pytest.raises(ValueError, match="unsupported-policy"):
        RUNNER.encode_request(request, protocol_version=8)


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
    assert len(payload) == RUNNER.MAILBOX_BYTES
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


def test_low_hp_trainer_item_fixture_reaches_the_rom_request():
    request = json.loads((ROOT / "tools/rom_oracle/dynamic_trainer_item.json").read_text(encoding="utf-8"))
    assert request["opponent_party"][0]["hp"] < request["opponent_party"][0]["max_hp"]
    assert request["trainer_items"] == {"ITEM_POTION": 1}
    encoded = RUNNER.encode_request(request, protocol_version=8)
    assert struct.unpack_from("<4H", encoded, 844) == (RUNNER.numeric_id("items", "ITEM_POTION"), 0, 0, 0)
    assert struct.unpack_from("<2H", encoded, 852) == (1, 0)


def test_p25_voluntary_player_switch_fixture_is_a_switch_action():
    request = json.loads((ROOT / "tools/rom_oracle/dynamic_switch.json").read_text(encoding="utf-8"))
    assert request["candidate_action"] == {"kind": "switch", "slot": 1}
    encoded = RUNNER.encode_request(request, protocol_version=8)
    assert struct.unpack_from("<3H", encoded, 12) == (1, 0, 1)


# ---------------------------------------------------------------------------
# v9 compact result (contract Appendix C) decoding helpers. These stay in this
# file until package B publishes tools/rom_oracle/sdr2.py as the production
# decoder; the layouts come from the frozen sdr2_vectors/tables.json.
# ---------------------------------------------------------------------------
TABLES = json.loads((ROOT / "tools/rom_oracle/sdr2_vectors/tables.json").read_text(encoding="utf-8"))
TAG_NAMES = {1: "party", 2: "active", 3: "field", 4: "side", 5: "battle", 6: "rng",
             7: "legal", 8: "trainer_action", 9: "events", 10: "terminal", 11: "unsupported"}


def decode_sdr2(blob):
    """Structural decoder for a committed SDR2 result; raises ValueError on any fault."""
    import zlib

    if len(blob) < 64 or blob[:4] != b"SDR2":
        raise ValueError("bad magic")
    fmt, header_bytes = struct.unpack_from("<HH", blob, 4)
    total, crc = struct.unpack_from("<II", blob, 8)
    if fmt != 1 or header_bytes != 64:
        raise ValueError("bad version/header size")
    if total != len(blob):
        raise ValueError("total_bytes does not match the buffer length")
    check = bytearray(blob)
    struct.pack_into("<I", check, 12, 0)
    if zlib.crc32(bytes(check[12:])) != crc:
        raise ValueError("bad crc")
    header = {"request_id": struct.unpack_from("<H", blob, 16)[0],
              "status": struct.unpack_from("<H", blob, 18)[0],
              "boundary": struct.unpack_from("<H", blob, 20)[0],
              "mode": struct.unpack_from("<H", blob, 22)[0],
              "critical_policy": struct.unpack_from("<H", blob, 24)[0],
              "record_count": struct.unpack_from("<H", blob, 26)[0],
              "turn": struct.unpack_from("<I", blob, 28)[0],
              "rng_main": list(struct.unpack_from("<4I", blob, 32)),
              "rng_secondary": list(struct.unpack_from("<4I", blob, 48))}
    records = {}
    off = 64
    while off < total:
        tag, size = struct.unpack_from("<HH", blob, off)
        payload = blob[off + 4:off + 4 + size]
        if tag == 9:
            events, index = [], 0
            while index < len(payload):
                kind, words = struct.unpack_from("<HH", payload, index)
                events.append({"kind": kind,
                               "payload": list(struct.unpack_from(f"<{words}I", payload, index + 4))})
                index += 4 + words * 4
            records["events"] = events
        else:
            count, words = struct.unpack_from("<HH", payload, 0)
            if words != TABLES["words"][TAG_NAMES[tag]] or size != 4 + count * words * 4:
                raise ValueError(f"bad framing for tag {tag}")
            flat = struct.unpack_from(f"<{count * words}I", payload, 4)
            fields = TABLES["fields"][TAG_NAMES[tag]]
            records[TAG_NAMES[tag]] = [dict(zip(fields, flat[i * words:(i + 1) * words]))
                                       for i in range(count)]
        off += 4 + size + (-size % 4)
    if len(records) != header["record_count"]:
        raise ValueError("record_count mismatch")
    if header["status"] != 0 and list(records) != ["unsupported"]:
        raise ValueError("nonzero status must carry only tag 11")
    return header, records


def trace_words(events, name):
    """Reassemble `ROMTRACE|phase|<turn>|state|c<name>|<base>|<words...>` chunks."""
    words = {}
    marker = f"|state|c{name}|"
    for line in events:
        if not line.startswith("ROMTRACE|phase|") or marker not in line:
            continue
        cells = line.split("|")
        base = int(cells[5])
        for offset, value in enumerate(cells[6:]):
            words[base + offset] = int(value) & 0xFFFFFFFF
    if not words:
        return []
    return [words[i] for i in range(max(words) + 1)]


def test_sdr2_decoder_reproduces_the_golden_vectors():
    vectors = ROOT / "tools/rom_oracle/sdr2_vectors"
    for name in ("choice_p1", "forced_switch_p26", "terminal_win", "unsupported_status"):
        blob = (vectors / f"{name}.bin").read_bytes()
        expected = json.loads((vectors / f"{name}.json").read_text(encoding="utf-8"))
        header, records = decode_sdr2(blob)
        assert header["boundary"] == expected["header"]["boundary"]
        assert header["status"] == expected["header"]["status"]
        assert header["record_count"] == expected["header"]["record_count"]
        assert set(records) == set(expected["records"])
        for tag, rows in expected["records"].items():
            assert records[tag] == rows, (name, tag)


def test_sdr2_decoder_rejects_the_negative_vectors():
    vectors = ROOT / "tools/rom_oracle/sdr2_vectors"
    for name in ("bad_crc", "bad_length", "bad_record_count", "duplicate_tag"):
        with pytest.raises(ValueError):
            decode_sdr2((vectors / f"{name}.bin").read_bytes())


@pytest.mark.skip(reason="live compact==trace equality needs B's v9 driver and a staged candidate ELF")
def test_compact_projection_matches_trace_for_frozen_fixtures():
    """P1, P26, P27, P28 and P29-first-move: tags 1-8 and 10 must equal the trace.

    Enabled once package B exposes a v9 request encoder/memory reader; the
    assertion is `decode_sdr2(result_bytes)` vs `trace_words(events, tag)` for
    each `cparty`/`cactive`/`cfield`/`cside`/`cbattle`/`crng`/`clegal`/`ctrainer`.
    """
