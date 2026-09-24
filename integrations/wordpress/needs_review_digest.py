"""
Cloud A - needs-address-review queue + alert.

The dup-gate (see address_dedup.py) flags a listing as wp_status=
"needs_address_review" when its address cannot be trusted for auto-matching -
no house number, or junk like "6 Parking Spaces". Those must be checked by a
human (confirm the real address with the wholesaler) before they become a bad
public listing. This module is the review surface: it collects the queue and,
when a recipient is configured, emails/texts a digest.

SAFETY: outward sends are OFF until NEEDS_REVIEW_ALERT_EMAIL (and/or
NEEDS_REVIEW_ALERT_SMS) is set in the environment, so nothing is sent to anyone
until someone deliberately chooses the recipient. A small JSON state file holds
the last-notified id set, so a digest goes out only when NEW items appear (no
re-spamming), and the state is cleared when the queue drains.
"""
from __future__ import annotations

import os
import json
import logging
from typing import Any, Dict, List, Set

STATE_FILE = os.getenv(
    "NEEDS_REVIEW_STATE_FILE",
    "/home/ubuntu/apps/RichListings/data/needs_review_alerted.json",
)
ALERT_EMAIL = os.getenv("NEEDS_REVIEW_ALERT_EMAIL", "").strip()
ALERT_SMS = os.getenv("NEEDS_REVIEW_ALERT_SMS", "").strip()
# Internal ops alerts go through server 02 /internalAlert (no cc to Rich), NOT /pofEmail.
INTERNAL_ALERT_URL = os.getenv("INTERNAL_ALERT_URL", "http://ec2-3-90-20-111.compute-1.amazonaws.com:8000/internalAlert")
INTERNAL_ALERT_TOKEN = os.getenv("INTERNAL_ALERT_TOKEN", "").strip()  # shared with server 02


def collect() -> List[Dict[str, str]]:
    """Every listing currently flagged needs_address_review."""
    from models import ParsedListing
    from pipeline.address_utils import resolve_street_address

    out: List[Dict[str, str]] = []
    for pl in ParsedListing.objects(wp_status="needs_address_review").only(
            "id", "address", "city", "address_review"):
        addr = resolve_street_address(pl) or getattr(pl, "address", "") or ""
        out.append({
            "id": str(pl.id),
            "address": addr,
            "city": getattr(pl, "city", "") or "",
            "reason": getattr(pl, "address_review", "") or "",
        })
    # Gallery fix (A1): listings HELD because the Dropbox gallery upload failed after retries.
    for pl in ParsedListing.objects(status="held_no_gallery").only("id", "address", "city"):
        addr = resolve_street_address(pl) or getattr(pl, "address", "") or ""
        out.append({
            "id": str(pl.id),
            "address": addr,
            "city": getattr(pl, "city", "") or "",
            "reason": "gallery upload failed - add the Dropbox link manually, then set status=passed & dropbox_retry_count=0",
        })
    return out


def _html(items: List[Dict[str, str]]) -> str:
    def esc(s: str) -> str:
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    rows = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td></tr>" % (esc(i["address"]), esc(i["city"]), esc(i["reason"]))
        for i in items)
    return (
        "<p>%d listing(s) need an address review before they can be posted "
        "(no house number, or an unusable address):</p>"
        "<table border='1' cellpadding='6' cellspacing='0'>"
        "<tr><th align='left'>Address</th><th align='left'>City</th><th align='left'>Reason</th></tr>"
        "%s</table>"
        "<p>Please confirm the exact address with the wholesaler.</p>" % (len(items), rows)
    )


def _load_state() -> Set[str]:
    try:
        return set(json.load(open(STATE_FILE)))
    except Exception:
        return set()


def _save_state(ids: Set[str]) -> None:
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        json.dump(sorted(ids), open(STATE_FILE, "w"))
    except Exception as e:
        logging.warning("needs_review state save failed: %s", e)


def run_needs_review_alert() -> Dict[str, Any]:
    """Scheduled entry point. Emails/texts a digest only when NEW items appear
    and a recipient is configured; otherwise it just reports counts."""
    items = collect()
    ids = {i["id"] for i in items}
    prev = _load_state()
    new_ids = ids - prev
    result: Dict[str, Any] = {
        "count": len(items),
        "new": len(new_ids),
        "email_sent": False,
        "sms_sent": False,
        "recipient_configured": bool(ALERT_EMAIL or ALERT_SMS),
    }

    if not items:
        _save_state(ids)          # queue empty -> reset so future items alert
        return result
    if not new_ids:
        return result             # nothing new since last notice

    if ALERT_EMAIL:
        try:
            import requests
            r = requests.post(
                INTERNAL_ALERT_URL,
                json={"to": ALERT_EMAIL,
                      "subject": "Needs review: %d listing(s)" % len(items),
                      "body": _html(items)},
                headers={"X-Alert-Token": INTERNAL_ALERT_TOKEN},
                timeout=20,
            )
            # /internalAlert returns 200 with a status string; success only on the exact success text
            # (a send error yields a non-2xx or an error status, so we must not advance state on it).
            ok = False
            try:
                ok = (r.status_code == 200 and (r.json() or {}).get("status") == "Email sent sucessfully")
            except Exception:
                ok = False
            result["email_sent"] = ok
        except Exception as e:
            logging.warning("needs_review email failed: %s", e)

    if ALERT_SMS:
        try:
            from buyers.matched_process import send_sms_to_buyer
            r = send_sms_to_buyer(
                ALERT_SMS,
                "Cloud A: %d listing(s) need an address review on the inventory site." % len(items),
            )
            result["sms_sent"] = bool(r.get("ok"))
        except Exception as e:
            logging.warning("needs_review sms failed: %s", e)

    if result["email_sent"] or result["sms_sent"]:
        _save_state(ids)          # only advance state for items we notified about
    return result
