"""
SMS suppression - people who replied STOP and must never be texted again.

The twin of RingCentral's src/suppression.js on server 02. Same list (identical
sha256), same rule: compare the LAST 10 DIGITS. The list holds +1XXXXXXXXXX and
Podio hands us anything from "(786) 555-0100" to "17865550100", and the last ten
digits are the only part that is reliably the same in all of them.

Source: the Twilio console export of 2026-09-01, 6,410 numbers going back to
July 2025 - the full 13-month retention window.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Optional, Set

_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIST_PATH = os.getenv("SMS_SUPPRESSION_FILE",
                      os.path.join(_DIR, "data", "sms_suppression.txt"))

_SUPPRESSED: Set[str] = set()
_LOAD_ERROR: Optional[str] = None


def normalise(value) -> Optional[str]:
    digits = re.sub(r"\D+", "", str(value or ""))
    return digits[-10:] if len(digits) >= 10 else None


def load() -> None:
    global _SUPPRESSED, _LOAD_ERROR
    try:
        found = set()
        with open(LIST_PATH, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                n = normalise(line)
                if n:
                    found.add(n)
        _SUPPRESSED = found
        _LOAD_ERROR = None
        logging.info("[suppression] loaded %d numbers from %s", len(found), LIST_PATH)
    except Exception as exc:
        _LOAD_ERROR = str(exc)
        # Deliberately fail OPEN: a missing file must not silently halt every
        # campaign. But it is loud - this line and one per send - so it cannot
        # be missed the way the absence of suppression itself was missed.
        logging.error("[suppression] COULD NOT LOAD %s - %s - "
                      "SENDING IS UNFILTERED UNTIL THIS IS FIXED", LIST_PATH, exc)


load()


def is_suppressed(phone) -> bool:
    """True when this number has opted out and must not be texted."""
    if _LOAD_ERROR:
        logging.error("[suppression] list unavailable, allowing send")
        return False
    n = normalise(phone)
    return bool(n and n in _SUPPRESSED)


def count() -> int:
    return len(_SUPPRESSED)


def stats() -> dict:
    return {"count": len(_SUPPRESSED), "error": _LOAD_ERROR, "path": LIST_PATH}
