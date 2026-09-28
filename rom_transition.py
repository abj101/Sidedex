"""Planner-agnostic transition adapter (contract sections 1, 4, 6, 7).

Turns a decision state plus candidate actions into `SIMULATE_TURN` jobs, lets
`rom_pool.EnginePool.run_jobs` dispatch them across all workers, and merges the
completed work into a versioned transition result set with common-random-number
pairing.

Deliberate seams (package C / rom_eval integration fills them):

* `encode_turn(state, action, rng_pair) -> bytes` builds the mailbox payload; the
  adapter never encodes the wire layout itself.
* `decode_turn(job, raw, state, action, rng_pair) -> dict | None` turns one raw
  executor result into a contract section-4 `TransitionResult`; the default is
  the identity, so raw worker output passes through untouched.

No ranking, no heuristic names, no invented outcomes: a comparison sample is
paired only when every required action completed, and a missing action is
reported as missing rather than as a zero.
"""

from __future__ import annotations

import itertools
import json
import threading
from collections.abc import Mapping

import rom_bridge
from rom_pool import (
    MAX_FRAMES_PER_JOB,
    EnginePool,
    Job,
    LimitError,
    NodeRecord,
    RequestRun,
)

TRANSITION_SCHEMA = rom_bridge.TRANSITION_SCHEMA
STATE_SCHEMA = rom_bridge.STATE_SCHEMA
PLAYER_BOUNDARIES = ("player-choice", "forced-switch-player")


def action_key(action: dict) -> str:
    """Canonical, planner-agnostic identity for one candidate action.

    Move ids may be pinned numeric ids or source-id strings; both canonicalize to
    the same opaque key shape so the scheduler never interprets them.
    """
    kind = action.get("kind")
    if kind == "move":
        move = action.get("move_id")
        if isinstance(move, bool) or not isinstance(move, (int, str)) or not str(move):
            raise ValueError("invalid-id: move action needs a move_id")
        return f"move:{move}"
    if kind == "switch":
        slot = action.get("slot")
        if type(slot) is not int:
            raise ValueError("invalid-id: switch action needs an integer slot")
        return f"switch:{slot}"
    raise ValueError(f"invalid-id: unsupported action kind {kind!r}")


def seed_key(rng_pair) -> str:
    """Canonical identity of a common-random-number seed pair."""
    main, secondary = _seed_pair(rng_pair)
    return json.dumps({"main": main, "secondary": secondary}, separators=(",", ":"))


def _seed_pair(rng_pair):
    if isinstance(rng_pair, dict):
        main, secondary = rng_pair.get("main"), rng_pair.get("secondary")
    else:
        main, secondary = rng_pair
    for stream in (main, secondary):
        if (not isinstance(stream, (list, tuple)) or len(stream) != 4
                or any(type(value) is not int or not 0 <= value <= 0xFFFFFFFF
                       for value in stream)):
            raise ValueError("incomplete-state: each RNG stream must be four u32 values")
    return list(main), list(secondary)


def _field(state, key, default=None):
    """Read a decision-state field from a mapping or a token-style object."""
    if isinstance(state, Mapping):
        return state.get(key, default)
    return getattr(state, key, default)


def _token_of(state) -> dict:
    """The contract section-2 token, from `state["token"]` or a token object itself."""
    token = _field(state, "token")
    if token is None and not isinstance(state, Mapping) and _field(state, "handle"):
        return {"handle": state.handle, "sha256": getattr(state, "sha256", None),
                "owner_worker": getattr(state, "owner_worker", None),
                "portable": getattr(state, "portable", False),
                "bytes": getattr(state, "bytes", 0)}
    if isinstance(token, Mapping):
        return dict(token)
    if token is not None:
        return {"handle": getattr(token, "handle", None),
                "sha256": getattr(token, "sha256", None),
                "owner_worker": getattr(token, "owner_worker", None),
                "portable": getattr(token, "portable", False),
                "bytes": getattr(token, "bytes", 0)}
    return {}


