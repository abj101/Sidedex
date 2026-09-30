"""Small pool of booted ROM workers with their in-memory ready checkpoints.

This module owns two things:

* `EnginePool.session(deadline)` -- the legacy single-worker checkout used by
  `rom_eval.py` (unchanged behavior).
* `EnginePool.run_jobs(...)` -- the planner-agnostic job scheduler required by
  ROM_TRANSITION_CONTRACT.md sections 7-9: jobs keyed by
  `(request_id, node_token_id, action, paired_seed)`, dispatched to *all*
  available workers in bounded local batches, merged in a deterministic order,
  returning completed partial work on deadline/cancellation, with node
  ownership routing (portable blobs vs. owner-local restore) and bounded
  retries that never double count an observation.

The scheduler knows nothing about battle mechanics, mailbox layout or ranking.
A caller supplies `executor(client, mailbox, node, payloads, *, deadline,
request_id) -> list[result]`; the pool routes, installs the node, batches and
merges.
"""

import atexit
import collections
import contextlib
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

import rom_bridge


# Contract section 8 bounds.
MAX_QUEUED_JOBS = 64
MAX_NODES_PER_WORKER = 3
MAX_NODE_BYTES = 64 * 1024 * 1024
MAX_FRAMES_PER_JOB = 24_000
MAX_OUTPUT_BYTES = 1_048_576
DEFAULT_BATCH_SIZE = 8
DEFAULT_MAX_ATTEMPTS = 2

# Contract section 9 canonical failure names.
CANONICAL_FAILURES = frozenset({
    "unsupported-mechanic", "unrepresentable-state", "incomplete-state",
    "stale-handle", "identity-mismatch", "instruction-budget-exceeded",
    "deadline-exceeded", "worker-failed", "no-legal-actions", "terminal-state",
    "artifact-mismatch", "source-mismatch", "invalid-id", "timeout",
})
# Only these may be retried (idempotent jobs after a worker failure).
RETRYABLE_FAILURES = frozenset({"worker-failed", "timeout"})


class LimitError(ValueError):
    """A contract section-8 bound was exceeded; fails closed, never truncated."""

    def __init__(self, detail: str, *, code: str = "worker-failed"):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


class CanonicalError(RuntimeError):
    """A canonical section-9 failure raised while routing or installing a node."""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def canonical_failure(exc: BaseException) -> str:
    """Map an exception to a contract section-9 failure name."""
    code = getattr(exc, "code", None)
    if isinstance(code, str):
        if code in CANONICAL_FAILURES:
            return code
        if code == "deadline_exceeded":  # rom_bridge historical spelling
            return "deadline-exceeded"
    if isinstance(exc, rom_bridge.WorkerTimeoutError) or isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, (rom_bridge.RomBridgeError, OSError)):
        return "worker-failed"
    return "worker-failed"


@dataclass(frozen=True)
class Job:
    """One idempotent unit of work, planner-agnostic.

    `node`, `action` and `seed` are opaque caller-chosen identities; together
    with the request id they form the contract job key. `payload` is the bytes
    the injected executor hands to the worker.
    """

    node: str
    action: str
    seed: str
    payload: bytes
    idempotent: bool = True
    max_frames: int | None = None

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.node, self.action, self.seed)


@dataclass
class NodeRecord:
    """Ownership/portability record for a decision-state node.

    `handle` must name a *named* worker slot (the scheduler's install step
    writes the unnamed "default" slot that `batch_turns` restores from, so an
    unnamed node handle would be clobbered). `artifact_sha256` and
    `worker_sha256` are checked against the worker before any install.
    """

    node: str
    handle: str | None = None
    owner: str | None = None
    portable: bool = False
    blob: str | None = None
    bytes: int = 0
    sha256: str | None = None
    artifact_sha256: str | None = None
    worker_sha256: str | None = None
    blob_bytes: int = 0


@dataclass
class ResultRecord:
    job: Job
    result: Any
    attempt: int


