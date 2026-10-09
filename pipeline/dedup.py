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

# Item 3 "live-only" reappear (Blagojche 30.09). A prior only counts as a live duplicate when its
# WP post is still LIVE. If the original was hidden (special-avails aged out, or GlobiFlow/Podio
# privated) a fresh re-send must be allowed back up - UNLESS Podio says the deal is Sold / Under
# Contract (then it stays hidden). Env kill-switch so it can be turned off without a redeploy.
DEDUP_REAPPEAR_PUBLISH = os.getenv("DEDUP_REAPPEAR_PUBLISH", "1").strip().lower() not in ("0", "false", "no", "")
_PODIO_HOLD_STATUSES = {
    x.strip().lower() for x in os.getenv(
        "DEDUP_PODIO_HOLD_STATUSES", "sold,under contract,pending,pending sale,closed,dead").split(",") if x.strip()
}
# A (Rich 08.10 / Blagojche 09.10): only a DIRECT sender may bring a hidden / non-active deal back.
# A re-send from anyone else stays hidden (490 NW 3rd Terrace, 08.10). Env kill-switch.
REVIVE_DIRECT_ONLY = os.getenv("REVIVE_DIRECT_ONLY", "1").strip().lower() not in ("0", "false", "no", "")
_PODIO_NONACTIVE_STATUSES = {
    x.strip().lower() for x in os.getenv(
        "DEDUP_PODIO_NONACTIVE_STATUSES", "non-active,inactive").split(",") if x.strip()
}


def _sender_is_direct(pl) -> bool:
    try:
        from services.direct_wholesaler_service import listing_sender_is_direct as _lsd
        return bool(_lsd(pl))
    except Exception:
        return False


def _podio_status_for(prior) -> Optional[str]:
    """Best-effort Podio 'Status' text (e.g. 'Sold', 'Under Contract', 'Active') for the listing's
    Podio Properties item (buyer_matching_podio_item_id). Returns None when it cannot be determined.
    Never raises - a lookup failure must not block the pipeline."""
    try:
        item_id = getattr(prior, "buyer_matching_podio_item_id", None)
        if not item_id:
            return None
        from integrations.podio.direct_wholesaler import (
            get_podio_access_token as _pd_tok, _get_item as _pd_item,
            _get_property_status as _pd_status)
        token = _pd_tok()
        if not token:
            return None
        item = _pd_item(token, int(item_id))
        if not item:
            return None
        return _pd_status(item)
    except Exception:
        import logging as _lg
        _lg.exception("dedup reappear: podio status lookup failed prior=%s", getattr(prior, "id", None))
        return None


def _prior_post_is_live(prior):
    """True/False if we could resolve the prior's WP post liveness, else None (unknown)."""
    pid = getattr(prior, "post_id", None)
    if not pid:
        return None
    try:
        from integrations.wordpress.address_dedup import post_is_live as _pil
        return _pil(pid)
    except Exception:
        import logging as _lg
        _lg.exception("dedup reappear: post_is_live failed post=%s", pid)
        return None


def _addr_untrusted(addr) -> bool:
    """True when an address is masked / has no clean house number / empty - not safe to auto-
    reappear. Uses the same review_reason() the poster uses, so behaviour stays consistent."""
    try:
        from integrations.wordpress.address_dedup import review_reason as _rr
        return _rr(addr) in ("masked", "no_house_number", "empty")
    except Exception:
        return False


def _reappear_addr_untrusted(pl, prior, cand_list) -> bool:
    """Block reappear (Blagojche 30.09) when the NEW record OR the PRIOR has a masked / no-house-
    number address. Otherwise an intentionally-hidden masked dup (e.g. 52595 "13XX SE 1st Way",
    hidden because 1328 SE 1st Way is live) would come back on the next re-send, since the masked
    key never finds the live full-address post. Masked -> stays in review, as now."""
    addrs = [c[0] for c in (cand_list or [])]
    addrs.append(getattr(pl, "address", None))
    addrs.append(getattr(prior, "address", None))
    return any(_addr_untrusted(a) for a in addrs)


def _now() -> datetime:
    return datetime.utcnow()




# #38 (Blagojche 05.10) - Carlos: a re-send with a better description never reached the website,
# because a duplicate of a live post stops here in dedup. When the re-send is clearly richer, write
# its description onto the live post. v2 of staged patch 24, with three changes:
#   1. cheap pre-filter on the raw text BEFORE any AI call (about 400 duplicates a day, ~5 richer);
#   2. the post is updated by its ID through /update-desc (description only), never through /create;
#   3. at most one refresh per post per DESC_REFRESH_COOLDOWN_DAYS.
# DESC_REFRESH_IN_DEDUP: 0 = off (default), report = log candidates only (no AI call, no write), 1 = on.
DESC_REFRESH_IN_DEDUP = os.getenv("DESC_REFRESH_IN_DEDUP", "0").strip().lower()