def node_record(state, *, node_id: str | None = None) -> NodeRecord:
    """Build the pool routing record from a DecisionState or a query token.

    Accepts the contract section-2 mapping or (`rom_query.DecisionStateToken`)
    attribute shape; owner/portability routing data that a token does not carry
    must come from the caller via `TransitionAdapter.transitions(node=...)`.
    """
    token = _token_of(state)
    if not token:
        raise ValueError("incomplete-state: decision state has no token")
    identity = _field(state, "identity") or {}
    handle = token.get("handle")
    node = node_id or token.get("sha256") or handle
    if not isinstance(node, str) or not node:
        raise ValueError("incomplete-state: token needs a handle or sha256")
    return NodeRecord(
        node=node,
        handle=handle,
        owner=token.get("owner_worker"),
        portable=bool(token.get("portable")),
        bytes=int(token.get("bytes") or 0),
        sha256=token.get("sha256"),
        artifact_sha256=identity.get("test_elf_sha256"),
        worker_sha256=identity.get("worker_sha256"),
    )


def bridge_executor(*, mailbox_bytes: int, default_max_frames: int = MAX_FRAMES_PER_JOB,
                    request_id_prefix: str = "transition"):
    """Default executor over `RomWorkerClient.batch_turns` (wire layout injected)."""
    counter = itertools.count(1)
    lock = threading.Lock()

    def execute(client, mailbox, node, payloads, *, deadline, request_id,
                max_frames=None):
        with lock:
            wire_id = f"{request_id_prefix}-{request_id}-{next(counter)}"[:128]
        reply = client.batch_turns(
            mailbox, list(payloads), mailbox_bytes=mailbox_bytes,
            engine_elf_sha256=client.rom_sha256,
            deadline_ms=int(deadline * 1000), request_id=wire_id,
            max_frames=min(max_frames or default_max_frames, MAX_FRAMES_PER_JOB))
        return reply["turns"]

    return execute


