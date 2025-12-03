# config_runtime.py
import os
import json
import threading
from typing import List
from dotenv import set_key, dotenv_values

_LOCK = threading.Lock()
ENV_FILE = os.getenv("ENV_FILE_PATH", ".env")

def _persist_env(key: str, value: str) -> None:
    # Persist to .env (idempotent)
    set_key(ENV_FILE, key, value)

def set_whatsapp_send_mode(mode: str) -> None:
    m = (mode or "").strip().lower()
    if m not in ("dm", "group"):
        raise ValueError("mode must be 'dm' or 'group'")
    with _LOCK:
        os.environ["WHATSAPP_SEND_MODE"] = m
        _persist_env("WHATSAPP_SEND_MODE", m)

def get_whatsapp_send_mode() -> str:
    with _LOCK:
        return os.getenv("WHATSAPP_SEND_MODE", "dm").strip().lower()

def get_group_jids() -> List[str]:
    with _LOCK:
        raw = os.getenv("WHATSAPP_GROUP_JIDS", "").strip()
        if not raw:
            return []
        try:
            arr = json.loads(raw)
            if isinstance(arr, list):
                return [str(x).strip() for x in arr if str(x).strip()]
        except Exception:
            pass
        return [x.strip() for x in raw.split(",") if x.strip()]
