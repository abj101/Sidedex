"""Strict low-level client for the persistent mGBA worker.

This module exposes emulator primitives only. It does not interpret game state
or calculate battle outcomes.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Sequence


PROTOCOL_VERSION = 1
WORKER_VERSION = "sidedex-mgba-worker/0.1.2"
MGBA_VERSION = "0.10.5+26b7884"
SAMPLER_VERSION = None
SOURCE_SHA = "2576d0f3f4c861c760f0c55198e37e6d7b91683e"
# Gate 0 artifact: default source pin and build configuration, SHA-256.
ROM_SHA256 = "0CCAAAD0878A9CBFB36FBEFFF800E9B6DD66055924B7595038EDE1E8771EC5A2"
ENGINE_ELF_SHA256 = "AF4DC9B68E02D2B377FB0EB1FE38765158E2F9D55174369990FA26827512CE71"
WORKER_SHA256 = "B49A8DE1BECACCC6B0CB5B670D25105BA5769EA4FE0BDF9F0A29F1D1B52978C9"

ROOT = Path(__file__).resolve().parent
DEFAULT_SOURCE_LOCK = ROOT / "data" / "source_lock.json"
DEFAULT_WORKER = ROOT / "native" / "rom_worker" / "build" / "mingw" / "rom_worker.exe"
MAX_FRAMES_PER_REQUEST = 100_000
EWRAM_START = 0x02000000
EWRAM_END = 0x02040000
MAX_MEMORY_WRITE_BYTES = 2048
MAX_BATCH_TURNS = 8
# 256 bounded trace events (up to 510 bytes each, JSON escaped) fit below 1 MiB.
MAX_MESSAGE_BYTES = 1_048_576


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


def _parse_reply(line: str) -> dict[str, Any]:
    if len(line.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise WorkerProtocolError("worker response exceeded the message limit")
    try:
        value = json.loads(line, object_pairs_hook=_strict_object, parse_constant=_reject_constant)
    except (json.JSONDecodeError, UnicodeError, ValueError) as exc:
        raise WorkerProtocolError(f"worker returned invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise WorkerProtocolError("worker response must be a JSON object")
    if type(value.get("protocol")) is not int or value["protocol"] != PROTOCOL_VERSION:
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


class RomWorkerClient:
    """One serialized request stream connected to a persistent mGBA process."""

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
    ):
        if type(timeout) not in (int, float) or timeout <= 0:
            raise ValueError("timeout must be a positive number")
        worker = Path(worker_path).expanduser().resolve()
        rom = Path(rom_path).expanduser().resolve()
        self._timeout = float(timeout)
        self._lock = threading.Lock()
        self._closed = False
        self._stderr_lines: queue.Queue = queue.Queue(maxsize=16)

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
        reply = _parse_reply(self._read_line())
        required = {
            "worker": WORKER_VERSION,
            "mgba": MGBA_VERSION,
            "platform": "gba",
        }
        for key, expected in required.items():
            if reply.get(key) != expected:
                raise WorkerProtocolError(f"worker {key} does not match the pinned build")
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
            request = {"protocol": PROTOCOL_VERSION, "op": op, **fields}
            wire = json.dumps(request, separators=(",", ":"), allow_nan=False)
            if len(wire.encode("utf-8")) + 1 > MAX_MESSAGE_BYTES:
                raise ValueError("request exceeds the worker message limit")
            try:
                self._process.stdin.write(wire + "\n")
                self._process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise WorkerExitedError("mGBA worker closed its input pipe") from exc
            reply = _parse_reply(self._read_line(_timeout))
            if op in {"status", "run_frames", "reset", "load_state", "write_memory"}:
                self._status = self._validate_status_reply(reply)
            return reply

    def _validate_status_reply(self, reply: dict[str, Any]) -> dict[str, Any]:
        if reply.get("worker") != WORKER_VERSION or reply.get("mgba") != MGBA_VERSION:
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

    def run_frames(self, frames: int) -> dict[str, Any]:
        if type(frames) is not int or not 0 <= frames <= MAX_FRAMES_PER_REQUEST:
            raise ValueError(f"frames must be an integer from 0 to {MAX_FRAMES_PER_REQUEST}")
        reply = self._request("run_frames", frames=frames)
        events = reply.get("trace_events")
        if (not isinstance(events, list) or any(not isinstance(event, str) for event in events) or
                type(reply.get("trace_overflow")) is not bool):
            raise WorkerProtocolError("worker returned invalid trace event data")
        if reply["trace_overflow"]:
            raise WorkerProtocolError("worker trace event buffer overflowed")
        return reply

    def reset(self) -> dict[str, Any]:
        return self._request("reset")

    def save_state(self) -> int:
        reply = self._request("save_state")
        size = reply.get("state_bytes")
        if type(size) is not int or size <= 0:
            raise WorkerProtocolError("worker returned an invalid saved state size")
        self._has_checkpoint = True
        return size

    def load_state(self) -> dict[str, Any]:
        return self._request("load_state")

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
    ) -> dict[str, Any]:
        """Run bounded mailbox turns from one saved pre-choice checkpoint."""
        if not self._has_checkpoint:
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
        reply = self._request(
            "batch_turns",
            _timeout=min(self._timeout, remaining),
            address=address,
            mailbox_bytes=mailbox_bytes,
            budget_ms=budget_ms,
            max_frames=max_frames,
            payloads="|".join(payload.hex() for payload in payloads),
            request_id=request_id,
            source_sha=SOURCE_SHA,
            engine_elf_sha256=engine_elf_sha256.upper(),
            worker_sha256=self.worker_sha256,
        )
        expected = {
            "request_id": request_id,
            "source_sha": SOURCE_SHA,
            "engine_elf_sha256": engine_elf_sha256.upper(),
            "worker_sha256": self.worker_sha256,
        }
        for key, value in expected.items():
            if reply.get(key) != value:
                raise WorkerProtocolError(f"batch response {key} mismatch")
        turns = reply.get("turns")
        if not isinstance(turns, list) or len(turns) != len(payloads):
            raise WorkerProtocolError("batch response has a mismatched turn count")
        for turn in turns:
            events = turn.get("events") if isinstance(turn, dict) else None
            if (type(turn.get("complete")) is not bool or type(turn.get("frames")) is not int or
                    not 0 <= turn["frames"] <= max_frames or type(turn.get("trace_overflow")) is not bool or
                    not isinstance(events, list) or len(events) > 256 or
                    any(not isinstance(event, str) or len(event.encode("utf-8")) >= 512 for event in events) or
                    turn["trace_overflow"]):
                raise WorkerProtocolError("batch response contains an invalid or oversized turn")
            if not turn["complete"]:
                raise WorkerResponseError("incomplete_turn")
        elapsed_ms = (time.monotonic() - started) * 1000
        return {"protocol_version": 3, **expected, "turns": turns,
                "elapsed_ms": elapsed_ms, "mean_turn_ms": elapsed_ms / len(turns)}

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

    def write_memory(self, address: int, data: bytes) -> dict[str, Any]:
        if type(address) is not int or not EWRAM_START <= address < EWRAM_END:
            raise ValueError("address must be within EWRAM (0x02000000..0x0203ffff)")
        if type(data) is not bytes or not 1 <= len(data) <= MAX_MEMORY_WRITE_BYTES:
            raise ValueError("data must be 1 to 256 bytes")
        if len(data) > EWRAM_END - address:
            raise ValueError("write must stay within EWRAM (0x02000000..0x0203ffff)")
        return self._request("write_memory", address=address, data=data.hex())

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._process.poll() is None:
                try:
                    self._process.stdin.write(
                        json.dumps({"protocol": PROTOCOL_VERSION, "op": "quit"}) + "\n"
                    )
                    self._process.stdin.flush()
                    _parse_reply(self._read_line())
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
