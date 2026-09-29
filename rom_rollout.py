"""Reference `rollout(root, items)` for planner.py on the worker's `batch_turns_v3` op.

Item `{rng1, rng2, prefix, max_turns}` restores the root, injects rng1/rng2 on the first turn,
plays the prefix action words, then lets the ROM trainer AI (`candidateKind=2`, contract C.7)
play the player until a terminal or `max_turns`. Every later turn injects the previous
result's tag-6 quads (verified equal to an uninterrupted run). An action word is
`(0, move_id)` or `(1, slot)`, taken from the tag-7 legal rows; a word outside the current
legal set stops the prefix (`prefix_consumed`). Only the root is stored: children are dropped.
The native engine's fused `rollout` op must return exactly these results.
"""
import concurrent.futures
import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path

import rom_bridge
import rom_identity
from tools.rom_oracle import run_dynamic, sdr2

AI = {"kind": "ai"}
_WIN, _LOSS = 1, 2  # SDR2 terminal result (contract C.3); draws and the rest count as "cap"


def _token(text):
    return int.from_bytes(hashlib.sha256(text.encode()).digest()[:2], "little")


def _rng(header):
    return ([header[f"rng_main{i}"] for i in range(4)], [header[f"rng_sec{i}"] for i in range(4)])


def words(rows):
    """Tag-7 legal rows -> action words (moves keyed by id, switches by slot)."""
    return [(0, r["id"]) if r["kind"] == 0 else (1, r["target_or_slot"]) for r in rows if r["kind"] in (0, 1)]


def _action(word):
    return {"kind": "move", "move_id": word[1]} if word[0] == 0 else {"kind": "switch", "slot": word[1]}


def _leaf(records):
    tot = {0: [0, 0, 0, 0], 1: [0, 0, 0, 0]}  # per side: present, alive, hp, max_hp
    for m in records["party"]:
        if m["present"]:
            t = tot[m["side"]]
            t[0] += 1
            t[1] += m["hp"] > 0
            t[2] += m["hp"]
            t[3] += m["max_hp"]
    frac = lambda a, b: a / b if b else 0.0  # noqa: E731
    p, o = tot[0], tot[1]
    return {"p_alive": frac(p[1], p[0]), "o_alive": frac(o[1], o[0]),
            "p_hp": frac(p[2], p[3]), "o_hp": frac(o[2], o[3])}


class Runner:
    """One booted v9 worker: `turn` runs one mailbox with unique u16 request tokens."""

    def __init__(self, client, ann):
        self.client, self.ann, self.n, self.last, self.root_key = client, ann, 0, 0, None
        self.ready_blob = client.export_state(client.save_checkpoint()["handle"])["blob"]

    def install(self, blob, key):
        """Make `blob` the worker's default slot (what handle=None restores)."""
        if self.root_key != key:
            self.client.load_state_blob(blob)
            self.client.save_checkpoint()
            self.root_key = key

    def turn(self, request, handle, timeout=60.0):
        self.n += 1
        text = f"r{self.n}"
        while _token(text) in (0, self.last):
            text += "x"
        self.last = _token(text)
        request = {**request, "request_id": self.last}
        payload = run_dynamic.encode_v9_request(request, self.ann, compact=True)
        a = self.ann
        reply = self.client.batch_turns_v3(
            a["address"], a["result_address"], a["result_capacity"], [payload], mailbox_bytes=876,
            engine_elf_sha256=self.client.rom_sha256, handle=handle,
            critical_policy=run_dynamic.CRITICAL_POLICIES[request.get("critical_policy", "item-ability-only")],
            deadline_ms=int(time.monotonic() * 1000 + timeout * 1000), request_id=text)
        return reply["turns"][0]


