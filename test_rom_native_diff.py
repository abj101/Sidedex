"""Gate N2: the native engine returns SDR2 byte-identical to the mGBA oracle.

Live: needs the oracle worker + hook ELF and the native binary. With
SIDEDEX_GATE=1 a missing engine, or any divergence, FAILS (never skips).

  1. every green fixture row (root + each action): native SDR2 == oracle SDR2;
  2. random chained battles (random legal action or the ROM AI each turn, chained
     through child handles to terminal or SIDEDEX_DIFF_MAX_TURNS) on both engines,
     byte-equal at every turn;
  3. negative controls: a flipped byte is detected, a different seed diverges.

env: SIDEDEX_DIFF_SEED master seed (printed on failure), SIDEDEX_DIFF_BATTLES
(default 300 gated, else 20), SIDEDEX_DIFF_MAX_TURNS (40), SIDEDEX_DIFF_ONLY=<battle
index> to replay one battle, SIDEDEX_DIFF_KEEP_GOING=1 to collect every divergent
battle instead of stopping at the first, SIDEDEX_ORACLE_ELF.
"""
from __future__ import annotations

import base64
import copy
import json
import os
import random
import struct
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from tools.rom_oracle import native_gate_n1 as n1  # noqa: E402
from tools.rom_oracle import run_dynamic, sdr2  # noqa: E402

GATE = os.environ.get("SIDEDEX_GATE") == "1"
ORACLE_ELF = Path(os.environ.get(  # the active pin (rom_identity.ENGINE_ELF_SHA256)
    "SIDEDEX_ORACLE_ELF", ROOT / "native/rom_worker/build/mingw/pokeemerald-test-headless.elf"))
BATTLES = int(os.environ.get("SIDEDEX_DIFF_BATTLES", 300 if GATE else 20))
MAX_TURNS = int(os.environ.get("SIDEDEX_DIFF_MAX_TURNS", 40))
ONLY = os.environ.get("SIDEDEX_DIFF_ONLY")
KEEP_GOING = os.environ.get("SIDEDEX_DIFF_KEEP_GOING") == "1"
MASTER_SEED = int(os.environ.get("SIDEDEX_DIFF_SEED") or (time.time_ns() & 0xFFFFFFFF))
FIXTURES = sorted((ROOT / "tools/rom_oracle/fixtures").glob("*.json"))
GREEN = [d for d in (json.loads(p.read_text(encoding="utf-8")) for p in FIXTURES)
         if d.get("status") == "green-target"]
STATS = {"rows": 0, "battles": 0, "turns": 0, "agreed_errors": 0, "divergent": []}
RNG_OFFSET = 20  # request header: magic4 ver2 sub2 ai4 kind2 move2 slot2 replay2 -> 8 rng words


def _missing() -> str | None:
    for path in (n1.ORACLE_WORKER, ORACLE_ELF, n1.NATIVE_BIN_WIN):
        if not path.is_file():
            return f"missing engine artifact: {path}"
    return None


def _skip_or_fail(reason: str):
    (pytest.fail if GATE else pytest.skip)(reason)


def compare(o: bytes, n: bytes) -> list[str] | None:
    """None when byte-equal, else a field-level diff (fallback: first differing offset)."""
    if o == n:
        return None
    first = next((i for i in range(min(len(o), len(n))) if o[i] != n[i]), min(len(o), len(n)))
    lines = [f"first differing byte {first}; lengths oracle={len(o)} native={len(n)}"]
    try:
        po, pn = (sdr2.parse(b, capacity=1 << 20) for b in (o, n))
    except Exception as error:  # an ERROR-prefixed reply or a malformed buffer
        return lines + [f"undecodable: {error}", f"oracle={o[:160]!r}", f"native={n[:600]!r}"]
    for key, value in po["header"].items():
        if value != pn["header"][key]:
            lines.append(f"header.{key}: oracle={value} native={pn['header'][key]}")
    for group in sorted(set(po["records"]) | set(pn["records"])):
        a, b = po["records"].get(group), pn["records"].get(group)
        if a == b:
            continue
        if a is None or b is None or len(a) != len(b):
            lines.append(f"{group}: rows oracle={None if a is None else len(a)} "
                         f"native={None if b is None else len(b)}")
            continue
        for i, (ra, rb) in enumerate(zip(a, b)):
            lines += [f"{group}[{i}].{f}: oracle={ra[f]} native={rb.get(f)}" for f in ra if ra[f] != rb.get(f)]
    return lines


