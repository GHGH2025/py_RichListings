# process_dupes_30d.py
from datetime import datetime, timedelta
from typing import Optional, Tuple
from mongoengine.queryset.visitor import Q

# from mongo_engine_conn import init_db
from models import ParsedListing
import re

NEXT_STATUS_ON_PASS = "processed"                 # what to set on pass
HISTORICAL_STATUSES = ("skipped", "posted", "ready_to_post", "passed","processed","ready_for_image_processing","ready_for_primary_image_check")
PRICE_DROP_THRESHOLD = 0.06                       # 6%


def _now() -> datetime:
    return datetime.utcnow()




_MASK_RUN_RE = re.compile(r"^(\s*\d+)\s*((?:[^\w\s]|_){2,})\s*")

def normalize_masked_street(addr: str) -> str:
    """
    If address starts with a street number followed by a masked run like *** ___ ---,
    convert that run to 'xxx' (lowercase) so we standardize.
    Examples:
      '2*** SW Natura Ave...' -> '2xxx SW Natura Ave...'
      '2___ SW Natura Ave...' -> '2xxx SW Natura Ave...'
      '2--- SW Natura Ave...' -> '2xxx SW Natura Ave...'
    """
    print("addr before masked func>>",addr)
    if not isinstance(addr, str) or not addr.strip():
        return addr
    return _MASK_RUN_RE.sub(r"\1xxx ", addr.strip(), count=1)



