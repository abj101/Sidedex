import threading
import time
import unittest

import rom_bridge
from rom_pool import (
    MAX_FRAMES_PER_JOB,
    EnginePool,
    Job,
    LimitError,
    NodeRecord,
)


class FakeWorker:
    """Minimal bridge-shaped worker with named slots and a restartable lifecycle."""

    def __init__(self, worker_id, *, rom_sha256="AB" * 32, worker_sha256="CD" * 32,
                 slots=None, blob_ms=0.0, restore_ms=0.0):
        self.worker_id = worker_id
        self.is_running = True
        self.rom_sha256 = rom_sha256
        self.worker_sha256 = worker_sha256
        self._slots = dict(slots or {})
        self.blob_ms = blob_ms
        self.restore_ms = restore_ms
        self.imports = 0
        self.loads = 0
        self.restores = 0
        self.saves = 0
        self.exports = 0

    def save_checkpoint(self, name=None):
        self.saves += 1
        handle = "default" if name is None else "%032x" % (len(self._slots) + 1)
        self._slots[handle] = (name or "default", b"state")
        return {"handle": handle, "name": name or "default", "bytes": 528,
                "sha256": "EE" * 32, "identity": {}}

    def restore_state(self, handle):
        if handle not in self._slots:
            raise rom_bridge.WorkerResponseError("stale-handle")
        time.sleep(self.restore_ms)
        self.restores += 1
        return {"frame_count": 1}

    def export_state(self, handle):
        if handle not in self._slots:
            raise rom_bridge.WorkerResponseError("stale-handle")
        time.sleep(self.blob_ms)
        self.exports += 1
        return {"handle": handle, "bytes": 528, "sha256": "EE" * 32,
                "blob": "QUJD", "blob_bytes": 560, "base64_bytes": 4}

    def load_state_blob(self, blob):
        time.sleep(self.blob_ms)
        self.loads += 1
        return {"frame_count": 1}

    def import_state(self, blob, name=None):
        self.imports += 1
        return {"handle": "imported", "name": name or "imported", "bytes": 528,
                "sha256": "EE" * 32, "identity": {}}

    def drop_state(self, handle):
        self._slots.pop(handle, None)
        return {"dropped": True}

    def close(self):
        self.is_running = False


def worker_id(client, _mailbox):
    return client.worker_id


def make_factory(created, *, slots=None, blob_ms=0.0, restore_ms=0.0):
    """Factory yielding w1, w2, ... (every start is a fresh worker)."""
    counter = [0]

    def factory(_deadline):
        counter[0] += 1
        worker = FakeWorker(f"w{counter[0]}", slots=dict(slots or {}),
                            blob_ms=blob_ms, restore_ms=restore_ms)
        created.append(worker)
        return (worker, 0x02000000)

    return factory


def make_executor(record=None, *, delay=0.0, barrier=None, fail_workers=(),
                  fail_times=1, raises=None):
    state = {"calls": 0, "failures": {}}

    def execute(client, mailbox, node, payloads, *, deadline, request_id, max_frames):
        state["calls"] += 1
        if record is not None:
            record.append({"worker": client.worker_id, "node": node.node,
                           "payloads": [payload.decode() for payload in payloads],
                           "request_id": request_id, "max_frames": max_frames})
        if barrier is not None and state["calls"] <= 3:
            try:
                barrier.wait(timeout=10)
            except threading.BrokenBarrierError:
                pass
        if raises is not None:
            code = raises(client)
            if code:
                raise rom_bridge.WorkerResponseError(code)
        if (client.worker_id in fail_workers
                and state["failures"].get(client.worker_id, 0) < fail_times):
            state["failures"][client.worker_id] = state["failures"].get(client.worker_id, 0) + 1
            client.is_running = False
            raise rom_bridge.WorkerExitedError("simulated worker exit")
        if delay:
            time.sleep(delay)
        return [{"complete": True, "worker": client.worker_id, "node": node.node,
                 "payload": payload.decode(), "stop": "boundary"} for payload in payloads]

    return execute


