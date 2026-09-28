"""Write the golden SDR2 wire vectors (contract Appendix C).

Independent reference *encoder* for package A1 (producer) and the reference
expectations for package B's decoder (tools/rom_oracle/sdr2.py). The vectors
are SYNTHETIC wire-format examples (not ROM-derived): they pin byte layout,
CRC, tag order, padding and negative handling. `python make_sdr2_vectors.py`
rewrites tools/rom_oracle/sdr2_vectors/; `--check` fails if files are stale.
"""

from __future__ import annotations

import hashlib
import json
import struct
import sys
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent / "sdr2_vectors"

# Word tables (contract Appendix C). words_per_entry is fixed per tag.
PARTY = ["side", "slot", "species", "level", "hp", "max_hp", "status1", "item", "ability", "form", "nature",
         "move1", "move2", "move3", "move4", "pp1", "pp2", "pp3", "pp4",
         *[f"iv_{s}" for s in ("hp", "atk", "def", "spe", "spa", "spd")],
         *[f"ev_{s}" for s in ("hp", "atk", "def", "spe", "spa", "spd")], "present"]
ACTIVE = ["battler", "party_slot", "species", "hp", "max_hp", "status1", "status2", "status3", "ability", "item", "form",
          "type1", "type2", "type3", *[f"stage_{s}" for s in ("atk", "def", "spe", "spa", "spd", "acc", "eva")],
          "move1", "move2", "move3", "move4", "pp1", "pp2", "pp3", "pp4", "volatile_flags", "disabled_move",
          "disable_timer", "protect_flags", "special_status_flags", "substitute_hp", "encore_timer", "taunt_timer"]
FIELD = ["weather", "weather_timer", "terrain", "terrain_timer", "trick_room", "trick_room_timer"]
SIDE = ["side", "screens", "hazards", "hazard_layers", "conditions", "reflect_timer", "light_screen_timer",
        "aurora_veil_timer", "mist_timer", "safeguard_timer", "tailwind_timer", "follow_me_timer"]
BATTLE = ["turn", "outcome", "type_flags", "battler_count", "ai_flags", "item_count", "item1", "item2", "item3", "item4"]
RNG = ["main_a", "main_b", "main_c", "main_ctr", "sec_a", "sec_b", "sec_c", "sec_ctr"]
LEGAL = ["kind", "id", "pp_or_species", "target_or_slot"]
TRAINER = ["kind", "id", "target"]
TERMINAL = ["result", "reason"]
UNSUPPORTED = ["reason", "source"]
TABLES = {1: PARTY, 2: ACTIVE, 3: FIELD, 4: SIDE, 5: BATTLE, 6: RNG, 7: LEGAL, 8: TRAINER, 10: TERMINAL, 11: UNSUPPORTED}
NAMES = {1: "party", 2: "active", 3: "field", 4: "side", 5: "battle", 6: "rng", 7: "legal", 8: "trainer_action",
         9: "events", 10: "terminal", 11: "unsupported"}


def entry(table, **kw):
    bad = set(kw) - set(table)
    assert not bad, bad
    return [kw.get(n, 0) for n in table]


def typed(tag, entries):
    words = len(TABLES[tag])
    assert all(len(e) == words for e in entries)
    return tag, struct.pack("<HH", len(entries), words) + b"".join(struct.pack(f"<{words}I", *e) for e in entries)


def events(items):
    return 9, b"".join(struct.pack(f"<HH{len(p)}I", k, len(p), *p) for k, p in items)


def build(records, *, request_id=1, status=0, boundary=1, mode=0, policy=0, turn=1, rng=(1, 2, 3, 4, 5, 6, 7, 8),
          crc_ok=True, count=None, total=None):
    body = b""
    for tag, payload in records:
        body += struct.pack("<HH", tag, len(payload)) + payload + b"\0" * (-len(payload) % 4)
    size = 64 + len(body)
    head = struct.pack("<4sHHIIHHHHHHI8I", b"SDR2", 1, 64, size if total is None else total, 0, request_id, status,
                       boundary, mode, policy, len(records) if count is None else count, turn, *rng)
    buf = bytearray(head + body)
    struct.pack_into("<I", buf, 12, zlib.crc32(bytes(buf[12:])) if crc_ok else 0xDEADBEEF)
    return bytes(buf)


def named(tag, payload):
    if tag == 9:
        out, i = [], 0
        while i < len(payload):
            kind, n = struct.unpack_from("<HH", payload, i)
            out.append({"kind": kind, "payload": list(struct.unpack_from(f"<{n}I", payload, i + 4))})
            i += 4 + 4 * n
        return out
    count, words = struct.unpack_from("<HH", payload)
    flat = struct.unpack_from(f"<{count * words}I", payload, 4)
    return [dict(zip(TABLES[tag], flat[i * words:(i + 1) * words])) for i in range(count)]


def mon(side, slot, species, hp, moves, item=0):
    return entry(PARTY, side=side, slot=slot, species=species, level=50, hp=hp, max_hp=max(hp, 100), item=item,
                 ability=1, nature=3, move1=moves[0], move2=moves[1], move3=moves[2], move4=moves[3],
                 pp1=25, pp2=25, pp3=10, pp4=5, iv_hp=31, iv_atk=31, iv_def=31, iv_spe=31, iv_spa=31, iv_spd=31,
                 ev_hp=0, present=1)


