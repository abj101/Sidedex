"""Client for the native i386 engine worker (`sidedex-native`, ROM_NATIVE_PLAN.md).

It speaks the worker JSON-lines protocol over `wsl.exe`. Beyond the mGBA worker's
ops it has `rollout` (see native/rollout.inc.h in the engine tree): restore a root
handle, play an optional prefix of chosen actions, then let the ROM AI choose the
player's action until terminal or `max_turns`.
"""
from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import time

NATIVE_BIN = "/home/abj1/rom-n1/sidedex-native"
NATIVE_BIN_WIN = r"\\wsl.localhost\Ubuntu" + NATIVE_BIN
SOURCE_SHA = "0" * 40
WORKER_SHA = "0" * 64


class NativeError(RuntimeError):
    def __init__(self, op: str, reply: dict):
        super().__init__(f"{op}: {reply}")
        self.reply = reply


class Engine:
    """One worker process. `sha` is the artifact (engine binary) sha256, lowercase hex."""

    def __init__(self, command: list[str], artifact_sha: str):
        self.sha = artifact_sha
        self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     text=True, bufsize=1)
        self.status = self._read()
        self.ready: dict | None = None

    @classmethod
    def native(cls) -> "Engine":
        sha = hashlib.sha256(open(NATIVE_BIN_WIN, "rb").read()).hexdigest()
        return cls(["wsl.exe", "-e", NATIVE_BIN, "--rom", NATIVE_BIN], sha)

    def _read(self) -> dict:
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("engine exited")
        try:
            return json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"bad worker line: {line[:600]!r}") from exc

    def op(self, op: str, **fields) -> dict:
        self.proc.stdin.write(json.dumps({"protocol": 3, "op": op, **fields}) + "\n")
        self.proc.stdin.flush()
        reply = self._read()
        if not reply.get("ok"):
            raise NativeError(op, reply)
        return reply

    def close(self) -> None:
        try:
            self.op("quit")
        finally:
            self.proc.wait(timeout=10)

    def announce(self) -> dict:
        from rom_bridge import parse_v9_ready
        events: list[str] = []
        for _ in range(400):
            events += self.op("run_frames", frames=60)["trace_events"]
            if any(e.startswith("ROMTRACE|ready|") for e in events):
                self.ready = parse_v9_ready(events)
                return self.ready
        raise TimeoutError("no ready announcement")

    def save(self, name: str) -> str:
        return self.op("save_state", name=name, artifact_sha256=self.sha)["slot"]["handle"]

    def _batch_fields(self, ready: dict, handle: str, request_id: str, token: int | None = None,
                      max_frames: int = 24_000) -> dict:
        if token is None:
            token = int.from_bytes(hashlib.sha256(request_id.encode()).digest()[:2], "little")
        return dict(handle=handle, address=ready["address"], result_address=ready["result_address"],
                    result_capacity=ready["result_capacity"], mailbox_bytes=876,
                    budget_ms=60_000, max_frames=max_frames, request_id=request_id, request_token=token,
                    source_sha=SOURCE_SHA, engine_elf_sha256=self.sha, worker_sha256=WORKER_SHA,
                    mode=0, critical_policy=0)

    def turn(self, handle: str, payload: bytes, ready: dict, request_id: str, *,
             hash_children: bool = True, request_token: int | None = None) -> dict:
        """One batch_turns_v3 transition (one 876-byte request); returns the turn dict + parsed `sdr2`."""
        from tools.rom_oracle import sdr2
        fields = self._batch_fields(ready, handle, request_id, request_token)
        extra = {} if hash_children else {"hash_children": False}
        wall = time.perf_counter()
        reply = self.op("batch_turns_v3", mailbox_count=1, mailboxes=base64.b64encode(payload).decode(),
                        **fields, **extra)
        turn = reply["turns"][0]
        turn["wall_ms"] = (time.perf_counter() - wall) * 1000
        turn["sdr2"] = base64.b64decode(turn["result"])
        sdr2.parse(turn["sdr2"], request_id=fields["request_token"], mode=0, critical_policy=0,
                   capacity=ready["result_capacity"])
        return turn

    def rollout(self, handle: str, items: list[dict], request_id: str = "rollout", *,
                debug: bool = False, ready: dict | None = None, request_token: int | None = None,
                max_frames: int = 24_000) -> list[dict]:
        """Run rollout items from a root handle (a child saved at a decision boundary).

        item: {"rng1": [a, b, c, ctr], "rng2": [a, b, c, ctr], "prefix": [[kind, id, ...], ...],
        "max_turns": N}. A prefix action is a tag-7 legal-action row; only kind and id are
        compared. Result per item: {outcome: win|loss|draw|other|cap, turns, prefix_consumed,
        frames, legal_at_prefix_end: [[kind, id, pp_or_species, target_or_slot], ...],
        leaf: {p_alive, o_alive, p_hp, o_hp}, final_sdr2 (debug only, base64)}. A turn that never
        reaches its result (a ROM hang, `max_frames` per turn) makes that item {outcome: "error",
        error: ...} and the others still run; an engine signal raises NativeError (engine-fault).
        request_id must differ from the one that produced `handle` (the ROM releases its
        boundary hold only on a new request token). Turn i of every item uses token
        `request_token + i` (mod 2**16), which is what a turn-by-turn drive must match to be byte-equal.
        """
        ready = ready or self.ready
        fields = self._batch_fields(ready, handle, request_id, request_token, max_frames)
        reply = self.op("rollout", items=items, **({"debug": True} if debug else {}), **fields)
        return reply["items"]

