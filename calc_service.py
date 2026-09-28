"""Background Calc evaluation with legacy and ROM request isolation.

Owned by the orchestrator. UI code calls request()/poll() and never blocks.
Import of battle_engine is lazy so the UI keeps working before the engine lands.
"""

import hashlib
import json
import os
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BUDGET_SECONDS = 2.0
DEBOUNCE_SECONDS = 0.35
CACHE_LIMIT = 32
MAX_WORKERS = 2

_EXECUTOR = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="calc")
_LOCK = threading.Lock()
_CACHE = OrderedDict()          # key -> result
_PARTIAL = {}                   # key -> latest published refinement
_DISK_CACHE = OrderedDict()     # last two completed ROM snapshots
_DISK_LOADED = False
_INFLIGHT = {}                  # key -> Future
_TIMERS = {}                    # key -> threading.Timer (pending debounce)
_LATEST = {"key": None, "ms": None}
_CURRENT = {"key": None}        # newest requested key, read by workers
_GENERATION = {"value": 0}       # invalidate fences older in-flight jobs
_ROOT = Path(__file__).parent
_DEFAULT_ELF = _ROOT / "native" / "rom_worker" / "build" / "mingw" / "pokeemerald-test-headless.elf"
_DISK_PATH = _ROOT / "native" / "rom_worker" / "build" / "calc_cache.json"


def _elf_sha256():
    try:
        import rom_bridge
        return rom_bridge.ENGINE_ELF_SHA256
    except Exception:
        return "unknown"


def _missing_artifact_detail(detail):
    return (f"{detail} Setup: copy the pinned {_DEFAULT_ELF.as_posix()} "
            "file (pokeemerald-test-headless.elf) "
            f"(SHA-256 {_elf_sha256()}) to native/rom_worker/build/mingw/ "
            "or set SIDEDEX_ENGINE_ELF_PATH, then run native/rom_worker/build.ps1; "
            "see tools/rom_oracle/README.md.")


def _engine():
    try:
        import battle_engine
    except Exception as exc:  # engine missing or broken must not break the page
        return None, f"{type(exc).__name__}: {exc}"
    return battle_engine, None


def _safe_engine():
    try:
        return _engine()
    except Exception as exc:  # a broken engine must never take the page down
        return None, f"{type(exc).__name__}: {exc}"


def _unavailable(reason, detail):
    return {"status": "unavailable", "reason": reason, "detail": detail,
            "missing": [], "mechanics": []}


def _rom_unavailable(reason, detail, source_sha):
    card = _unavailable(reason, detail)
    result = {kind: dict(card) for kind in
              ("player_moves", "enemy_moves", "ai_actions", "recommendation")}
    result.update(status="unavailable", source_sha=source_sha)
    return result


def snapshot_key(snapshot):
    """Canonical key including the relevant evaluator identity.

    A v2 snapshot is a ROM request and is keyed by the full contract section-1
    `TransitionIdentity` tuple (source, test ELF, worker + version, mGBA,
    wire/ROM-hook protocol, save format, state/decode schemas, execution mode,
    critical policy, reserved planner identity). Opaque worker-local handles are
    never part of the snapshot, so nothing worker-local is cached across a
    process restart.
    """
    if snapshot.get("schema_version") == 2:
        # v2 is a ROM request. Never normalize it through the legacy Python
        # engine, which only understands v1 and loses the second RNG stream.
        try:
            import rom_transition
            identity = tuple(sorted(rom_transition.transition_identity(
                execution_mode="normal", critical_policy="item-ability-only").items()))
        except Exception:
            import rom_bridge
            identity = (("test_elf_sha256", getattr(rom_bridge, "ENGINE_ELF_SHA256", None)),
                        ("worker_sha256", getattr(rom_bridge, "WORKER_SHA256", None)),
                        ("worker_version", getattr(rom_bridge, "WORKER_VERSION", None)))
        payload = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(repr(("rom-v5", snapshot.get("source_sha"), identity, payload)).encode("utf-8")).hexdigest()
    engine, _ = _safe_engine()
    try:
        canonical = engine.normalize_snapshot(snapshot) if engine else snapshot
        payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str)
        salt = (getattr(engine, "ENGINE_VERSION", "0"), getattr(engine, "SOURCE_SHA", ""))
    except Exception as exc:
        payload = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str)
        salt = ("unnormalized", f"{type(exc).__name__}: {exc}")
    return hashlib.sha256((repr(salt) + payload).encode("utf-8")).hexdigest()


def _load_disk():
    global _DISK_LOADED
    if _DISK_LOADED:
        return
    _DISK_LOADED = True
    try:
        rows = json.loads(_DISK_PATH.read_text(encoding="utf-8"))
        if isinstance(rows, list):
            for row in rows[-2:]:
                if isinstance(row, dict) and isinstance(row.get("key"), str) and isinstance(row.get("result"), dict):
                    _DISK_CACHE[row["key"]] = row["result"]
    except (OSError, ValueError):
        pass


def _save_disk(key, result):
    _load_disk()
    _DISK_CACHE[key] = result
    _DISK_CACHE.move_to_end(key)
    while len(_DISK_CACHE) > 2:
        _DISK_CACHE.popitem(last=False)
    try:
        _DISK_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp = _DISK_PATH.with_suffix(".tmp")
        temp.write_text(json.dumps([{"key": k, "result": v} for k, v in _DISK_CACHE.items()],
                                   separators=(",", ":")), encoding="utf-8")
        os.replace(temp, _DISK_PATH)
    except (OSError, TypeError, ValueError):
        pass


