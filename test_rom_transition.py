"""Scheduler/adaptor tests: pairing accounting, CRN, partials, versioned results."""

import threading
import time
import unittest

import rom_bridge
from rom_pool import EnginePool, NodeRecord
from rom_query import DecisionStateToken
from rom_transition import TransitionAdapter, action_key, bridge_executor, node_record, seed_key
from test_rom_pool import make_factory, worker_id

NODE_SHA = "EE" * 32
ELF_SHA = "AB" * 32
WORKER_SHA = "CD" * 32
IDENTITY = {"test_elf_sha256": ELF_SHA, "worker_sha256": WORKER_SHA,
            "wire_protocol": 2, "source_sha": "2576d0f"}
MOVES = [{"kind": "move", "move_id": 57}, {"kind": "move", "move_id": 85},
         {"kind": "switch", "slot": 2}]
SEED_A = {"main": [1, 2, 3, 4], "secondary": [5, 6, 7, 8]}
SEED_B = {"main": [9, 10, 11, 12], "secondary": [13, 14, 15, 16]}


def make_state(*, boundary="player-choice", actions=None, portable=True, owner="w1",
               handle="root", identity=None, terminal=None, schema="sidedex-state-v1"):
    return {
        "state_schema": schema,
        "identity": dict(identity if identity is not None else IDENTITY),
        "token": {"handle": handle, "owner_worker": owner, "portable": portable,
                  "bytes": 528, "sha256": NODE_SHA},
        "boundary": boundary,
        "turn": 3,
        "legal_player_actions": list(MOVES if actions is None else actions),
        "terminal": terminal,
        "rng": {"main": [1, 2, 3, 4], "secondary": [5, 6, 7, 8]},
        "metrics": {},
    }


def make_encoder(record=None):
    def encode_turn(state, action, rng_pair):
        key = action_key(action)
        if record is not None:
            record.append((key, tuple(rng_pair[0]), tuple(rng_pair[1])))
        return f"{key}|{seed_key(rng_pair)}".encode()

    return encode_turn


def make_executor(*, fail_payloads=(), delay=0.0, record=None, barrier=None,
                  fail_workers=(), fail_times=1, workers_seen=None):
    state = {"calls": 0, "failures": {}}

    def execute(client, mailbox, node, payloads, *, deadline, request_id, max_frames):
        state["calls"] += 1
        if record is not None:
            record.extend(payload.decode() for payload in payloads)
        if workers_seen is not None:
            workers_seen.add(client.worker_id)
        if barrier is not None and state["calls"] <= 3:
            try:
                barrier.wait(timeout=10)
            except threading.BrokenBarrierError:
                pass
        if any(payload.decode() in fail_payloads for payload in payloads):
            raise rom_bridge.WorkerResponseError("incomplete-state")
        if (client.worker_id in fail_workers
                and state["failures"].get(client.worker_id, 0) < fail_times):
            state["failures"][client.worker_id] = state["failures"].get(client.worker_id, 0) + 1
            client.is_running = False
            raise rom_bridge.WorkerExitedError("simulated worker exit")
        if delay:
            time.sleep(delay)
        return [{"complete": True, "payload": payload.decode(),
                 "worker": client.worker_id} for payload in payloads]

    return execute


def make_pool(created, *, size=1, slots=None):
    return EnginePool(make_factory(created, slots=slots or {"root": ("root", b"state")}),
                      size=size)


def make_adapter(pool, *, executor=None, encoder=None, decode=None, identity=IDENTITY,
                 **kwargs):
    return TransitionAdapter(
        pool, executor=executor or make_executor(), encode_turn=encoder or make_encoder(),
        decode_turn=decode, identity=identity, worker_id=worker_id, **kwargs)


