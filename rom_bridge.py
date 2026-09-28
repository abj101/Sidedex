"""Strict low-level client for the persistent mGBA worker.

This module exposes emulator primitives only. It does not interpret game state
or calculate battle outcomes.
"""

from __future__ import annotations

import hashlib
import base64
import binascii
import json
import os
import queue
import re
import subprocess
import threading
import time
import zlib
from pathlib import Path
from typing import Any, Sequence


from rom_identity import (
    PROTOCOL_VERSION,
    WORKER_VERSION,
    MGBA_VERSION,
    SAMPLER_VERSION,
    SAVE_FORMAT_ID,
    STATE_SCHEMA,
    TRANSITION_SCHEMA,
    SOURCE_SHA,
    ROM_SHA256,
    ENGINE_ELF_SHA256,
    WORKER_SHA256,
)  # noqa: F401  (re-exported)

# Contract Appendix B (v2 execution amendment). Gate V1 retired wire 2: the
# pinned worker is wire 3 (rom_identity.PROTOCOL_VERSION). The v9 ROM hook and
# the compact-turn schemas are named here explicitly so neither side ever
# guesses a layout from a reply.
ROM_HOOK_V9 = 9
TRANSITION_SCHEMA_V2 = "sidedex-transition-v2"
DECODE_SCHEMA = "decode-v2"

# Contract C.1: tools/rom_oracle/sdr2.py is the only SDR2 parser; rom_bridge
# only maps its result onto the wire/child rules.
from tools.rom_oracle import sdr2  # noqa: E402

ROOT = Path(__file__).resolve().parent
DEFAULT_SOURCE_LOCK = ROOT / "data" / "source_lock.json"
DEFAULT_WORKER = ROOT / "native" / "rom_worker" / "build" / "mingw" / "rom_worker.exe"
MAX_FRAMES_PER_REQUEST = 100_000
EWRAM_START = 0x02000000
EWRAM_END = 0x02040000
IWRAM_START = 0x03000000
IWRAM_END = 0x03008000
MAX_MEMORY_WRITE_BYTES = 2048
MAX_MEMORY_BLOCK_BYTES = 4096
MAX_BATCH_TURNS = 8
# Contract section 8 bounds; the worker enforces the same numbers.
MAX_STATE_SLOTS = 3
MAX_STATE_TOTAL_BYTES = 64 * 1024 * 1024
MAX_STATE_NAME_BYTES = 64
MAX_HANDLE_HEX = 32
MAX_KEYS_MASK = 0x3FF
MAX_BLOB_BASE64_BYTES = 900_000
STOP_KINDS = ("boundary", "terminal", "turn-complete")
# 256 bounded trace events (up to 510 bytes each, JSON escaped) fit below 1 MiB.
MAX_MESSAGE_BYTES = 1_048_576
# Contract C.3: a nonzero SDR2 status carries a tag 11 {reason, source} record.
V3_RESULT_ERROR_CODES = {(1, 1, 1): "incomplete-state"}

# Optional JSONL transport profiling for the B0 profile. Default off (no
# behaviour change); tools/rom_oracle/profile_b0.py sets this True and reads
# RomWorkerClient.transport_samples for the encode/write/decode split.
PROFILE_TRANSPORT = False


def _percentile(values: Sequence[float], percentile: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(len(ordered) * percentile + 0.999999) - 1))
    return ordered[index]


def parse_v9_ready(events: Sequence[str]) -> dict[str, int]:
    """Select the announced v9/v3 request and result buffers from ROMTRACE."""
    for event in reversed(events):
        match = re.fullmatch(r"ROMTRACE\|ready\|(\d+)\|876\|9\|3\|(\d+)\|(\d+)", event)
        if match:
            address, result_address, capacity = map(int, match.groups())
            if (not EWRAM_START <= address <= EWRAM_END - 876 or
                    not EWRAM_START <= result_address < EWRAM_END or
                    capacity != 8192 or capacity > EWRAM_END - result_address or
                    address < result_address + capacity and result_address < address + 876):
                raise WorkerProtocolError("ROM announced overlapping or out-of-range v9 buffers")
            return {"address": address, "mailbox_bytes": 876,
                    "result_address": result_address, "result_capacity": capacity,
                    "rom_hook_protocol": 9, "wire_protocol": 3}
    raise WorkerProtocolError("ROM did not announce a v9/v3 mailbox")


class RomBridgeError(RuntimeError):
    """Base class for low-level ROM worker failures."""


class RomIdentityError(RomBridgeError):
    """The ROM or local source lock differs from the pinned identity."""


class WorkerUnavailableError(RomBridgeError):
    """The worker executable could not be started."""


class WorkerExitedError(RomBridgeError):
    """The worker stopped before replying."""


class WorkerTimeoutError(RomBridgeError):
    """The worker failed to reply before the request deadline."""


class WorkerProtocolError(RomBridgeError):
    """The worker returned malformed or incompatible JSONL."""


