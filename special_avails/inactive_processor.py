"""
Daily job: Active Podio properties (created in past X days) that stop appearing
in the wholesaler's avail email for N consecutive days → Podio catch webhook.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

import requests
from mongoengine.queryset.visitor import Q

from db.mongo_engine_conn import init_db
from integrations.podio.direct_wholesaler import get_podio_access_token
from models import FilteredListingEmail, ParsedListing
from models.special_avail_inactive import (
    SpecialAvailInactiveJobRun,
    SpecialAvailInactiveTracker,
)
from services.special_avail_list_service import (
    get_wholesaler_config,
    get_wholesaler_podio_bucket,
)
from integrations.wordpress.post_status import set_wp_post_status
from special_avails.processor import (
    _fetch_active_properties_for_wholesaler,
    _format_full_address,
    _normalize_address_for_match,
    _sender_email_q,
)

EASTERN = ZoneInfo("America/New_York")

LOOKBACK_DAYS = int(os.getenv("SPECIAL_AVAIL_INACTIVE_LOOKBACK_DAYS", "7"))
MISS_THRESHOLD = int(os.getenv("SPECIAL_AVAIL_INACTIVE_MISS_DAYS", "3"))
WEBHOOK_URL = os.getenv(
    "SPECIAL_AVAIL_INACTIVE_WEBHOOK_URL",
    "https://workflow-automation.podio.com/catch/5z8e56v1258k2hj",
).strip()
WEBHOOK_TIMEOUT = int(os.getenv("SPECIAL_AVAIL_INACTIVE_WEBHOOK_TIMEOUT", "20"))


def _est_day_range_utc(day) -> Tuple[datetime, datetime]:
    """Return naive UTC [start, end) for an America/New_York calendar date."""
    start_est = datetime(day.year, day.month, day.day, 0, 0, 0, tzinfo=EASTERN)
    end_est = start_est + timedelta(days=1)
    start_utc = start_est.astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = end_est.astimezone(timezone.utc).replace(tzinfo=None)
    return start_utc, end_utc


def _today_email_address_set(
    sender_emails: List[str],
    *,
    start_utc: datetime,
    end_utc: datetime,
) -> Set[str]:
    """Normalized full-address set from today's parsed listings for these senders."""
    emails_norm = [(e or "").strip() for e in sender_emails if (e or "").strip()]
    if not emails_norm:
        return set()

    email_q = (
        Q(created_at__gte=start_utc)
        & Q(created_at__lt=end_utc)
        & _sender_email_q(emails_norm)
    )
    source_emails = list(FilteredListingEmail.objects(email_q).only("id"))
    if not source_emails:
        return set()

    email_ids = [e.id for e in source_emails]
    listings = ParsedListing.objects(source_email__in=email_ids).only(
        "address", "city", "state", "zip"
    )

    out: Set[str] = set()
    for pl in listings:
        full = _format_full_address(
            getattr(pl, "address", "") or "",
            getattr(pl, "city", "") or "",
            getattr(pl, "state", "") or "",
            getattr(pl, "zip", "") or "",
        )
        if not full:
            continue
        norm = _normalize_address_for_match(full)
        if norm:
            out.add(norm)
    return out


def _prop_created_within_lookback(
    created_on: Optional[datetime],
    *,
    cutoff_utc: datetime,
) -> bool:
    if created_on is None:
        return False
    dt = created_on
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt >= cutoff_utc


def _fire_inactive_webhook(address: str) -> bool:
    if not WEBHOOK_URL:
        logging.warning("special_avail_inactive: webhook URL not configured")
        return False
    payload = {"add": address}
    try:
        resp = requests.post(
            WEBHOOK_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=WEBHOOK_TIMEOUT,
        )
        ok = resp.status_code in (200, 201, 202)
        if not ok:
            logging.warning(
                "special_avail_inactive: webhook non-2xx status=%s body=%s",
                resp.status_code,
                (resp.text or "")[:300],
            )
        return ok
    except requests.RequestException:
        logging.exception("special_avail_inactive: webhook request failed for %s", address)
        return False