ERRFILE = "/tmp/n2-native.err"


def crash_info() -> str:
    """Symbolized frames of the native engine's last fault backtrace (empty if none)."""
    import subprocess
    script = (rf"a=$(grep -o '\[0x[0-9a-f]*\]' {ERRFILE} | tr -d '[]' | head -8 | tr '\n' ' '); "
              rf"grep -m1 signal {ERRFILE}; [ -n \"$a\" ] && addr2line -f -C -e {n1.NATIVE_BIN} $a "
              "| paste - - | sed 's#/home/abj1/sidedex-native/##'")
    out = subprocess.run(["wsl.exe", "-e", "sh", "-c", script], capture_output=True, text=True).stdout
    return out.strip()


class Pair:
    """Oracle + native engine, each parked at the ready announcement with a base snapshot."""

    def __init__(self):
        self.osha, self.nsha = n1.sha256(ORACLE_ELF), n1.sha256(n1.NATIVE_BIN_WIN)
        self.oracle = self.native = None
        self.start_oracle()
        self.start_native()

    def _boot(self, engine):
        ready = engine.announce()
        return ready, engine.save("n2-base")

    def start_oracle(self):
        self.oracle = n1.Engine([str(n1.ORACLE_WORKER), "--rom", str(ORACLE_ELF)], self.osha)
        self.oready, self.obase = self._boot(self.oracle)

    def start_native(self):
        # stderr carries the engine's fault backtrace: keep it for crash_info().
        # The binary may be rebuilt under us: retry, re-hashing each time.
        for attempt in range(30):
            try:
                self.nsha = n1.sha256(n1.NATIVE_BIN_WIN)
                self.native = n1.Engine(["wsl.exe", "-e", "sh", "-c",
                                         f"exec {n1.NATIVE_BIN} --rom {n1.NATIVE_BIN} 2>{ERRFILE}"], self.nsha)
                self.nready, self.nbase = self._boot(self.native)
                return
            except Exception:
                if attempt == 29:
                    raise
                time.sleep(2)

    def close(self):
        for engine in (self.oracle, self.native):
            try:
                engine.close()
            except Exception:
                engine.proc.kill()

    def encode(self, request: dict) -> tuple[bytes, bytes]:
        return (run_dynamic.encode_v9_request(request, self.oready),
                run_dynamic.encode_v9_request(request, self.nready))

    def turn(self, o_handle, n_handle, o_payload, n_payload, rid):
        """One turn on both engines -> (oracle bytes, native bytes, oracle child, native child)."""
        o_handle = self.obase if o_handle is None else o_handle  # None = the (possibly restarted) base
        n_handle = self.nbase if n_handle is None else n_handle
        out = []
        for engine, ready, handle, payload in ((self.oracle, self.oready, o_handle, o_payload),
                                               (self.native, self.nready, n_handle, n_payload)):
            try:
                t = engine.turn(handle, payload, ready, rid)
                out.append((t["sdr2"], (t["child"] or {}).get("handle")))
            except Exception as error:  # engine fault / decode failure is itself a divergence
                out.append((f"ERROR {error} {crash_info() if engine is self.native else ''}".encode(), None))
                engine.proc.kill()  # its state after a fault is unknown: fresh process, fresh handles
                (self.start_native if engine is self.native else self.start_oracle)()
        if bool(out[0][1]) != bool(out[1][1]):  # one side has no child: free the other
            self.drop(out[0][1], out[1][1])
            out[0], out[1] = (out[0][0], None), (out[1][0], None)
        return out[0][0], out[1][0], out[0][1], out[1][1]

    def drop(self, o_handle, n_handle):
        for engine, handle in ((self.oracle, o_handle), (self.native, n_handle)):
            if handle:
                try:
                    engine.op("drop_state", handle=handle)
                except Exception:
                    pass


