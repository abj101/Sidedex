"""Live checks of the native engine's `rollout` op (skipped unless the binary exists).

Root = the P1 player-choice boundary. Every rollout is compared with a turn-by-turn drive of the
same native binary through `batch_turns_v3` (same requests, same per-turn tokens, RNG carried from
each result's header): the final SDR2 must be byte-equal.
"""
from __future__ import annotations

import base64
import hashlib
import json
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import rom_native as rn  # noqa: E402
from tools.rom_oracle import run_dynamic, sdr2  # noqa: E402

pytestmark = pytest.mark.skipif(not Path(rn.NATIVE_BIN_WIN).is_file(),
                                reason=f"native engine {rn.NATIVE_BIN} is missing")

FIXTURE = ROOT / "tools/rom_oracle/fixtures/p01_physical_neutral.json"
TOKEN = 0x2000
FRAMES = 2000  # ordinary turns take under ~600 frames; a hung ROM turn fails fast


def seeded(seed: int, **item) -> dict:
    return {"rng1": [seed * 7919 + 1, seed * 104729 + 3, seed * 13 + 5, 1], "rng2": [1, 2, seed + 3, 29],
            "prefix": [], "max_turns": 6, **item}


def rows(binary: dict) -> list[list[int]]:
    return [list(r.values()) for r in binary["records"].get("legal", [])]


@pytest.fixture(scope="module")
def env():
    engine = rn.Engine.native()
    ready = engine.announce()
    payload = run_dynamic.encode_v9_request(json.loads(FIXTURE.read_text())["root"], ready)
    base = engine.save("base")
    root = engine.turn(base, payload, ready, "test-root", hash_children=False)
    engine.op("drop_state", handle=base)
    token = int.from_bytes(hashlib.sha256(b"test-root").digest()[:2], "little")
    parsed = sdr2.parse(root["sdr2"], request_id=token, mode=0, critical_policy=0,
                        capacity=ready["result_capacity"])
    yield {"engine": engine, "ready": ready, "payload": payload, "handle": root["child"]["handle"],
           "root_legal": rows(parsed)}
    engine.close()


def rollout(env, items, rid="test-rollout", debug=True):
    return env["engine"].rollout(env["handle"], items, rid, debug=debug, ready=env["ready"],
                                 request_token=TOKEN, max_frames=FRAMES)


def turn_by_turn(env, item: dict) -> dict:
    """Reference drive of one item through batch_turns_v3."""
    engine, ready = env["engine"], env["ready"]
    handle, legal, rng = env["handle"], env["root_legal"], item["rng1"] + item["rng2"]
    consumed, at_end, last, terminal, turns = 0, None, None, False, 0
    while not terminal and turns < item["max_turns"]:
        kind, move, slot = 2, 0, 0
        prefix = item["prefix"][consumed] if consumed < len(item["prefix"]) else None
        if at_end is None and prefix and any(r[:2] == prefix[:2] for r in legal):
            kind = prefix[0]
            move, slot = (prefix[1], 0) if kind == 0 else (0, prefix[1])
            consumed += 1
        elif at_end is None:
            at_end = legal
        request = bytearray(env["payload"])
        struct.pack_into("<HHH", request, 12, kind, move, slot)
        struct.pack_into("<8I", request, 20, *rng)
        token = (TOKEN + turns) & 0xFFFF
        turn = engine.turn(handle, bytes(request), ready, f"tbt-{turns}", hash_children=False,
                           request_token=token)
        if handle != env["handle"]:
            engine.op("drop_state", handle=handle)
        turns += 1
        last = sdr2.parse(turn["sdr2"], request_id=token, mode=0, critical_policy=0,
                          capacity=ready["result_capacity"])
        rng = list(struct.unpack_from("<8I", turn["sdr2"], 32))
        terminal = last["header"]["boundary"] == sdr2.BOUNDARY_TERMINAL
        legal = [] if terminal else rows(last)
        handle = turn["child"]["handle"] if turn["child"] else None
        final = turn["sdr2"]
    if handle and handle != env["handle"]:
        engine.op("drop_state", handle=handle)
    return {"turns": turns, "prefix_consumed": consumed, "legal_at_prefix_end": legal if at_end is None else at_end,
            "final_sdr2": final, "terminal": terminal, "last": last}