def party(hp0=100, hp1=100):
    rows = [entry(PARTY) for _ in range(12)]
    rows[0] = mon(0, 0, 1, hp0, (33, 45, 73, 22))
    rows[1] = mon(0, 1, 4, hp1, (10, 52, 83, 108))
    rows[6] = mon(1, 0, 485, 100, (33, 53, 89, 126))
    rows[7] = mon(1, 1, 92, 80, (101, 95, 109, 122))
    return typed(1, rows)


def active(a0=0, a1=0, hp0=100):
    def one(b, slot, species, hp):
        return entry(ACTIVE, battler=b, party_slot=slot, species=species, hp=hp, max_hp=100, ability=1,
                     type1=12, type2=12, type3=0, move1=33, move2=45, move3=73, move4=22, pp1=25, pp2=25, pp3=10, pp4=5)
    return typed(2, [one(0, a0, 1, hp0), one(1, a1, 485, 100)])


def common(nonterm_legal, boundary_turn, hp0=100, status_ev=True, terminal=None):
    recs = [party(hp0), active(hp0=hp0), typed(3, [entry(FIELD, weather=1, weather_timer=5)]),
            typed(4, [entry(SIDE, side=0), entry(SIDE, side=1, hazards=1, hazard_layers=2)]),
            typed(5, [entry(BATTLE, turn=boundary_turn, outcome=0 if terminal is None else terminal, type_flags=8,
                            battler_count=2, ai_flags=0x20000000, item_count=1, item1=13)]),
            typed(6, [list(range(1, 9))])]
    if nonterm_legal is not None:
        recs.append(typed(7, nonterm_legal))
    recs.append(typed(8, [entry(TRAINER, kind=0, id=33, target=0)]))
    recs.append(events([(1, [1, 33, 0]), (2, [0, 12, 88])] if status_ev else []))
    return recs


CHOICE = [[0, 33, 25, 1], [0, 45, 25, 1], [0, 73, 10, 1], [0, 22, 5, 1], [1, 1, 4, 1]]
FORCED = [[1, 1, 4, 1]]


def vectors():
    good = {
        "choice_p1": build(common(CHOICE, 2), turn=2, boundary=1),
        "forced_switch_p26": build(common(FORCED, 2, hp0=0), turn=2, boundary=2),
        "terminal_win": build(common(None, 3, terminal=1) + [typed(10, [entry(TERMINAL, result=1, reason=0)])],
                              turn=3, boundary=3),
        "unsupported_status": build([typed(11, [entry(UNSUPPORTED, reason=3, source=4)])], status=6, boundary=0),
    }
    base = common(CHOICE, 2)
    bad = {
        "bad_crc": build(base, crc_ok=False),
        "bad_length": build(base, total=len(good["choice_p1"]) + 4),
        "duplicate_tag": build(base + [base[0]]),
        "missing_tag7": build([r for r in base if r[0] != 7]),
        "oversize": build(base[:-1] + [(9, b"\0" * 65532)]),  # > 8192 announced capacity
        "wrong_request_id": build(base, request_id=2),
        "bad_record_count": build(base, count=len(base) + 1),
    }
    return good, bad


def expectation(name, blob, ok):
    exp = {"name": name, "synthetic": True, "sha256": hashlib.sha256(blob).hexdigest(), "bytes": len(blob),
           "valid": ok, "expect_request_id": 1, "capacity": 8192}
    if ok:
        h = struct.unpack_from("<4sHHIIHHHHHHI8I", blob)
        exp["header"] = dict(zip(["magic", "format", "header_bytes", "total", "crc", "request_id", "status", "boundary",
                                  "mode", "critical_policy", "record_count", "turn", "rng_main0", "rng_main1", "rng_main2",
                                  "rng_main3", "rng_sec0", "rng_sec1", "rng_sec2", "rng_sec3"],
                                 [h[0].decode(), *h[1:]]))
        recs, off = {}, 64
        while off < len(blob):
            tag, n = struct.unpack_from("<HH", blob, off)
            recs[NAMES[tag]] = named(tag, blob[off + 4:off + 4 + n])
            off += 4 + n + (-n % 4)
        exp["records"] = recs
        exp["child_expected"] = h[6] == 0 and h[7] in (1, 2)
    return exp


def main():
    good, bad = vectors()
    files = {}
    for name, blob in {**good, **bad}.items():
        files[f"{name}.bin"] = blob
        exp = expectation(name, blob, name in good)
        if name in bad:
            exp["reject"] = name
        files[f"{name}.json"] = (json.dumps(exp, indent=1, sort_keys=True) + "\n").encode()
    files["tables.json"] = (json.dumps({"words": {NAMES[t]: len(v) for t, v in TABLES.items()},
                                        "fields": {NAMES[t]: v for t, v in TABLES.items()}}, indent=1) + "\n").encode()
    if "--check" in sys.argv:
        stale = [n for n, b in files.items() if not (OUT / n).exists() or (OUT / n).read_bytes() != b]
        sys.exit(f"stale: {stale}" if stale else 0)
    OUT.mkdir(exist_ok=True)
    for n, b in files.items():
        (OUT / n).write_bytes(b)
    print(f"wrote {len(files)} files to {OUT}")


if __name__ == "__main__":
    main()
