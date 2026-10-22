"""Shared, process-global Podio rate-limit guard + per-path request counter.

Blagojche 20.10 (#1). matching_api._podio_request AND direct_wholesaler._podio_request (which share the
SAME Podio OAuth app, so they share the 250/hr limit) call podio_allow(path) before every api.podio.com
call. Non-blocking: when the process is over the hourly budget, podio_allow returns False and the caller
treats it like a failed call (returns None -> fail-closed for opt-out, keep-pending for matching). This
keeps the WHOLE process under the cap instead of hammering Podio into HTTP 420 (which is what rate-limited
the opt-out lookups on 19.10). A per-path counter logs the hourly totals so "calls per job per hour" is a
direct read from the logs.
"""
import os
import re
import time
import logging
import threading
from collections import deque, defaultdict

_LOG = logging.getLogger("podio.throttle")
_LOCK = threading.Lock()

# rolling 1-hour window of accepted-call timestamps (epoch seconds)
_CALLS: "deque[float]" = deque()
# per-normalised-path attempt counts for the current clock hour
_PER_PATH: "defaultdict[str, int]" = defaultdict(int)
_HOUR_BUCKET = [None]  # current clock hour = int(now // 3600)

_WINDOW = 3600.0
# default 200/hr leaves headroom under Podio's 250/hr for opt-out + special-avails
_MAX_PER_HOUR = int(os.getenv("PODIO_MAX_CALLS_PER_HOUR", "200"))

_ID_RE = re.compile(r"/\d{3,}")


def is_read_path(method: str, path: str) -> bool:
    """True for a Podio READ (safe to throttle/drop - caller retries or fails closed). We gate ONLY
    reads so a WRITE (PUT/POST create/comment) is never dropped under budget and silently lost.
    Reads = any GET, plus the POST-based /search and /filter endpoints (the 420 storm was /search)."""
    m = (method or "").upper()
    if m == "GET":
        return True
    p = path or ""
    return "/search/" in p or p.endswith("/filter") or "/filter?" in p or "/filter/" in p


def _norm(path: str) -> str:
    """/item/123456/value/7 -> /item/{id}/value/{id} so the counter groups by endpoint."""
    return _ID_RE.sub("/{id}", path or "")


def _roll_hour_locked(now: float) -> None:
    hb = int(now // 3600)
    if _HOUR_BUCKET[0] != hb:
        if _HOUR_BUCKET[0] is not None and _PER_PATH:
            top = dict(sorted(_PER_PATH.items(), key=lambda kv: -kv[1]))
            _LOG.info("[podio-throttle] last-hour calls by path (total=%d, cap=%d): %s",
                      sum(_PER_PATH.values()), _MAX_PER_HOUR, top)
        _PER_PATH.clear()
        _HOUR_BUCKET[0] = hb


def podio_allow(path: str = "") -> bool:
    """Record and gate one Podio call. True = within the hourly budget (call is counted, proceed).
    False = over budget this hour; caller must NOT make the call (treat like a failed request).
    Non-blocking by design so it never stalls the 1-minute scheduler jobs."""
    p = _norm(path)
    with _LOCK:
        now = time.time()
        while _CALLS and now - _CALLS[0] >= _WINDOW:
            _CALLS.popleft()
        _roll_hour_locked(now)
        _PER_PATH[p] += 1  # count attempts (allowed + dropped) for visibility
        if len(_CALLS) < _MAX_PER_HOUR:
            _CALLS.append(now)
            return True
    _LOG.warning("[podio-throttle] over %d/hr cap - dropping Podio call to %s", _MAX_PER_HOUR, p)
    return False


def podio_throttle_stats() -> dict:
    """Read-only snapshot for a dry run / health check."""
    with _LOCK:
        now = time.time()
        while _CALLS and now - _CALLS[0] >= _WINDOW:
            _CALLS.popleft()
        return {"in_window": len(_CALLS), "cap": _MAX_PER_HOUR,
                "per_path_this_hour": dict(_PER_PATH)}
