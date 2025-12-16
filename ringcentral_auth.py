# ringcentral_auth.py
"""
RingCentral auth helper (JWT → access/refresh tokens) with:
- robust .env loading (optional python-dotenv)
- local JSON cache for tokens
- early refresh with safety skew
- fallback to fresh JWT exchange if refresh fails
- rc_request() wrapper that retries once on 401 with a forced refresh

ENV (required):
  RC_SERVER_URL      = https://platform.ringcentral.com    # or sandbox URL
  RC_CLIENT_ID       = <your app client id>
  RC_CLIENT_SECRET   = <your app client secret>
  RC_JWT             = <your user JWT>

ENV (optional):
  RC_TOKEN_STORE_PATH = ./rc_token.json      # where to cache tokens
  RC_AUTH_DEBUG       = 0/1                  # print minimal debug logs

Usage:
  from ringcentral_auth import get_access_token, rc_request, RC_API_BASE
  token = get_access_token()
  r = rc_request("GET", f"{RC_API_BASE}/account/~/extension/~/message-store",
                 params={"messageType": "SMS"})
"""

import os
import json
import time
from pathlib import Path
from typing import Optional, Dict, Any
import tempfile

import requests

# -------------------- optional .env loading --------------------
try:
    from dotenv import load_dotenv, find_dotenv
    # 1) search upward from CWD
    load_dotenv(find_dotenv(usecwd=True), override=False)
    # 2) try alongside this file
    _mod_dir = Path(__file__).resolve().parent
    load_dotenv(_mod_dir / ".env", override=False)
    # 3) try project root
    load_dotenv(_mod_dir.parent / ".env", override=False)
except Exception:
    pass

# -------------------- config --------------------
def _clean_env(v: Optional[str]) -> str:
    return (v or "").strip().strip("'").strip('"')

RC_SERVER_URL   = _clean_env(os.getenv("RC_SERVER_URL") or "https://platform.ringcentral.com")
RC_CLIENT_ID    = _clean_env(os.getenv("RC_CLIENT_ID"))
RC_CLIENT_SECRET= _clean_env(os.getenv("RC_CLIENT_SECRET"))
RC_JWT          = _clean_env(os.getenv("RC_JWT"))
TOKEN_STORE_PATH= _clean_env(os.getenv("RC_TOKEN_STORE_PATH") or "./rc_token.json")
AUTH_DEBUG      = (_clean_env(os.getenv("RC_AUTH_DEBUG")) in {"1", "true", "yes"})

if not RC_CLIENT_ID or not RC_CLIENT_SECRET or not RC_JWT:
    missing = [k for k, v in [
        ("RC_CLIENT_ID", RC_CLIENT_ID),
        ("RC_CLIENT_SECRET", RC_CLIENT_SECRET),
        ("RC_JWT", RC_JWT),
    ] if not v]
    raise RuntimeError(f"Missing required env: {', '.join(missing)}. "
                       "Verify your .env and that python-dotenv can find it.")

RC_SERVER_URL = RC_SERVER_URL.rstrip("/")
RC_API_BASE   = f"{RC_SERVER_URL}/restapi/v1.0"

SAFETY_SKEW_SEC = 90  # refresh ~1.5 min early to avoid edge expiries

# -------------------- tiny logger --------------------
def _dbg(msg: str) -> None:
    if AUTH_DEBUG:
        print(f"[RC-AUTH] {msg}")