class PairingTest(unittest.TestCase):
    def test_paired_sample_requires_every_action(self):
        created = []
        pool = make_pool(created)
        missing = f"{action_key(MOVES[2])}|{seed_key(SEED_B)}"
        try:
            adapter = make_adapter(pool, executor=make_executor(fail_payloads={missing}),
                                   batch_size=1, max_attempts=1)
            envelope = adapter.transitions(
                request_id="req-1", state=make_state(), actions=MOVES,
                seeds=[SEED_A, SEED_B], deadline=time.monotonic() + 10)
            self.assertEqual(envelope["status"], "partial")
            self.assertEqual(len(envelope["paired_samples"]), 1)
            self.assertEqual(envelope["paired_samples"][0]["seed"], seed_key(SEED_A))
            self.assertEqual(len(envelope["paired_samples"][0]["actions"]), 3)
            partial = envelope["partial_samples"]
            self.assertEqual(len(partial), 1)
            self.assertEqual(partial[0]["seed"], seed_key(SEED_B))
            self.assertEqual(partial[0]["missing"], [action_key(MOVES[2])])
            self.assertEqual(sorted(partial[0]["actions"]), sorted(action_key(m) for m in MOVES[:2]))
            # No invented zero outcome for the missing action.
            self.assertNotIn(action_key(MOVES[2]), partial[0]["actions"])
            self.assertEqual(envelope["progress"]["observations"], 5)
        finally:
            pool.close()

    def test_common_random_numbers_shared_across_actions(self):
        created = []
        pool = make_pool(created)
        encodes = []
        try:
            adapter = make_adapter(pool, encoder=make_encoder(encodes))
            envelope = adapter.transitions(
                request_id="req-crn", state=make_state(), actions=MOVES,
                seeds=[SEED_A, SEED_B], deadline=time.monotonic() + 10)
            self.assertEqual(envelope["status"], "ok")
            self.assertEqual(len(envelope["paired_samples"]), 2)
            by_seed = {}
            for key, main, secondary in encodes:
                by_seed.setdefault((main, secondary), set()).add(key)
            self.assertEqual(len(by_seed), 2)
            for pairs in by_seed.values():
                self.assertEqual(len(pairs), 3)  # every action of a comparison
            pairs = {(main, secondary) for _, main, secondary in encodes}
            self.assertEqual(len(pairs), 2)
        finally:
            pool.close()

    def test_deadline_returns_partial_work_without_inventing_zeros(self):
        created = []
        pool = make_pool(created)
        try:
            adapter = make_adapter(
                pool, executor=make_executor(delay=0.2), batch_size=1)
            envelope = adapter.transitions(
                request_id="req-deadline", state=make_state(), actions=MOVES,
                seeds=[SEED_A], deadline=time.monotonic() + 0.35)
            self.assertIn(envelope["status"], ("partial", "unavailable"))
            self.assertEqual(envelope["paired_samples"], [])
            for sample in envelope["partial_samples"]:
                self.assertTrue(sample["actions"])
                self.assertTrue(sample["missing"])
                for key in sample["missing"]:
                    self.assertNotIn(key, sample["actions"])
            self.assertLessEqual(envelope["progress"]["observations"], 3)
        finally:
            pool.close()

    def test_all_three_workers_used(self):
        created = []
        pool = make_pool(created, size=3)
        seen = set()
        try:
            adapter = make_adapter(
                pool, executor=make_executor(barrier=threading.Barrier(3), workers_seen=seen),
                batch_size=1)
            envelope = adapter.transitions(
                request_id="req-3", state=make_state(), actions=MOVES,
                seeds=[SEED_A, SEED_B], deadline=time.monotonic() + 20)
            self.assertEqual(envelope["progress"]["workers_used"], ["w1", "w2", "w3"])
            self.assertEqual(seen, {"w1", "w2", "w3"})
            self.assertEqual(envelope["progress"]["observations"], 6)
        finally:
            pool.close()