def _best_addr_city_zip(pl: ParsedListing) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    ONLY use fields supported by your schema.
    Top-level first, then fallback to complete_info.*.
    """
    ci = pl.complete_info or {}
    addr = getattr(pl, "address", None) or ci.get("address")
    city = getattr(pl, "city", None) or ci.get("city")
    zip_ = getattr(pl, "zip", None) or ci.get("zip")

    def tidy(s): return s.strip() if isinstance(s, str) else None
    return tidy(addr), tidy(city), tidy(zip_)


def _price(pl: ParsedListing) -> Optional[float]:
    """Prefer top-level price; fallback to complete_info.list_price_usd."""
    if getattr(pl, "price", None) is not None:
        try:
            return float(pl.price)
        except Exception:
            pass
    ci = pl.complete_info or {}
    if ci.get("list_price_usd") is not None:
        try:
            return float(ci["list_price_usd"])
        except Exception:
            return None
    return None


def _reason(prefix: str, extra: str) -> str:
    return f"[dup-30d] {prefix}: {extra}"


def _addr_candidates(pl) -> list[tuple[str, str | None, str | None]]:
    """
    Return up to two (addr, city, zip) candidates:
    1) formatted/top-level fields (address, city, zip)
    2) raw/complete_info fields (complete_info.address, complete_info.city, complete_info.zip)
    Dedupes if both are identical/blank.
    """
    def _t(x): 
        return (x or "").strip()

    top_addr  = _t(getattr(pl, "address", None))
    top_city  = _t(getattr(pl, "city", None))
    top_zip   = _t(getattr(pl, "zip", None))

    ci        = getattr(pl, "complete_info", {}) or {}
    raw_addr  = _t(ci.get("address"))
    raw_city  = _t(ci.get("city"))
    raw_zip   = _t(ci.get("zip"))

    cands = []
    if top_addr:
        cands.append((top_addr, top_city or None, top_zip or None))
    if raw_addr:
        tup = (raw_addr, raw_city or None, raw_zip or None)
        if not cands or tup != cands[0]:
            cands.append(tup)
    return cands

def _find_recent_prior(addr: str, city: Optional[str], zip_: Optional[str],
                       since: datetime, exclude_id) -> Optional[ParsedListing]:
    """
    Find most-recent prior in last 30 days with SAME address (and city/zip when present),
    across top-level vs complete_info fields. Excludes the current doc.
    """
    # address match (no address_line anywhere)
    addr_q = Q(address__iexact=addr) | Q(complete_info__address__iexact=addr)

    loc_q = Q()
    if city:
        loc_q &= (Q(city__iexact=city) | Q(complete_info__city__iexact=city))
    if zip_:
        loc_q &= (Q(zip__iexact=zip_) | Q(complete_info__zip__iexact=zip_))

    qs = (
        ParsedListing.objects(
            addr_q
            & loc_q
            & Q(status__in=HISTORICAL_STATUSES)
            & Q(skipped_or_posted_at__gte=since)
            & Q(id__ne=exclude_id)
        )
        .only("price", "complete_info.list_price_usd", "skipped_or_posted_at", "status")
        .order_by("-skipped_or_posted_at")
    )
    print("addr>>",addr,">>",qs.first())
    return qs.first()

def _compose_raw_for_google(addr: str, city: str, state: str, zip_: str) -> str:
    parts = [p.strip() for p in [addr, city, state, zip_] if p and str(p).strip()]
    return ", ".join(parts + ["USA"]) if parts else ""

def _geo_extract(geo: dict) -> dict:
    """
    Pulls the bits we need from a standard Google Geocoding result object.
    Returns keys: pid, fa, postal, route, is_full.
    """
    if not isinstance(geo, dict):
        return {}
    fa   = geo.get("formatted_address")
    pid  = geo.get("place_id")
    types = geo.get("types", []) or []
    comps = geo.get("address_components", []) or []

    postal = route = street_number = None
    for c in comps:
        ts = c.get("types", []) or []
        if "postal_code" in ts:
            postal = c.get("long_name") or c.get("short_name")
        elif "route" in ts:
            route = c.get("short_name") or c.get("long_name")
        elif "street_number" in ts:
            street_number = c.get("long_name") or c.get("short_name")

    is_full = ("premise" in types) or ("street_address" in types)
    return {"pid": pid, "fa": fa, "postal": postal, "route": route,
            "street_number": street_number, "is_full": is_full}


def _ensure_geo(pl) -> Optional[dict]:
    """
    Ensure the current ParsedListing has geo_code_response.
    If missing, try to geocode from complete_info (fail-open).
    Persist if obtained.
    """
    geo = getattr(pl, "geo_code_response", None)
    ci = getattr(pl, "complete_info", {}) or {}
    addr_src = (ci.get("address") or getattr(pl, "address", "") or "")

    if isinstance(geo, dict) and geo and not _MASK_RUN_RE.match(addr_src.strip()):
        return geo

    # lazy-populate from complete_info if possible

    ci = getattr(pl, "complete_info", {}) or {}
    raw_addr = normalize_masked_street((ci.get("address") or getattr(pl, "address", "") or "").strip())
    raw = _compose_raw_for_google(
        raw_addr,
        (ci.get("city") or getattr(pl, "city", "") or "").strip(),
        (ci.get("state") or getattr(pl, "state", "") or "").strip(),
        (ci.get("zip") or getattr(pl, "zip", "") or "").strip(),
    )

    if not raw:
        return None

    try:
        # import from your google_formatter module
        from google_formatter import geocode_response
        geo = geocode_response(raw)
        if isinstance(geo, dict) and geo:
            ParsedListing.objects(id=pl.id).update_one(
                set__geo_code_response=geo,
                set__updated_at=_now(),
            )
            return geo
    except Exception:
        pass
    return None

def _find_recent_prior_geo(pl, since: datetime) -> Optional[ParsedListing]:
    """
    Fallback search for recent prior using stored (or freshly-fetched) geo_code_response.
    Priority: place_id → formatted_address → postal+route substring match → lat/lng proximity.
    """

    geo = _ensure_geo(pl)
    if not geo:
        return None

    x = _geo_extract(geo)
    base_q = (
        Q(status__in=HISTORICAL_STATUSES)
        & Q(skipped_or_posted_at__gte=since)
        & Q(id__ne=pl.id)
    )

    # 1) exact place_id
    if x.get("pid"):
        qs = (
            ParsedListing.objects(base_q & Q(geo_code_response__place_id=x["pid"]))
            .only("price", "complete_info.list_price_usd", "skipped_or_posted_at", "status", "geo_code_response")
            .order_by("-skipped_or_posted_at")
        )
        hit = qs.first()
        if hit:
            return hit

    # 2) exact formatted_address (case-insensitive)
    if x.get("fa"):
        qs = (
            ParsedListing.objects(base_q & Q(geo_code_response__formatted_address__iexact=x["fa"]))
            .only("price", "complete_info.list_price_usd", "skipped_or_posted_at", "status", "geo_code_response")
            .order_by("-skipped_or_posted_at")
        )
        hit = qs.first()
        if hit:
            return hit

    # 3) partial: require street_number + route + postal in formatted_address
    #    (skipped if street_number is missing to avoid false matches across
    #     different addresses on the same street + postal — e.g. 1589 vs 1701 NW 6th Ave)
    postal = x.get("postal")
    route  = x.get("route")
    street_number = x.get("street_number")
    if postal and route and street_number:
        street_prefix = f"{street_number} {route}"   # e.g. "1701 NW 6th Ave"
        qs = (
            ParsedListing.objects(
                base_q
                & Q(geo_code_response__formatted_address__icontains=street_prefix)
                & Q(geo_code_response__formatted_address__icontains=postal)
            )
            .only("price", "complete_info.list_price_usd", "skipped_or_posted_at", "status", "geo_code_response")
            .order_by("-skipped_or_posted_at")
        )
        hit = qs.first()
        if hit:
            return hit

    return None

def process_not_processed_with_duplicate_rule(limit: int = 500) -> dict:
    """
    For each `not_processed` listing:
      - If NO prior (same address/city/zip) within 30d => status -> processed (clear reason)
      - If prior exists:
          * If current price is >= 6% lower than prior => processed
          * Else => skipped with rules_ai_reason explaining why
    """

    since = _now() - timedelta(days=30)
    checked = processed = skipped = missing_addr = 0

    candidates = (
        ParsedListing.objects(status="verified")
        .only("address", "city", "zip", "state", "price", "complete_info", "geo_code_response", "skipped_or_posted_at", "status")
        .limit(limit)
    )

    for pl in candidates:

        checked += 1

        cand_list = _addr_candidates(pl)
        if not cand_list:
            # No usable address in either formatted or raw → skip conservatively
            pl.update(
                set__status="skipped",
                set__rules_ai_reason=_reason("no address available to match", "cannot dedupe"),
                set__skipped_or_posted_at=_now(),
                set__updated_at=_now(),
            )
            skipped += 1
            missing_addr += 1
            continue

        # addr, city, zip_ = _best_addr_city_zip(pl)
        # if not addr:
        #     # No address => cannot dedupe reliably; conservative skip
        #     pl.update(
        #         set__status="skipped",
        #         set__rules_ai_reason=_reason("no address available to match", "cannot dedupe"),
        #         set__skipped_or_posted_at=_now(),
        #         set__updated_at=_now(),
        #     )
        #     skipped += 1
        #     missing_addr += 1
        #     continue

            # try both: (formatted first, then raw complete_info)
        prior = None
        for (addr, city, zip_) in cand_list:
            prior = _find_recent_prior(addr, city, zip_, since, pl.id)
            if prior:
                break


        # NEW: geo fallback if not found by address/city
        if not prior:
            prior = _find_recent_prior_geo(pl, since)


        # prior = _find_recent_prior(addr, city, zip_, since, pl.id)

        if not prior:
            # No recent duplicate -> pass
            pl.update(
                set__status=NEXT_STATUS_ON_PASS,
                set__rules_ai_reason=None,
                set__updated_at=_now(),
            )
            processed += 1
            continue

        prev_price = _price(prior)
        curr_price = _price(pl)

        if prev_price is None or curr_price is None or prev_price <= 0:
            pl.update(
                set__status="skipped",
                set__rules_ai_reason=_reason(
                    "duplicate found but price comparison unavailable",
                    f"prev_id={prior.id} prev={prev_price}, curr={curr_price}"
                ),
                set__skipped_or_posted_at=_now(),
                set__updated_at=_now(),
            )
            skipped += 1
            continue

        drop = (prev_price - curr_price) / prev_price
        if drop >= PRICE_DROP_THRESHOLD:
            pl.update(
                set__status=NEXT_STATUS_ON_PASS,
                set__rules_ai_reason=None,
                set__updated_at=_now(),
            )
            processed += 1
        else:
            pl.update(
                set__status="skipped",
                set__rules_ai_reason=_reason(
                    "duplicate found; price not low enough",
                    f"prev_id={prior.id} drop={drop:.1%} (< 6%) prev={prev_price:.0f} -> curr={curr_price:.0f}"
                ),
                set__skipped_or_posted_at=_now(),
                set__updated_at=_now(),
            )
            skipped += 1

    return {
        "checked": checked,
        "processed": processed,
        "skipped": skipped,
        "missing_address": missing_addr,
        "lookback_days": 30,
        "price_drop_threshold": PRICE_DROP_THRESHOLD,
        "next_status_on_pass": NEXT_STATUS_ON_PASS,
    }


# if __name__ == "__main__":
#     stats = process_not_processed_with_duplicate_rule(limit=500)
#     print(stats)