def _mark_wp_private(address: str) -> bool:
    """Set WordPress post_status to private (same as POST /public/wp/create)."""
    ok, status_code, payload = set_wp_post_status(address, "private")
    if not ok:
        logging.warning(
            "special_avail_inactive: WP private failed status=%s address=%s body=%s",
            status_code,
            address,
            str(payload)[:300],
        )
    return ok


def _get_or_create_tracker(
    *,
    podio_item_id: int,
    address: str,
    address_normalized: str,
    wholesaler_name: str,
) -> SpecialAvailInactiveTracker:
    tracker = SpecialAvailInactiveTracker.objects(podio_item_id=podio_item_id).first()
    if tracker:
        tracker.address = address
        tracker.address_normalized = address_normalized
        tracker.wholesaler_name = wholesaler_name
        return tracker
    return SpecialAvailInactiveTracker(
        podio_item_id=podio_item_id,
        address=address,
        address_normalized=address_normalized,
        wholesaler_name=wholesaler_name,
        miss_streak=0,
        status="watching",
    )


def _persist_job_run(result: Dict[str, Any]) -> None:
    try:
        SpecialAvailInactiveJobRun(
            run_at=datetime.utcnow(),
            target_date=str(result.get("target_date") or ""),
            ok=bool(result.get("ok")),
            skipped=bool(result.get("skipped")),
            reason=str(result.get("reason") or ""),
            lookback_days=int(result.get("lookback_days") or LOOKBACK_DAYS),
            miss_threshold=int(result.get("miss_threshold") or MISS_THRESHOLD),
            wholesalers_checked=int(result.get("wholesalers_checked") or 0),
            properties_checked=int(result.get("properties_checked") or 0),
            found_count=int(result.get("found_count") or 0),
            missed_count=int(result.get("missed_count") or 0),
            skipped_no_email=int(result.get("skipped_no_email") or 0),
            webhooks_fired=int(result.get("webhooks_fired") or 0),
            webhook_failures=int(result.get("webhook_failures") or 0),
            wp_privates_ok=int(result.get("wp_privates_ok") or 0),
            wp_private_failures=int(result.get("wp_private_failures") or 0),
            fired_addresses=list(result.get("fired_addresses") or []),
            wholesaler_summaries=list(result.get("wholesaler_summaries") or []),
            errors=list(result.get("errors") or []),
        ).save()
    except Exception:
        logging.exception("special_avail_inactive: failed to persist job run: %s", result)


