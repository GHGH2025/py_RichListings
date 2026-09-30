# process_dupes_30d.py
from datetime import datetime, timedelta
from typing import Optional, Tuple
from mongoengine.queryset.visitor import Q

# from db.mongo_engine_conn import init_db
from models import ParsedListing
import os
import re

NEXT_STATUS_ON_PASS = "processed"                 # what to set on pass
# Only a real post starts/resets the 30-day clock. Skipped copies must not
# extend the window (otherwise every dup-skip stamps skipped_or_posted_at=now).
HISTORICAL_STATUSES = ("posted",)
PRICE_DROP_THRESHOLD = 0.06                       # 6%
PRICE_DROP_MAX_AUTO = float(os.getenv("PRICE_DROP_MAX_AUTO", "0.50"))  # Rich 30.09: >50% drop = likely misread -> hold for review, never auto-update


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
        return _sanitize_match_text(x) or ""

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

def _sanitize_match_text(value: Optional[str]) -> Optional[str]:
    """Strip NULs/control chars that break Mongo regex (iexact) queries."""
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    cleaned = "".join(ch for ch in value if ch == "\t" or ord(ch) >= 32).strip()
    return cleaned or None


def _find_recent_prior(addr: str, city: Optional[str], zip_: Optional[str],
                       since: datetime, exclude_id) -> Optional[ParsedListing]:
    """
    Find most-recent prior POST in last 30 days with SAME address (and city/zip when present),
    across top-level vs complete_info fields. Excludes the current doc. Skipped listings
    are ignored so a dup-skip cannot keep the 30-day window rolling.
    """
    addr = _sanitize_match_text(addr)
    city = _sanitize_match_text(city)
    zip_ = _sanitize_match_text(zip_)
    if not addr:
        return None

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
            # Dev/test dry-run seeds use gmail_message_id prefix test_ — never treat as live history.
            & Q(gmail_message_id__not__startswith="test_")
        )
        .only("price", "complete_info.list_price_usd", "skipped_or_posted_at", "status")
        .order_by("-skipped_or_posted_at")
    )
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
        from integrations.google_formatter import geocode_response
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
    Fallback search for a recent posted prior using stored (or freshly-fetched) geo_code_response.
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
        & Q(gmail_message_id__not__startswith="test_")
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

import re as _re

def _house_number_token(addr):
    if not isinstance(addr, str):
        return None, "none"
    m = _re.match(r"\s*([0-9Xx\*_\-]+)\b", addr.strip())
    if not m:
        return None, "none"
    tok = m.group(1)
    has_digit = any(c.isdigit() for c in tok)
    has_wild = any(c in "Xx*_-" for c in tok)
    if has_digit and has_wild:
        return tok, "masked_digits"
    if has_digit:
        return tok, "digits"
    return tok, "masked_all"

def _digit_consistent(mask_tok, full_num):
    if not mask_tok or not full_num:
        return False
    m = mask_tok.strip(); n = str(full_num).strip()
    if len(m) != len(n):
        return False
    for cm, cn in zip(m, n):
        if cm.isdigit() and cm != cn:
            return False
    return True

def _bb_sqft_match(pl, cand):
    a = pl.complete_info or {}; b = cand.complete_info or {}
    def _i(v):
        try: return int(v)
        except Exception: return None
    beds_a, beds_b = _i(a.get("bedrooms")), _i(b.get("bedrooms"))
    def _bath(d):
        if d.get("bathrooms_full") is None: return None
        return (_i(d.get("bathrooms_full")) or 0) + (_i(d.get("bathrooms_half")) or 0)
    bath_a, bath_b = _bath(a), _bath(b)
    bb = (beds_a is not None and beds_a == beds_b) and (bath_a is not None and bath_a == bath_b)
    sa, sb = _i(a.get("living_area_sqft")), _i(b.get("living_area_sqft"))
    sq = (sa is not None and sb is not None and sa > 0 and abs(sa - sb) <= max(1, 0.02 * sa))
    return bool(bb or sq)

def _is_masked_num(sn):
    return (not sn) or (str(sn).strip() == "0")

