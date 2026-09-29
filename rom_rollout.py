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
import functools
import hashlib
import logging
import os
import shlex
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import rom_bridge
import rom_identity
import rom_native
import rom_pool
from tools.rom_oracle import run_dynamic, sdr2

AI = {"kind": "ai"}
log = logging.getLogger(__name__)
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
        self.client, self.ann, self.n, self.last, self.avoid, self.root_key = client, ann, 0, 0, 0, None
        self.native, self.root_handle = client.worker_version == rom_identity.NATIVE_WORKER_VERSION, None
        self.ready_blob = client.export_state(client.save_checkpoint()["handle"])["blob"]

    def install(self, blob, key, hold=0):
        """Make `blob` the worker's default slot (what handle=None restores). The ROM only releases a
        held boundary for a request id different from the hold's, so `hold` is never issued again."""
        self.avoid = hold
        if self.root_key != key:
            self.client.load_state_blob(blob)
            self.root_handle = self.client.save_checkpoint()["handle"]
            self.root_key = key

    def rollout(self, root, items, deadline_ms):
        """Native fused `rollout` op: the whole chunk in one call, results mapped like `rollout_item`."""
        self.install(root.blob, root.key, root.hold)
        wire = [{"rng1": list(i["rng1"]), "rng2": list(i["rng2"]), "max_turns": i["max_turns"],
                 "prefix": [list(w) for w in i["prefix"]]} for i in items]
        a = self.ann
        policy = run_dynamic.CRITICAL_POLICIES[root.request.get("critical_policy", "item-ability-only")]
        self.n += 1
        got = self.client.rollout(  # tokens base..base+max_turns never hit the root's hold
            a["address"], a["result_address"], a["result_capacity"], wire, mailbox_bytes=876,
            handle=self.root_handle, request_token=root.hold % 30000 + 2, engine_elf_sha256=self.client.rom_sha256,
            deadline_ms=deadline_ms, request_id=f"n{self.n}", critical_policy=policy)
        out = []
        size = (len(root.request["player_party"]), len(root.request["opponent_party"]))  # native counts alive mons
        for r in got:
            if r["outcome"] == "error":
                raise RuntimeError(f"engine-error: {r.get('error')}")
            out.append({"outcome": r["outcome"] if r["outcome"] in ("win", "loss") else "cap", "turns": r["turns"],
                        "prefix_consumed": r["prefix_consumed"], "leaf": r["leaf"] and {
                            **r["leaf"], "p_alive": r["leaf"]["p_alive"] / size[0], "o_alive": r["leaf"]["o_alive"] / size[1]},
                        "legal_at_prefix_end": [(0, x[1]) if x[0] == 0 else (1, x[3]) for x in r["legal_at_prefix_end"]
                                                if x[0] in (0, 1)]})
        return out

    def turn(self, request, handle, timeout=60.0):
        self.n += 1
        text = f"r{self.n}"
        while _token(text) in (0, self.last, self.avoid):
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


def _native_bin():
    return Path(os.environ.get("SIDEDEX_NATIVE_BIN", rom_native.NATIVE_BIN_WIN))


@functools.lru_cache(maxsize=None)
def _native_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest().upper()


@functools.lru_cache(maxsize=None)
def _auto(path):
    if not Path(path).is_file():
        reason = f"native engine {path} is not reachable"
    elif _native_sha(path) != rom_identity.NATIVE_ENGINE_SHA256:
        reason = f"native engine {path} does not match the pinned sha256"
    else:
        return "native"
    log.warning("falling back to the mGBA engine: %s", reason)
    return "mgba"


def engine():
    """SIDEDEX_ENGINE=native|mgba, else native when the pinned binary is reachable, else mgba (logged)."""
    choice = os.environ.get("SIDEDEX_ENGINE")
    if choice in ("mgba", "native"):
        return choice
    if choice:
        raise ValueError(f"SIDEDEX_ENGINE must be native or mgba, not {choice!r}")
    return _auto(str(_native_bin()))


