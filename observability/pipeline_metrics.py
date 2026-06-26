"""
Best-effort pipeline metrics for the email → WhatsApp / Podio / WordPress flow.
Never raises — tracking failures must not break the main pipeline.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Optional

from models import (
    FilteredListingEmail,
    ListingPipelineMetric,
    ParsedListing,
    StageEvent,
)
from pipeline.address_utils import resolve_street_address

logger = logging.getLogger(__name__)

STAGE_TIMESTAMP_FIELDS = {
    "email_ingested": "ingested_at",
    "parsed": "parsed_at",
    "verified": "verified_at",
    "dedup": "dedup_at",
    "rules": "rules_at",
    "post_selection": "post_selection_at",
    "image_curation": "image_curation_at",
    "primary_image": "primary_image_at",
    "ready_to_post": "ready_to_post_at",
    "posted": "posted_at",
    "podio_webhook": "podio_webhook_at",
    "whatsapp_sent": "whatsapp_sent_at",
    "whatsapp_failed": "whatsapp_sent_at",
    "podio_linked": "podio_linked_at",
    "wp_keys": "wp_keys_at",
    "wp_des": "wp_des_at",
    "wp_synced": "wp_synced_at",
    "wp_already_found": "wp_synced_at",
    "wp_failed": "wp_synced_at",
}


def _now() -> datetime:
    return datetime.utcnow()


def _compose_full_address(
    street: Optional[str],
    city: Optional[str],
    state: Optional[str] = None,
    zip_code: Optional[str] = None,
) -> Optional[str]:
    street = (street or "").strip()
    city = (city or "").strip()
    if not street and not city:
        return None
    parts = [p for p in [street, city] if p]
    tail = " ".join(p for p in [(state or "").strip(), (zip_code or "").strip()] if p)
    if tail:
        parts.append(tail)
    return ", ".join(parts)


def _email_received_at(email_doc: FilteredListingEmail) -> Optional[datetime]:
    internal = getattr(email_doc, "internal_date", None)
    if internal and getattr(internal, "ts_ms", None):
        try:
            return datetime.utcfromtimestamp(int(internal.ts_ms) / 1000)
        except Exception:
            pass
    return getattr(email_doc, "created_at", None)


def _ensure_email_trace(email_doc: FilteredListingEmail) -> str:
    trace_id = getattr(email_doc, "trace_id", None)
    if trace_id:
        return trace_id
    trace_id = str(uuid.uuid4())
    FilteredListingEmail.objects(id=email_doc.id).update_one(
        set__trace_id=trace_id,
        set__pipeline_stage="queued",
        set__updated_at=_now(),
    )
    return trace_id


def _duration_sec(start: Optional[datetime], end: datetime) -> Optional[float]:
    if not start:
        return None
    return round((end - start).total_seconds(), 2)


def _recompute_durations(metric: ListingPipelineMetric, now: datetime) -> None:
    base = metric.email_received_at or metric.ingested_at or metric.parsed_at
    if not base:
        return
    metric.duration_to_parsed_sec = _duration_sec(base, metric.parsed_at) if metric.parsed_at else None
    metric.duration_to_posted_sec = _duration_sec(base, metric.posted_at) if metric.posted_at else None
    metric.duration_to_whatsapp_sec = (
        _duration_sec(base, metric.whatsapp_sent_at) if metric.whatsapp_sent_at else None
    )
    metric.duration_to_wp_sec = _duration_sec(base, metric.wp_synced_at) if metric.wp_synced_at else None
    metric.duration_to_podio_sec = (
        _duration_sec(base, metric.podio_linked_at) if metric.podio_linked_at else None
    )


def _append_event(
    metric: ListingPipelineMetric,
    stage: str,
    *,
    status: Optional[str] = None,
    detail: Optional[str] = None,
    at: Optional[datetime] = None,
) -> None:
    at = at or _now()
    prev_at = metric.events[-1].at if metric.events else (metric.email_received_at or metric.parsed_at)
    duration = _duration_sec(prev_at, at) if prev_at else None
    metric.events.append(
        StageEvent(stage=stage, status=status, at=at, duration_sec=duration, detail=detail)
    )


def record_email_ingested(email_id: str) -> None:
    """Call after a FilteredListingEmail is upserted from Gmail."""
    try:
        email_doc = FilteredListingEmail.objects(id=email_id).first()
        if not email_doc:
            return

        trace_id = _ensure_email_trace(email_doc)
        received_at = _email_received_at(email_doc) or _now()

        FilteredListingEmail.objects(id=email_doc.id).update_one(
            set__trace_id=trace_id,
            set__pipeline_stage="queued",
            set__pipeline_processed_at=received_at,
            set__updated_at=_now(),
        )
    except Exception:
        logger.exception("record_email_ingested failed for email_id=%s", email_id)


def record_listing_created(listing_id: str) -> None:
    """Call after a ParsedListing is upserted from email parsing."""
    try:
        pl = ParsedListing.objects(id=listing_id).first()
        if not pl:
            return

        email_doc = getattr(pl, "source_email", None)
        trace_id = None
        email_received_at = None
        email_id = None

        if email_doc:
            trace_id = _ensure_email_trace(email_doc)
            email_received_at = _email_received_at(email_doc)
            email_id = str(email_doc.id)

        street = resolve_street_address(pl)
        city = (pl.city or "").strip()
        state = (pl.state or "").strip()
        zip_code = (pl.zip or "").strip()
        now = _now()

        metric = ListingPipelineMetric.objects(listing_id=str(pl.id)).first()
        if not metric:
            metric = ListingPipelineMetric(listing_id=str(pl.id))

        metric.trace_id = trace_id
        metric.email_id = email_id
        metric.account_label = pl.account_label
        metric.gmail_message_id = pl.gmail_message_id
        metric.list_index = pl.list_index
        metric.address_received = street or (pl.address or "").strip() or None
        metric.city_received = city or None
        metric.state_received = state or None
        metric.zip_received = zip_code or None
        metric.full_address_received = _compose_full_address(street or pl.address, city, state, zip_code)
        metric.email_received_at = email_received_at
        metric.parsed_at = now
        metric.current_stage = "parsed"
        metric.listing_status = pl.status
        metric.updated_at = now
        if not metric.created_at:
            metric.created_at = now

        _append_event(metric, "parsed", status=pl.status, at=now)
        _recompute_durations(metric, now)
        metric.save()

        if email_doc:
            FilteredListingEmail.objects(id=email_doc.id).update_one(
                set__pipeline_stage="parse",
                set__updated_at=now,
            )
    except Exception:
        logger.exception("record_listing_created failed for listing_id=%s", listing_id)


def _capture_posted_address(metric: ListingPipelineMetric, pl: ParsedListing) -> None:
    posted_street = resolve_street_address(pl)
    city = (pl.city or "").strip()
    metric.address_posted = posted_street or None
    metric.city_posted = city or None
    metric.full_address_posted = _compose_full_address(posted_street, city, pl.state, pl.zip)
    recv = (metric.address_received or "").strip().lower()
    post = (posted_street or "").strip().lower()
    metric.address_changed = bool(recv and post and recv != post)


def record_listing_stage(
    listing_id: str,
    stage: str,
    *,
    status: Optional[str] = None,
    detail: Optional[str] = None,
    listing_status: Optional[str] = None,
    wp_status: Optional[str] = None,
    whatsapp_status: Optional[str] = None,
    direct_wholeseller: Optional[str] = None,
    skip_reason: Optional[str] = None,
) -> None:
    """Record a pipeline stage transition for a listing."""
    try:
        pl = ParsedListing.objects(id=listing_id).first()
        if not pl:
            return

        now = _now()
        metric = ListingPipelineMetric.objects(listing_id=str(pl.id)).first()
        if not metric:
            record_listing_created(str(pl.id))
            metric = ListingPipelineMetric.objects(listing_id=str(pl.id)).first()
        if not metric:
            return

        ts_field = STAGE_TIMESTAMP_FIELDS.get(stage)
        if ts_field and not getattr(metric, ts_field, None):
            setattr(metric, ts_field, now)

        if stage == "posted":
            _capture_posted_address(metric, pl)

        metric.current_stage = stage
        if listing_status is not None:
            metric.listing_status = listing_status
        elif pl.status:
            metric.listing_status = pl.status
        if wp_status is not None:
            metric.wp_status = wp_status
        elif getattr(pl, "wp_status", None):
            metric.wp_status = pl.wp_status
        if whatsapp_status is not None:
            metric.whatsapp_status = whatsapp_status
        elif getattr(pl, "whatsapp_status", None):
            metric.whatsapp_status = pl.whatsapp_status
        if direct_wholeseller is not None:
            metric.direct_wholeseller = direct_wholeseller
        elif getattr(pl, "direct_wholeseller", None):
            metric.direct_wholeseller = pl.direct_wholeseller
        if skip_reason:
            metric.skip_reason = skip_reason
        elif detail and stage.endswith("skipped"):
            metric.skip_reason = detail

        _append_event(metric, stage, status=status or listing_status, detail=detail, at=now)
        _recompute_durations(metric, now)
        metric.updated_at = now
        metric.save()

        email_doc = getattr(pl, "source_email", None)
        if email_doc:
            email_stage_map = {
                "parsed": "parse",
                "verified": "media_verify",
                "dedup": "dup30",
                "rules": "ai_rules",
                "post_selection": "post_policy",
                "image_curation": "image_curation",
                "primary_image": "image_curation",
                "posted": "publish",
            }
            email_stage = email_stage_map.get(stage)
            if email_stage:
                FilteredListingEmail.objects(id=email_doc.id).update_one(
                    set__pipeline_stage=email_stage,
                    set__updated_at=now,
                )
            if stage == "posted":
                FilteredListingEmail.objects(id=email_doc.id).update_one(
                    set__pipeline_completed_at=now,
                    set__updated_at=now,
                )
    except Exception:
        logger.exception(
            "record_listing_stage failed listing_id=%s stage=%s",
            listing_id,
            stage,
        )