def _carlos_raw_len(x) -> int:
    ci = getattr(x, "complete_info", None) or {}
    if isinstance(ci.get("complete_info"), dict):
        ci = ci["complete_info"]
    return len(str(ci.get("raw_description_excerpt") or "").strip())


def _carlos_refresh_desc_on_live_dup(pl, prior) -> None:
    mode = DESC_REFRESH_IN_DEDUP
    if mode not in ("1", "true", "yes", "on", "report"):
        return
    import logging as _lg
    import re as _re2
    try:
        if prior is None:
            return
        # 1. cheap pre-filter, no network, no AI
        min_gain = int(os.getenv("DESC_REFRESH_MIN_GAIN_CHARS", "250"))
        min_ratio = float(os.getenv("DESC_REFRESH_MIN_RATIO", "1.25"))
        new_raw = _carlos_raw_len(pl)
        if new_raw < min_gain:
            return
        # the caller may have loaded `prior` with .only(...): read the fields we need fresh
        from models import ParsedListing as _PL
        prior = _PL.objects(id=prior.id).only(
            "post_id", "address", "complete_info", "wp_property_description", "desc_refreshed_at").first()
        if not prior or not getattr(prior, "post_id", None):
            return
        prior_pid = prior.post_id
        old_raw = _carlos_raw_len(prior)
        if not (new_raw >= old_raw + min_gain and new_raw >= old_raw * min_ratio):
            return
        # 3. cooldown per live post
        last = getattr(prior, "desc_refreshed_at", None)
        cooldown = int(os.getenv("DESC_REFRESH_COOLDOWN_DAYS", "7"))
        if last and (_now() - last).days < cooldown:
            return
        house_no = (_re2.match(r"\s*(\d+[A-Za-z]?)\b", str(getattr(prior, "address", "") or "")) or [None, ""])[1]
        if not house_no:
            return  # no real house number -> cannot double-check the post, skip
        from integrations.wordpress.address_dedup import post_is_live as _pil
        if _pil(prior_pid) is not True:
            return
        if mode == "report":
            _lg.info("carlos refresh REPORT: would refresh post=%s id=%s raw new=%d old=%d", prior_pid, pl.id, new_raw, old_raw)
            return
        from integrations.wordpress.ai_property_description import ai_build_wp_property_description_by_id
        ai_build_wp_property_description_by_id(str(pl.id))
        pl.reload("wp_property_description")
        new_desc = (getattr(pl, "wp_property_description", None) or "").strip()
        old_desc = (getattr(prior, "wp_property_description", None) or "").strip()

        def _tlen(h):
            return len(_re2.sub(r"<[^>]+>", " ", h or "").strip())
        nl, ol = _tlen(new_desc), _tlen(old_desc)
        if not new_desc or not (nl >= ol + min_gain and nl >= ol * min_ratio):
            _lg.info("carlos refresh: generated desc not richer post=%s id=%s new=%d old=%d", prior_pid, pl.id, nl, ol)
            return
        import requests as _rq
        base = os.getenv("WP_API_BASE", "https://inventory.joinbuyerslist.com/wp-json/addproperty/v1")
        # 2. by post ID, description only; token in a header, never in the URL
        resp = _rq.post(base + "/update-desc", timeout=30,
                        headers={"X-Api-Token": os.getenv("WP_API_TOKEN") or ""},
                        json={"post_id": int(prior_pid), "postdesc": new_desc, "expect_house_no": house_no})
        ok = False
        try:
            ok = resp.status_code == 200 and bool(resp.json().get("success"))
        except Exception:
            ok = False
        if ok:
            _PL.objects(id=prior.id).update(set__wp_property_description=new_desc, set__desc_refreshed_at=_now())
            _lg.info("carlos refresh: post=%s id=%s desc %d -> %d chars", prior_pid, pl.id, ol, nl)
        else:
            _lg.warning("carlos refresh: post=%s id=%s -> %s %s", prior_pid, pl.id, resp.status_code, (resp.text or "")[:200])
    except Exception:
        _lg.exception("carlos refresh failed id=%s", getattr(pl, "id", None))


_MASK_RUN_RE = re.compile(r"^(\s*\d+)\s*((?:[^\w\s]|_){2,})\s*")  # legacy: used by the re-geocode gate
_MASK_CHARS_RE = re.compile(r"[*xX_#•]")                     # B.1: mask characters in a house number


def _jbl_masked_house_tok(tok: str) -> bool:
    """A leading house-number token that mixes real digits with mask chars (X/x/*/_/#) or a run of
    2+ dashes - i.e. the house number is partly or fully unknown. (Named uniquely to avoid the
    pre-existing _is_masked_num(sn) helper later in this file.)"""
    return bool(re.search(r"\d", tok)) and bool(_MASK_CHARS_RE.search(tok) or re.search(r"-{2,}", tok))


