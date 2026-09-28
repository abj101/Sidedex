"""Focused checks for the dynamic oracle request and pinned source IDs."""
import hashlib
import importlib.util
import json
import os
import struct
import sys
import time
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


def test_compact_projection_matches_trace_for_frozen_fixtures():
    """P1, P26, P27, P28 and P29-first-move against a staged v9 candidate ELF.

    For each fixture: decode the committed SDR2 and check Appendix C.1 (CRC,
    record count, required tags for the boundary kind, switch-only tag 7 at a
    forced switch); then run the same request with the compact flag set and
    compare every field of tags 1-8 and 10 against the `c<tag>` ROMTRACE mirror
    the trace-flag run emits, so the transport path and the diagnostic path
    cannot drift.
    """
    elf = _v9_candidate_elf()
    if elf is None:
        pytest.skip("no staged v9 candidate ELF (set SIDEDEX_V9_ELF)")
    worker_path = ROOT / "native/rom_worker/build/mingw/rom_worker.exe"
    if not worker_path.is_file():
        pytest.skip("pinned mGBA worker is not staged")
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from tools.rom_oracle import rom_diff_harness as harness  # noqa: PLC0415
    import rom_bridge  # noqa: PLC0415

    sha = hashlib.sha256(elf.read_bytes()).hexdigest().upper()
    for fixture_id, stem, expected_boundary in V9_FIXTURES:
        fixture = json.loads(
            (ROOT / "tools/rom_oracle/fixtures" / f"{stem}.json").read_text(encoding="utf-8"))
        with rom_bridge.RomWorkerClient(rom_path=elf, expected_artifact_sha256=sha,
                                        timeout=60.0) as worker:
            rows = _run_v9_fixture(harness, rom_bridge, worker, sha, fixture, fixture_id)
        assert rows, fixture_id
        assert rows[0]["boundary"] == expected_boundary, (fixture_id, rows[0])
        for row in rows:
            assert row["mismatches"] == [], (fixture_id, row["label"], row["mismatches"])
            assert row["order"] == sorted(row["order"])
        if expected_boundary == 3:
            expect = fixture["actions"][0]["expect"]["result"]
            code = {"win": 1, "loss": 2, "draw": 3}[expect]
            assert rows[0]["terminal"] == [{"result": code, "reason": 0}], (fixture_id, rows[0])
        if fixture_id == "P29":
            assert len(rows) == 2 and rows[0]["order"] == rows[1]["order"]


def _run_v9_fixture(harness, rom_bridge, worker, sha, fixture, fixture_id):
    events, ready = [], None
    for _ in range(400):
        events.extend(worker.run_frames(60)["trace_events"])
        found = [e for e in events if e.startswith("ROMTRACE|ready|")]
        if found:
            ready = found[-1]
            break
    assert ready, "the ROM never announced a v9 mailbox"
    cells = ready.split("|")
    worker.address = int(cells[2])
    mailbox_bytes, result_addr = int(cells[3]), int(cells[6])
    base = worker.save_checkpoint(name="a1b-base")
    rid = [0]

    def submit(request, flags, handle):
        rid[0] += 1
        payload = bytearray(harness.encode_request(request, request_id=rid[0]))
        payload[4:6] = struct.pack("<H", 9)
        payload = bytes(payload) + struct.pack("<IIHH", result_addr, 8192, 1, flags)
        reply = worker.batch_turns(worker.address, [payload], mailbox_bytes=mailbox_bytes,
                                   engine_elf_sha256=sha,
                                   deadline_ms=int((time.monotonic() + 120) * 1000),
                                   request_id=f"a1b-{fixture_id}-{rid[0]}", max_frames=24000,
                                   handle=handle)
        return reply["turns"][0]

    root = fixture["root"]
    submit(json.loads(json.dumps(root)), 0, base["handle"])
    opening = worker.save_checkpoint(name="a1b-opening")
    rows = []
    for entry in fixture["actions"]:
        branch = json.loads(json.dumps(root))
        branch["candidate_action"] = entry["action"]
        runs = {}
        for flags in (0, 1):
            worker.restore_state(opening["handle"])
            turn = submit(branch, flags, opening["handle"])
            head = worker.read_memory_block(result_addr, 64)
            blob = None
            if head[:4] == b"SDR2":
                total = struct.unpack_from("<I", head, 8)[0]
                if 64 <= total <= 8192:
                    blob = head + worker.read_memory_block(result_addr + 64, total - 64)
            runs[flags] = {"turn": turn, "blob": blob}
        assert runs[0]["blob"] is not None, f"{fixture_id}: trace run committed no result"
        assert runs[1]["blob"] is not None, f"{fixture_id}: compact run committed no result"
        header, records = decode_sdr2(runs[1]["blob"])
        order = sdr2_tag_order(runs[1]["blob"])
        _check_c1(header, order, records)
        # The request id is per-run by design; every other header field must match.
        trace_header = decode_sdr2(runs[0]["blob"])[0]
        assert {k: v for k, v in trace_header.items() if k != "request_id"} ==                {k: v for k, v in header.items() if k != "request_id"},             f"{fixture_id}: the compact flag changed the committed header"
        mismatches = []
        for mirror_name, tag_name, words in MIRROR:
            if (tag_name == "legal" and header["boundary"] == 3) or \
                    (tag_name == "terminal" and header["boundary"] != 3):
                continue
            traced = trace_words(runs[0]["turn"]["events"], mirror_name)
            if not traced:
                mismatches.append((tag_name, -1, None, None))
                continue
            flat = [word for row in records[tag_name] for word in row.values()]
            if flat != traced[:len(flat)]:
                mismatches.extend((tag_name, i, a, b)
                                  for i, (a, b) in enumerate(zip(flat, traced)) if a != b)
        rows.append({"label": entry["label"], "boundary": header["boundary"],
                     "status": header["status"], "total": len(runs[1]["blob"]),
                     "order": order, "mismatches": mismatches,
                     "terminal": records.get("terminal")})
    return rows