@pytest.fixture(scope="module")
def pair():
    reason = _missing()
    if reason:
        _skip_or_fail(reason)
    p = Pair()
    yield p
    p.close()


def _fail_report(context: str, request: bytes, turn: int, o: bytes, n: bytes, diff: list[str]) -> str:
    return (f"DIVERGENCE {context} (master seed {MASTER_SEED}) turn {turn}\n"
            f"request b64: {base64.b64encode(request).decode()}\n  " + "\n  ".join(diff[:40]))


# ------------------------------------------------------------------ 1. fixture rows

@pytest.mark.parametrize("fixture", GREEN, ids=[f["id"] for f in GREEN])
def test_fixture_row_byte_equal(pair, fixture):
    root = copy.deepcopy(fixture["root"])
    op, np_ = pair.encode(root)
    o, n, oc, nc = pair.turn(None, None, op, np_, f"n2-{fixture['id']}-root")
    diff = compare(o, n)
    assert diff is None, _fail_report(f"{fixture['id']} root", op, 0, o, n, diff or [])
    STATS["rows"] += 1
    if not (oc and nc):
        return
    try:
        for entry in fixture["actions"]:
            branch = copy.deepcopy(root)
            branch["candidate_action"] = entry["action"]
            for key in ("rng_main", "rng_secondary"):
                if key in fixture:
                    branch[key] = fixture[key]
            inject = entry.get("inject_rng") or fixture.get("inject_rng")
            if inject:
                branch["rng_main"], branch["rng_secondary"] = inject["main"], inject["secondary"]
            bo, bn = pair.encode(branch)
            o, n, o2, n2 = pair.turn(oc, nc, bo, bn, f"n2-{fixture['id']}-{entry['label']}")
            pair.drop(o2, n2)
            diff = compare(o, n)
            assert diff is None, _fail_report(f"{fixture['id']}:{entry['label']}", bo, 1, o, n, diff or [])
            STATS["rows"] += 1
    finally:
        pair.drop(oc, nc)


# --------------------------------------------------------- 2. random chained battles

def _roots() -> list[dict]:
    """Every green fixture root plus the dynamic_*.json states the encoder accepts."""
    roots = [copy.deepcopy(f["root"]) for f in GREEN]
    for name in ("dynamic_sample", "dynamic_switch", "dynamic_trainer_item", "dynamic_replay"):
        roots.append(json.loads((ROOT / f"tools/rom_oracle/{name}.json").read_text(encoding="utf-8")))
    ok = []
    for r in roots:
        try:
            run_dynamic.encode_request(r, protocol_version=8)
            ok.append(r)
        except ValueError:
            pass
    return ok


def _patch(payload: bytes, kind: int, move: int, slot: int, rng: list[int]) -> bytes:
    """Rewrite the candidate action (offset 12) and both RNG streams (offset 20) of a v9 request."""
    out = bytearray(payload)
    struct.pack_into("<3H", out, 12, kind, move, slot)
    struct.pack_into("<8I", out, RNG_OFFSET, *rng)
    return bytes(out)