class TransitionAdapter:
    """Dispatch `SIMULATE_TURN` jobs and merge paired transition results."""

    def __init__(self, pool: EnginePool, *, executor, encode_turn, decode_turn=None,
                 identity: dict | None = None, worker_id=None,
                 batch_size: int = 8, max_attempts: int = 2,
                 max_frames: int = MAX_FRAMES_PER_JOB):
        self.pool = pool
        self.executor = executor
        self.encode_turn = encode_turn
        self.decode_turn = decode_turn
        self.identity = dict(identity) if identity else None
        self.worker_id = worker_id
        self.batch_size = batch_size
        self.max_attempts = max_attempts
        self.max_frames = max_frames

    def transitions(self, *, request_id: str, state, actions, seeds, deadline,
                    cancelled=None, node_id: str | None = None,
                    node: NodeRecord | None = None) -> dict:
        """Return a versioned transition envelope for one comparison request.

        Pass `node` when the caller owns routing state (owner worker, portability,
        a cached portable blob); otherwise it is derived from the decision state.
        """
        try:
            node = node or node_record(state, node_id=node_id)
        except (ValueError, TypeError) as exc:
            return self._unavailable(request_id, state, str(exc).split(":")[0], str(exc))
        identity = _field(state, "identity")
        if self.identity is not None:
            mismatch = self._identity_mismatch(identity)
            if mismatch:
                return self._unavailable(request_id, state, "identity-mismatch", mismatch)
        schema = _field(state, "state_schema")
        if isinstance(state, Mapping) and schema != STATE_SCHEMA:
            return self._unavailable(
                request_id, state, "incomplete-state",
                f"state_schema must be {STATE_SCHEMA!r}")
        boundary = _field(state, "boundary")
        if boundary == "terminal":
            return self._unavailable(request_id, state, "terminal-state",
                                     "no transition may be requested from a terminal node")
        if boundary not in PLAYER_BOUNDARIES:
            return self._unavailable(
                request_id, state, "incomplete-state",
                f"unsupported boundary {boundary!r}")
        legal = _field(state, "legal_player_actions")
        if not legal:
            return self._unavailable(request_id, state, "no-legal-actions",
                                     "node has no mechanically legal actions")
        legal_keys = {action_key(item) for item in legal}
        action_items = list(actions)
        if not action_items:
            return self._unavailable(request_id, state, "no-legal-actions",
                                     "no candidate actions supplied")
        keys = []
        for action in action_items:
            try:
                key = action_key(action)
            except ValueError as exc:
                return self._unavailable(request_id, state, "invalid-id", str(exc))
            if key not in legal_keys:
                return self._unavailable(
                    request_id, state, "invalid-id",
                    f"action {key} is not in the node's legal_player_actions")
            keys.append(key)
        seed_items = [(seed_key(seed), _seed_pair(seed)) for seed in seeds]
        if not seed_items:
            return self._unavailable(request_id, state, "incomplete-state",
                                     "no comparison seeds supplied")

        jobs = []
        for skey, pair in seed_items:
            for action, key in zip(action_items, keys):
                payload = self.encode_turn(state, action, pair)
                if type(payload) is not bytes:
                    return self._unavailable(
                        request_id, state, "incomplete-state",
                        "encode_turn must return bytes")
                jobs.append(Job(node=node.node, action=key, seed=skey, payload=payload))
        try:
            run = self.pool.run_jobs(
                jobs, request_id=request_id, nodes=[node], executor=self.executor,
                deadline=deadline, cancelled=cancelled, batch_size=self.batch_size,
                max_attempts=self.max_attempts, worker_id=self.worker_id,
                max_frames=self.max_frames)
        except LimitError as exc:
            return self._unavailable(request_id, state, exc.code, exc.detail)
        return self._envelope(request_id, state, node, keys, dict(zip(keys, action_items)),
                              seed_items, run)

    # ------------------------------------------------------------ helpers
    def _identity_mismatch(self, identity) -> str | None:
        if not isinstance(identity, dict):
            return "decision state has no identity"
        for key, expected in self.identity.items():
            actual = identity.get(key)
            if actual != expected:
                return f"identity field {key!r}: {actual!r} != {expected!r}"
        return None

    def _decode(self, job, raw, state, action, pair):
        if self.decode_turn is None:
            return raw
        decoded = self.decode_turn(job, raw, state, action, pair)
        return decoded if decoded is not None else raw

    def _envelope(self, request_id, state, node, keys, action_by_key, seed_items,
                  run: RequestRun) -> dict:
        pairs = {skey: pair for skey, pair in seed_items}
        paired, partial = [], []
        for skey in sorted(pairs):
            pair = pairs[skey]
            completed, missing = {}, []
            for key in keys:
                record = run.results.get((node.node, key, skey))
                if record is None:
                    missing.append(key)
                    continue
                completed[key] = self._decode(record.job, record.result, state,
                                              action_by_key[key], pair)
            if not completed:
                continue  # never invent a zero outcome for a wholly missing sample
            sample = {"seed": skey, "rng": {"main": pair[0], "secondary": pair[1]},
                      "actions": {key: completed[key] for key in keys if key in completed}}
            if missing:
                sample["missing"] = missing
                partial.append(sample)
            else:
                paired.append(sample)
        token = _token_of(state)
        failures = {f"{key[0]}|{key[1]}|{key[2]}": code
                    for key, code in sorted(run.failures.items())}
        return {
            "transition_schema": TRANSITION_SCHEMA,
            "contract_version": 1,
            "status": ("ok" if paired and not partial
                       else "partial" if (paired or partial) else "unavailable"),
            "identity": _field(state, "identity"),
            "request_id": request_id,
            "state_schema": _field(state, "state_schema"),
            "boundary": _field(state, "boundary"),
            "input_token_sha256": token.get("sha256"),
            "node": node.node,
            "owner_worker": node.owner,
            "portable": node.portable,
            "actions": list(keys),
            "paired_samples": paired,
            "partial_samples": partial,
            "failures": failures,
            "progress": {
                "dispatched": len(keys) * len(seed_items),
                "observations": run.observations,
                "paired_samples": len(paired),
                "partial_samples": len(partial),
                "pending": len(run.pending),
                "duplicates_dropped": run.duplicates_dropped,
                "attempts": run.attempts,
                "batches": run.batches,
                "workers_used": list(run.workers_used),
                "transfer": dict(run.transfer),
            },
        }

    def _unavailable(self, request_id, state, code, detail) -> dict:
        state = state if isinstance(state, Mapping) else {}
        token = _token_of(state) if state else {}
        return {
            "transition_schema": TRANSITION_SCHEMA,
            "contract_version": 1,
            "status": "unavailable",
            "reason": code,
            "detail": detail,
            "identity": state.get("identity"),
            "request_id": request_id,
            "boundary": state.get("boundary"),
            "input_token_sha256": token.get("sha256"),
            "actions": [],
            "paired_samples": [],
            "partial_samples": [],
            "failures": {},
            "progress": {"dispatched": 0, "observations": 0, "paired_samples": 0,
                         "partial_samples": 0, "pending": 0, "duplicates_dropped": 0,
                         "attempts": 0, "batches": 0, "workers_used": [],
                         "transfer": {}},
        }
