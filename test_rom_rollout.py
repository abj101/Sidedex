"""rom_rollout: unit contract on a fake worker, plus a live mGBA check (skipped unless staged).

    python -m pytest test_rom_rollout.py -q
Live: SIDEDEX_V9_ELF (default: the H oracle ELF under ~/.codex/artifacts) + SIDEDEX_ROM_WORKER_EXE.
"""
import json
import os
from pathlib import Path

import pytest

import rom_rollout as rr

ROOT = Path(__file__).parent


class FakeClient:
    def __init__(self):
        self.live = set()
        self.peak = 0

    def drop_state(self, handle):
        self.live.remove(handle)


class FakeRunner:
    """Player needs 3 hits (word (0, 1)) to win; word (0, 2) is a dud; every AI turn hits.
    The opponent kills the player after `loss_at` turns. RNG advances by +1 per turn; the child
    handle encodes the turn count, so a wrong-slot restore would show."""

    def __init__(self, loss_at=99):
        self.client, self.loss_at, self.seen, self.n = FakeClient(), loss_at, [], 0

    def install(self, blob, key, hold=0):
        pass

    def turn(self, req, handle):
        done = int(handle.split(":")[1]) if handle else 0
        self.seen.append((req["candidate_action"], req["rng_main"][0], handle))
        act = req["candidate_action"]
        done += act == {"kind": "ai"} or act == {"kind": "move", "move_id": 1}
        turns = (int(handle.split(":")[2]) if handle else 0) + 1
        win, loss = done >= 3, turns >= self.loss_at
        rng = req["rng_main"][0] + 1
        header = {**{f"rng_main{i}": rng if i == 0 else 0 for i in range(4)},
                  **{f"rng_sec{i}": 0 for i in range(4)}, "boundary": 3 if win or loss else 1}
        records = {"party": [{"side": 0, "slot": 0, "hp": 0 if loss else 5, "max_hp": 10, "present": 1},
                             {"side": 1, "slot": 0, "hp": 0 if win else 5, "max_hp": 10, "present": 1}],
                   "legal": [] if header["boundary"] == 3 else
                   [{"kind": 0, "id": 1, "target_or_slot": 1}, {"kind": 1, "id": 1, "target_or_slot": 1}]}
        if header["boundary"] == 3:
            records["terminal"] = [{"result": 1 if win else 2, "reason": 0}]
        self.n += 1
        child = None if header["boundary"] == 3 else {"handle": f"c{self.n}:{done}:{turns}"}
        if child:
            self.client.live.add(child["handle"])
            self.client.peak = max(self.client.peak, len(self.client.live))
        return {"binary": {"header": header, "records": records}, "child": child}


ROOT_ = rr.Root({"base": 1}, [(0, 1), (1, 1)], "blob", "k", 0)
ITEM = {"rng1": (7, 0, 0, 0), "rng2": (0, 0, 0, 0), "max_turns": 60}


def run(runner, **kw):
    return rr.Rollout(runner)(ROOT_, [{**ITEM, "prefix": [], **kw}])[0]


def test_ai_plays_to_terminal_carries_rng_and_keeps_one_child():
    runner = FakeRunner()
    res = run(runner)
    assert res == {"outcome": "win", "turns": 3, "prefix_consumed": 0, "legal_at_prefix_end": [(0, 1), (1, 1)],
                   "leaf": {"p_alive": 1.0, "o_alive": 0.0, "p_hp": 0.5, "o_hp": 0.0}}
    assert [s[1] for s in runner.seen] == [7, 8, 9]  # rng1 injected first, then each result's carry
    assert [s[2] is None for s in runner.seen] == [True, False, False]
    assert runner.client.live == set() and runner.client.peak == 2  # never leaks; worker slots: default + two children


def test_prefix_is_played_then_stops_at_an_illegal_word():
    runner = FakeRunner()
    assert run(runner, prefix=[(0, 1), (0, 1)])["prefix_consumed"] == 2
    assert [s[0] for s in runner.seen[:3]] == [{"kind": "move", "move_id": 1}] * 2 + [{"kind": "ai"}]
    runner = FakeRunner()
    res = run(runner, prefix=[(0, 1), (0, 99), (0, 1)])  # (0, 99) is not legal after one turn
    assert (res["prefix_consumed"], res["turns"], res["outcome"]) == (1, 3, "win")
    assert [s[0]["kind"] for s in runner.seen] == ["move", "ai", "ai"]
    assert run(FakeRunner(), prefix=[(1, 1)])["prefix_consumed"] == 1  # switch words are legal words too


def test_cap_and_loss():
    res = run(FakeRunner(), max_turns=2)
    assert (res["outcome"], res["turns"], res["legal_at_prefix_end"]) == ("cap", 2, [(0, 1), (1, 1)])
    assert res["leaf"]["o_hp"] == 0.5
    assert run(FakeRunner(loss_at=2), prefix=[(0, 2)])["outcome"] == "loss"  # illegal (0, 2): the AI plays, the opponent wins on turn 2


# --------------------------------------------------------------------------- #
# Live (mGBA)
# --------------------------------------------------------------------------- #
ELF = Path(os.environ.get("SIDEDEX_V9_ELF", Path.home() / ".codex/artifacts/H/14177d962d/pokeemerald-test-headless.elf"))
WORKER = Path(os.environ.get("SIDEDEX_ROM_WORKER_EXE", rr.rom_bridge.DEFAULT_WORKER))
live = pytest.mark.skipif(not (ELF.is_file() and WORKER.is_file()), reason="v9 ELF or worker not staged")


def p1_root(runner):
    """P1 (physical neutral) as a 1v1, so the battle ends inside the cap."""
    data = json.loads((ROOT / "tools/rom_oracle/fixtures/p01_physical_neutral.json").read_text(encoding="utf-8"))
    request = {**data["root"], "critical_policy": "normal", "operation": "simulate-turn", "mode": "normal"}
    request["player_party"], request["opponent_party"] = request["player_party"][:1], request["opponent_party"][:1]
    return rr.make_root(runner, request)


@live
def test_live_p1_rollout_reaches_a_terminal_and_is_seed_deterministic():
    client, runner = rr.open_worker(ELF, WORKER)
    try:
        root = p1_root(runner)
        rollout = rr.Rollout(runner)
        item = {"rng1": (1, 2, 3, 4), "rng2": (5, 6, 7, 8), "prefix": [], "max_turns": 60}
        first, again = rollout(root, [item])[0], rollout(root, [item])[0]
        assert first["outcome"] in ("win", "loss") and 1 < first["turns"] < 60
        assert first == again
        assert client.list_states()["states"][0]["name"] == "default" and len(client.list_states()["states"]) == 1
        # Negative control: other seeds give another trajectory (turns or the leaf HP differ).
        others = rollout(root, [{**item, "rng1": (s, 2, 3, 4), "rng2": (5, 6, 7, s)} for s in range(2, 8)])
        assert any((o["turns"], o["leaf"]) != (first["turns"], first["leaf"]) for o in others)
        # A prefix word plays that move first; an unlisted word stops the prefix at once.
        assert rollout(root, [{**item, "prefix": [(0, 56)]}])[0]["prefix_consumed"] == 1
        assert rollout(root, [{**item, "prefix": [(0, 9999)]}])[0]["prefix_consumed"] == 0
    finally:
        client.close()