def _play_battle(pair: Pair, index: int, roots: list[dict]) -> tuple[int, str | None]:
    """Returns (turns compared, divergence report or None)."""
    r = random.Random(f"{MASTER_SEED}-{index}")
    root = copy.deepcopy(r.choice(roots))
    root["rng_main"] = [r.getrandbits(32) for _ in range(4)]
    root["rng_secondary"] = [r.getrandbits(32) for _ in range(4)]
    o_pay, n_pay = pair.encode(root)
    o_h, n_h, turns = None, None, 0
    for turn in range(MAX_TURNS + 1):
        rid = f"n2-b{index}-t{turn}"
        o, n, oc, nc = pair.turn(o_h, n_h, o_pay, n_pay, rid)
        if o_h:
            pair.drop(o_h, n_h)
        turns += 1
        STATS["turns"] += 1
        diff = compare(o, n)
        if diff:
            pair.drop(oc, nc)
            return turns, _fail_report(f"battle {index}", o_pay, turn, o, n, diff)
        status = 1 if o.startswith(b"ERROR") else sdr2.parse(o, capacity=1 << 20)["header"]["status"]
        if status:  # both engines refused identically (e.g. the known hook incomplete-state)
            STATS["agreed_errors"] += 1
            print(f"agreed error battle {index} turn {turn}: status {status} {o[:100]!r}")
        if not (oc and nc) or turn == MAX_TURNS:
            pair.drop(oc, nc)
            return turns, None
        parsed = sdr2.parse(o, capacity=1 << 20)
        rows = [x for x in parsed["records"]["legal"] if x["kind"] in (0, 1)]
        choice = r.choice(rows + [None])
        rng = ([parsed["records"]["rng"][0][f] for f in sdr2.FIELDS["rng"]] if r.random() < 0.8
               else [r.getrandbits(32) for _ in range(8)])
        acts = (2, 0, 0) if choice is None else (
            (0, choice["id"], 0) if choice["kind"] == 0 else (1, 0, choice["target_or_slot"]))
        o_pay, n_pay = _patch(o_pay, *acts, rng), _patch(n_pay, *acts, rng)
        o_h, n_h = oc, nc
    return turns, None


def test_random_chained_battles_byte_equal(pair):
    roots = _roots()
    indices = [int(ONLY)] if ONLY else range(BATTLES)
    reports = []
    for index in indices:
        turns, report = _play_battle(pair, index, roots)
        STATS["battles"] += 1
        if report:
            STATS["divergent"].append(index)
            print(report)
            reports.append(report)
            if not KEEP_GOING:
                break
    assert not reports, f"{len(reports)} divergent battle(s) {STATS['divergent']}\n" + reports[0]


# -------------------------------------------------------------- 3. negative controls

def test_negative_controls(pair):
    fixture = next(f for f in GREEN if f["id"] == "P1")
    root = copy.deepcopy(fixture["root"])
    rp = pair.encode(root)
    _, _, oc, nc = pair.turn(None, None, *rp, "n2-neg-root")
    branch = {**root, "candidate_action": fixture["actions"][0]["action"]}
    bo, bn = pair.encode(branch)
    o, n, o2, n2 = pair.turn(oc, nc, bo, bn, "n2-neg-branch")
    pair.drop(o2, n2)
    assert compare(o, n) is None
    flipped = bytearray(n)
    flipped[len(flipped) // 2] ^= 1
    assert compare(o, bytes(flipped)), "flipped byte went undetected"
    seeded = {**branch, "rng_main": [(v + 1) & 0xFFFFFFFF for v in branch["rng_main"]]}
    so, sn = pair.encode(seeded)
    o3, n3, o4, n4 = pair.turn(oc, nc, so, sn, "n2-neg-seeded")
    pair.drop(o4, n4)
    pair.drop(oc, nc)
    assert compare(o3, n3) is None, "engines disagree on the shifted seed"
    assert compare(o, n3), "a different seed did not change the result"


def test_zz_summary(pair):
    print(f"\nN2 summary: master seed {MASTER_SEED}; fixture rows compared {STATS['rows']}; "
          f"battles {STATS['battles']}; turns {STATS['turns']}; agreed errors {STATS['agreed_errors']}; divergent battles {STATS['divergent']}")
