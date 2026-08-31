"""Enqueue pending scraped deals into parsed_listings and sync filtered outcomes."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from models import (
    Bodies,
    FilteredListingEmail,
    FromInfo,
    InternalDate,
    ParsedListing,
    WindowRange,
)
from models.scraper_listings import FilteredListing
from pipeline.listing_details import (
    _clean_images,
    _compose_raw_for_google,
    _normalize_city_for_google,
)
from integrations.google_formatter import geocode_response, get_street_and_city
from pipeline.address_utils import is_bed_bath_descriptor_address

logger = logging.getLogger(__name__)

ACCOUNT_LABEL = "scraper"
SOURCE_EMAIL = "floridaoffmarket@scraper"

REJECT_STATUSES = {
    "skipped",
    "skipped_quota",
    "image_curation_failed",
    "primary_image_failed",
    "bypassed",
}

TRI_COUNTY = {
    "miami-dade": "miami_dade",
    "miami dade": "miami_dade",
    "broward": "broward",
    "palm beach": "palm_beach",
}


def _now() -> datetime:
    return datetime.utcnow()


def _region(county: Optional[str], state: Optional[str], city: Optional[str]) -> tuple[str, Optional[str]]:
    c = (county or "").strip().lower().replace(" county", "")
    st = (state or "").strip().upper()
    city_l = (city or "").strip().lower()
    if c in TRI_COUNTY:
        return "south_florida_tri_county", TRI_COUNTY[c]
    if city_l == "fort pierce":
        return "fort_pierce", None
    if c in {"st. lucie", "st lucie", "saint lucie"}:
        return "st_lucie", None
    if st in {"FL", "FLORIDA"}:
        return "rest_of_florida", None
    if st:
        return "outside_florida", None
    return "unknown", None


def _listing_blob(fl: FilteredListing) -> dict[str, Any]:
    blob = dict(fl.listing or {})
    addr = (fl.address or blob.get("address") or blob.get("title") or "").strip()
    city = (fl.city or blob.get("city") or "").strip()
    state = (fl.state or blob.get("state") or "").strip()
    zip_ = (fl.zip or blob.get("zip") or "").strip()
    county = (fl.county or blob.get("county") or "").strip()
    region_bucket, tri_county_name = _region(county, state, city)

    price_val = fl.price_usd
    if price_val is None:
        raw_price = blob.get("price_usd")
        try:
            price_val = float(raw_price) if raw_price is not None else None
        except (TypeError, ValueError):
            price_val = None

    images = _clean_images(blob.get("images"))
    description = blob.get("description")
    complete = {
        "complete_info": description,
        "source_title": blob.get("title") or fl.title,
        "listing_url": blob.get("url") or fl.url,
        "mls_id": blob.get("listing_id") or fl.listing_id,
        "agent_name": blob.get("wholesaler"),
        "agent_phone": None,
        "agent_email": None,
        "address": addr or None,
        "city": city or None,
        "county": county or None,
        "state": state or None,
        "zip": zip_ or None,
        "list_price_usd": price_val,
        "bedrooms": blob.get("beds"),
        "bathrooms_full": blob.get("baths"),
        "living_area_sqft": blob.get("sqft"),
        "year_built": blob.get("year_built"),
        "lot_size_sqft": blob.get("lot_sqft"),
        "lot_size_acres": blob.get("lot_acres"),
        "images": images,
        "other_images_source": None,
        "raw_description_excerpt": description,
        "estimated_arv": blob.get("estimated_arv"),
        "estimated_repairs": blob.get("estimated_repairs"),
        "occupancy": blob.get("occupancy"),
        "pool": blob.get("pool"),
        "garage_carport": blob.get("garage_carport"),
        "transaction_type": blob.get("transaction_type"),
        "payment_methods": blob.get("payment_methods"),
        "wholesaler": blob.get("wholesaler"),
        "region_bucket": region_bucket,
        "tri_county_name": tri_county_name,
    }
    return complete


def _ensure_source_email(fl: FilteredListing, gmail_message_id: str, blob: dict[str, Any]) -> FilteredListingEmail:
    now = _now()
    epoch = int(now.timestamp())
    description = (blob.get("raw_description_excerpt") or blob.get("complete_info") or "") or ""
    title = blob.get("source_title") or fl.title or fl.address or fl.listing_id or "FOM listing"
    url = blob.get("listing_url") or fl.url or ""
    text_parts = [title]
    if url:
        text_parts.append(url)
    if description:
        text_parts.append(str(description))
    text = "\n".join(text_parts)
    html = "<br>\n".join(text_parts)

    q = FilteredListingEmail.objects(
        account_label=ACCOUNT_LABEL,
        gmail_message_id=gmail_message_id,
    )
    q.update_one(
        upsert=True,
        set__subject=f"FOM {title}",
        set__window=WindowRange(after_epoch=epoch, before_epoch=epoch + 1),
        set__from_info=FromInfo(
            raw=SOURCE_EMAIL,
            name=blob.get("agent_name") or blob.get("wholesaler") or "Florida Off Market",
            email=SOURCE_EMAIL,
        ),
        set__rfc822_date=now.isoformat(),
        set__internal_date=InternalDate(ts_ms=int(epoch * 1000), iso=now.isoformat()),
        set__bodies=Bodies(text=text, html_full=html, html_ai=html),
        set__status="processed",
        set__forward_status="skipped",
        set__updated_at=now,
        set_on_insert__created_at=now,
    )
    saved = q.first()
    if not saved:
        raise RuntimeError(f"Failed to upsert scraper FilteredListingEmail for {gmail_message_id}")
    return saved


def _geocode(addr: str, city: str, state: str, zip_: str) -> tuple[str, str, str, Optional[dict]]:
    geo_js = None
    try:
        norm_city = _normalize_city_for_google(city)
        raw_line = _compose_raw_for_google(addr, norm_city, state, zip_)
        if raw_line:
            fa, fc, fz = get_street_and_city(raw_line)
            if fa and fc:
                addr, city = fa, fc
            if fz and not zip_:
                zip_ = fz
            geo_js = geocode_response(raw_line)
    except Exception:
        logger.exception("scrape geo format failed addr=%s", addr)
    return addr, city, zip_, geo_js


def _enqueue_one(fl: FilteredListing) -> Optional[str]:
    blob = _listing_blob(fl)
    addr = (blob.get("address") or "").strip()
    city = (blob.get("city") or "").strip()
    state = (blob.get("state") or "").strip()
    zip_ = (blob.get("zip") or "").strip()

    if not addr or is_bed_bath_descriptor_address(addr):
        fl.update(
            set__status="rejected",
            set__reject_reason="no usable address",
            set__updated_at=_now(),
        )
        return None

    addr, city, zip_, geo_js = _geocode(addr, city, state, zip_)
    blob["address"] = addr
    blob["city"] = city or None
    blob["zip"] = zip_ or None

    gmail_message_id = f"fom:{fl.listing_id}:{fl.id}"
    source_email = _ensure_source_email(fl, gmail_message_id, blob)
    price_val = blob.get("list_price_usd")

    q = ParsedListing.objects(
        account_label=ACCOUNT_LABEL,
        gmail_message_id=gmail_message_id,
        list_index=1,
    )
    updates = {
        "upsert": True,
        "set__source_email": source_email,
        "set__address": addr,
        "set__city": city or None,
        "set__state": state or None,
        "set__zip": zip_ or None,
        "set__price": price_val,
        "set__images": blob.get("images") or [],
        "set__other_images_source": None,
        "set__complete_info": blob,
        "set_on_insert__status": "not_processed",
        "set__direct_wholeseller": "not_found",
        "set__updated_at": _now(),
        "set_on_insert__created_at": _now(),
    }
    if geo_js is not None:
        updates["set__geo_code_response"] = geo_js
    q.update_one(**updates)

    saved = q.only("id").first()
    if not saved:
        raise RuntimeError(f"Failed to upsert ParsedListing for filtered {fl.id}")

    listing_id = str(saved.id)
    fl.update(
        set__parsed_listing_id=listing_id,
        set__address=addr,
        set__city=city or None,
        set__zip=zip_ or None,
        set__updated_at=_now(),
    )
    try:
        from observability.pipeline_metrics import record_listing_created
        record_listing_created(listing_id)
    except Exception:
        pass
    if addr and city:
        try:
            from pipeline.listing_details import ADDRESS_KEYS_POOL, _update_keys_async
            ADDRESS_KEYS_POOL.submit(_update_keys_async, listing_id, addr, city)
        except Exception:
            logger.exception("address_search_keys submit failed for %s", listing_id)
    return listing_id


def enqueue_pending_filtered(limit: int = 5) -> dict:
    created = 0
    rejected = 0
    errors = 0
    pending = FilteredListing.objects(status="pending", parsed_listing_id=None).limit(limit)
    for fl in pending:
        try:
            listing_id = _enqueue_one(fl)
            if listing_id:
                created += 1
            else:
                rejected += 1
        except Exception:
            errors += 1
            logger.exception("enqueue filtered %s failed", fl.id)
    return {"enqueued": created, "rejected_on_enqueue": rejected, "enqueue_errors": errors}


def sync_filtered_outcomes(limit: int = 50) -> dict:
    posted = 0
    rejected = 0
    in_flight = FilteredListing.objects(
        status="pending",
        parsed_listing_id__ne=None,
    ).limit(limit)
    for fl in in_flight:
        pl = ParsedListing.objects(id=fl.parsed_listing_id).only("status", "rules_ai_reason").first()
        if not pl:
            continue
        status = (pl.status or "").strip().lower()
        now = _now()
        if status == "posted":
            fl.update(
                set__status="posted",
                set__posted_at=now,
                set__reject_reason=None,
                set__updated_at=now,
            )
            posted += 1
        elif status in REJECT_STATUSES:
            reason = (pl.rules_ai_reason or status or "rejected").strip()
            fl.update(
                set__status="rejected",
                set__reject_reason=reason[:500],
                set__updated_at=now,
            )
            rejected += 1
    return {"synced_posted": posted, "synced_rejected": rejected}


def process_pending_scraped_listings(limit: int = 5) -> dict:
    sync_stats = sync_filtered_outcomes(limit=50)
    enqueue_stats = enqueue_pending_filtered(limit=limit)
    stats = {**enqueue_stats, **sync_stats}
    logger.info("process_pending_scraped_listings: %s", stats)
    return stats