V9_FIXTURES = (
    ("P1", "p01_physical_neutral", 1),
    ("P26", "p26_faint_forced_replacement", 2),
    ("P27", "p27_terminal_win", 3),
    ("P28", "p28_terminal_loss", 3),
    ("P29", "p29_multiturn_branch", 1),
)

# Appendix C.1 required tag sets per boundary kind.
C1_REQUIRED = {1: {1, 2, 3, 4, 5, 6, 7, 8, 9},
               2: {1, 2, 3, 4, 5, 6, 7, 8, 9},
               3: {1, 2, 3, 4, 5, 6, 8, 9, 10}}
# name on the ROMTRACE mirror, tag name in the decoded result, words per entry.
MIRROR = (("party", "party", 32), ("active", "active", 37), ("field", "field", 6),
          ("side", "side", 12), ("battle", "battle", 10), ("rng", "rng", 8),
          ("legal", "legal", 4), ("trainer", "trainer_action", 3),
          ("terminal", "terminal", 2))


def _v9_candidate_elf():
    """The staged v9 candidate ELF, from SIDEDEX_V9_ELF or the A1 artifact dir."""
    env = os.environ.get("SIDEDEX_V9_ELF")
    if env:
        path = Path(env)
        return path if path.is_file() else None
    artifacts = Path.home() / ".codex/artifacts/A1"
    found = sorted(artifacts.glob("*/pokeemerald-test-headless.elf"),
                   key=lambda path: path.stat().st_mtime)
    return found[-1] if found else None


def sdr2_tag_order(blob):
    """The tags in wire order, walked from the record headers."""
    total = struct.unpack_from("<I", blob, 8)[0]
    order, off = [], 64
    while off < total:
        tag, size = struct.unpack_from("<HH", blob, off)
        order.append(tag)
        off += 4 + size + (-size % 4)
    return order


def _check_c1(header, order, records):
    """Appendix C.1 structural rules for a committed result."""
    if header["status"] != 0:
        assert order == [11], "a nonzero status carries exactly tag 11"
        return
    assert order == sorted(order) and len(set(order)) == len(order), order
    required = C1_REQUIRED[header["boundary"]]
    assert required <= set(order), sorted(required - set(order))
    assert len(records["party"]) == 12 and len(records["active"]) == 2
    if header["boundary"] in (1, 2):
        assert len(records["legal"]) >= 1, "a nonterminal boundary needs a legal action"
    if header["boundary"] == 2:
        assert {row["kind"] for row in records["legal"]} == {1}, "switch-only at a forced switch"