# -------------------- cache helpers --------------------
def _load_cached() -> Optional[Dict[str, Any]]:
    try:
        with open(TOKEN_STORE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            # must have an access_token at minimum
            if isinstance(data, dict) and data.get("access_token"):
                return data
    except Exception:
        pass
    return None

def _atomic_write(path: str, payload: Dict[str, Any]) -> None:
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8", dir=str(p.parent)) as tmp:
            json.dump(payload, tmp)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_path = tmp.name
        os.replace(tmp_path, path)  # atomic on POSIX/NTFS
    except Exception as e:
        _dbg(f"cache write skipped: {e}")

def _save_cached(d: Dict[str, Any]) -> None:
    _atomic_write(TOKEN_STORE_PATH, d)

def _is_expired(payload: Dict[str, Any]) -> bool:
    exp = payload.get("expires_at_epoch")
    if not isinstance(exp, (int, float)):
        return True
    return time.time() >= (float(exp) - SAFETY_SKEW_SEC)

# -------------------- token endpoints --------------------
def _token_url() -> str:
    return f"{RC_SERVER_URL}/restapi/oauth/token"

def _with_expiry(tok: Dict[str, Any]) -> Dict[str, Any]:
    try:
        ttl = float(tok.get("expires_in", 3600))
    except Exception:
        ttl = 3600.0
    tok["expires_at_epoch"] = time.time() + ttl
    return tok

def _post_form(url: str, data: Dict[str, str], attempts: int = 2) -> requests.Response:
    """
    Minimal retry for transient network/server errors on token calls.
    """
    last_err = None
    backoff = 0.5
    for attempt in range(1, attempts + 1):
        try:
            r = requests.post(url, data=data, auth=(RC_CLIENT_ID, RC_CLIENT_SECRET), timeout=20)
            return r
        except requests.RequestException as e:
            last_err = e
            _dbg(f"token POST failed (attempt {attempt}/{attempts}): {e}")
            time.sleep(backoff)
            backoff *= 2
    if last_err:
        raise last_err  # bubble up

def _exchange_jwt() -> Dict[str, Any]:
    _dbg("exchanging JWT for new tokens")
    r = _post_form(
        _token_url(),
        {"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": RC_JWT},
    )
    if r.status_code != 200:
        raise RuntimeError(f"JWT exchange failed: {r.status_code} {r.text[:200]}")
    tok = _with_expiry(r.json())
    _save_cached(tok)
    return tok

def _refresh(refresh_token: str) -> Dict[str, Any]:
    _dbg("refreshing access token")
    r = _post_form(
        _token_url(),
        {"grant_type": "refresh_token", "refresh_token": refresh_token},
    )
    if r.status_code != 200:
        _dbg(f"refresh failed ({r.status_code}); falling back to JWT exchange")
        return _exchange_jwt()
    tok = _with_expiry(r.json())
    _save_cached(tok)
    return tok

# -------------------- public API --------------------
def get_access_token(force: bool = False) -> str:
    """
    Returns a valid access token. Uses cached token until near expiry.
    If force=True, skips refresh and exchanges JWT immediately.
    """
    cache = None if force else _load_cached()

    if not cache or _is_expired(cache):
        try:
            if not force and cache and cache.get("refresh_token"):
                cache = _refresh(cache["refresh_token"])
            else:
                cache = _exchange_jwt()
        except Exception as e:
            # last-chance fallback if something odd happened during refresh
            _dbg(f"refresh/exchange error, retry JWT once: {e}")
            cache = _exchange_jwt()

    return cache["access_token"]

def rc_auth_header() -> Dict[str, str]:
    """Convenience header builder."""
    return {"Authorization": f"Bearer {get_access_token()}"}

def rc_request(method: str, url: str, **kwargs) -> requests.Response:
    """
    Wrapper around requests.request with one-time 401 recovery:
      1) call with current token
      2) on 401, force-refresh and retry exactly once
    Also sets a default timeout (20s) if not provided.
    """
    timeout = kwargs.pop("timeout", 20)
    headers = kwargs.pop("headers", {}) or {}
    headers.update(rc_auth_header())

    resp = requests.request(method, url, headers=headers, timeout=timeout, **kwargs)
    if resp.status_code != 401:
        return resp

    _dbg("401 received; forcing token refresh and retrying once")
    headers["Authorization"] = f"Bearer {get_access_token(force=True)}"
    return requests.request(method, url, headers=headers, timeout=timeout, **kwargs)

__all__ = [
    "RC_SERVER_URL",
    "RC_API_BASE",
    "get_access_token",
    "rc_auth_header",
    "rc_request",
]