def normalize_masked_street(addr: str) -> str:
    """Build the GEOCODING input for a (possibly masked) street address.

    B.1 option B (Blagojche 01.10): a masked house number is UNKNOWN, so DROP it and let Google
    resolve the STREET (route level). That yields a stable place_id regardless of how the mask was
    written (2**, 2****, 2XX0, 2**0 all resolve to the same street), so dedup of masked listings
    stays consistent across sources and we never feed Google a fabricated number. A clean house
    number is kept as-is. Only the geocode INPUT is affected; the stored address (guarded to stay
    masked, never '0') and review_reason are unchanged.
    Examples:
      '2**0 NW 91st St'       -> 'NW 91st St'
      '2XX0 NW 91st St'       -> 'NW 91st St'
      '2 *** SW Natura Ave'   -> 'SW Natura Ave'   (space between number and mask)
      '22**/22** NW 56th Ave' -> 'NW 56th Ave'
      '2490 NW 91st St'       -> '2490 NW 91st St' (clean number kept)
      '644-646 Main St'       -> '644-646 Main St' (real range kept)
    """
    print("addr before masked func>>",addr)
    if not isinstance(addr, str) or not addr.strip():
        return addr
    s = addr.strip()
    # merge a space between a leading number and a mask run ("2 *** ..." -> "2*** ...") so the
    # house-number token is seen as one unit.
    s = re.sub(r"^(\d+)\s+([0-9xX*_#•]*[xX*_#•][0-9xX*_#•]*)(?=\s|$)", r"\1\2", s, count=1)
    parts = s.split(None, 1)
    head = parts[0]
    rest = parts[1] if len(parts) > 1 else ""
    if any(_jbl_masked_house_tok(t) for t in head.split("/")):
        return rest if rest else head           # drop the masked house number -> street-level geocode
    return head + ((" " + rest) if rest else "")



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

        # Item 3 "live-only" reappear (Blagojche 30.09): if the matched prior's WP post is NOT
        # live, it no longer occupies a live slot for this address, so drop the duplicate and let
        # this listing go back up (it still re-enters the poster, which keeps its own masked->review
        # and address gates). The remaining "prior is live" path below IS the live-address gate:
        # while a live post exists for the matched address, dups stay suppressed. Podio Sold /
        # Under Contract keeps it hidden even when the post is down.
        if prior is not None and DEDUP_REAPPEAR_PUBLISH:
            _prior_pid = getattr(prior, "post_id", None)
            _prior_live = _prior_post_is_live(prior)
            _is_direct = _sender_is_direct(pl)
            _pstat = None
            # A: a DIRECT sender also revives a deal whose Podio record is Non-Active while the WP
            # post is still up (the record is then taken over in the Podio linking step).
            _podio_nonactive = False
            if _prior_pid and _is_direct and _prior_live is True:
                _pstat = _podio_status_for(prior)
                _podio_nonactive = bool(_pstat) and _pstat.strip().lower() in _PODIO_NONACTIVE_STATUSES
            if _prior_pid and (_prior_live is False or _podio_nonactive):
                if _pstat is None:
                    _pstat = _podio_status_for(prior)
                if REVIVE_DIRECT_ONLY and not _is_direct:
                    import logging as _lg
                    _lg.info("dedup reappear: prior post %s not live (Podio=%s) but sender NOT direct "
                             "-> stay hidden (A) (id=%s src=%s)", _prior_pid, _pstat, pl.id, dedup_src)
                    # keep prior -> existing dup handling below (skipped / price-drop)
                elif _pstat and _pstat.strip().lower() in _PODIO_HOLD_STATUSES:
                    import logging as _lg
                    _lg.info("dedup reappear: prior post %s hidden but Podio=%s -> stay hidden "
                             "(id=%s src=%s)", _prior_pid, _pstat, pl.id, dedup_src)
                elif _reappear_addr_untrusted(pl, prior, cand_list):
                    import logging as _lg
                    _lg.info("dedup reappear: prior post %s not live but address masked/no-house "
                             "-> stay in review, no reappear (id=%s src=%s)", _prior_pid, pl.id, dedup_src)
                    # keep prior -> existing dup handling (masked stays in review, as now)
                else:
                    import logging as _lg
                    _lg.info("dedup reappear: prior post %s live=%s Podio=%s direct=%s -> re-publish (A) "
                             "(id=%s src=%s)", _prior_pid, _prior_live, _pstat, _is_direct, pl.id, dedup_src)
                    prior = None
                    dedup_src = None

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
            _carlos_refresh_desc_on_live_dup(pl, prior)  # 38, env-gated (default off)
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