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
SOURCE_EMAILS = {
    "florida_off_market": "floridaoffmarket@scraper",
    "rezzie": "rezzie@scraper",
}

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
        raw_price = blob.get("price_usd") or blob.get("purchase_price") or blob.get("price")
        try:
            price_val = float(str(raw_price).replace("$", "").replace(",", "")) if raw_price is not None else None
        except (TypeError, ValueError):
            price_val = None

    images = _clean_images(blob.get("images"))
    description = blob.get("description")
    source = (fl.source_website or fl.source or "florida_off_market").strip().lower()
    is_rezzie = source == "rezzie"
    agent_name = blob.get("seller_name") if is_rezzie else blob.get("wholesaler")
    agent_email = blob.get("seller_email") if is_rezzie else None
    agent_phone = blob.get("seller_phone") if is_rezzie else None
    complete = {
        "complete_info": description,
        "source_title": blob.get("title") or fl.title,
        "listing_url": blob.get("url") or fl.url,
        "mls_id": blob.get("listing_id") or fl.listing_id,
        "agent_name": agent_name,
        "agent_phone": agent_phone,
        "agent_email": agent_email,
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
        "estimated_repairs": blob.get("estimated_repairs") or blob.get("estimated_rehab"),
        "occupancy": blob.get("occupancy"),
        "pool": blob.get("pool"),
        "garage_carport": blob.get("garage_carport"),
        "transaction_type": blob.get("transaction_type"),
        "payment_methods": blob.get("payment_methods"),
        "wholesaler": agent_name,
        "input_source": "web",
        "source_website": source,
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
    source = (fl.source_website or fl.source or "florida_off_market").strip().lower()
    source_email = SOURCE_EMAILS.get(source, f"{source}@scraper")
    q.update_one(
        upsert=True,
        set__subject=f"{source.replace('_', ' ').title()} {title}",
        set__window=WindowRange(after_epoch=epoch, before_epoch=epoch + 1),
        set__from_info=FromInfo(
            raw=source_email,
            name=blob.get("agent_name") or blob.get("wholesaler") or source.replace("_", " ").title(),
            email=source_email,
        ),
        set__rfc822_date=now.isoformat(),
        set__internal_date=InternalDate(ts_ms=int(epoch * 1000), iso=now.isoformat()),
        set__bodies=Bodies(text=text, html_full=html, html_ai=html),
        set__status="processed",
        set__input_source="web",
        set__source_website=source,
        set__forward_status="skipped",
        set__updated_at=now,
        set_on_insert__created_at=now,
    )
    saved = q.first()
    if not saved:
        raise RuntimeError(f"Failed to upsert scraper FilteredListingEmail for {gmail_message_id}")
    return saved


def _ensure_web_wholesaler(blob: dict[str, Any]) -> bool:
    """Upsert a structured website seller into the local wholesaler directory."""
    email = (blob.get("agent_email") or "").strip().lower()
    if "@" not in email:
        return False
    name = (blob.get("agent_name") or blob.get("wholesaler") or email.split("@", 1)[0]).strip()
    try:
        from services.direct_wholesaler_service import create_wholesaler, get_by_sender_email

        if not get_by_sender_email(email):
            create_wholesaler(
                sender_email=email,
                email=email,
                name=name,
                phone=(blob.get("agent_phone") or "").strip(),
                update_flag_for_podio=True,
            )
        return True
    except Exception:
        logger.exception("web wholesaler upsert failed for %s", email)
        return False


def _enrich_fom_wholesaler_contact(blob: dict[str, Any]) -> None:
    """Use the existing AI contact extractor only for FOM's unstructured seller text."""
    if (blob.get("agent_email") or "").strip():
        return
    raw = "\n".join(
        part for part in (
            str(blob.get("source_title") or "").strip(),
            str(blob.get("wholesaler") or "").strip(),
            str(blob.get("complete_info") or "").strip(),
        ) if part
    )
    if not raw:
        return
    try:
        from pipeline.listing_details import extract_listings_from_email_html

        extracted = extract_listings_from_email_html(f"<pre>{raw}</pre>")
        candidate = next(iter(extracted.get("listings") or []), {})
        for field in ("agent_name", "agent_email", "agent_phone"):
            if candidate.get(field) and not blob.get(field):
                blob[field] = candidate[field]
    except Exception:
        # Contact enrichment must not block a valid property from entering the
        # normal rules/media pipeline.
        logger.exception("FOM wholesaler AI enrichment failed listing=%s", blob.get("mls_id"))


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

    source = (fl.source_website or fl.source or "florida_off_market").strip().lower()
    if source == "florida_off_market":
        _enrich_fom_wholesaler_contact(blob)
    # A wholesaler is mandatory for web listings because the web source must
    # be traceable to a local/Podio wholesaler record. Email and WhatsApp do
    # not use this gate; their senders may be non-wholesaler accounts.
    if source in {"florida_off_market", "rezzie"} and not _ensure_web_wholesaler(blob):
        fl.update(
            set__status="rejected",
            set__reject_reason="web listing missing a usable wholesaler email",
            set__updated_at=_now(),
        )
        return None
    gmail_message_id = f"web:{source}:{fl.listing_id}:{fl.id}"
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
        "set__direct_wholeseller": "not_processed" if blob.get("agent_email") else "no_agent_email",
        "set__input_source": "web",
        "set__source_website": source,
        "set__web_publish_enabled": False,
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