def terminal_seed(env) -> int:
    seeds = range(24)
    out = rollout(env, [seeded(s, max_turns=100) for s in seeds], "test-find", debug=False)
    for seed, r in zip(seeds, out):
        if r["outcome"] in ("win", "loss"):
            return seed
    pytest.fail(f"no seed of {len(seeds)} reached a terminal result: {[r['outcome'] for r in out]}")


def test_rollout_reaches_terminal(env):
    seed = terminal_seed(env)
    (r,) = rollout(env, [seeded(seed, max_turns=100)])
    assert r["outcome"] in ("win", "loss") and 1 < r["turns"] < 100
    assert r["legal_at_prefix_end"] == env["root_legal"] and r["prefix_consumed"] == 0  # empty prefix ends at the root
    leaf = r["leaf"]
    assert (leaf["p_alive"] == 0) == (r["outcome"] == "loss") and (leaf["o_alive"] == 0) == (r["outcome"] == "win")
    binary = sdr2.parse(base64.b64decode(r["final_sdr2"]), request_id=(TOKEN + r["turns"] - 1) & 0xFFFF,
                        mode=0, critical_policy=0, capacity=env["ready"]["result_capacity"])
    assert binary["header"]["boundary"] == sdr2.BOUNDARY_TERMINAL


def test_cap_and_root_legal(env):
    (r0,) = rollout(env, [seeded(1, max_turns=0)])
    assert (r0["outcome"], r0["turns"], r0["leaf"]) == ("cap", 0, None)
    assert r0["legal_at_prefix_end"] == env["root_legal"] and r0["legal_at_prefix_end"]
    (r3,) = rollout(env, [seeded(1, max_turns=3)])
    assert (r3["outcome"], r3["turns"]) == ("cap", 3)


def test_same_seed_identical_other_seed_differs(env):
    a1, b, a2 = rollout(env, [seeded(5), seeded(6), seeded(5)])
    assert a1 == a2
    assert a1["final_sdr2"] != b["final_sdr2"]  # negative control: the RNG seed matters


def test_rollout_equals_turn_by_turn(env):
    move = env["root_legal"][0]
    switch = next(r for r in env["root_legal"] if r[0] == 1)
    items = [
        seeded(100),
        seeded(101, prefix=[move[:2]]),
        seeded(102, prefix=[switch[:2], [0, 33]]),  # the second action may or may not be legal
        seeded(103, prefix=[[0, 9999]]),  # illegal: the prefix stops at once, the AI plays
        seeded(terminal_seed(env), max_turns=100),
    ]
    for item, got in zip(items, rollout(env, items)):
        want = turn_by_turn(env, item)
        assert got["outcome"] != "error", got
        assert base64.b64decode(got["final_sdr2"]) == want["final_sdr2"]
        assert got["turns"] == want["turns"]
        assert got["prefix_consumed"] == want["prefix_consumed"]
        assert got["legal_at_prefix_end"] == want["legal_at_prefix_end"]
        party = want["last"]["records"]["party"]
        hp = [sum(p["hp"] for p in party if p["side"] == s and p["present"]) for s in (0, 1)]
        mx = [sum(p["max_hp"] for p in party if p["side"] == s and p["present"]) for s in (0, 1)]
        assert got["leaf"]["p_hp"] == pytest.approx(hp[0] / mx[0], abs=1e-6)
        assert got["leaf"]["o_hp"] == pytest.approx(hp[1] / mx[1], abs=1e-6)
    assert [g["prefix_consumed"] for g in rollout(env, items[1:4], debug=False)][2] == 0


def test_bad_requests_leave_the_worker_usable(env):
    engine = env["engine"]
    with pytest.raises(rn.NativeError) as stale:
        engine.rollout("0" * 32, [seeded(1)], "test-stale", ready=env["ready"])
    assert stale.value.reply["error"] == "stale-handle"
    with pytest.raises(rn.NativeError) as bad:
        rollout(env, [{"rng1": [1, 2, 3]}], "test-bad")
    assert bad.value.reply["error"] == "invalid-rollout-request"
    assert rollout(env, [seeded(1, max_turns=1)])[0]["turns"] == 1