def cached(snapshot):
    """Return the stored result for exactly this snapshot, or None."""
    key = snapshot_key(snapshot)
    with _LOCK:
        if snapshot.get("schema_version") == 2 and key not in _CACHE:
            _load_disk()
            if key in _DISK_CACHE:
                _CACHE[key] = _DISK_CACHE[key]
        if key not in _CACHE:
            return None
        _CACHE.move_to_end(key)
        return _CACHE[key]


def invalidate():
    global _DISK_LOADED
    with _LOCK:
        _GENERATION["value"] += 1
        _CACHE.clear()
        _PARTIAL.clear()
        _DISK_CACHE.clear()
        _DISK_LOADED = True
        try:
            _DISK_PATH.unlink(missing_ok=True)
        except OSError:
            pass
        for timer in _TIMERS.values():
            timer.cancel()
        _TIMERS.clear()
        _LATEST["key"] = None
        _LATEST["ms"] = None
        _CURRENT["key"] = None


def request(snapshot, game_data):
    """Ask for an evaluation of this snapshot. Non-blocking, debounced."""
    try:
        key = snapshot_key(snapshot)
    except (TypeError, ValueError):
        return
    with _LOCK:
        _CURRENT["key"] = key
        if key in _CACHE or key in _INFLIGHT or key in _TIMERS:
            return
        for other, timer in list(_TIMERS.items()):
            timer.cancel()
            _TIMERS.pop(other, None)
        timer = threading.Timer(DEBOUNCE_SECONDS, _submit, args=(key, snapshot, game_data))
        timer.daemon = True
        _TIMERS[key] = timer
        timer.start()


def _submit(key, snapshot, game_data):
    with _LOCK:
        _TIMERS.pop(key, None)
        if _CURRENT["key"] != key or key in _CACHE or key in _INFLIGHT:
            return
        future = _EXECUTOR.submit(_run, key, snapshot, game_data, _GENERATION["value"])
        _INFLIGHT[key] = future


def _run(key, snapshot, game_data, generation=None):
    # The budget starts when work actually starts, so a queued job still gets
    # its full two seconds. ponytail: queued work is not cancelled, only ignored.
    started = time.monotonic()
    if generation is None:
        with _LOCK:
            generation = _GENERATION["value"]
    def publish(partial):
        if not isinstance(partial, dict):
            return
        with _LOCK:
            if (_CURRENT["key"] == key and _GENERATION["value"] == generation
                    and key not in _CACHE):
                _PARTIAL[key] = partial

    if snapshot.get("schema_version") == 2:
        source_sha = snapshot.get("source_sha")
        if source_sha != game_data.get("source", {}).get("commit"):
            result = _rom_unavailable("source-mismatch",
                                      "The battle state uses a different pinned source commit.",
                                      source_sha)
        else:
            try:
                import rom_eval
                evaluate = getattr(rom_eval, "evaluate", None)
                if evaluate is None:
                    raise RuntimeError("compiled-engine turn evaluator is not implemented")
                result = evaluate(
                    snapshot, game_data, deadline=started + 10.0,
                    cancelled=lambda: _CURRENT["key"] != key,
                    publish=publish,
                )
                if not isinstance(result, dict):
                    raise TypeError("ROM evaluator did not return a result object")
                if any(result.get(kind, {}).get("status") not in
                       {"engine-exact", "engine-fixed-seed", "verified-fixed-seed", "estimated", "unavailable"} for kind in
                       ("player_moves", "enemy_moves", "ai_actions", "recommendation")):
                    raise ValueError("ROM evaluator returned an invalid card status")
            except Exception as exc:
                detail = f"Compiled engine unavailable: {type(exc).__name__}: {exc}"
                reason = ("timeout" if type(exc).__name__ == "WorkerTimeoutError" else
                          "missing-artifact" if isinstance(exc, FileNotFoundError) or "is missing" in str(exc) else
                          "artifact-mismatch" if type(exc).__name__ == "RomIdentityError" else
                          "engine-error")
                if reason == "missing-artifact":
                    detail = _missing_artifact_detail(detail)
                result = _rom_unavailable(reason, detail, source_sha)
    else:
        engine, error = _safe_engine()
        if engine is None:
            result = _unavailable("engine-error", f"battle engine unavailable: {error}")
        else:
            try:
                result = engine.evaluate_head_to_head(snapshot, game_data,
                                                      deadline=started + BUDGET_SECONDS)
                if not isinstance(result, dict):
                    raise TypeError("engine returned %s" % type(result).__name__)
            except Exception as exc:  # engine bugs must not take the page down
                result = _unavailable("engine-error", f"battle engine raised {type(exc).__name__}: {exc}")
    elapsed_ms = int((time.monotonic() - started) * 1000)
    with _LOCK:
        _PARTIAL.pop(key, None)
        if _GENERATION["value"] == generation:
            _CACHE[key] = result
            if snapshot.get("schema_version") == 2 and result.get("status") != "unavailable":
                _save_disk(key, result)
            _CACHE.move_to_end(key)
            while len(_CACHE) > CACHE_LIMIT:
                _CACHE.popitem(last=False)
        _INFLIGHT.pop(key, None)
        _LATEST["key"] = key
        _LATEST["ms"] = elapsed_ms
    return result


def poll(snapshot):
    """Result for exactly this snapshot, else None. Stale keys never render."""
    key = snapshot_key(snapshot)
    with _LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return _CACHE[key]
        if key in _PARTIAL:
            return _PARTIAL[key]
        future = _INFLIGHT.get(key)
    if future is not None and future.done():
        with _LOCK:
            return _CACHE.get(key)
    return None


def status():
    with _LOCK:
        return {"running": len(_INFLIGHT), "pending_debounce": len(_TIMERS),
                "cached": len(_CACHE), "refining": len(_PARTIAL),
                "last_key": _LATEST["key"], "last_ms": _LATEST["ms"]}