def run_special_avail_inactive_check(
    *,
    lookback_days: Optional[int] = None,
    miss_threshold: Optional[int] = None,
    force: bool = False,
    target_day: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    For each active special-avail wholesaler:
      1) Load Active Podio properties created within lookback_days
      2) Build today's (EST) email address set from that wholesaler's sender emails
      3) If wholesaler sent no email today → skip miss increment for their props
      4) Else mark found / miss; after miss_threshold consecutive misses →
         webhook once, then set WP post_status to private
    """
    init_db()

    lookback = int(lookback_days if lookback_days is not None else LOOKBACK_DAYS)
    threshold = int(miss_threshold if miss_threshold is not None else MISS_THRESHOLD)

    now_est = datetime.now(EASTERN)
    day = target_day or now_est.date()
    target_date = day.isoformat()
    start_utc, end_utc = _est_day_range_utc(day)
    cutoff_utc = datetime.utcnow() - timedelta(days=lookback)

    existing = (
        SpecialAvailInactiveJobRun.objects(target_date=target_date, ok=True)
        .order_by("-run_at")
        .first()
    )
    if existing and not force:
        return {
            "ok": True,
            "skipped": True,
            "reason": "already_ran_today",
            "target_date": target_date,
            "lookback_days": lookback,
            "miss_threshold": threshold,
            "wholesalers_checked": int(existing.wholesalers_checked or 0),
            "properties_checked": int(existing.properties_checked or 0),
            "found_count": int(existing.found_count or 0),
            "missed_count": int(existing.missed_count or 0),
            "skipped_no_email": int(existing.skipped_no_email or 0),
            "webhooks_fired": int(existing.webhooks_fired or 0),
            "webhook_failures": int(existing.webhook_failures or 0),
            "wp_privates_ok": int(getattr(existing, "wp_privates_ok", 0) or 0),
            "wp_private_failures": int(getattr(existing, "wp_private_failures", 0) or 0),
            "fired_addresses": list(existing.fired_addresses or []),
            "wholesaler_summaries": list(existing.wholesaler_summaries or []),
            "errors": [],
            "existing_run_id": str(existing.id),
        }

    cfg = get_wholesaler_config()
    bucket = get_wholesaler_podio_bucket()
    if not cfg:
        result = {
            "ok": True,
            "skipped": True,
            "reason": "no_active_wholesalers",
            "target_date": target_date,
            "lookback_days": lookback,
            "miss_threshold": threshold,
            "wholesalers_checked": 0,
            "properties_checked": 0,
            "found_count": 0,
            "missed_count": 0,
            "skipped_no_email": 0,
            "webhooks_fired": 0,
            "webhook_failures": 0,
            "wp_privates_ok": 0,
            "wp_private_failures": 0,
            "fired_addresses": [],
            "wholesaler_summaries": [],
            "errors": [],
        }
        _persist_job_run(result)
        return result

    try:
        access_token = get_podio_access_token()
    except Exception as exc:
        logging.exception("special_avail_inactive: Podio token failed")
        result = {
            "ok": False,
            "skipped": True,
            "reason": f"podio_token_failed:{exc}",
            "target_date": target_date,
            "lookback_days": lookback,
            "miss_threshold": threshold,
            "wholesalers_checked": 0,
            "properties_checked": 0,
            "found_count": 0,
            "missed_count": 0,
            "skipped_no_email": 0,
            "webhooks_fired": 0,
            "webhook_failures": 0,
            "wp_privates_ok": 0,
            "wp_private_failures": 0,
            "fired_addresses": [],
            "wholesaler_summaries": [],
            "errors": [str(exc)],
        }
        _persist_job_run(result)
        return result

    found_count = 0
    missed_count = 0
    skipped_no_email = 0
    webhooks_fired = 0
    webhook_failures = 0
    wp_privates_ok = 0
    wp_private_failures = 0
    properties_checked = 0
    wholesalers_checked = 0
    fired_addresses: List[str] = []
    wholesaler_summaries: List[Dict[str, Any]] = []
    errors: List[str] = []

    for wholesaler_name, sender_emails in cfg.items():
        wh_key = (wholesaler_name or "").strip().lower()
        podio_ids = bucket.get(wh_key, []) or []
        wh_summary: Dict[str, Any] = {
            "wholesaler_name": wholesaler_name,
            "podio_ids": podio_ids,
            "properties": 0,
            "found": 0,
            "missed": 0,
            "skipped_no_email": 0,
            "webhooks_fired": 0,
            "wp_privates_ok": 0,
            "wp_private_failures": 0,
            "email_address_count": 0,
        }

        if not podio_ids:
            wh_summary["note"] = "no_podio_mapping"
            wholesaler_summaries.append(wh_summary)
            continue

        wholesalers_checked += 1

        email_set = _today_email_address_set(
            sender_emails,
            start_utc=start_utc,
            end_utc=end_utc,
        )
        wh_summary["email_address_count"] = len(email_set)

        # Silent day: do not increment miss streaks
        if not email_set:
            wh_summary["note"] = "no_email_today"
            # Still count candidate props for observability
            try:
                for pid in podio_ids:
                    props = _fetch_active_properties_for_wholesaler(access_token, int(pid))
                    for prop in props:
                        if not _prop_created_within_lookback(
                            prop.get("created_on"), cutoff_utc=cutoff_utc
                        ):
                            continue
                        if not prop.get("podio_item_id") or not (prop.get("address") or "").strip():
                            continue
                        skipped_no_email += 1
                        wh_summary["skipped_no_email"] += 1
                        tracker = _get_or_create_tracker(
                            podio_item_id=int(prop["podio_item_id"]),
                            address=(prop.get("address") or "").strip(),
                            address_normalized=_normalize_address_for_match(
                                (prop.get("address") or "").strip()
                            ),
                            wholesaler_name=wholesaler_name,
                        )
                        if tracker.last_checked_date == target_date and not force:
                            continue
                        tracker.last_result = "skipped_no_email"
                        tracker.last_checked_date = target_date
                        tracker.touch()
                        tracker.save()
            except Exception as exc:
                msg = f"{wholesaler_name}: silent-day fetch failed: {exc}"
                logging.exception(msg)
                errors.append(msg)
            wholesaler_summaries.append(wh_summary)
            continue

        try:
            all_props: List[Dict[str, Any]] = []
            for pid in podio_ids:
                all_props.extend(
                    _fetch_active_properties_for_wholesaler(access_token, int(pid))
                )
        except Exception as exc:
            msg = f"{wholesaler_name}: podio fetch failed: {exc}"
            logging.exception(msg)
            errors.append(msg)
            wholesaler_summaries.append(wh_summary)
            continue

        # Dedupe by podio_item_id
        seen_ids: Set[int] = set()
        for prop in all_props:
            prop_id = prop.get("podio_item_id")
            addr = (prop.get("address") or "").strip()
            if not prop_id or not addr:
                continue
            prop_id = int(prop_id)
            if prop_id in seen_ids:
                continue
            seen_ids.add(prop_id)

            if not _prop_created_within_lookback(prop.get("created_on"), cutoff_utc=cutoff_utc):
                continue

            properties_checked += 1
            wh_summary["properties"] += 1

            addr_norm = _normalize_address_for_match(addr)
            tracker = _get_or_create_tracker(
                podio_item_id=prop_id,
                address=addr,
                address_normalized=addr_norm,
                wholesaler_name=wholesaler_name,
            )

            if tracker.last_checked_date == target_date and not force:
                continue

            found = bool(addr_norm) and addr_norm in email_set
            already_today = tracker.last_checked_date == target_date
            prior_result = tracker.last_result

            if found:
                found_count += 1
                wh_summary["found"] += 1
                tracker.miss_streak = 0
                tracker.last_seen_in_email = target_date
                tracker.last_result = "found"
                if tracker.status == "fired":
                    tracker.status = "resolved"
            else:
                missed_count += 1
                wh_summary["missed"] += 1
                if not already_today:
                    tracker.miss_streak = int(tracker.miss_streak or 0) + 1
                elif force and prior_result == "found":
                    # Same-day re-run: previously found, now missed
                    tracker.miss_streak = int(tracker.miss_streak or 0) + 1
                # else: already counted a miss for this date — keep streak
                tracker.last_result = "missed"

                if (
                    tracker.miss_streak >= threshold
                    and tracker.webhook_fired_at is None
                    and tracker.status != "fired"
                ):
                    ok = _fire_inactive_webhook(addr)
                    if ok:
                        tracker.webhook_fired_at = datetime.utcnow()
                        tracker.webhook_ok = True
                        tracker.status = "fired"
                        webhooks_fired += 1
                        wh_summary["webhooks_fired"] += 1
                        fired_addresses.append(addr)

                        wp_ok = _mark_wp_private(addr)
                        tracker.wp_private_ok = wp_ok
                        tracker.wp_private_at = datetime.utcnow()
                        if wp_ok:
                            wp_privates_ok += 1
                            wh_summary["wp_privates_ok"] += 1
                        else:
                            wp_private_failures += 1
                            wh_summary["wp_private_failures"] += 1
                    else:
                        tracker.webhook_ok = False
                        webhook_failures += 1

            tracker.last_checked_date = target_date
            tracker.touch()
            tracker.save()

        wholesaler_summaries.append(wh_summary)

    result = {
        "ok": len(errors) == 0,
        "skipped": False,
        "reason": "",
        "target_date": target_date,
        "lookback_days": lookback,
        "miss_threshold": threshold,
        "wholesalers_checked": wholesalers_checked,
        "properties_checked": properties_checked,
        "found_count": found_count,
        "missed_count": missed_count,
        "skipped_no_email": skipped_no_email,
        "webhooks_fired": webhooks_fired,
        "webhook_failures": webhook_failures,
        "wp_privates_ok": wp_privates_ok,
        "wp_private_failures": wp_private_failures,
        "fired_addresses": fired_addresses,
        "wholesaler_summaries": wholesaler_summaries,
        "errors": errors,
        "webhook_url": WEBHOOK_URL or None,
    }
    _persist_job_run(result)
    logging.info(
        "special_avail_inactive: done target=%s checked=%s found=%s missed=%s fired=%s wp_private_ok=%s",
        target_date,
        properties_checked,
        found_count,
        missed_count,
        webhooks_fired,
        wp_privates_ok,
    )
    return result