def open_worker(elf, worker=rom_bridge.DEFAULT_WORKER, timeout=60.0):
    """Boot `elf` (identity = its own sha256) to the v9 ready announcement -> (client, Runner)."""
    sha = hashlib.sha256(Path(elf).read_bytes()).hexdigest().upper()
    client = rom_bridge.RomWorkerClient(
        worker_path=worker, rom_path=elf, expected_artifact_sha256=sha,
        expected_worker_sha256=rom_identity.CANDIDATE_WORKER_V3_SHA256, wire_protocol=3, timeout=timeout)
    try:
        for _ in range(400):
            ready = [x for x in run_dynamic._trace(client.run_frames(60)) if x.startswith("ROMTRACE|ready|")]
            if ready:
                client.save_state()
                return client, Runner(client, rom_bridge.parse_v9_ready(ready))
        raise TimeoutError("timeout: the ELF never announced a v9 mailbox")
    except BaseException:
        client.close()
        raise


@dataclass
class Root:
    """A decision point: the request template, its tag-7 words, and the state blob (portable)."""

    request: dict
    legal: list
    blob: str
    key: str


def make_root(runner, request):
    """Hold at the opening boundary of `request` (from the pristine ready state) -> Root."""
    runner.install(runner.ready_blob, "ready")
    client, turn = runner.client, None
    turn = runner.turn(request, None)
    child = turn["child"]["handle"]
    try:
        blob = client.export_state(child)["blob"]
    finally:
        client.drop_state(child)
    legal = words(turn["binary"]["records"]["legal"])
    if not legal:
        raise ValueError("no-legal-actions: the root has no player action")
    return Root(request, legal, blob, hashlib.sha256(blob.encode()).hexdigest())


def rollout_item(runner, root, item):
    """One rollout from `root` on `runner` (see the module docstring)."""
    runner.install(root.blob, root.key)
    prefix, consumed, legal, end = item["prefix"], 0, root.legal, None
    rng = (list(item["rng1"]), list(item["rng2"]))
    handle, prev, outcome, turns, leaf = None, None, "cap", 0, None
    try:
        while turns < item["max_turns"]:
            if end is None and (consumed == len(prefix) or prefix[consumed] not in legal):
                end = list(legal)
            action = _action(prefix[consumed]) if end is None else AI
            consumed += end is None
            req = {**root.request, "candidate_action": action, "rng_main": rng[0], "rng_secondary": rng[1]}
            turn = runner.turn(req, handle)
            turns += 1
            if prev:  # ponytail: one child at a time (worker keeps 3 slots incl. the default root)
                runner.client.drop_state(prev)
            binary = turn["binary"]
            prev = handle = (turn["child"] or {}).get("handle")
            rng, records = _rng(binary["header"]), binary["records"]
            leaf = _leaf(records)
            legal = words(records.get("legal", []))
            if binary["header"]["boundary"] == sdr2.BOUNDARY_TERMINAL:
                outcome = {_WIN: "win", _LOSS: "loss"}.get(records["terminal"][0]["result"], "cap")
                break
    finally:
        if prev:
            runner.client.drop_state(prev)
    return {"outcome": outcome, "turns": turns, "prefix_consumed": consumed,
            "legal_at_prefix_end": legal if end is None else end, "leaf": leaf}


class Rollout:
    """planner rollout over one runner: `Rollout(runner)(root, items)`."""

    def __init__(self, runner):
        self.runner = runner

    def __call__(self, root, items):
        return [rollout_item(self.runner, root, item) for item in items]


class PoolRollout:
    """The same contract spread over every worker of an `rom_pool.EnginePool` of (client, Runner)."""

    def __init__(self, pool, deadline_s=60.0):
        self.pool, self.deadline_s = pool, deadline_s

    def __call__(self, root, items):
        size = max(1, -(-len(items) // (self.pool.size * 4)))  # ~4 chunks per worker balances slow items
        chunks = [items[i:i + size] for i in range(0, len(items), size)]

        def run(chunk):
            with self.pool.session(time.monotonic() + self.deadline_s) as (_client, runner):
                return Rollout(runner)(root, chunk)

        with concurrent.futures.ThreadPoolExecutor(self.pool.size) as ex:
            return [r for part in ex.map(run, chunks) for r in part]
