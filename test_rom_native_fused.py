"""Live: the native fused `rollout` op equals the reference `rom_rollout` drive on the mGBA oracle ELF.

Each engine builds its own root from the same request; items are identical. Skipped unless both are staged.
"""
import json
import os
import random
from pathlib import Path

import pytest

import rom_rollout as rr

ROOT = Path(__file__).parent
ELF = Path(os.environ.get("SIDEDEX_ENGINE_ELF_PATH", ROOT / "native/rom_worker/build/mingw/pokeemerald-test-headless.elf"))
WORKER = Path(os.environ.get("SIDEDEX_ROM_WORKER_EXE", rr.rom_bridge.DEFAULT_WORKER))
NATIVE = rr._native_bin()
live = pytest.mark.skipif(not (ELF.is_file() and WORKER.is_file() and NATIVE.is_file()), reason="mGBA oracle or native engine not staged")


def request(one_v_one):
    data = json.loads((ROOT / "tools/rom_oracle/fixtures/p01_physical_neutral.json").read_text(encoding="utf-8"))
    req = {**data["root"], "critical_policy": "normal", "operation": "simulate-turn", "mode": "normal"}
    if one_v_one:
        req["player_party"], req["opponent_party"] = req["player_party"][:1], req["opponent_party"][:1]
    return req


def items(root, n, max_turns, seed):
    g, out = random.Random(seed), []
    for i in range(n):
        prefix = [g.choice(root.legal) for _ in range(g.randrange(3))]
        if i % 7 == 3:
            prefix.append((0, 9999))  # illegal word: the prefix stops there
        out.append({"rng1": [g.getrandbits(32) | 1 for _ in range(4)], "rng2": [g.getrandbits(32) for _ in range(4)],
                    "prefix": prefix, "max_turns": max_turns if i % 9 else 0})
    return out


def same(a, b):
    la, lb = a.pop("leaf"), b.pop("leaf")  # the native leaf is rounded to 6 decimals
    return a == b and (la is None) == (lb is None) and (la is None or la == pytest.approx(lb, abs=1e-6))


@live
@pytest.mark.parametrize("one_v_one,n,turns", [(False, 24, 6), (True, 12, 60)])
def test_fused_equals_reference(one_v_one, n, turns):
    ref_client, ref = rr.open_worker(ELF, WORKER)
    nat_client, nat = rr.open_worker(NATIVE, NATIVE, command=["wsl.exe", "-e", rr.rom_native.NATIVE_BIN, "--rom", rr.rom_native.NATIVE_BIN])
    try:
        req = request(one_v_one)
        rroot, nroot = rr.make_root(ref, req), rr.make_root(nat, req)
        assert rroot.legal == nroot.legal
        batch = items(rroot, n, turns, 7)
        want = rr.Rollout(ref)(rroot, batch)
        got = rr.Rollout(nat)(nroot, batch)
        assert sum(same(dict(g), dict(w)) for g, w in zip(got, want)) == n, (got, want)
        assert any(w["prefix_consumed"] for w in want) and any(w["turns"] > 1 for w in want)
        # negative control: shifted seeds must break the equality
        shifted = [{**i, "rng1": [i["rng1"][0] + 1, *i["rng1"][1:]]} for i in batch]
        bad = rr.Rollout(nat)(nroot, shifted)
        assert sum(same(dict(g), dict(w)) for g, w in zip(bad, want)) < n
    finally:
        ref_client.close()
        nat_client.close()
