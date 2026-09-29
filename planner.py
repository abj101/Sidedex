"""Engine-agnostic open-loop UCT planner with common random numbers (see ROM_NATIVE_PLAN.md).

The engine is an injected callable matching the native `rollout` op:
    rollout(root, items) -> [result]
    item   = {"rng1": (4 u32), "rng2": (4 u32), "prefix": [action, ...], "max_turns": int[, "policy": ...]}
    result = {"outcome": "win"|"loss"|"cap", "turns": int, "prefix_consumed": int,
              "legal_at_prefix_end": [action, ...],
              "leaf": {"p_alive", "o_alive", "p_hp", "o_hp"}}   # fractions 0..1
"""
import math
import random
import time
from statistics import fmean, stdev


class Node:
    """One player-action path. `legal` = union of legal sets observed here; `kids` = tried actions."""

    __slots__ = ("n", "sum", "vl", "legal", "kids")

    def __init__(self):
        self.n, self.sum, self.vl, self.legal, self.kids = 0, 0.0, 0, {}, {}


def leaf_value(res):
    if res["outcome"] == "win":
        return 1.0
    if res["outcome"] == "loss":
        return 0.0
    lf = res["leaf"]
    v = 0.5 + 0.25 * ((lf["p_alive"] - lf["o_alive"]) + (lf["p_hp"] - lf["o_hp"]))
    return min(1.0, max(0.0, v))


def _select(node, action, rng, c):
    """Walk by UCB1 over known kids, expand one untried legal action; apply virtual loss to the path."""
    prefix, path = [action], [node]
    while True:
        untried = [x for x in node.legal if not (x in node.kids and node.kids[x].n + node.kids[x].vl)]
        if untried:  # includes kids only ever tried in samples where they were illegal
            x = rng.choice(untried)
            node = node.kids.setdefault(x, Node())
            prefix.append(x)
            path.append(node)
            break
        live = [(x, k) for x, k in node.kids.items() if k.n + k.vl]
        if not live:
            break
        total = sum(k.n + k.vl for _, k in live)
        x, node = max(live, key=lambda xk: (xk[1].sum + c * math.sqrt(math.log(total) * (xk[1].n + xk[1].vl)))
                      / (xk[1].n + xk[1].vl))  # mean + c*sqrt(ln N / n); vl counts as zero-value visits
        prefix.append(x)
        path.append(node)
    for nd in path:
        nd.vl += 1
    return prefix, path


def _snapshot(acts, vals, wins, tree, z, min_seeds, done, reason):
    n = len(vals[acts[0]])
    stats = {a: {"mean": fmean(vals[a]), "p_win": wins[a] / n,
                 "half_width": z * stdev(vals[a]) / math.sqrt(n) if n > 1 else 1.0, "samples": n} for a in acts}
    best = max(acts, key=lambda a: stats[a]["mean"])
    lead = [best]
    for o in acts:
        if o == best:
            continue
        d = [x - y for x, y in zip(vals[best], vals[o])]  # paired: same seed per row
        # ponytail: repeated looks at a fixed 95% CI inflate false stops slightly; sequential bound if it matters
        if n < min_seeds or fmean(d) - z * stdev(d) / math.sqrt(n) <= 0:
            lead.append(o)
    lead.sort(key=lambda a: -stats[a]["mean"])
    return {"actions": stats, "best": best, "leading": lead, "seeds": n, "done": done, "reason": reason, "tree": tree}


def plan(rollout, root, root_actions, rng=None, budget_s=3.0, max_turns=60, policy=None,
         clock=time.monotonic, max_seeds=4096, min_seeds=16, c=1.0, z=1.96):
    """Generator: yields a snapshot after every seed batch (8, 16, 32, ... seeds; every root action per seed).

    Snapshot: {"actions": {a: {mean, p_win, half_width, samples}}, "best", "leading", "seeds", "done",
    "reason" (None|"resolved"|"tied"|"budget"|"max_seeds"), "tree"}. `mean` is the mean value (= P(win) without caps).
    The last snapshot has done=True. The whole batch goes to one rollout call so a pool can split it.
    """
    acts = list(root_actions)
    if not acts:
        raise ValueError("no root actions")
    rng = rng or random.Random()
    tree = {a: Node() for a in acts}
    vals = {a: [] for a in acts}
    wins = dict.fromkeys(acts, 0)
    t0, size, last = clock(), 8, 0.0
    while True:
        sel, items = [], []
        for _ in range(size):
            r1, r2 = (tuple(rng.getrandbits(32) for _ in range(4)) for _ in range(2))  # CRN: shared by all root actions
            for a in acts:
                prefix, path = _select(tree[a], a, rng, c)
                it = {"rng1": r1, "rng2": r2, "prefix": prefix, "max_turns": max_turns}
                if policy is not None:
                    it["policy"] = policy
                items.append(it)
                sel.append((a, path))
        tb = clock()
        results = rollout(root, items)
        last = clock() - tb
        for (a, path), it, r in zip(sel, items, results):
            v = leaf_value(r)
            vals[a].append(v)
            wins[a] += r["outcome"] == "win"
            k = min(r["prefix_consumed"], len(it["prefix"]))  # illegal in this sample: back up consumed part only
            if k:
                path[k - 1].legal.update(dict.fromkeys(r["legal_at_prefix_end"]))
            for i, nd in enumerate(path):
                nd.vl -= 1
                if i < k:
                    nd.n += 1
                    nd.sum += v
        n = len(vals[acts[0]])
        snap = _snapshot(acts, vals, wins, tree, z, min_seeds, False, None)
        best_vals = vals[snap["best"]]
        if len(snap["leading"]) == 1:
            reason = "resolved"
        elif n >= min_seeds and all(vals[o] == best_vals for o in snap["leading"]):
            reason = "tied"  # leaders identical on every paired seed (e.g. all win 100%): sampling cannot separate them
        elif n >= max_seeds:
            reason = "max_seeds"
        elif clock() - t0 + 2 * last > budget_s:  # next (doubled) batch would overrun
            reason = "budget"
        else:
            yield snap
            size = min(2 * size, max_seeds - n)
            continue
        snap.update(done=True, reason=reason)
        yield snap
        return