class EnvelopeTest(unittest.TestCase):
    def test_versioned_envelope_and_deterministic_sample_order(self):
        created = []
        pool = make_pool(created)
        try:
            adapter = make_adapter(pool)
            envelope = adapter.transitions(
                request_id="req-order", state=make_state(), actions=MOVES,
                seeds=[SEED_B, SEED_A], deadline=time.monotonic() + 10)
            self.assertEqual(envelope["transition_schema"], "sidedex-transition-v1")
            self.assertEqual(envelope["contract_version"], 1)
            self.assertEqual(envelope["input_token_sha256"], NODE_SHA)
            self.assertEqual(envelope["status"], "ok")
            self.assertEqual([sample["seed"] for sample in envelope["paired_samples"]],
                             sorted([seed_key(SEED_A), seed_key(SEED_B)]))
            self.assertEqual(envelope["actions"], [action_key(action) for action in MOVES])
        finally:
            pool.close()

    def test_decode_seam_shapes_transition_results(self):
        created = []
        pool = make_pool(created)

        def decode(job, raw, state, action, pair):
            return {"transition_schema": "sidedex-transition-v1",
                    "player_action": action, "raw_payload": raw["payload"]}

        try:
            adapter = make_adapter(pool, decode=decode)
            envelope = adapter.transitions(
                request_id="req-decode", state=make_state(), actions=MOVES[:1],
                seeds=[SEED_A], deadline=time.monotonic() + 10)
            sample = envelope["paired_samples"][0]["actions"][action_key(MOVES[0])]
            self.assertEqual(sample["player_action"], MOVES[0])
            self.assertTrue(sample["raw_payload"].startswith(action_key(MOVES[0])))
        finally:
            pool.close()

    def test_identity_mismatch_fails_closed_without_spawning_workers(self):
        created = []
        pool = make_pool(created)
        try:
            state = make_state(identity={**IDENTITY, "test_elf_sha256": "00" * 32})
            envelope = make_adapter(pool).transitions(
                request_id="req-identity", state=state, actions=MOVES,
                seeds=[SEED_A], deadline=time.monotonic() + 10)
            self.assertEqual(envelope["status"], "unavailable")
            self.assertEqual(envelope["reason"], "identity-mismatch")
            self.assertEqual(created, [])
        finally:
            pool.close()

    def test_terminal_and_no_legal_actions_and_illegal_action(self):
        created = []
        pool = make_pool(created)
        adapter = make_adapter(pool)
        try:
            envelope = adapter.transitions(
                request_id="req-terminal",
                state=make_state(boundary="terminal", actions=[], terminal={"result": "win"}),
                actions=MOVES, seeds=[SEED_A], deadline=time.monotonic() + 10)
            self.assertEqual(envelope["reason"], "terminal-state")
            envelope = adapter.transitions(
                request_id="req-empty", state=make_state(actions=[]), actions=MOVES,
                seeds=[SEED_A], deadline=time.monotonic() + 10)
            self.assertEqual(envelope["reason"], "no-legal-actions")
            envelope = adapter.transitions(
                request_id="req-illegal", state=make_state(),
                actions=[{"kind": "move", "move_id": 1}], seeds=[SEED_A],
                deadline=time.monotonic() + 10)
            self.assertEqual(envelope["reason"], "invalid-id")
        finally:
            pool.close()

    def test_worker_exit_survives_without_duplicate_samples(self):
        created = []
        pool = make_pool(created, size=1)
        try:
            adapter = make_adapter(
                pool, executor=make_executor(fail_workers=("w1",), fail_times=1),
                batch_size=1, max_attempts=2)
            envelope = adapter.transitions(
                request_id="req-exit", state=make_state(), actions=MOVES,
                seeds=[SEED_A, SEED_B], deadline=time.monotonic() + 20)
            self.assertEqual(envelope["status"], "ok")
            self.assertEqual(len(envelope["paired_samples"]), 2)
            self.assertEqual(envelope["progress"]["observations"], 6)
            self.assertEqual(envelope["progress"]["duplicates_dropped"], 0)
            self.assertEqual(envelope["progress"]["attempts"], 7)
            self.assertEqual(envelope["failures"], {})
        finally:
            pool.close()

    def test_node_record_from_token(self):
        state = make_state(portable=False, owner="w7", handle="h1")
        node = node_record(state)
        self.assertEqual(node.node, NODE_SHA)
        self.assertFalse(node.portable)
        self.assertEqual(node.owner, "w7")
        self.assertEqual(node.handle, "h1")
        self.assertEqual(node.artifact_sha256, ELF_SHA)
        self.assertEqual(node.worker_sha256, WORKER_SHA)

    def test_decision_state_token_from_query_api_is_accepted(self):
        created = []
        pool = make_pool(created)
        token = DecisionStateToken(
            handle="root", sha256=NODE_SHA, bytes=528, identity=dict(IDENTITY),
            rng={"main": [1, 2, 3, 4], "secondary": [5, 6, 7, 8]},
            boundary="player-choice", legal_player_actions=tuple(MOVES))
        node = NodeRecord(node=NODE_SHA, handle="root", owner="w1", portable=True,
                          bytes=528, sha256=NODE_SHA, artifact_sha256=ELF_SHA,
                          worker_sha256=WORKER_SHA)
        try:
            adapter = make_adapter(pool)
            envelope = adapter.transitions(
                request_id="req-token", state=token, actions=MOVES, seeds=[SEED_A],
                node=node, deadline=time.monotonic() + 10)
            self.assertEqual(envelope["status"], "ok")
            self.assertEqual(envelope["input_token_sha256"], NODE_SHA)
            # Without routing data a token cannot be routed, and that fails closed.
            bare = adapter.transitions(
                request_id="req-token-bare", state=token, actions=MOVES,
                seeds=[SEED_A], deadline=time.monotonic() + 10)
            self.assertEqual(bare["status"], "unavailable")
            self.assertEqual(set(bare["failures"].values()), {"stale-handle"})
        finally:
            pool.close()

    def test_bridge_executor_calls_batch_turns_with_the_pool_keywords(self):
        calls = []

        class Client:
            rom_sha256 = ELF_SHA

            def batch_turns(self, mailbox, payloads, **fields):
                calls.append((mailbox, list(payloads), fields))
                return {"turns": [{"complete": True} for _ in payloads]}

        executor = bridge_executor(mailbox_bytes=856)
        turns = executor(Client(), 0x02000000, object(), [b"a", b"b"],
                         deadline=time.monotonic() + 5, request_id="req-9",
                         max_frames=1000)
        self.assertEqual(len(turns), 2)
        mailbox, payloads, fields = calls[0]
        self.assertEqual(mailbox, 0x02000000)
        self.assertEqual(payloads, [b"a", b"b"])
        self.assertEqual(fields["mailbox_bytes"], 856)
        self.assertEqual(fields["engine_elf_sha256"], ELF_SHA)
        self.assertEqual(fields["max_frames"], 1000)
        self.assertLessEqual(len(fields["request_id"]), 128)


if __name__ == "__main__":
    unittest.main()