def _find_recent_prior_cross_source(pl, since: datetime) -> Optional[ParsedListing]:
    """
    Cross-source duplicate on route + city + zip + price +/-1%, 30 days, prior status 'posted'.
    OPT1: geocoder '0' street_number == masked. OPT2: zipless -> require city + bb/sqft, never route-only.
    Forward (masked new): prior_masked | digit_consistent | bb_sqft | zipless_city_bbsqft.
    Reverse (full new):   masked prior whose house number is digit-consistent with the new full number
                          (Rich's 147th case: '1070' vs earlier '1**0'/'0 NW 147th').
    """
    geo = _ensure_geo(pl)
    if not geo:
        return None
    x = _geo_extract(geo)
    sn = x.get("street_number")
    new_full = bool(sn) and str(sn).strip() != "0"      # OPT1: '0' == masked, else a real number
    route = x.get("route")
    if not route:
        return None
    postal = x.get("postal")
    price = _price(pl)
    if price is None or price <= 0:
        return None
    lo, hi = price * 0.99, price * 1.01
    _, city, _ = _best_addr_city_zip(pl)
    zipless = not postal
    if zipless and not city:                            # OPT2: never route-only without city
        return None
    new_addr = (pl.complete_info or {}).get("address") or getattr(pl, "address", "") or ""
    new_tok, new_kind = _house_number_token(new_addr)
    q = (
        Q(status__in=HISTORICAL_STATUSES)
        & Q(skipped_or_posted_at__gte=since)
        & Q(id__ne=pl.id)
        & Q(gmail_message_id__not__startswith="test_")
        & Q(geo_code_response__formatted_address__icontains=route)
    )
    if postal:
        q &= Q(geo_code_response__formatted_address__icontains=postal)
    if city:
        q &= (
            Q(city__iexact=city)
            | Q(complete_info__city__iexact=city)
            | Q(geo_code_response__formatted_address__icontains=city)
        )
    qs = (
        ParsedListing.objects(q)
        .only("price", "complete_info", "skipped_or_posted_at", "status", "geo_code_response", "address", "city")
        .order_by("-skipped_or_posted_at")
    )
    for cand in qs.limit(50):
        cp = _price(cand)
        if cp is None or not (lo <= cp <= hi):
            continue
        cgeo = _ensure_geo(cand)
        csn = _geo_extract(cgeo).get("street_number") if cgeo else None
        if new_full:
            # REVERSE: full new vs a MASKED prior, house number digit-consistent
            if not _is_masked_num(csn):
                continue
            ptok, _pk = _house_number_token((cand.complete_info or {}).get("address") or getattr(cand, "address", "") or "")
            if not _digit_consistent(ptok, sn):
                continue
            guard = "reverse_digit_consistent"
        elif zipless:
            if not _bb_sqft_match(pl, cand):
                continue
            guard = "zipless_city_bbsqft"
        elif _is_masked_num(csn):
            guard = "prior_masked"
        elif new_kind == "masked_digits":
            if not _digit_consistent(new_tok, csn):
                continue
            guard = "digit_consistent"
        else:
            if not _bb_sqft_match(pl, cand):
                continue
            guard = "bb_sqft"
        print(f"[dedup] cross-source match new={pl.id} prior={cand.id} route={route!r} zip={postal} city={city!r} price={price}/{cp} guard={guard}")
        return cand
    return None



