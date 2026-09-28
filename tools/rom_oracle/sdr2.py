"""The single production SDR2 decoder (contract Appendix C.1-C.3).

`tools/rom_oracle/make_sdr2_vectors.py` is the reference *encoder* and
`tools/rom_oracle/sdr2_vectors/tables.json` is its field table; this module is
the only decoder. Everything the client learns about a compact turn comes from
here, so a layout is never guessed from a reply.

    parse(buf, request_id=None, mode=None, critical_policy=None, capacity=8192)
        -> {"header": {...}, "records": {group: [dict, ...]}, "child_expected": bool}

Raises SDR2Error with one of the C.3 codes. C.1 pins most of them; the rest are
mapped by kind, and that mapping is what this module documents:

    sdr2-version       bad magic/format/header size, unknown tag or event kind,
                       boundary kind outside the C.3 enum 0..3 (an unknown enum
                       value is a version mismatch, as for tags and event kinds)
    sdr2-length        total_bytes != len(buf), a record that does not decode to
                       entry_count*words_per_entry*4 + 4, nonzero padding
    sdr2-oversize      total_bytes above capacity or 1 MiB
    sdr2-crc           CRC-32 of byte 12..end (CRC field zeroed) mismatch
    sdr2-record-count  record_count does not match the records walked
    sdr2-duplicate-tag duplicate or descending tag
    sdr2-missing-tag   a required tag is absent or empty (no legal actions)
    sdr2-request-id    request id differs from the request
    sdr2-mode          mode or critical policy differs from the request
    sdr2-identity      tag 6 RNG does not equal the header RNG pair
    sdr2-status        nonzero status carrying anything but tag 11, or a status
                       record in a status-0 result
"""

from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path

MAGIC = b"SDR2"
FORMAT_VERSION = 1
HEADER_BYTES = 64
DEFAULT_CAPACITY = 8192
MAX_BYTES = 1 << 20

# Boundary kinds (C.3).
BOUNDARY_NONE, BOUNDARY_CHOICE, BOUNDARY_FORCED_SWITCH, BOUNDARY_TERMINAL = 0, 1, 2, 3
STATUS_OK = 0

# C.3 enum tables, used for the fail-closed value checks.
LEGAL_KIND_SWITCH = 1
EVENT_KINDS = frozenset(range(1, 16))
TAG_GROUPS = {1: "party", 2: "active", 3: "field", 4: "side", 5: "battle", 6: "rng",
              7: "legal", 8: "trainer_action", 9: "events", 10: "terminal", 11: "unsupported"}

_TABLES = json.loads(
    (Path(__file__).with_name("sdr2_vectors") / "tables.json").read_text(encoding="utf-8"))
WORDS = _TABLES["words"]      # group -> u32 words per entry
FIELDS = _TABLES["fields"]    # group -> field names, in wire order

# Fixed shapes: entry count and width for every singleton/repeating typed group.
FIXED_ENTRIES = {"party": 12, "active": 2, "field": 1, "side": 2, "battle": 1,
                 "rng": 1, "trainer_action": 1, "terminal": 1, "unsupported": 1}


