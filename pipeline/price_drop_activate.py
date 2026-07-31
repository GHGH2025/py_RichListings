"""
Activate listings that passed the 30-day ≥6% price-drop dedup gate:
  1) Set WordPress post_status to public
  2) Fire Podio catch webhook to mark property Active
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

import requests

from models import ParsedListing
from integrations.wordpress.post_status import set_wp_post_status

WEBHOOK_URL = os.getenv(
    "PRICE_DROP_PODIO_ACTIVE_WEBHOOK_URL",
    "https://workflow-automation.podio.com/catch/2rtkutxl47po7x7",
).strip()
WEBHOOK_TIMEOUT = int(os.getenv("PRICE_DROP_PODIO_ACTIVE_WEBHOOK_TIMEOUT", "20"))


def _now() -> datetime:
    return datetime.utcnow()


def _best_address_parts(pl: ParsedListing) -> Tuple[str, str, str, str]:
    addr = (getattr(pl, "address", None) or "").strip()
    city = (getattr(pl, "city", None) or "").strip()
    state = (getattr(pl, "state", None) or "").strip()
    zip_code = (getattr(pl, "zip", None) or "").strip()

    ci = getattr(pl, "complete_info", None) or {}
    if isinstance(ci, dict):
        if not addr:
            addr = (ci.get("address") or "").strip()
        if not city:
            city = (ci.get("city") or "").strip()
        if not state:
            state = (ci.get("state") or "").strip()
        if not zip_code:
            zip_code = (ci.get("zip") or "").strip()

    return addr, city, state, zip_code


def build_activation_address(pl: ParsedListing) -> Optional[str]:
    """
    Prefer Google formatted_address; else 'addr, city, ST zip, USA'.
    """
    geo = getattr(pl, "geo_code_response", None) or {}
    if isinstance(geo, dict):
        formatted = (geo.get("formatted_address") or "").strip()
        if formatted:
            return formatted

    addr, city, state, zip_code = _best_address_parts(pl)
    if not addr and not city:
        return None

    parts = []
    if addr:
        parts.append(addr)
    if city:
        parts.append(city)

    state_zip = " ".join(p for p in (state, zip_code) if p).strip()
    if state_zip:
        parts.append(state_zip)

    full = ", ".join(parts)
    if full and not full.upper().endswith(", USA") and not full.upper().endswith(" USA"):
        full = f"{full}, USA"
    return full or None


def _fire_podio_active_webhook(address: str) -> bool:
    if not WEBHOOK_URL:
        logging.warning("price_drop_activate: webhook URL not configured")
        return False
    try:
        resp = requests.post(
            WEBHOOK_URL,
            json={"add": address},
            headers={"Content-Type": "application/json"},
            timeout=WEBHOOK_TIMEOUT,
        )
        ok = resp.status_code in (200, 201, 202)
        if not ok:
            logging.warning(
                "price_drop_activate: webhook non-2xx status=%s body=%s",
                resp.status_code,
                (resp.text or "")[:300],
            )
        return ok
    except requests.RequestException:
        logging.exception("price_drop_activate: webhook failed for %s", address)
        return False


def process_price_drop_activations(limit: int = 50) -> Dict[str, Any]:
    """
    For each listing with price_drop_pass and not yet activated:
      - set WP public
      - fire Podio Active webhook
      - mark price_drop_activated when both succeed
    """
    checked = activated = wp_ok = podio_ok = failed = skipped_no_addr = 0

    candidates = (
        ParsedListing.objects(
            price_drop_pass=True,
            price_drop_activated__ne=True,
        )
        .order_by("updated_at")
        .limit(limit)
    )

    for pl in candidates:
        checked += 1
        address = build_activation_address(pl)
        if not address:
            skipped_no_addr += 1
            pl.update(
                set__price_drop_activate_error="no address available for activation",
                set__updated_at=_now(),
            )
            failed += 1
            continue

        posttitle = address
        wp_success, wp_status, wp_payload = set_wp_post_status(posttitle, "public")
        if not wp_success:
            err = f"wp_public_failed status={wp_status} detail={str(wp_payload)[:200]}"
            pl.update(
                set__price_drop_activate_error=err,
                set__updated_at=_now(),
            )
            failed += 1
            continue

        wp_ok += 1
        wp_at = _now()

        podio_success = _fire_podio_active_webhook(address)
        if not podio_success:
            pl.update(
                set__price_drop_wp_public_at=wp_at,
                set__price_drop_activate_error="podio_active_webhook_failed",
                set__updated_at=_now(),
            )
            failed += 1
            continue

        podio_ok += 1
        now = _now()
        pl.update(
            set__price_drop_wp_public_at=wp_at,
            set__price_drop_podio_webhook_at=now,
            set__price_drop_activated=True,
            set__price_drop_activated_at=now,
            set__price_drop_activate_error=None,
            set__updated_at=now,
        )
        activated += 1

    return {
        "checked": checked,
        "activated": activated,
        "wp_ok": wp_ok,
        "podio_ok": podio_ok,
        "failed": failed,
        "skipped_no_addr": skipped_no_addr,
    }