@dataclass
class RequestRun:
    """Merged outcome of one `run_jobs` call (deterministic, partial-safe)."""

    request_id: str
    results: dict[tuple[str, str, str], ResultRecord] = field(default_factory=dict)
    failures: dict[tuple[str, str, str], str] = field(default_factory=dict)
    pending: list[Job] = field(default_factory=list)
    duplicates_dropped: int = 0
    attempts: int = 0
    batches: int = 0
    workers_used: tuple[str, ...] = ()
    transfer: dict[str, float] = field(default_factory=dict)
    limits: dict[str, Any] = field(default_factory=dict)

    @property
    def observations(self) -> int:
        return len(self.results)

    @property
    def keys(self) -> set[tuple[str, str, str]]:
        return set(self.results)

    def ordered_results(self) -> list[ResultRecord]:
        """Deterministic merge order: ascending contract job key."""
        return [self.results[key] for key in sorted(self.results)]


def _validate_jobs(jobs: Sequence[Job], max_frames: int) -> list[Job]:
    if type(max_frames) is not int or not 1 <= max_frames <= MAX_FRAMES_PER_JOB:
        raise LimitError(f"max_frames must be 1..{MAX_FRAMES_PER_JOB}, got {max_frames!r}",
                         code="worker-failed")
    validated = []
    for job in jobs:
        if not isinstance(job, Job):
            raise TypeError("jobs must be rom_pool.Job instances")
        for value in (job.node, job.action, job.seed):
            if not isinstance(value, str) or not value:
                raise ValueError("job node/action/seed must be non-empty strings")
        if type(job.payload) is not bytes:
            raise ValueError("job payload must be bytes")
        if len(job.payload) > MAX_OUTPUT_BYTES:
            raise LimitError(f"job payload for {job.key} exceeds {MAX_OUTPUT_BYTES} bytes")
        if job.max_frames is not None and (type(job.max_frames) is not int
                                           or not 1 <= job.max_frames <= MAX_FRAMES_PER_JOB):
            raise LimitError(f"job max_frames must be 1..{MAX_FRAMES_PER_JOB}")
        validated.append(job)
    return validated


def _validate_nodes(nodes: Sequence[NodeRecord]) -> dict[str, NodeRecord]:
    node_map: dict[str, NodeRecord] = {}
    per_owner: dict[str, int] = {}
    for node in nodes:
        if not isinstance(node, NodeRecord):
            raise TypeError("nodes must be rom_pool.NodeRecord instances")
        if node.node in node_map:
            raise LimitError(f"duplicate node id {node.node!r}")
        if type(node.bytes) is not int or node.bytes < 0:
            raise LimitError(f"node {node.node!r} has an invalid byte size")
        if node.bytes > MAX_NODE_BYTES:
            raise LimitError(
                f"node {node.node!r} is {node.bytes} bytes, over the {MAX_NODE_BYTES}-byte "
                "per-node bound")
        if node.owner is not None:
            per_owner[node.owner] = per_owner.get(node.owner, 0) + 1
            if per_owner[node.owner] > MAX_NODES_PER_WORKER:
                raise LimitError(
                    f"worker {node.owner!r} would own {per_owner[node.owner]} nodes, over the "
                    f"{MAX_NODES_PER_WORKER}-node per-worker bound")
        node_map[node.node] = node
    return node_map


