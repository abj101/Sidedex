"""Background Calc evaluation: debounce, two-second budget, stale-result safety.

Owned by the orchestrator. UI code calls request()/poll() and never blocks.
Import of battle_engine is lazy so the UI keeps working before the engine lands.
"""

import hashlib
import json
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

BUDGET_SECONDS = 2.0
DEBOUNCE_SECONDS = 0.35
CACHE_LIMIT = 32
MAX_WORKERS = 2

_EXECUTOR = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="calc")
_LOCK = threading.Lock()
_CACHE = OrderedDict()          # key -> result
_INFLIGHT = {}                  # key -> Future
_TIMERS = {}                    # key -> threading.Timer (pending debounce)
_LATEST = {"key": None, "ms": None}
_CURRENT = {"key": None}        # newest requested key, read by workers


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


def snapshot_key(snapshot):
    """Canonical key: normalized snapshot + source SHA + engine version."""
    engine, _ = _safe_engine()
    try:
        canonical = engine.normalize_snapshot(snapshot) if engine else snapshot
        payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str)
        salt = (getattr(engine, "ENGINE_VERSION", "0"), getattr(engine, "SOURCE_SHA", ""))
    except Exception as exc:
        payload = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str)
        salt = ("unnormalized", f"{type(exc).__name__}: {exc}")
    return hashlib.sha256((repr(salt) + payload).encode("utf-8")).hexdigest()


def cached(snapshot):
    """Return the stored result for exactly this snapshot, or None."""
    key = snapshot_key(snapshot)
    with _LOCK:
        if key not in _CACHE:
            return None
        _CACHE.move_to_end(key)
        return _CACHE[key]


def invalidate():
    with _LOCK:
        _CACHE.clear()
        for timer in _TIMERS.values():
            timer.cancel()
        _TIMERS.clear()
        _LATEST["key"] = None
        _LATEST["ms"] = None


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
        future = _EXECUTOR.submit(_run, key, snapshot, game_data)
        _INFLIGHT[key] = future


def _run(key, snapshot, game_data):
    # The budget starts when work actually starts, so a queued job still gets
    # its full two seconds. ponytail: queued work is not cancelled, only ignored.
    started = time.monotonic()
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
        _CACHE[key] = result
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
        future = _INFLIGHT.get(key)
    if future is not None and future.done():
        with _LOCK:
            return _CACHE.get(key)
    return None


def status():
    with _LOCK:
        return {"running": len(_INFLIGHT), "pending_debounce": len(_TIMERS),
                "cached": len(_CACHE), "last_key": _LATEST["key"], "last_ms": _LATEST["ms"]}
