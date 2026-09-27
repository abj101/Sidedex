"""Strict low-level client for the persistent mGBA worker.

This module exposes emulator primitives only. It does not interpret game state
or calculate battle outcomes.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import subprocess
import threading
from pathlib import Path
from typing import Any, Sequence


PROTOCOL_VERSION = 1
WORKER_VERSION = "sidedex-mgba-worker/0.1.0"
MGBA_VERSION = "0.10.5+26b7884"
SAMPLER_VERSION = None
SOURCE_SHA = "2576d0f3f4c861c760f0c55198e37e6d7b91683e"
# Gate 0 artifact: default source pin and build configuration, SHA-256.
ROM_SHA256 = "0CCAAAD0878A9CBFB36FBEFFF800E9B6DD66055924B7595038EDE1E8771EC5A2"

ROOT = Path(__file__).resolve().parent
DEFAULT_SOURCE_LOCK = ROOT / "data" / "source_lock.json"
DEFAULT_WORKER = ROOT / "native" / "rom_worker" / "build" / "mingw" / "rom_worker.exe"
MAX_FRAMES_PER_REQUEST = 100_000
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

    def _read_line(self) -> str:
        try:
            line = self._stdout_lines.get(timeout=self._timeout)
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

    def _request(self, op: str, **fields) -> dict[str, Any]:
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
            reply = _parse_reply(self._read_line())
            if op in {"status", "run_frames", "reset", "load_state"}:
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
        return size

    def load_state(self) -> dict[str, Any]:
        return self._request("load_state")

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