def portable_node(node="n1", owner="w1", handle="root", blob="QUJD"):
    return NodeRecord(node=node, handle=handle, owner=owner, portable=True,
                      blob=blob, bytes=528, sha256="EE" * 32)


def jobs_for(node, count, *, prefix="b"):
    return [Job(node=node, action=f"move:{index}", seed="seed-1",
                payload=f"{prefix}-{index}".encode()) for index in range(count)]


class PoolTest(unittest.TestCase):
    def test_prewarm_and_reuse_three_checkpoints(self):
        built = []

        def factory(_deadline):
            item = (FakeWorker(f"w{len(built) + 1}"), 0x02000000 + len(built))
            built.append(item)
            return item

        pool = EnginePool(factory, size=3)
        try:
            pool.prewarm()
            deadline = time.monotonic() + 2
            while len(built) < 3 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(len(built), 3)
            with pool.session(deadline) as first:
                with pool.session(deadline) as second:
                    with pool.session(deadline) as third:
                        self.assertEqual(len({id(first[0]), id(second[0]), id(third[0])}), 3)
            with pool.session(deadline) as reused:
                self.assertIn(reused, built)
            self.assertEqual(len(built), 3)
        finally:
            pool.close()
        self.assertTrue(all(not worker.is_running for worker, _ in built))

    def test_one_request_uses_all_three_workers(self):
        created = []
        pool = EnginePool(make_factory(created, slots={"root": ("root", b"state")}), size=3)
        try:
            barrier = threading.Barrier(3)
            run = pool.run_jobs(
                jobs_for("n1", 9), request_id="req-all", nodes=[portable_node()],
                executor=make_executor(barrier=barrier), deadline=time.monotonic() + 20,
                batch_size=1, worker_id=worker_id)
            self.assertEqual(run.observations, 9)
            self.assertEqual(sorted(run.workers_used), ["w1", "w2", "w3"])
            self.assertEqual(run.failures, {})
            self.assertEqual(run.duplicates_dropped, 0)
        finally:
            pool.close()

    def test_merge_order_is_deterministic(self):
        created = []
        pool = EnginePool(make_factory(created, slots={"root": ("root", b"state")}), size=3)
        record = []
        try:
            ordered_submission = [jobs_for("n1", 6)[index] for index in (3, 0, 5, 1, 4, 2)]
            run = pool.run_jobs(
                ordered_submission, request_id="req-order", nodes=[portable_node()],
                executor=make_executor(record, delay=0.01), deadline=time.monotonic() + 20,
                batch_size=1, worker_id=worker_id)
            seen = [item["payloads"][0] for item in record]
            self.assertNotEqual(seen, sorted(seen))  # not a pre-sorted stream
            ordered = [item.job.key for item in run.ordered_results()]
            self.assertEqual(ordered, sorted(run.keys))
            second = pool.run_jobs(
                list(reversed(ordered_submission)), request_id="req-order-2",
                nodes=[portable_node()], executor=make_executor(delay=0.01),
                deadline=time.monotonic() + 20, batch_size=1, worker_id=worker_id)
            self.assertEqual([item.job.key for item in second.ordered_results()], ordered)
        finally:
            pool.close()

    def test_worker_exit_survives_without_duplicate_or_lost_observations(self):
        created = []
        pool = EnginePool(make_factory(created, slots={"root": ("root", b"state")}), size=3)
        try:
            run = pool.run_jobs(
                jobs_for("n1", 6), request_id="req-exit", nodes=[portable_node()],
                executor=make_executor(delay=0.01, fail_workers=("w1",), fail_times=1),
                deadline=time.monotonic() + 20, batch_size=1, max_attempts=2,
                worker_id=worker_id)
            self.assertEqual(run.observations, 6)
            self.assertEqual(set(run.keys), {job.key for job in jobs_for("n1", 6)})
            self.assertEqual(run.failures, {})
            self.assertEqual(run.duplicates_dropped, 0)
            self.assertEqual(run.attempts, 7)  # six jobs, one retried exactly once
            self.assertTrue(any(not worker.is_running for worker in created
                                if worker.worker_id == "w1"))
            self.assertEqual(len(created), 4)  # the dead worker was replaced
        finally:
            pool.close()

    def test_deadline_returns_completed_partial_work(self):
        created = []
        pool = EnginePool(make_factory(created, slots={"root": ("root", b"state")}), size=1)
        try:
            run = pool.run_jobs(
                jobs_for("n1", 6), request_id="req-deadline", nodes=[portable_node()],
                executor=make_executor(delay=0.2), deadline=time.monotonic() + 0.35,
                batch_size=1, worker_id=worker_id)
            self.assertGreater(run.observations, 0)
            self.assertLess(run.observations, 6)
            self.assertTrue(run.pending)
            for job in run.pending:
                self.assertNotIn(job.key, run.results)  # completed work only, no zeros
                self.assertEqual(run.failures[job.key], "deadline-exceeded")
            self.assertEqual(run.observations + len(run.pending), 6)
        finally:
            pool.close()

    def test_duplicate_submission_is_one_observation(self):
        created = []
        pool = EnginePool(make_factory(created, slots={"root": ("root", b"state")}), size=1)
        try:
            job = jobs_for("n1", 1)[0]
            run = pool.run_jobs(
                [job, job], request_id="req-dup", nodes=[portable_node()],
                executor=make_executor(), deadline=time.monotonic() + 10,
                batch_size=1, worker_id=worker_id)
            self.assertEqual(run.observations, 1)
            self.assertEqual(run.duplicates_dropped, 1)
            self.assertEqual(run.attempts, 1)
        finally:
            pool.close()

    def test_stale_handle_propagates_and_is_never_retried(self):
        created = []
        pool = EnginePool(make_factory(created), size=1)
        try:
            missing = Job(node="ghost", action="move:1", seed="s", payload=b"x")
            run = pool.run_jobs(
                [missing], request_id="req-missing", nodes=[],
                executor=make_executor(), deadline=time.monotonic() + 10,
                batch_size=1, worker_id=worker_id)
            self.assertEqual(run.failures[missing.key], "stale-handle")
            self.assertEqual(run.attempts, 0)

            ghost_owner = Job(node="n2", action="move:1", seed="s", payload=b"x")
            run = pool.run_jobs(
                [ghost_owner], request_id="req-ghost",
                nodes=[NodeRecord(node="n2", handle="root", owner="ghost", portable=False)],
                executor=make_executor(), deadline=time.monotonic() + 10,
                batch_size=1, worker_id=worker_id)
            self.assertEqual(run.failures[ghost_owner.key], "stale-handle")

            # Live owner whose named slot is gone: the worker raises stale-handle
            # and the pool surfaces it without re-creating the node.
            live = Job(node="n3", action="move:1", seed="s", payload=b"x")
            run = pool.run_jobs(
                [live], request_id="req-dropped",
                nodes=[NodeRecord(node="n3", handle="gone", owner="w1", portable=False)],
                executor=make_executor(), deadline=time.monotonic() + 10,
                batch_size=1, worker_id=worker_id)
            self.assertEqual(run.failures[live.key], "stale-handle")
            self.assertEqual(run.attempts, 1)
            self.assertEqual(created[0].saves, 0)  # never silently re-created
        finally:
            pool.close()

    def test_identity_mismatch_fails_closed(self):
        created = []
        pool = EnginePool(make_factory(created, slots={"root": ("root", b"state")}), size=1)
        try:
            node = NodeRecord(node="n1", handle="root", owner="w1", portable=False,
                              artifact_sha256="00" * 32)
            job = jobs_for("n1", 1)[0]
            run = pool.run_jobs([job], request_id="req-identity", nodes=[node],
                                executor=make_executor(), deadline=time.monotonic() + 10,
                                batch_size=1, worker_id=worker_id)
            self.assertEqual(run.failures[job.key], "identity-mismatch")
            self.assertEqual(created[0].saves, 0)
        finally:
            pool.close()

    def test_contract_limits_fail_closed(self):
        created = []
        pool = EnginePool(make_factory(created, slots={"root": ("root", b"state")}), size=1)
        try:
            with self.assertRaises(LimitError):
                pool.run_jobs(jobs_for("n1", 65), request_id="req-65", nodes=[portable_node()],
                              executor=make_executor(), deadline=time.monotonic() + 10,
                              worker_id=worker_id)
            with self.assertRaises(LimitError):
                pool.run_jobs(jobs_for("n1", 1), request_id="req-nodes", nodes=[
                    NodeRecord(node=f"n{index}", portable=False, owner="w1", handle="root")
                    for index in range(4)],
                    executor=make_executor(), deadline=time.monotonic() + 10,
                    worker_id=worker_id)
            with self.assertRaises(LimitError):
                pool.run_jobs(jobs_for("n1", 1), request_id="req-bytes",
                              nodes=[NodeRecord(node="big", portable=False, owner="w1",
                                                handle="root", bytes=64 * 1024 * 1024 + 1)],
                              executor=make_executor(), deadline=time.monotonic() + 10,
                              worker_id=worker_id)
            with self.assertRaises(LimitError):
                pool.run_jobs(jobs_for("n1", 1), request_id="req-frames", nodes=[portable_node()],
                              executor=make_executor(), deadline=time.monotonic() + 10,
                              max_frames=MAX_FRAMES_PER_JOB + 1, worker_id=worker_id)
            with self.assertRaises(LimitError):
                pool.run_jobs([Job(node="n1", action="move:1", seed="s",
                                   payload=b"x" * (1024 * 1024 + 1))],
                              request_id="req-output", nodes=[portable_node()],
                              executor=make_executor(), deadline=time.monotonic() + 10,
                              worker_id=worker_id)
        finally:
            pool.close()

    def test_transfer_cost_measured_and_portable_blob_cached(self):
        created = []
        pool = EnginePool(make_factory(created, slots={"root": ("root", b"state")},
                                       blob_ms=0.01), size=1)
        node = portable_node(blob=None)
        try:
            run = pool.run_jobs(jobs_for("n1", 2), request_id="req-transfer", nodes=[node],
                                executor=make_executor(), deadline=time.monotonic() + 10,
                                batch_size=1, worker_id=worker_id)
            self.assertEqual(run.observations, 2)
            self.assertEqual(run.transfer["exports"], 1)
            self.assertEqual(run.transfer["transfers"], 1)
            self.assertEqual(run.transfer["local_restores"], 0)
            self.assertGreater(run.transfer["export_ms"], 0.0)
            self.assertIsNotNone(node.blob)
            second = pool.run_jobs(jobs_for("n1", 2), request_id="req-transfer-2", nodes=[node],
                                   executor=make_executor(), deadline=time.monotonic() + 10,
                                   batch_size=1, worker_id=worker_id)
            self.assertEqual(second.transfer["exports"], 0)  # cached blob reused
            self.assertEqual(second.transfer["transfers"], 1)
        finally:
            pool.close()

        created = []
        pool = EnginePool(make_factory(created, slots={"root": ("root", b"state")},
                                       restore_ms=0.01), size=1)
        try:
            local = NodeRecord(node="n1", handle="root", owner="w1", portable=False)
            run = pool.run_jobs(jobs_for("n1", 2), request_id="req-local", nodes=[local],
                                executor=make_executor(), deadline=time.monotonic() + 10,
                                batch_size=1, worker_id=worker_id)
            self.assertEqual(run.observations, 2)
            self.assertEqual(run.transfer["transfers"], 0)
            self.assertEqual(run.transfer["local_restores"], 1)
            self.assertGreater(run.transfer["local_restore_ms"], 0.0)
        finally:
            pool.close()


if __name__ == "__main__":
    unittest.main()