def process_not_processed_with_duplicate_rule(
    limit: int = 500,
    gmail_message_id: Optional[str] = None,
) -> dict:
    """
    For each `verified` listing:
      - If NO prior posted listing (same address/city/zip) within 30d => status -> processed
      - If a posted prior exists:
          * If current price is >= 6% lower than that post => processed
          * Else => skipped with rules_ai_reason explaining why
    Skipped listings are not used as history.
    """

    since = _now() - timedelta(days=30)
    checked = processed = skipped = missing_addr = 0

    q = ParsedListing.objects(status="verified")
    if gmail_message_id:
        q = q.filter(gmail_message_id=gmail_message_id)
    else:
        q = q.filter(gmail_message_id__not__startswith="test_")
    candidates = (
        q.only("address", "city", "zip", "state", "price", "complete_info", "geo_code_response", "skipped_or_posted_at", "status")
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
            try:
                from observability.pipeline_metrics import record_listing_stage
                record_listing_stage(str(pl.id), "dedup_skipped", listing_status="skipped", skip_reason="no address")
            except Exception:
                pass
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
        dedup_src = None
        for (addr, city, zip_) in cand_list:
            prior = _find_recent_prior(addr, city, zip_, since, pl.id)
            if prior:
                dedup_src = "exact"
                break

        # NEW: geo fallback if not found by address/city
        if not prior:
            prior = _find_recent_prior_geo(pl, since)
            if prior:
                dedup_src = "geo"

        # NEW: cross-source dup (masked or full via reverse), guarded opt1+opt2
        if not prior:
            prior = _find_recent_prior_cross_source(pl, since)
            if prior:
                dedup_src = "cross_source"


        # prior = _find_recent_prior(addr, city, zip_, since, pl.id)

        if not prior:
            # No recent duplicate -> pass
            pl.update(
                set__status=NEXT_STATUS_ON_PASS,
                set__rules_ai_reason=None,
                set__updated_at=_now(),
            )
            try:
                from observability.pipeline_metrics import record_listing_stage
                record_listing_stage(str(pl.id), "dedup", listing_status=NEXT_STATUS_ON_PASS)
            except Exception:
                pass
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
            try:
                from observability.pipeline_metrics import record_listing_stage
                record_listing_stage(str(pl.id), "dedup_skipped", listing_status="skipped", skip_reason=f"price comparison unavailable [{dedup_src}]")
            except Exception:
                pass
            skipped += 1
            continue

        drop = (prev_price - curr_price) / prev_price
        if drop > PRICE_DROP_MAX_AUTO:
            # Rich 30.09: >50% drop is almost always an extractor misread (52270 "$410,00"
            # -> 41000 = 89.7%). Never auto-update: hold for review (price_drop_pass stays
            # False so process_price_drop_activations skips it). Findable via this reason /
            # the "price_drop_review_held" metric stage / price_drop_pct>0.5 & pass=False.
            pl.update(
                set__status="price_drop_review",
                set__price_drop_pass=False,
                set__price_drop_pct=float(drop),
                set__price_drop_prev_id=str(prior.id),
                set__price_drop_prev_price=float(prev_price),
                set__price_drop_curr_price=float(curr_price),
                set__price_drop_activated=False,
                set__rules_ai_reason=_reason(
                    "price drop > 50% held for review (likely extractor misread)",
                    f"prev_id={prior.id} drop={drop:.1%} prev={prev_price:.0f} -> curr={curr_price:.0f}"
                ),
                set__skipped_or_posted_at=_now(),
                set__updated_at=_now(),
            )
            try:
                from observability.pipeline_metrics import record_listing_stage
                record_listing_stage(str(pl.id), "price_drop_review_held", listing_status="price_drop_review", skip_reason="price_drop_gt_50pct")
            except Exception:
                pass
            skipped += 1
        elif drop > 0:
            pl.update(
                set__status=NEXT_STATUS_ON_PASS,
                set__rules_ai_reason=None,
                set__price_drop_pass=True,
                set__price_drop_pct=float(drop),
                set__price_drop_prev_id=str(prior.id),
                set__price_drop_prev_price=float(prev_price),
                set__price_drop_curr_price=float(curr_price),
                set__price_drop_activated=False,
                set__price_drop_activate_error=None,
                set__updated_at=_now(),
            )
            try:
                from observability.pipeline_metrics import record_listing_stage
                record_listing_stage(str(pl.id), "dedup", listing_status=NEXT_STATUS_ON_PASS)
            except Exception:
                pass
            processed += 1
        else:
            pl.update(
                set__status="skipped",
                set__rules_ai_reason=_reason(
                    "duplicate found; no price reduction",
                    f"prev_id={prior.id} drop={drop:.1%} (<= 0) prev={prev_price:.0f} -> curr={curr_price:.0f}"
                ),
                set__skipped_or_posted_at=_now(),
                set__updated_at=_now(),
            )
            try:
                from observability.pipeline_metrics import record_listing_stage
                record_listing_stage(str(pl.id), "dedup_skipped", listing_status="skipped", skip_reason=f"duplicate; price not low enough [{dedup_src}]")
            except Exception:
                pass
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