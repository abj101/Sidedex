"""Engine bench: `SIDEDEX_ENGINE=native|mgba python bench.py` (defaults to the auto choice)."""
import json
import random
import statistics
import time
from pathlib import Path

import planner
import rom_rollout as rr

FIX = Path(__file__).parent / "tools/rom_oracle/fixtures"


def request(name):
    data = json.loads((FIX / name).read_text(encoding="utf-8"))
    return {**data["root"], "critical_policy": "normal", "operation": "simulate-turn", "mode": "normal"}


def main():
    pool = rr.engine_pool()
    out = {"engine": rr.engine(), "workers": pool.size}
    roots = {}
    for name in ("P1", "P29"):
        with pool.session(time.monotonic() + 60) as (_c, runner):
            roots[name] = rr.make_root(runner, request({"P1": "p01_physical_neutral.json", "P29": "p29_multiturn_branch.json"}[name]))
    root = roots["P1"]
    with pool.session(time.monotonic() + 60) as (_c, runner):  # one transition = restore root + one turn
        ms = []
        for _ in range(100):
            t = time.perf_counter()
            runner.install(root.blob, root.key, root.hold)
            runner.client.drop_state(runner.turn({**root.request, "candidate_action": rr._action(root.legal[0])}, None)["child"]["handle"])
            ms.append((time.perf_counter() - t) * 1000)
    ms.sort()
    out["transition_ms_p50"], out["transition_ms_p95"] = round(ms[50], 2), round(ms[95], 2)
    g = random.Random(1)
    items = [{"rng1": [g.getrandbits(32) | 1 for _ in range(4)], "rng2": [g.getrandbits(32) for _ in range(4)],
              "prefix": [], "max_turns": 60} for _ in range(512)]
    rr.PoolRollout(pool, time.monotonic() + 600)(root, items[:64])  # warm
    t = time.perf_counter()
    res = rr.PoolRollout(pool, time.monotonic() + 600)(root, items)
    dt = time.perf_counter() - t
    out["rollouts_per_s"], out["turns_per_s"] = round(len(res) / dt), round(sum(r["turns"] for r in res) / dt)
    for name, root in roots.items():
        t0, first = time.monotonic(), None
        for snap in planner.plan(rr.PoolRollout(pool, time.monotonic() + 60), root, root.legal, rng=random.Random(3), budget_s=3.0):
            first = first or time.monotonic() - t0
        out[name] = {"first_publish_s": round(first, 3), "done_s": round(time.monotonic() - t0, 3), "seeds": snap["seeds"], "reason": snap["reason"]}
    print(json.dumps(out))


if __name__ == "__main__":
    main()