class SDR2Error(Exception):
    """A rejected SDR2 buffer, carrying the contract C.3 error code."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def _reject(code: str, detail: str = "") -> None:
    raise SDR2Error(code, detail)


def _decode_typed(name: str, payload: bytes, *, boundary: int) -> list[dict]:
    """Decode `entry_count:u16, words_per_entry:u16` + fixed-width u32 entries."""
    if len(payload) < 4:
        _reject("sdr2-length", f"{name} payload is shorter than its table header")
    count, words = struct.unpack_from("<HH", payload)
    width = WORDS[name]
    if words != width:
        _reject("sdr2-length", f"{name} words_per_entry {words} != {width}")
    if len(payload) != 4 + count * words * 4:
        _reject("sdr2-length", f"{name} payload does not match entry_count*words")
    wanted = FIXED_ENTRIES.get(name)
    if wanted is not None and count != wanted:
        _reject("sdr2-length", f"{name} entry_count {count} != {wanted}")
    values = list(struct.unpack_from(f"<{count * words}I", payload, 4)) if count * words else []
    fields = FIELDS[name]
    rows = [dict(zip(fields, values[i * words:(i + 1) * words])) for i in range(count)]
    if name == "legal":
        for row in rows:
            if row["kind"] not in (0, 1, 2):
                _reject("sdr2-version", f"legal kind {row['kind']} is unknown")
            if boundary == BOUNDARY_FORCED_SWITCH and row["kind"] != LEGAL_KIND_SWITCH:
                _reject("sdr2-missing-tag", "a forced-switch boundary offers a non-switch action")
        if boundary in (BOUNDARY_CHOICE, BOUNDARY_FORCED_SWITCH) and not rows:
            _reject("sdr2-missing-tag", "a choice boundary needs at least one legal action")
    return rows


def _decode_events(payload: bytes) -> list[dict]:
    """Decode tag 9: repeated `event_kind:u16, payload_words:u16, payload:u32[]`."""
    events, offset = [], 0
    while offset < len(payload):
        if offset + 4 > len(payload):
            _reject("sdr2-length", "event subrecord header is truncated")
        kind, words = struct.unpack_from("<HH", payload, offset)
        if kind not in EVENT_KINDS:
            _reject("sdr2-version", f"event kind {kind} is unknown")
        end = offset + 4 + words * 4
        if end > len(payload):
            _reject("sdr2-length", "event subrecord payload is truncated")
        events.append({"kind": kind, "payload": list(struct.unpack_from(f"<{words}I", payload, offset + 4))})
        offset = end
    return events


def parse(buf: bytes, request_id: int | None = None, mode: int | None = None,
          critical_policy: int | None = None, capacity: int = DEFAULT_CAPACITY) -> dict:
    """Validate and decode one SDR2 result buffer. See the module docstring."""
    if type(buf) is not bytes:
        _reject("sdr2-length", "buffer must be bytes")
    if len(buf) < HEADER_BYTES:
        _reject("sdr2-length", "buffer is shorter than its 64-byte header")
    if buf[:4] != MAGIC:
        _reject("sdr2-version", "magic is not SDR2")
    version, header_bytes = struct.unpack_from("<HH", buf, 4)
    if version != FORMAT_VERSION or header_bytes != HEADER_BYTES:
        _reject("sdr2-version", f"format {version} / header {header_bytes} is not 1 / 64")
    total = struct.unpack_from("<I", buf, 8)[0]
    if total != len(buf):
        _reject("sdr2-length", f"total_bytes {total} != buffer length {len(buf)}")
    if total > capacity or total > MAX_BYTES:
        _reject("sdr2-oversize", f"total_bytes {total} exceeds capacity {capacity}")
    expected_crc = struct.unpack_from("<I", buf, 12)[0]
    crc_input = bytearray(buf[12:])
    crc_input[:4] = b"\0" * 4
    if zlib.crc32(crc_input) & 0xFFFFFFFF != expected_crc:
        _reject("sdr2-crc", "CRC-32 does not match")

    (rid, status, boundary, header_mode, header_policy, record_count, turn) = struct.unpack_from("<HHHHHHI", buf, 16)
    if request_id is not None and rid != request_id:
        _reject("sdr2-request-id", f"request id {rid} != {request_id}")
    if mode is not None and header_mode != mode:
        _reject("sdr2-mode", f"mode {header_mode} != {mode}")
    if critical_policy is not None and header_policy != critical_policy:
        _reject("sdr2-mode", f"critical policy {header_policy} != {critical_policy}")
    if boundary > BOUNDARY_TERMINAL:
        _reject("sdr2-version", f"boundary kind {boundary} is outside 0..3")
    rng_main = list(struct.unpack_from("<4I", buf, 32))
    rng_sec = list(struct.unpack_from("<4I", buf, 48))

    records: dict[str, list] = {}
    offset, previous_tag = HEADER_BYTES, 0
    while offset < total:
        if offset + 4 > total:
            _reject("sdr2-length", "record header is truncated")
        tag, length = struct.unpack_from("<HH", buf, offset)
        if tag not in TAG_GROUPS:
            _reject("sdr2-version", f"record tag {tag} is unknown")
        if tag <= previous_tag:
            _reject("sdr2-duplicate-tag", f"tag {tag} is duplicate or descending")
        previous_tag = tag
        end = offset + 4 + length
        padded_end = (end + 3) & ~3
        if padded_end > total:
            _reject("sdr2-length", f"tag {tag} payload overruns the buffer")
        if any(buf[end:padded_end]):
            _reject("sdr2-length", f"tag {tag} padding is nonzero")
        payload = buf[offset + 4:end]
        name = TAG_GROUPS[tag]
        if tag == 9:
            records[name] = _decode_events(payload)
        else:
            records[name] = _decode_typed(name, payload, boundary=boundary)
        offset = padded_end
    if len(records) != record_count:
        _reject("sdr2-record-count", f"record_count {record_count} != {len(records)} decoded")

    if status != STATUS_OK:
        # A failed turn carries the versioned error record and nothing else.
        if set(records) != {"unsupported"}:
            _reject("sdr2-status", "a nonzero status must carry only the tag 11 record")
        return {"header": _header(buf, version, header_bytes, total, expected_crc, rid, status, boundary,
                                  header_mode, header_policy, record_count, turn, rng_main, rng_sec),
                "records": records, "child_expected": False}
    if boundary == BOUNDARY_NONE:
        _reject("sdr2-version", "a successful result needs a boundary kind")

    required = {"party", "active", "field", "side", "battle", "rng", "trainer_action", "events"}
    if boundary == BOUNDARY_TERMINAL:
        required.add("terminal")
    else:
        required.add("legal")
    missing = sorted(required - set(records))
    if missing:
        _reject("sdr2-missing-tag", "missing required tag(s): " + ", ".join(missing))
    if "terminal" in records and boundary != BOUNDARY_TERMINAL:
        _reject("sdr2-version", "tag 10 is only valid at a terminal boundary")
    if "legal" in records and boundary == BOUNDARY_TERMINAL:
        _reject("sdr2-version", "tag 7 is not valid at a terminal boundary")
    if "unsupported" in records:
        _reject("sdr2-status", "a status-0 result must not carry the tag 11 record")
    header_rng = [rng_main[0], rng_main[1], rng_main[2], rng_main[3],
                  rng_sec[0], rng_sec[1], rng_sec[2], rng_sec[3]]
    if [records["rng"][0][field] for field in FIELDS["rng"]] != header_rng:
        _reject("sdr2-identity", "tag 6 RNG does not equal the header RNG pair")

    return {"header": _header(buf, version, header_bytes, total, expected_crc, rid, status, boundary,
                              header_mode, header_policy, record_count, turn, rng_main, rng_sec),
            "records": records,
            "child_expected": boundary in (BOUNDARY_CHOICE, BOUNDARY_FORCED_SWITCH)}


def _header(buf: bytes, version: int, header_bytes: int, total: int, crc: int, rid: int, status: int,
            boundary: int, mode: int, critical_policy: int, record_count: int, turn: int,
            rng_main: list, rng_sec: list) -> dict:
    """The named header exactly as the golden vectors spell it."""
    return {"magic": MAGIC.decode(), "format": version, "header_bytes": header_bytes, "total": total,
            "crc": crc, "request_id": rid, "status": status, "boundary": boundary, "mode": mode,
            "critical_policy": critical_policy, "record_count": record_count, "turn": turn,
            **{f"rng_main{i}": value for i, value in enumerate(rng_main)},
            **{f"rng_sec{i}": value for i, value in enumerate(rng_sec)}}
