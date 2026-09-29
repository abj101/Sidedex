import random

from planner import leaf_value, plan

A, B = 1, 2
LEAF = {"p_alive": 1, "o_alive": 1, "p_hp": 1, "o_hp": 1}


def res(win, consumed, legal=()):
    return {"outcome": "win" if win else "loss", "turns": 3, "prefix_consumed": consumed,
            "legal_at_prefix_end": list(legal), "leaf": LEAF}


def u_of(it, crn=True):
    key = f"{it['rng1']}{it['rng2']}" + ("" if crn else str(it["prefix"][0]))
    return random.Random(key).random()


def flat_engine(p, crn=True, invert=False):
    """One-decision game: root action a wins with probability p[a]; the seed decides via one shared uniform."""
    def rollout(root, items):
        return [res((u_of(it, crn) < p[it["prefix"][0]]) != invert, len(it["prefix"])) for it in items]
    return rollout


def run(engine, actions=(A, B), seed=0, **kw):
    snaps = list(plan(engine, "root", actions, random.Random(seed), **kw))
    return snaps[-1]


def test_picks_better_action_and_stops_on_separation():
    s = run(flat_engine({A: 0.7, B: 0.5}))
    assert s["done"] and s["reason"] == "resolved" and s["leading"] == [A] and s["best"] == A
    assert s["seeds"] < 4096
    a = s["actions"][A]
    assert abs(a["p_win"] - 0.7) < 3 * a["half_width"] + 0.05


def test_negative_control_wrong_value_flips_choice():
    # the assertion above must be falsifiable: an inverted engine makes the planner pick B
    assert run(flat_engine({A: 0.7, B: 0.5}, invert=True))["best"] == B


def test_crn_pairing_stops_earlier_than_decorrelated_seeds():
    p = {A: 0.7, B: 0.5}
    paired = sum(run(flat_engine(p), seed=i)["seeds"] for i in range(10))
    # same planner, but the engine breaks the pairing
    unpaired = sum(run(flat_engine(p, crn=False), seed=i)["seeds"] for i in range(10))
    assert paired < unpaired


def test_equal_actions_return_leading_set():
    s = run(flat_engine({A: 0.5, B: 0.5}, crn=False), max_seeds=256)
    assert s["done"] and s["reason"] == "max_seeds" and sorted(s["leading"]) == [A, B]


def test_identical_on_every_seed_resolves_early_as_a_tie():
    # all actions always win: nothing more to learn, so no run to the budget
    s = run(flat_engine({A: 1.0, B: 1.0}), max_seeds=4096)
    assert s["reason"] == "resolved" and s["seeds"] == 16 and sorted(s["leading"]) == [A, B]


def test_budget_stops_with_fake_clock():
    t = [0.0]

    def clock():
        t[0] += 1.0
        return t[0]
    s = run(flat_engine({A: 0.5, B: 0.5}), clock=clock, budget_s=3.0)
    assert s["reason"] == "budget" and s["seeds"] == 8


# Two-step game: greedy first move (A: AI-finish wins 0.6) is wrong; B then 10 wins 0.9.
DEEP = {(A, 10): 0.5, (A, 11): 0.5, (B, 10): 0.9, (B, 11): 0.2}
BASE = {A: 0.6, B: 0.4}


def deep_engine(log=None, odd_illegal=False):
    def rollout(root, items):
        out = []
        for it in items:
            pre, u = it["prefix"], u_of(it)
            legal = [10, 11]
            if odd_illegal and pre[0] == B and it["rng1"][0] % 2:
                legal = [10]  # 11 is illegal in half the samples
            if len(pre) >= 2 and pre[1] in legal:
                out.append(res(u < DEEP[pre[0], pre[1]], 2))
                if log is not None:
                    log.append(tuple(pre[:2]))
            else:
                out.append(res(u < BASE[pre[0]], 1, legal))
        return out
    return rollout


def test_deep_search_beats_greedy_first_move():
    s = run(deep_engine(), max_seeds=2048)
    assert s["best"] == B and s["leading"] == [B]


def test_illegal_prefix_backs_up_consumed_part_only():
    log = []
    s = run(deep_engine(log, odd_illegal=True), max_seeds=1024, min_seeds=10**9)
    kid = s["tree"][B].kids[11]
    assert kid.n == log.count((B, 11)) and 0 < kid.n < s["tree"][B].n and kid.vl == 0
    assert s["tree"][B].vl == 0 and s["best"] == B


def test_cap_leaf_value():
    cap = lambda **lf: {"outcome": "cap", "leaf": {**LEAF, **lf}}
    assert leaf_value(cap()) == 0.5
    assert leaf_value(cap(p_alive=1, o_alive=0.5, p_hp=1, o_hp=0.5)) == 0.75
    assert leaf_value(cap(p_alive=0, o_alive=1, p_hp=0, o_hp=1)) == 0.0
    assert leaf_value({"outcome": "win"}) == 1.0 and leaf_value({"outcome": "loss"}) == 0.0
    # end to end: cap outcomes decide on hp, and beat a certain loss but not a win
    def rollout(root, items):
        return [{"outcome": "cap", "turns": 60, "prefix_consumed": 1, "legal_at_prefix_end": [],
                 "leaf": {**LEAF, "p_hp": 0.9 if it["prefix"][0] == A else 0.1}} for it in items]
    assert run(rollout)["best"] == A


def test_generator_first_update_after_one_batch_and_batch_sizes():
    sizes = []

    def rollout(root, items):
        sizes.append(len(items))
        return flat_engine({A: 0.5, B: 0.5})(root, items)
    g = plan(rollout, "root", [A, B], random.Random(1), max_seeds=512, min_seeds=10**9)
    first = next(g)
    assert sizes == [16] and first["seeds"] == 8 and not first["done"]
    list(g)
    assert sizes[:4] == [16, 32, 64, 128] and sizes[-1] <= 512 * 2


def test_policy_passthrough_and_single_action():
    seen = []

    def rollout(root, items):
        seen.extend(it.get("policy") for it in items)
        return [res(True, 1) for _ in items]
    s = run(rollout, actions=(A,), policy=7)
    assert s["done"] and s["seeds"] == 8 and set(seen) == {7}