class EnginePool:
    def __init__(self, factory, size=3):
        self.factory = factory
        self.size = size
        self._ready = []
        self._created = 0
        self._condition = threading.Condition()
        self._closed = False
        atexit.register(self.close)

    # ------------------------------------------------------------- checkout
    @contextlib.contextmanager
    def session(self, deadline):
        worker = self._acquire(deadline)
        healthy = True
        try:
            yield worker
        except BaseException:
            healthy = False
            raise
        finally:
            self._release(worker, healthy)

    def _acquire(self, deadline):
        with self._condition:
            while not self._ready and self._created >= self.size and not self._closed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("timeout: no ROM worker became available")
                self._condition.wait(remaining)
            if self._closed:
                raise RuntimeError("ROM worker pool is closed")
            if self._ready:
                worker = self._ready.pop()
            else:
                self._created += 1
                worker = None
        if worker is None:
            try:
                worker = self.factory(deadline)
            except BaseException:
                with self._condition:
                    self._created -= 1
                    self._condition.notify()
                raise
        return worker

    def _release(self, worker, healthy):
        discard = False
        with self._condition:
            if healthy and not self._closed and worker[0].is_running:
                self._ready.append(worker)
            else:
                self._created -= 1
                discard = True
            self._condition.notify()
        if discard:
            worker[0].close()
            self.prewarm()

    # ---------------------------------------------------------------- jobs
    def run_jobs(
        self,
        jobs: Sequence[Job],
        *,
        request_id: str,
        nodes: Sequence[NodeRecord],
        executor: Callable[..., list],
        deadline: float,
        cancelled: Callable[[], bool] | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        worker_id: Callable[[Any, Any], str] | None = None,
        max_frames: int = MAX_FRAMES_PER_JOB,
    ) -> RequestRun:
        """Dispatch `jobs` across every available worker and merge the results.

        Merge rule: results are returned in ascending contract job key
        `(node_token_id, action, paired_seed)`, independent of completion order.
        Jobs whose node cannot be routed fail closed with `stale-handle`.
        """
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("request_id must be a non-empty string")
        if self.size <= 0:
            raise ValueError("pool size must be positive")
        if type(batch_size) is not int or not 1 <= batch_size <= rom_bridge.MAX_BATCH_TURNS:
            raise LimitError(f"batch_size must be 1..{rom_bridge.MAX_BATCH_TURNS}")
        if type(max_attempts) is not int or max_attempts < 1:
            raise LimitError("max_attempts must be a positive integer")
        job_list = _validate_jobs(list(jobs), max_frames)
        if len(job_list) > MAX_QUEUED_JOBS:
            raise LimitError(
                f"request {request_id!r} queued {len(job_list)} jobs, over the "
                f"{MAX_QUEUED_JOBS}-job per-pool bound")
        node_map = _validate_nodes(list(nodes))

        run = RequestRun(request_id=request_id, limits={
            "queued_jobs": len(job_list), "max_queued_jobs": MAX_QUEUED_JOBS,
            "max_nodes_per_worker": MAX_NODES_PER_WORKER, "max_node_bytes": MAX_NODE_BYTES,
            "max_frames_per_job": max_frames, "max_output_bytes": MAX_OUTPUT_BYTES,
            "batch_size": batch_size, "max_attempts": max_attempts,
        })
        run.transfer.update({"exports": 0, "transfers": 0, "export_ms": 0.0,
                             "import_ms": 0.0, "local_restores": 0, "local_restore_ms": 0.0})

        # Deduplicate by the contract job key; a repeated key is one observation.
        deduped: list[Job] = []
        seen: set[tuple[str, str, str]] = set()
        for job in job_list:
            if job.key in seen:
                run.duplicates_dropped += 1
                continue
            seen.add(job.key)
            deduped.append(job)

        slot_id = self._worker_id(worker_id)
        live_ids = self._survey_workers(node_map, deadline, run, slot_id)

        pending: collections.deque = collections.deque()
        for job in deduped:
            node = node_map.get(job.node)
            if node is None:
                run.failures[job.key] = "stale-handle"
                continue
            if not node.portable:
                if not node.owner or node.owner not in live_ids:
                    run.failures[job.key] = "stale-handle"
                    continue
            elif node.blob is None and not (node.owner in live_ids and node.handle):
                run.failures[job.key] = "stale-handle"
                continue
            pending.append(job)

        if not pending:
            return run

        if cancelled is not None and cancelled():
            self._finish_pending(run, pending)
            return run

        install_cache: dict[int, str] = {}
        attempt_by_key: dict[tuple[str, str, str], int] = {}
        lock = threading.Lock()
        work = threading.Condition(lock)
        stop = False
        active = 0

        def take_batch(sid: str, budget: int) -> list[Job]:
            with lock:
                chosen = None
                for job in pending:
                    node = node_map[job.node]
                    if node.portable or node.owner == sid:
                        chosen = job.node
                        break
                if chosen is None:
                    return []
                batch = []
                for job in list(pending):
                    if len(batch) >= budget:
                        break
                    node = node_map[job.node]
                    if job.node == chosen and (node.portable or node.owner == sid):
                        batch.append(job)
                        pending.remove(job)
                return batch

        def rescue_unroutable() -> bool:
            """Fail closed on pending jobs whose non-portable owner is not live."""
            with lock:
                if len(_seen_ids) < self.size:
                    return False
                rescued = False
                for job in list(pending):
                    node = node_map.get(job.node)
                    if node is None or (not node.portable and node.owner not in _seen_ids):
                        run.failures[job.key] = "stale-handle"
                        pending.remove(job)
                        rescued = True
                return rescued

        _seen_ids = set(live_ids)

        def loop():
            nonlocal stop, active
            while True:
                with lock:
                    if stop or (not pending and active == 0):
                        return
                    if cancelled is not None and cancelled():
                        stop = True
                        return
                if time.monotonic() >= deadline:
                    with lock:
                        stop = True
                    return
                try:
                    slot = self._acquire(deadline)
                except (TimeoutError, RuntimeError, OSError, rom_bridge.RomBridgeError):
                    with lock:
                        stop = True
                    return
                sid = slot_id(slot[0], slot[1])
                with lock:
                    _seen_ids.add(sid)
                healthy = True
                batch: list[Job] = []
                attempts = 0
                try:
                    batch = take_batch(sid, batch_size)
                    if not batch:
                        if rescue_unroutable():
                            continue
                        with lock:
                            if stop or (not pending and active == 0):
                                return
                        with work:
                            work.wait(0.01)
                        continue
                    node = node_map[batch[0].node]
                    with lock:
                        for job in batch:
                            attempt_by_key[job.key] = attempt_by_key.get(job.key, 0) + 1
                        attempts = attempt_by_key[batch[0].key]
                        run.attempts += len(batch)
                        run.batches += 1
                        if attempts == 1:
                            active += 1
                    self._install_node(slot[0], node, install_cache, run.transfer)
                    payloads = [job.payload for job in batch]
                    effective_frames = max(job.max_frames or max_frames for job in batch)
                    results = executor(slot[0], slot[1], node, payloads,
                                       deadline=deadline, request_id=request_id,
                                       max_frames=effective_frames)
                    if not isinstance(results, list) or len(results) != len(batch):
                        raise rom_bridge.WorkerProtocolError(
                            "executor returned a mismatched result count")
                    with lock:
                        run.workers_used = tuple(sorted(set(run.workers_used) | {sid}))
                        for job, result in zip(batch, results):
                            if _size_bytes(result) > MAX_OUTPUT_BYTES:
                                run.failures[job.key] = "worker-failed"
                                continue
                            if job.key in run.results:
                                run.duplicates_dropped += 1
                                continue
                            run.results[job.key] = ResultRecord(
                                job, result, attempt_by_key.get(job.key, 1))
                except BaseException as exc:  # noqa: BLE001 - mapped to a canonical name
                    healthy = False
                    code = canonical_failure(exc)
                    with lock:
                        if code == "deadline-exceeded":
                            stop = True
                        for job in batch:
                            if job.key in run.results or job.key in run.failures:
                                continue
                            if (job.idempotent and code in RETRYABLE_FAILURES
                                    and attempt_by_key.get(job.key, 1) < max_attempts):
                                pending.appendleft(job)
                                work.notify_all()
                            else:
                                run.failures[job.key] = code
                finally:
                    self._release(slot, healthy)
                    with lock:
                        if attempts == 1:
                            active -= 1
                        work.notify_all()

        threads = [threading.Thread(target=loop, name=f"rom-job-{index}", daemon=True)
                   for index in range(self.size)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self._finish_pending(run, pending)
        return run

    # ------------------------------------------------------------ helpers
    def _finish_pending(self, run: RequestRun, pending) -> None:
        """Return completed work; unfinished jobs fail closed as deadline-exceeded."""
        run.pending = [job for job in list(pending) if job.key not in run.results]
        for job in run.pending:
            run.failures.setdefault(job.key, "deadline-exceeded")
        pending.clear()

    def _worker_id(self, worker_id):
        if worker_id is not None:
            return worker_id
        def default(client, mailbox):
            return f"w{id(client):x}"
        return default

    def _survey_workers(self, node_map, deadline, run, slot_id):
        """Hold every available worker at once; record ids and export node blobs.

        Portable nodes without a cached blob need one export from their owner
        before any worker can import them, so all `size` slots are held while
        the survey runs (a released slot would be handed straight back out).
        """
        live_ids: set[str] = set()
        needed_owners = {node.owner for node in node_map.values()
                         if node.portable and node.blob is None and node.handle and node.owner}
        held = []
        healthy: dict[int, bool] = {}
        try:
            for _ in range(self.size):
                try:
                    held.append(self._acquire(deadline))
                except (TimeoutError, RuntimeError, OSError, rom_bridge.RomBridgeError):
                    break
            for slot in held:
                wid = slot_id(slot[0], slot[1])
                live_ids.add(wid)
                healthy[id(slot[0])] = True
                if wid not in needed_owners:
                    continue
                for node in node_map.values():
                    if node.owner != wid or node.blob is not None or not node.handle:
                        continue
                    try:
                        started = time.monotonic()
                        exported = slot[0].export_state(node.handle)
                        run.transfer["export_ms"] += (time.monotonic() - started) * 1000
                        run.transfer["exports"] += 1
                        node.blob = exported["blob"]
                        node.blob_bytes = int(exported.get("blob_bytes") or 0)
                        node.sha256 = node.sha256 or exported.get("sha256")
                    except rom_bridge.WorkerResponseError as exc:
                        if exc.code != "stale-handle":
                            raise
                        # The blob stays unset; routing turns this into stale-handle.
                    except (OSError, rom_bridge.RomBridgeError):
                        healthy[id(slot[0])] = False
                needed_owners.discard(wid)
        finally:
            for slot in held:
                self._release(slot, healthy.get(id(slot[0]), True))
        return live_ids

    def _install_node(self, client, node: NodeRecord, cache: dict[int, str], transfer: dict):
        """Make `node` the worker's unnamed default slot (what batches restore).

        Portable nodes transfer via `export_state`/`load_state_blob`; owner
        nodes restore from their named slot. Identity mismatches and missing
        handles fail closed.
        """
        key = id(client)
        if cache.get(key) == node.node:
            return
        artifact = getattr(client, "rom_sha256", None)
        worker = getattr(client, "worker_sha256", None)
        if node.artifact_sha256 and artifact and str(artifact).upper() != node.artifact_sha256.upper():
            raise CanonicalError(
                "identity-mismatch",
                f"node {node.node!r} artifact {node.artifact_sha256} != worker {artifact}")
        if node.worker_sha256 and worker and str(worker).upper() != node.worker_sha256.upper():
            raise CanonicalError(
                "identity-mismatch",
                f"node {node.node!r} worker {node.worker_sha256} != worker {worker}")
        if node.portable:
            if node.blob is None:
                raise CanonicalError(
                    "stale-handle",
                    f"portable node {node.node!r} has no exported blob")
            started = time.monotonic()
            if hasattr(client, "load_state_blob"):
                client.load_state_blob(node.blob)
            else:  # fallback for clients without the blob-restore op
                handle = client.import_state(node.blob)["handle"]
                client.restore_state(handle)
            transfer["import_ms"] += (time.monotonic() - started) * 1000
            transfer["transfers"] += 1
        else:
            if not node.handle:
                raise CanonicalError("stale-handle", f"node {node.node!r} has no handle")
            started = time.monotonic()
            client.restore_state(node.handle)
            transfer["local_restore_ms"] += (time.monotonic() - started) * 1000
            transfer["local_restores"] += 1
        client.save_checkpoint()
        cache[key] = node.node

    # ------------------------------------------------------------ lifecycle
    def prewarm(self):
        """Start idle workers in the background; a failed start remains retryable."""
        def start():
            for _ in range(self.size):
                with self._condition:
                    if self._closed or self._created >= self.size:
                        return
                    self._created += 1
                try:
                    worker = self.factory(time.monotonic() + 30)
                except Exception:
                    with self._condition:
                        self._created -= 1
                        self._condition.notify()
                    return
                with self._condition:
                    if self._closed:
                        self._created -= 1
                        discard = True
                    else:
                        self._ready.append(worker)
                        discard = False
                    self._condition.notify()
                if discard:
                    worker[0].close()
        threading.Thread(target=start, name="rom-prewarm", daemon=True).start()

    def close(self):
        with self._condition:
            self._closed = True
            idle, self._ready = self._ready, []
            self._created -= len(idle)
            self._condition.notify_all()
        for client, _mailbox in idle:
            client.close()


def _size_bytes(value: Any) -> int:
    try:
        return len(json.dumps(value, separators=(",", ":"), default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return 0