def native_command():
    """Launch argv of the native engine (`SIDEDEX_NATIVE_CMD` overrides); None when the engine is mgba."""
    if engine() != "native":
        return None
    if os.environ.get("SIDEDEX_NATIVE_CMD"):
        return shlex.split(os.environ["SIDEDEX_NATIVE_CMD"])
    return ["wsl.exe", "-e", rom_native.NATIVE_BIN, "--rom", rom_native.NATIVE_BIN]


def open_worker(elf, worker=rom_bridge.DEFAULT_WORKER, timeout=60.0, command=None):
    """Boot `elf` (identity = its own sha256) to the v9 ready announcement -> (client, Runner)."""
    sha = _native_sha(str(elf))
    client = rom_bridge.RomWorkerClient(
        worker_path=worker, rom_path=elf, expected_artifact_sha256=sha,
        expected_worker_sha256=rom_identity.CANDIDATE_WORKER_V3_SHA256,
        wire_protocol=3, timeout=timeout,
        command=command)
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
    hold: int  # the request id the ROM holds this boundary on


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
    return Root(request, legal, blob, hashlib.sha256(blob.encode()).hexdigest(), runner.last)


def rollout_item(runner, root, item):
    """One rollout from `root` on `runner` (see the module docstring)."""
    runner.install(root.blob, root.key, root.hold)
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


_POOLS, _POOL_LOCK = {}, threading.Lock()


def engine_paths():
    """(elf, worker): SIDEDEX_V9_ELF, else the pinned ELF location (SIDEDEX_ENGINE_ELF_PATH)."""
    if engine() == "native":
        return _native_bin(), _native_bin()
    elf = Path(os.environ.get("SIDEDEX_V9_ELF") or os.environ.get(
        "SIDEDEX_ENGINE_ELF_PATH", rom_bridge.DEFAULT_WORKER.with_name("pokeemerald-test-headless.elf")))
    worker = Path(os.environ.get("SIDEDEX_ROM_WORKER_EXE", rom_bridge.DEFAULT_WORKER))
    for path in (elf, worker):
        if not path.is_file():
            raise FileNotFoundError(f"missing-artifact: {path} is missing (SIDEDEX_V9_ELF / SIDEDEX_ROM_WORKER_EXE)")
    return elf.resolve(), worker.resolve()


def engine_pool():
    """The shared rollout pool: SIDEDEX_WORKERS engines (native: cores - 2, mgba: 3)."""
    elf, worker = engine_paths()
    command = native_command()
    size = int(os.environ.get("SIDEDEX_WORKERS") or (max(1, (os.cpu_count() or 4) - 2) if command else 3))
    key = (str(elf), str(worker), tuple(command or ()), size)
    with _POOL_LOCK:
        if key not in _POOLS:
            _POOLS[key] = rom_pool.EnginePool(lambda d: open_worker(elf, worker, command=command), size=size)
            _POOLS[key].prewarm()
        return _POOLS[key]


class Rollout:
    """planner rollout over one runner: `Rollout(runner)(root, items)`."""

    def __init__(self, runner):
        self.runner = runner

    def __call__(self, root, items, deadline=None):
        if getattr(self.runner, "native", False):
            return self.runner.rollout(root, items, int((deadline or time.monotonic() + 60) * 1000))
        return [rollout_item(self.runner, root, item) for item in items]


class PoolRollout:
    """The same contract spread over every worker of an `rom_pool.EnginePool` of (client, Runner)."""

    def __init__(self, pool, deadline):
        self.pool, self.deadline = pool, deadline  # absolute time.monotonic()

    def __call__(self, root, items):
        size = max(1, -(-len(items) // (self.pool.size * (4 - 3 * (engine() == "native")))))  # native: 1 call/worker
        chunks = [items[i:i + size] for i in range(0, len(items), size)]

        def run(chunk):
            if time.monotonic() >= self.deadline:
                raise TimeoutError("timeout: rollout deadline expired")
            with self.pool.session(self.deadline) as (_client, runner):
                return Rollout(runner)(root, chunk, self.deadline)

        with concurrent.futures.ThreadPoolExecutor(self.pool.size) as ex:
            return [r for part in ex.map(run, chunks) for r in part]
