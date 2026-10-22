"""IP allowlist for server 01 write routes (Blagojche/Claude 2026-10-20).

Guards every POST/PUT/PATCH/DELETE except the public buyer form (/api/buyer-submissions*) and the
RingCentral media webhook (/rc/webhook/*). Reads (GET) are not guarded here.
Client address = request.client.host. uvicorn (0.49, proxy_headers on, forwarded_allow_ips=127.0.0.1)
only rewrites it from X-Forwarded-For for requests that come through the local nginx, using the
right-most untrusted hop, so a direct caller on :8000 cannot fake it with a header.

Env: S01_ALLOWLIST_MODE=off|log|enforce (default off), S01_ALLOWLIST_EXTRA=ip,ip (quick additions).
Never switches a route off: a blocked call gets 403 and one log line "[allowlist] BLOCK ip method path".
"""
import logging
import os

from starlette.responses import JSONResponse

BASE_IPS = {
    "127.0.0.1", "::1",                 # local processes + nginx (form.wholesaledealfinder.ai)
    "100.51.131.116", "172.31.47.123",          # this box (public, private)
    "3.90.20.111", "172.31.40.142",     # server 02 (public, private)
    "108.128.182.146", "54.171.83.63", "3.248.131.28",  # GlobiFlow egress
}
EXEMPT_PREFIXES = ("/api/buyer-submissions", "/rc/webhook/")
GUARDED_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
log = logging.getLogger("ip_allowlist")


def _ips():
    extra = {x.strip() for x in (os.getenv("S01_ALLOWLIST_EXTRA") or "").split(",") if x.strip()}
    return BASE_IPS | extra


def needs_check(method: str, path: str) -> bool:
    return method.upper() in GUARDED_METHODS and not path.startswith(EXEMPT_PREFIXES)


def decide(mode: str, ips: set, method: str, path: str, host: str) -> str:
    """Return 'pass', 'log' or 'block'."""
    if mode not in ("log", "enforce") or not needs_check(method, path):
        return "pass"
    if host.startswith("::ffff:"):
        host = host[7:]
    if host in ips:
        return "pass"
    return "log" if mode == "log" else "block"


def install(app):
    mode = (os.getenv("S01_ALLOWLIST_MODE") or "off").strip().lower()
    ips = _ips()
    print(f"[allowlist] mode={mode} ips={len(ips)}", flush=True)

    @app.middleware("http")
    async def _ip_allowlist(request, call_next):
        host = (request.client.host if request.client else "") or ""
        d = decide(mode, ips, request.method, request.url.path, host)
        if d == "pass":
            return await call_next(request)
        if d == "log":
            print(f"[allowlist] WOULD-BLOCK {host} {request.method} {request.url.path}", flush=True)
            return await call_next(request)
        print(f"[allowlist] BLOCK {host} {request.method} {request.url.path}", flush=True)
        return JSONResponse({"error": "forbidden"}, status_code=403)