class WorkerResponseError(RomBridgeError):
    """The worker returned a structured error response."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(f"mGBA worker rejected the request: {code}")


def sha256_file(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError(f"invalid JSON constant: {value}")


def _parse_reply(line: str, expected_protocol: int = PROTOCOL_VERSION) -> dict[str, Any]:
    if len(line.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise WorkerProtocolError("worker response exceeded the message limit")
    try:
        value = json.loads(line, object_pairs_hook=_strict_object, parse_constant=_reject_constant)
    except (json.JSONDecodeError, UnicodeError, ValueError) as exc:
        raise WorkerProtocolError(f"worker returned invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise WorkerProtocolError("worker response must be a JSON object")
    if type(value.get("protocol")) is not int or value["protocol"] != expected_protocol:
        raise WorkerProtocolError("worker protocol version mismatch")
    if type(value.get("ok")) is not bool:
        raise WorkerProtocolError("worker response is missing a boolean ok field")
    if not value["ok"]:
        code = value.get("error")
        if not isinstance(code, str) or not code or len(code) > 120:
            raise WorkerProtocolError("worker error response has no valid error code")
        raise WorkerResponseError(code)
    return value


def _reader(stream, output: queue.Queue):
    try:
        for line in iter(stream.readline, ""):
            output.put(line)
    except (OSError, ValueError):
        pass
    finally:
        output.put(None)


def worker_identity(worker_version: str, pid: int, started_at: float, artifact_sha256: str) -> str:
    """Contract §2/§8 owner_worker = worker version + process identity + artifact identity."""
    return "{}|pid={}|start={:.6f}|artifact={}".format(
        worker_version, int(pid), float(started_at), str(artifact_sha256).lower()
    )


class RomWorkerClient:
    """One serialized request stream connected to a persistent mGBA process."""

    # Defaults for the pinned wire-3 path, so a partially built client
    # (object.__new__ in the tests) still validates identity predictably.
    wire_protocol = PROTOCOL_VERSION
    worker_version = WORKER_VERSION

    def __init__(
        self,
        worker_path: str | os.PathLike[str] = DEFAULT_WORKER,
        rom_path: str | os.PathLike[str] = "",
        *,
        timeout: float = 5.0,
        source_lock_path: str | os.PathLike[str] = DEFAULT_SOURCE_LOCK,
        expected_artifact_sha256: str = ROM_SHA256,
        expected_worker_sha256: str = WORKER_SHA256,
        worker_args: Sequence[str] = (),
        wire_protocol: int = PROTOCOL_VERSION,
    ):
        if type(timeout) not in (int, float) or timeout <= 0:
            raise ValueError("timeout must be a positive number")
        worker = Path(worker_path).expanduser().resolve()
        rom = Path(rom_path).expanduser().resolve()
        self._timeout = float(timeout)
        if type(wire_protocol) is not int or wire_protocol != PROTOCOL_VERSION:
            raise ValueError(f"wire_protocol must be {PROTOCOL_VERSION}")
        self.wire_protocol = wire_protocol
        self.worker_version = WORKER_VERSION
        self._lock = threading.Lock()
        self._closed = False
        self._stderr_lines: queue.Queue = queue.Queue(maxsize=16)
        self._transport_samples: list[dict[str, Any]] = []

        if not isinstance(expected_artifact_sha256, str) or len(expected_artifact_sha256) != 64 or any(
            char not in "0123456789abcdefABCDEF" for char in expected_artifact_sha256
        ):
            raise ValueError("expected_artifact_sha256 must be a 64-character SHA-256 hex digest")
        self.rom_sha256 = self._verify_identity(
            rom, Path(source_lock_path), expected_artifact_sha256.upper()
        )
        if not isinstance(expected_worker_sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_worker_sha256):
            raise ValueError("expected_worker_sha256 must be a 64-character SHA-256 hex digest")
        self.worker_sha256 = sha256_file(worker)
        if self.worker_sha256 != expected_worker_sha256.upper():
            raise RomIdentityError(f"worker SHA-256 {self.worker_sha256} does not match pinned worker {expected_worker_sha256.upper()}")
        self._rom_size = rom.stat().st_size
        if not worker.is_file():
            raise WorkerUnavailableError(f"mGBA worker executable is missing: {worker}")

        command = [str(worker), *[str(arg) for arg in worker_args], "--rom", str(rom)]
        self._started_at = time.time()
        try:
            self._process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="strict",
                bufsize=1,
            )
        except OSError as exc:
            raise WorkerUnavailableError(f"could not start mGBA worker: {exc}") from exc
        self._stdout_lines: queue.Queue = queue.Queue(maxsize=8)
        self._stdout_reader = threading.Thread(
            target=_reader, args=(self._process.stdout, self._stdout_lines), daemon=True
        )
        self._stderr_reader = threading.Thread(
            target=_reader, args=(self._process.stderr, self._stderr_lines), daemon=True
        )
        self._stdout_reader.start()
        self._stderr_reader.start()
        self._has_checkpoint = False

        try:
            self._status = self._read_status()
        except Exception:
            self._terminate()
            raise
        self._worker_id = worker_identity(
            self._status["worker"], self._process.pid, self._started_at, self.rom_sha256
        )

    @staticmethod
    def _verify_identity(rom: Path, source_lock_path: Path, expected_sha256: str = ROM_SHA256) -> str:
        if not rom.is_file():
            raise RomIdentityError(f"ROM file is missing: {rom}")
        try:
            lock = json.loads(source_lock_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RomIdentityError(f"could not read source lock: {source_lock_path}") from exc
        locked_commit = lock.get("commit") if isinstance(lock, dict) else None
        if locked_commit != SOURCE_SHA:
            raise RomIdentityError(
                f"local source lock commit {locked_commit!r} does not match pinned {SOURCE_SHA}"
            )
        actual = sha256_file(rom)
        if actual != expected_sha256:
            raise RomIdentityError(
                f"artifact SHA-256 {actual} does not match pinned artifact {expected_sha256}"
            )
        return actual

    @property
    def transport_samples(self) -> list[dict[str, Any]]:
        """Per-request JSONL transport timings, populated only when PROFILE_TRANSPORT."""
        return list(self._transport_samples)

    @property
    def worker_id(self) -> str:
        """Stable owner_worker identity for this client's lifetime (contract §2/§8).

        Derived from the running worker version, the child process id, the start
        timestamp, and the loaded artifact sha256. Deterministic for one client;
        two client processes differ (distinct pid/start).
        """
        return self._worker_id

    @property
    def status(self) -> dict[str, Any]:
        return {
            **self._status,
            "source_sha": SOURCE_SHA,
            "rom_sha256": self.rom_sha256,
        }

    @property
    def is_running(self) -> bool:
        return not self._closed and self._process.poll() is None

    def _read_line(self, timeout: float | None = None) -> str:
        try:
            line = self._stdout_lines.get(timeout=self._timeout if timeout is None else timeout)
        except queue.Empty as exc:
            self._terminate()
            raise WorkerTimeoutError("mGBA worker response timed out") from exc
        if line is None:
            detail = self._stderr_detail()
            raise WorkerExitedError("mGBA worker exited before replying" + (f": {detail}" if detail else ""))
        if not line.endswith("\n"):
            raise WorkerProtocolError("worker response was not newline terminated")
        return line[:-1]

    def _stderr_detail(self) -> str:
        pieces = []
        while True:
            try:
                item = self._stderr_lines.get_nowait()
            except queue.Empty:
                break
            if item is not None:
                pieces.append(item.strip())
        return " ".join(piece for piece in pieces if piece)[:500]

    def _read_status(self) -> dict[str, Any]:
        reply = _parse_reply(self._read_line(), self.wire_protocol)
        required = {
            "worker": self.worker_version,
            "mgba": MGBA_VERSION,
            "platform": "gba",
        }
        for key, expected in required.items():
            if reply.get(key) != expected:
                raise WorkerProtocolError(f"worker {key} does not match the pinned build")
        if reply.get("supported_wire_protocols") != [PROTOCOL_VERSION]:
            raise WorkerProtocolError(f"worker does not advertise wire protocol {PROTOCOL_VERSION}")
        for key, expected in (("state_schema", STATE_SCHEMA), ("transition_schema", TRANSITION_SCHEMA_V2),
                              ("decode_schema", DECODE_SCHEMA)):
            if reply.get(key) != expected:
                raise WorkerProtocolError(f"worker {key} does not match the transition contract")
        if type(reply.get("rom_bytes")) is not int or reply["rom_bytes"] <= 0:
            raise WorkerProtocolError("worker status has an invalid ROM size")
        if reply["rom_bytes"] != self._rom_size:
            raise WorkerProtocolError("worker loaded a ROM with a different size")
        if type(reply.get("frame_count")) is not int or reply["frame_count"] < 0:
            raise WorkerProtocolError("worker status has an invalid frame count")
        title, code = reply.get("rom_title"), reply.get("rom_code")
        if not isinstance(title, str) or not isinstance(code, str):
            raise WorkerProtocolError("worker status is missing ROM title or code")
        return reply

    def _request(self, op: str, *, _timeout: float | None = None, **fields) -> dict[str, Any]:
        with self._lock:
            if self._closed or self._process.poll() is not None:
                raise WorkerExitedError("mGBA worker is not running")
            request = {"protocol": self.wire_protocol, "op": op, **fields}
            t_encode_start = time.perf_counter()
            wire = json.dumps(request, separators=(",", ":"), allow_nan=False)
            if len(wire.encode("utf-8")) + 1 > MAX_MESSAGE_BYTES:
                raise ValueError("request exceeds the worker message limit")
            t_encode_end = time.perf_counter()
            try:
                request_wire_bytes = len(wire.encode("utf-8")) + 1
                self._process.stdin.write(wire + "\n")
                self._process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise WorkerExitedError("mGBA worker closed its input pipe") from exc
            t_write_end = time.perf_counter()
            reply_line = self._read_line(_timeout)
            t_read_end = time.perf_counter()
            reply = _parse_reply(reply_line, self.wire_protocol)
            t_decode_end = time.perf_counter()
            if PROFILE_TRANSPORT:
                self._transport_samples.append({
                    "op": op,
                    "encode_ns": (t_encode_end - t_encode_start) * 1e9,
                    "write_ns": (t_write_end - t_encode_end) * 1e9,
                    "read_ns": (t_read_end - t_write_end) * 1e9,
                    "decode_ns": (t_decode_end - t_read_end) * 1e9,
                })
            reply["_wire_bytes"] = len(reply_line.encode("utf-8")) + 1
            reply["_request_wire_bytes"] = request_wire_bytes
            if op in {"status", "run_frames", "reset", "load_state", "load_state_blob", "write_memory"}:
                self._status = self._validate_status_reply(reply)
            return reply

    def _validate_status_reply(self, reply: dict[str, Any]) -> dict[str, Any]:
        if reply.get("worker") != self.worker_version or reply.get("mgba") != MGBA_VERSION:
            raise WorkerProtocolError("worker identity changed during the session")
        if reply.get("platform") != "gba":
            raise WorkerProtocolError("worker returned a non-GBA core")
        if type(reply.get("rom_bytes")) is not int or reply["rom_bytes"] <= 0:
            raise WorkerProtocolError("worker returned an invalid ROM size")
        if reply["rom_bytes"] != self._rom_size:
            raise WorkerProtocolError("worker ROM size changed during the session")
        if type(reply.get("frame_count")) is not int or reply["frame_count"] < 0:
            raise WorkerProtocolError("worker returned an invalid frame count")
        if not isinstance(reply.get("rom_title"), str) or not isinstance(reply.get("rom_code"), str):
            raise WorkerProtocolError("worker status is missing ROM title or code")
        return reply

    def status_request(self) -> dict[str, Any]:
        return self._request("status")

    def run_frames(self, frames: int, keys: int | None = None) -> dict[str, Any]:
        if type(frames) is not int or not 0 <= frames <= MAX_FRAMES_PER_REQUEST:
            raise ValueError(f"frames must be an integer from 0 to {MAX_FRAMES_PER_REQUEST}")
        fields: dict[str, Any] = {"frames": frames}
        if keys is not None:
            fields["keys"] = self._validate_keys(keys)
        reply = self._request("run_frames", **fields)
        events = reply.get("trace_events")
        if (not isinstance(events, list) or any(not isinstance(event, str) for event in events) or
                type(reply.get("trace_overflow")) is not bool):
            raise WorkerProtocolError("worker returned invalid trace event data")
        if reply["trace_overflow"]:
            raise WorkerProtocolError("worker trace event buffer overflowed")
        return reply

    def reset(self) -> dict[str, Any]:
        return self._request("reset")

    @staticmethod
    def _validate_keys(keys: int) -> int:
        if type(keys) is not int or not 0 <= keys <= MAX_KEYS_MASK:
            raise ValueError(f"keys must be an integer key mask from 0 to 0x{MAX_KEYS_MASK:x}")
        return keys

    @staticmethod
    def _validate_state_name(name: str) -> str:
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,%d}" % MAX_STATE_NAME_BYTES, name):
            raise ValueError("state name must be 1 to 64 safe ASCII characters")
        return name

    @staticmethod
    def _validate_handle(handle: str) -> str:
        if not isinstance(handle, str) or not re.fullmatch(r"[0-9a-f]{%d}" % MAX_HANDLE_HEX, handle):
            raise ValueError("state handle must be a 32-character lowercase hex string")
        return handle

    @staticmethod
    def _validate_blob_text(blob: str) -> str:
        if not isinstance(blob, str) or not blob or len(blob) > MAX_BLOB_BASE64_BYTES:
            raise ValueError(f"blob must be a non-empty base64 string of at most {MAX_BLOB_BASE64_BYTES} characters")
        try:
            base64.b64decode(blob, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("blob must be valid base64") from exc
        return blob

    def _validate_state_identity(self, identity: Any) -> dict[str, Any]:
        if not isinstance(identity, dict):
            raise WorkerProtocolError("worker state slot is missing its identity")
        for key, expected in (
            ("worker_version", self.worker_version),
            ("mgba_version", MGBA_VERSION),
            ("wire_protocol", self.wire_protocol),
            ("save_format", SAVE_FORMAT_ID),
        ):
            if identity.get(key) != expected:
                raise WorkerProtocolError(f"worker state slot identity {key} does not match the pinned build")
        artifact = identity.get("artifact_sha256")
        if not isinstance(artifact, str) or artifact.upper() != self.rom_sha256:
            raise WorkerProtocolError("worker state slot artifact identity does not match this ROM/ELF")
        return identity

    def _validate_slot_reply(self, reply: dict[str, Any]) -> dict[str, Any]:
        slot = reply.get("slot")
        if not isinstance(slot, dict):
            raise WorkerProtocolError("worker reply is missing a state slot")
        handle = slot.get("handle")
        if not isinstance(handle, str) or not re.fullmatch(r"[0-9a-f]{%d}" % MAX_HANDLE_HEX, handle):
            raise WorkerProtocolError("worker returned an invalid state handle")
        name = slot.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,%d}" % MAX_STATE_NAME_BYTES, name):
            raise WorkerProtocolError("worker returned an invalid state name")
        size = slot.get("bytes")
        if type(size) is not int or not 0 < size <= MAX_STATE_TOTAL_BYTES:
            raise WorkerProtocolError("worker returned an invalid state size")
        if not isinstance(slot.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", slot["sha256"]):
            raise WorkerProtocolError("worker returned an invalid state digest")
        self._validate_state_identity(slot.get("identity"))
        return slot

    def save_checkpoint(self, name: str | None = None) -> dict[str, Any]:
        """Save an immutable named state slot; returns {handle, bytes, sha256, identity}."""
        fields: dict[str, Any] = {"artifact_sha256": self.rom_sha256.lower()}
        if name is not None:
            fields["name"] = self._validate_state_name(name)
        slot = self._validate_slot_reply(self._request("save_state", **fields))
        self._has_checkpoint = True
        return {"handle": slot["handle"], "name": slot["name"], "bytes": slot["bytes"],
                "sha256": slot["sha256"], "identity": slot["identity"]}

    def save_state(self) -> int:
        """Legacy single-slot save used by the query path; returns the byte size."""
        return self.save_checkpoint()["bytes"]

    def restore_state(self, handle: str) -> dict[str, Any]:
        return self._request("load_state", handle=self._validate_handle(handle),
                             artifact_sha256=self.rom_sha256.lower())

    def load_state(self) -> dict[str, Any]:
        """Legacy restore of the unnamed default slot."""
        return self._request("load_state", artifact_sha256=self.rom_sha256.lower())

    def drop_state(self, handle: str) -> dict[str, Any]:
        reply = self._request("drop_state", handle=self._validate_handle(handle))
        if reply.get("dropped") is not True:
            raise WorkerProtocolError("worker did not confirm the dropped state slot")
        return reply

    def list_states(self) -> dict[str, Any]:
        reply = self._request("list_states")
        states = reply.get("states")
        if not isinstance(states, list) or len(states) > MAX_STATE_SLOTS:
            raise WorkerProtocolError("worker returned an invalid state list")
        for slot in states:
            if not isinstance(slot, dict):
                raise WorkerProtocolError("worker returned a non-object state slot")
            handle = slot.get("handle")
            if not isinstance(handle, str) or not re.fullmatch(r"[0-9a-f]{%d}" % MAX_HANDLE_HEX, handle):
                raise WorkerProtocolError("worker returned an invalid state handle")
            if type(slot.get("bytes")) is not int or slot["bytes"] <= 0:
                raise WorkerProtocolError("worker returned an invalid state slot size")
        if type(reply.get("slot_limit")) is not int or reply["slot_limit"] != MAX_STATE_SLOTS:
            raise WorkerProtocolError("worker returned an unexpected state slot limit")
        if type(reply.get("memory_limit")) is not int or reply["memory_limit"] <= 0:
            raise WorkerProtocolError("worker returned an invalid state memory limit")
        return reply

    def export_state(self, handle: str) -> dict[str, Any]:
        """Export a slot as a portable base64 blob for an identical worker."""
        reply = self._request("export_state", handle=self._validate_handle(handle),
                              artifact_sha256=self.rom_sha256.lower())
        blob = self._validate_blob_text(reply.get("blob"))
        if reply.get("handle") != handle:
            raise WorkerProtocolError("worker returned a mismatched export handle")
        size = reply.get("bytes")
        if type(size) is not int or not 0 < size <= MAX_STATE_TOTAL_BYTES:
            raise WorkerProtocolError("worker returned an invalid exported state size")
        if not isinstance(reply.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", reply["sha256"]):
            raise WorkerProtocolError("worker returned an invalid exported state digest")
        blob_bytes = reply.get("blob_bytes")
        if type(blob_bytes) is not int or blob_bytes <= size:
            raise WorkerProtocolError("worker returned an invalid exported blob size")
        return {"handle": handle, "bytes": size, "sha256": reply["sha256"],
                "blob": blob, "blob_bytes": blob_bytes, "base64_bytes": len(blob)}

    def import_state(self, blob: str, name: str | None = None) -> dict[str, Any]:
        """Install a host-supplied blob as a new immutable slot without restoring it."""
        fields: dict[str, Any] = {"blob": self._validate_blob_text(blob),
                                  "artifact_sha256": self.rom_sha256.lower()}
        if name is not None:
            fields["name"] = self._validate_state_name(name)
        slot = self._validate_slot_reply(self._request("import_state", **fields))
        return {"handle": slot["handle"], "name": slot["name"], "bytes": slot["bytes"],
                "sha256": slot["sha256"], "identity": slot["identity"]}

    def load_state_blob(self, blob: str) -> dict[str, Any]:
        """Restore a host-supplied blob into the core without keeping a slot."""
        return self._request("load_state_blob", blob=self._validate_blob_text(blob),
                             artifact_sha256=self.rom_sha256.lower())

    @staticmethod
    def _validate_batch_reply(reply: dict[str, Any], payload_count: int, max_frames: int) -> list[dict[str, Any]]:
        turns = reply.get("turns")
        if not isinstance(turns, list) or len(turns) != payload_count:
            raise WorkerProtocolError("batch response has a mismatched turn count")
        for turn in turns:
            if not isinstance(turn, dict):
                raise WorkerProtocolError("batch response contains a non-object turn")
            events = turn.get("events")
            if (type(turn.get("complete")) is not bool or type(turn.get("frames")) is not int or
                    not 0 <= turn["frames"] <= max_frames or type(turn.get("trace_overflow")) is not bool or
                    ("elapsed_ms" in turn and (type(turn["elapsed_ms"]) not in (int, float) or turn["elapsed_ms"] < 0)) or
                    ("restore_ms" in turn and (type(turn["restore_ms"]) not in (int, float) or turn["restore_ms"] < 0)) or
                    not isinstance(events, list) or len(events) > 256 or
                    any(not isinstance(event, str) or len(event.encode("utf-8")) >= 512 for event in events) or
                    turn["trace_overflow"]):
                raise WorkerProtocolError("batch response contains an invalid or oversized turn")
            stop = turn.get("stop")
            marker = turn.get("stop_marker")
            if stop is not None:
                if stop not in STOP_KINDS or not isinstance(marker, str) or not marker.startswith("ROMTRACE|"):
                    raise WorkerProtocolError("batch response contains an invalid stop record")
            elif marker is not None:
                raise WorkerProtocolError("batch response has a stop marker without a stop kind")
            if not turn["complete"]:
                raise WorkerResponseError("incomplete_turn")
        return turns

    def batch_turns(
        self,
        address: int,
        payloads: Sequence[bytes],
        *,
        mailbox_bytes: int,
        engine_elf_sha256: str,
        deadline_ms: int,
        request_id: str,
        max_frames: int = 24_000,
        handle: str | None = None,
    ) -> dict[str, Any]:
        """Run bounded mailbox turns from one saved ready checkpoint.

        By default restores the unnamed ``default`` slot (legacy behaviour). Pass
        ``handle`` (from save_checkpoint/import_state) to restore a specific named
        slot instead, so named node handles are not clobbered by installs.
        """
        if handle is not None:
            handle = self._validate_handle(handle)
        elif not self._has_checkpoint:
            raise ValueError("save a pre-choice checkpoint before batch_turns")
        if type(address) is not int or not EWRAM_START <= address < EWRAM_END:
            raise ValueError("mailbox address must be within EWRAM")
        if type(mailbox_bytes) is not int or not 1 <= mailbox_bytes <= MAX_MEMORY_WRITE_BYTES:
            raise ValueError("mailbox_bytes is outside the bounded mailbox limit")
        if not isinstance(payloads, Sequence) or isinstance(payloads, (str, bytes)) or not 1 <= len(payloads) <= MAX_BATCH_TURNS:
            raise ValueError(f"payloads must contain 1 to {MAX_BATCH_TURNS} byte strings")
        if any(type(payload) is not bytes or len(payload) != mailbox_bytes or len(payload) > EWRAM_END - address for payload in payloads):
            raise ValueError("each payload must match the announced EWRAM mailbox length")
        if type(max_frames) is not int or not 1 <= max_frames <= MAX_FRAMES_PER_REQUEST:
            raise ValueError("max_frames is outside the worker limit")
        if type(deadline_ms) is not int:
            raise ValueError("deadline_ms must be an absolute monotonic millisecond integer")
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", request_id):
            raise ValueError("request_id must be 1 to 128 safe ASCII characters")
        if not isinstance(engine_elf_sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", engine_elf_sha256):
            raise ValueError("engine_elf_sha256 must be a 64-character SHA-256 hex digest")
        if engine_elf_sha256.upper() != self.rom_sha256:
            raise RomIdentityError("batch engine ELF identity differs from the artifact loaded by this worker")
        remaining = (deadline_ms - int(time.monotonic() * 1000)) / 1000
        if remaining <= 0:
            raise WorkerTimeoutError("batch deadline has expired")
        budget_ms = max(1, int(remaining * 1000))
        started = time.monotonic()
        fields: dict[str, Any] = {
            "address": address,
            "mailbox_bytes": mailbox_bytes,
            "budget_ms": budget_ms,
            "max_frames": max_frames,
            "payloads": "|".join(payload.hex() for payload in payloads),
            "request_id": request_id,
            "source_sha": SOURCE_SHA,
            "engine_elf_sha256": engine_elf_sha256.upper(),
            "worker_sha256": self.worker_sha256,
        }
        if handle is not None:
            fields["handle"] = handle
        try:
            reply = self._request(
                "batch_turns",
                _timeout=min(self._timeout, remaining),
                **fields,
            )
        except WorkerResponseError as exc:
            # rom_eval (package D, frozen) matches the historical snake_case code.
            if exc.code == "deadline-exceeded":
                raise WorkerResponseError("deadline_exceeded") from exc
            raise
        expected = {
            "request_id": request_id,
            "source_sha": SOURCE_SHA,
            "engine_elf_sha256": engine_elf_sha256.upper(),
            "worker_sha256": self.worker_sha256,
        }
        for key, value in expected.items():
            if reply.get(key) != value:
                raise WorkerProtocolError(f"batch response {key} mismatch")
        turns = self._validate_batch_reply(reply, len(payloads), max_frames)
        elapsed_ms = (time.monotonic() - started) * 1000
        timed_turns = [turn for turn in turns if "elapsed_ms" in turn and "restore_ms" in turn]
        return {"protocol_version": PROTOCOL_VERSION, **expected, "turns": turns,
                "elapsed_ms": elapsed_ms, "mean_turn_ms": elapsed_ms / len(turns),
                "ipc_request_bytes": reply["_request_wire_bytes"],
                "ipc_response_bytes": reply["_wire_bytes"],
                "transition_per_second": len(turns) / (elapsed_ms / 1000) if elapsed_ms else 0.0,
                "turn_p50_ms": _percentile([turn["elapsed_ms"] for turn in timed_turns], 0.50) if timed_turns else None,
                "turn_p95_ms": _percentile([turn["elapsed_ms"] for turn in timed_turns], 0.95) if timed_turns else None,
                "restore_p50_ms": _percentile([turn["restore_ms"] for turn in timed_turns], 0.50) if timed_turns else None,
                "restore_p95_ms": _percentile([turn["restore_ms"] for turn in timed_turns], 0.95) if timed_turns else None}

    def batch_turns_v3(
        self, address: int, result_address: int, result_capacity: int,
        payloads: Sequence[bytes], *, mailbox_bytes: int, engine_elf_sha256: str,
        deadline_ms: int, request_id: str, max_frames: int = 24_000,
        handle: str | None = None, mode: int = 0, critical_policy: int = 0,
    ) -> dict[str, Any]:
        """Run v9 mailboxes and return verified SDR2 results plus immutable children."""
        if self.wire_protocol != PROTOCOL_VERSION:
            raise WorkerProtocolError("batch_turns_v3 requires a wire-3 worker")
        if handle is not None:
            handle = self._validate_handle(handle)
        elif not self._has_checkpoint:
            raise ValueError("save a pre-choice checkpoint before batch_turns_v3")
        if type(address) is not int or not EWRAM_START <= address < EWRAM_END:
            raise ValueError("mailbox address must be within EWRAM")
        if type(result_address) is not int or not EWRAM_START <= result_address < EWRAM_END:
            raise ValueError("result address must be within EWRAM")
        if type(result_capacity) is not int or not 64 <= result_capacity <= EWRAM_END - result_address:
            raise ValueError("result capacity must fit the announced EWRAM buffer")
        if type(mailbox_bytes) is not int or not 1 <= mailbox_bytes <= MAX_MEMORY_WRITE_BYTES:
            raise ValueError("mailbox_bytes is outside the bounded mailbox limit")
        if address < result_address + result_capacity and result_address < address + mailbox_bytes:
            raise ValueError("result and mailbox ranges must not overlap")
        if not isinstance(payloads, Sequence) or isinstance(payloads, (str, bytes)) or not 1 <= len(payloads) <= MAX_BATCH_TURNS:
            raise ValueError(f"payloads must contain 1 to {MAX_BATCH_TURNS} byte strings")
        if any(type(payload) is not bytes or len(payload) != mailbox_bytes for payload in payloads):
            raise ValueError("each payload must match the announced v9 mailbox length")
        if type(mode) is not int or mode not in (0, 1) or type(critical_policy) is not int or critical_policy not in (0, 1):
            raise ValueError("mode and critical_policy must be 0 or 1")
        if type(max_frames) is not int or not 1 <= max_frames <= 24_000:
            raise ValueError("max_frames must be from 1 to 24000")
        if type(deadline_ms) is not int or deadline_ms <= int(time.monotonic() * 1000):
            raise WorkerTimeoutError("batch deadline has expired")
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", request_id):
            raise ValueError("request_id must be 1 to 128 safe ASCII characters")
        if not isinstance(engine_elf_sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", engine_elf_sha256):
            raise ValueError("engine_elf_sha256 must be a 64-character SHA-256 hex digest")
        if engine_elf_sha256.upper() != self.rom_sha256:
            raise RomIdentityError("batch engine ELF identity differs from the artifact loaded by this worker")
        remaining = (deadline_ms - int(time.monotonic() * 1000)) / 1000
        started = time.monotonic()
        fields: dict[str, Any] = {
            "address": address, "result_address": result_address,
            "result_capacity": result_capacity, "mailbox_bytes": mailbox_bytes,
            "mailbox_count": len(payloads), "mailboxes": base64.b64encode(b"".join(payloads)).decode("ascii"),
            "budget_ms": max(1, int(remaining * 1000)), "max_frames": max_frames,
            "request_id": request_id,
            "request_token": int.from_bytes(hashlib.sha256(request_id.encode("ascii")).digest()[:2], "little"),
            "source_sha": SOURCE_SHA, "engine_elf_sha256": engine_elf_sha256.upper(),
            "worker_sha256": self.worker_sha256, "mode": mode, "critical_policy": critical_policy,
        }
        if handle is not None:
            fields["handle"] = handle
        reply = self._request("batch_turns_v3", _timeout=min(self._timeout, remaining), **fields)
        expected = {"request_id": request_id, "source_sha": SOURCE_SHA,
                    "engine_elf_sha256": engine_elf_sha256.upper(), "worker_sha256": self.worker_sha256}
        for key, value in expected.items():
            if reply.get(key) != value:
                raise WorkerProtocolError(f"v3 batch response {key} mismatch")
        turns = reply.get("turns")
        if not isinstance(turns, list) or len(turns) != len(payloads):
            raise WorkerProtocolError("v3 batch response has a mismatched turn count")
        parsed = []
        turn_times = []
        save_times = []
        restore_times = []
        for turn in turns:
            if not isinstance(turn, dict) or type(turn.get("frames")) is not int or not 0 <= turn["frames"] <= max_frames:
                raise WorkerProtocolError("v3 batch contains an invalid frame count")
            result_text = turn.get("result")
            if not isinstance(result_text, str):
                raise WorkerProtocolError("v3 batch result is missing compact bytes")
            try:
                result_bytes = base64.b64decode(result_text, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise WorkerProtocolError("v3 batch result is not valid base64") from exc
            if len(result_bytes) > result_capacity:
                raise WorkerProtocolError("v3 batch result exceeds the announced result capacity")
            try:
                binary = sdr2.parse(result_bytes, request_id=fields["request_token"], mode=mode,
                                    critical_policy=critical_policy, capacity=result_capacity)
            except sdr2.SDR2Error as exc:
                raise WorkerResponseError(exc.code) from exc
            for key in ("elapsed_ms", "save_ms", "restore_ms"):
                if type(turn.get(key)) not in (int, float) or turn[key] < 0:
                    raise WorkerProtocolError(f"v3 batch turn has invalid {key}")
            turn_times.append(float(turn["elapsed_ms"]))
            save_times.append(float(turn["save_ms"]))
            restore_times.append(float(turn["restore_ms"]))
            status = binary["header"]["status"]
            if status != 0:
                error = (binary["records"].get("unsupported") or [{}])[0]
                reason, source = error.get("reason", 0), error.get("source", 0)
                code = V3_RESULT_ERROR_CODES.get((status, reason, source))
                if code is None:
                    code = "binary-result-status-%d-reason-%d-source-%d" % (status, reason, source)
                raise WorkerResponseError(code)
            child = turn.get("child")
            if binary["header"]["boundary"] == sdr2.BOUNDARY_TERMINAL:
                if child is not None:
                    raise WorkerProtocolError("terminal result must not have a child state")
            else:
                if not isinstance(child, dict):
                    raise WorkerProtocolError("nonterminal result has no saved child state")
                if (not isinstance(child.get("handle"), str) or
                        not re.fullmatch(r"[0-9a-f]{32}", child["handle"]) or
                        type(child.get("bytes")) is not int or child["bytes"] <= 0 or
                        not isinstance(child.get("sha256"), str) or
                        not re.fullmatch(r"[0-9a-f]{64}", child["sha256"])):
                    raise WorkerProtocolError("child state summary is malformed")
                self._validate_state_identity(child.get("identity"))
                child = {**child, "owner_worker": self.worker_id, "portable": True}
            parsed.append({**turn, "binary": binary, "child": child})
        elapsed_ms = (time.monotonic() - started) * 1000
        return {"protocol_version": PROTOCOL_VERSION, **expected, "turns": parsed,
                "ipc_request_bytes": len(json.dumps({"protocol": 3, "op": "batch_turns_v3", **fields},
                                                     separators=(",", ":")).encode("utf-8")) + 1,
                "ipc_response_bytes": reply["_wire_bytes"], "elapsed_ms": elapsed_ms,
                "mean_turn_ms": elapsed_ms / len(parsed),
                "transition_per_second": len(parsed) / (elapsed_ms / 1000) if elapsed_ms else 0.0,
                "turn_p50_ms": _percentile(turn_times, 0.50),
                "turn_p95_ms": _percentile(turn_times, 0.95),
                "save_p50_ms": _percentile(save_times, 0.50),
                "save_p95_ms": _percentile(save_times, 0.95),
                "restore_p50_ms": _percentile(restore_times, 0.50),
                "restore_p95_ms": _percentile(restore_times, 0.95)}

    def read_memory(self, address: int, width: int = 4) -> int:
        if type(address) is not int or not 0 <= address <= 0xFFFFFFFF:
            raise ValueError("address must be an unsigned 32-bit integer")
        if type(width) is not int or width not in (1, 2, 4):
            raise ValueError("width must be 1, 2, or 4")
        reply = self._request("read_memory", address=address, width=width)
        if (type(reply.get("address")) is not int or reply["address"] != address or
                type(reply.get("width")) is not int or reply["width"] != width):
            raise WorkerProtocolError("worker returned a mismatched memory address or width")
        value = reply.get("value")
        if type(value) is not int or not 0 <= value < (1 << (8 * width)):
            raise WorkerProtocolError("worker returned an invalid memory value")
        return value

    @staticmethod
    def _memory_region_end(address: int, length: int) -> int:
        if type(address) is not int:
            raise ValueError("address must be an unsigned 32-bit integer")
        if EWRAM_START <= address < EWRAM_END:
            end = EWRAM_END
        elif IWRAM_START <= address < IWRAM_END:
            end = IWRAM_END
        else:
            raise ValueError("address must be within EWRAM (0x02000000..0x0203ffff) or IWRAM (0x03000000..0x03007fff)")
        if type(length) is not int or length <= 0 or length > end - address:
            raise ValueError("memory range must stay inside a single mapped region")
        return end

    def read_memory_block(self, address: int, length: int = 16) -> bytes:
        """Read a bounded EWRAM/IWRAM block; length is capped at MAX_MEMORY_BLOCK_BYTES."""
        if length > MAX_MEMORY_BLOCK_BYTES:
            raise ValueError(f"length must be at most {MAX_MEMORY_BLOCK_BYTES} bytes")
        self._memory_region_end(address, length)
        reply = self._request("read_memory_block", address=address, length=length)
        if reply.get("address") != address or reply.get("length") != length:
            raise WorkerProtocolError("worker returned a mismatched memory block range")
        data = reply.get("data")
        if not isinstance(data, str) or len(data) != length * 2 or re.fullmatch(r"[0-9a-f]*", data) is None:
            raise WorkerProtocolError("worker returned invalid memory block data")
        return bytes.fromhex(data)

    def set_keys(self, keys: int) -> dict[str, Any]:
        """Hold a GBA key mask (active high) for later run_frames calls."""
        validated = self._validate_keys(keys)
        reply = self._request("set_keys", keys=validated)
        if reply.get("keys") != validated:
            raise WorkerProtocolError("worker did not acknowledge the key mask")
        return reply

    def write_memory(self, address: int, data: bytes) -> dict[str, Any]:
        if type(data) is not bytes or not 1 <= len(data) <= MAX_MEMORY_WRITE_BYTES:
            raise ValueError("data must be 1 to 256 bytes")
        self._memory_region_end(address, len(data))
        return self._request("write_memory", address=address, data=data.hex())

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._process.poll() is None:
                try:
                    self._process.stdin.write(
                        json.dumps({"protocol": self.wire_protocol, "op": "quit"}) + "\n"
                    )
                    self._process.stdin.flush()
                    _parse_reply(self._read_line(), self.wire_protocol)
                    self._process.wait(timeout=self._timeout)
                except (OSError, RomBridgeError, subprocess.TimeoutExpired):
                    self._terminate()
            self._closed = True
            for stream in (self._process.stdin, self._process.stdout, self._process.stderr):
                if stream:
                    stream.close()

    def _terminate(self) -> None:
        if getattr(self, "_process", None) is None:
            return
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=1)
        self._closed = True
        for stream in (self._process.stdin, self._process.stdout, self._process.stderr):
            if stream and not stream.closed:
                stream.close()

    def __enter__(self) -> "RomWorkerClient":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()
