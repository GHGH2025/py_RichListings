# wp_sync_poster.py
import os
import time
from datetime import datetime
import json
import requests
from typing import Optional, Dict, Any, List
from urllib.parse import urlparse, parse_qsl, urlunparse, urlencode
from models import ParsedListing  # mongoengine document
from pipeline.address_utils import resolve_street_address
from ai.media_verify import _image_mirror_updates, mirror_images_to_s3
from integrations.wordpress.wp_lookup import search_keys, UNREACHABLE
from integrations.wordpress.address_dedup import (
    classify as _dedup_classify, mongo_finder as _dedup_finder,
    DUPLICATE as _DEDUP_DUP, NEEDS_REVIEW as _DEDUP_REVIEW, NEW as _DEDUP_NEW,
    canonical_key as _dedup_key,
    post_is_live as _post_is_live,
)
import logging

WP_TOKEN = os.getenv("WP_API_TOKEN")  # <-- set in env


# Ruben/ZCG (Rich 29.09): direct wholesalers who never include a house number. Exempt these
# senders from the no-house-number review BLOCK - publish anyway, de-duped by street+city+PRICE.
ADDRESS_REVIEW_EXEMPT_SENDERS = {
    s.strip().lower() for s in os.getenv(
        "ADDRESS_REVIEW_EXEMPT_SENDERS",
        "investors@ecologicteam.com,info@zcginvestments.com").split(",") if s.strip()
}

import re as _re_sp
_STREET_SUFFIX = _re_sp.compile(r"\b(st|street|ave|avenue|blvd|boulevard|ct|court|dr|drive|"
    r"ln|lane|way|ter|terrace|rd|road|pl|place|cir|circle|trl|trail|hwy|highway|pkwy|parkway|"
    r"loop|run|pt|point|sq|square|walk|row|path|cove|manor|oaks?|park|estates?|crossing|"
    r"landing|ridge|hills?)\b", _re_sp.I)
_STREET_CR = _re_sp.compile(r"\b(cr|county road|sr|state road|us|route)\b", _re_sp.I)
_STREET_JUNK = {"next to", "high st", "w line st"}


def _is_real_street(addr, city):
    """Only auto-publish an exempt no-house-number listing when it looks like a REAL street
    (Blagojche 30.09): needs a city, a street-type suffix (or County Road), a street NAME of
    >=3 letters, and not a known junk fragment. Catches 'A Ln' (name<3), 'W Line St'/'High St'/
    'Next To'. A real Ecologic address ('Jessamine Ave, Sanford') passes."""
    a = (addr or "").strip(); c = (city or "").strip()
    if not a or not c:
        return False
    if a.lower() in _STREET_JUNK:
        return False
    if _STREET_CR.search(a) and _re_sp.search(r"\d", a):
        return True   # County/State Road + number (e.g. 'CR 422') is a valid address
    if not _STREET_SUFFIX.search(a):
        return False
    name = _STREET_SUFFIX.split(a)[0]
    if len(_re_sp.sub(r"[^A-Za-z]", "", name)) < 3:
        return False
    return True


# --- #2 defer listing_posted (Blagojche 01.10); env-gated OFF by default ---
DEFER_LISTING_POSTED = os.getenv("DEFER_LISTING_POSTED", "0").strip().lower() not in ("0", "false", "no", "")
# --- #26 (Blagojche 02.10, option B): re-email buyers on a real price drop WITHOUT a dup
# Podio item. SEPARATE flag, default OFF; only active when DEFER_LISTING_POSTED is also on.
DEFER_PRICE_DROP_REMATCH = os.getenv("DEFER_PRICE_DROP_REMATCH", "0").strip().lower() not in ("0", "false", "no", "")
try:
    # dedup sets price_drop_pass on ANY drop>0 (<=50%); require >= this to rematch so tiny
    # drops (e.g. 1201 $325k->$320k = 1.5%) do NOT spam buyers. 0.06 == Today's Deals threshold.
    DEFER_PRICE_DROP_MIN_PCT = float(os.getenv("DEFER_PRICE_DROP_MIN_PCT", "0.06"))
except Exception:
    DEFER_PRICE_DROP_MIN_PCT = 0.06


def _defer_fire_listing_posted(pl):
    """Fire listing_posted here (after the WP decision) for genuinely-new/review listings, only when
    DEFER_LISTING_POSTED is on. Idempotent (Blagojche 01.10): fire at most once per listing and only
    when it has no Podio item yet - so a listing re-seen on a later pass (e.g. while still in
    needs_address_review) never creates a second Podio item."""
    if not DEFER_LISTING_POSTED:
        return
    if getattr(pl, "buyer_matching_podio_item_id", None):
        return   # already has a Podio item -> never fire again
    if getattr(pl, "listing_posted_fired_at", None):
        return   # already fired on an earlier pass
    try:
        from ai.whatsapp_posts import _post_listing_to_webhook
        _ok = _post_listing_to_webhook(pl.id)
        if _ok:
            pl.update(set__listing_posted_fired_at=datetime.utcnow())
        else:
            # Blagojche 02.10: webhook NOT confirmed -> do NOT record fired_at, so the next
            # pass can re-fire (no permanent item-less listing on a transient webhook failure).
            logging.warning("defer: listing_posted webhook unconfirmed, fired_at NOT set id=%s", getattr(pl, "id", None))
    except Exception:
        logging.exception("defer listing_posted fire failed id=%s", getattr(pl, "id", None))


def _defer_maybe_rematch_price_drop(pl, post_id):
    """(B) gated by DEFER_PRICE_DROP_REMATCH (default OFF): on a real price drop, re-email
    buyers the NEW price with NO duplicate Podio item. The existing item is already copied
    onto this (already_found) drop-copy PL; set it pending+rematch so run_buyer_matching_cron
    re-runs match_buyers, which reads the new price from the PL (matching_api ~1892:
    list_price_usd/price). Threshold: price_drop_pass (dedup, any drop>0<=50%) AND
    price_drop_pct >= DEFER_PRICE_DROP_MIN_PCT (default 0.06). Once-per-drop: skip if a sibling
    PL with the SAME post_id AND SAME price is already pending/processing/matched (acct1+acct2
    2-min race). NOTE: the Podio ITEM price is refreshed by the existing price-drop Active
    webhook (process_price_drop_activations); confirm GlobiFlow's buyer email shows the new price."""
    if not (DEFER_LISTING_POSTED and DEFER_PRICE_DROP_REMATCH):
        return
    if not getattr(pl, "price_drop_pass", False):
        return
    try:
        drop = float(getattr(pl, "price_drop_pct", 0) or 0)
    except Exception:
        drop = 0.0
    if drop < DEFER_PRICE_DROP_MIN_PCT:
        logging.info("defer price-drop: skip rematch (drop %.3f < %.3f) id=%s", drop, DEFER_PRICE_DROP_MIN_PCT, getattr(pl, "id", None))
        return
    try:
        dup = ParsedListing.objects(post_id=post_id, price=pl.price, id__ne=pl.id,
                                    buyer_matching_status__in=["pending", "processing", "matched"]).first()
        if dup:
            logging.info("defer price-drop: sibling already rematched post=%s price=%s id=%s", post_id, pl.price, getattr(pl, "id", None))
            return
        pl.update(set__buyer_matching_status="pending", set__rematch=True,
                  set__re_matched_buyer_ids=[], set__updated_at=datetime.utcnow())
        logging.info("defer price-drop: rematch queued (drop %.3f) post=%s price=%s id=%s", drop, post_id, pl.price, getattr(pl, "id", None))
    except Exception:
        logging.exception("defer price-drop rematch failed id=%s post=%s", getattr(pl, "id", None), post_id)


def _defer_link_existing_podio(pl, post_id):
    """For a dup (already_found/dedup_linked): reuse the existing post's Podio item instead of
    creating a new one. Copies buyer_matching_podio_item_id from the ParsedListing that owns this
    post_id. Only when DEFER_LISTING_POSTED is on. If no sibling with an item is found (e.g. the
    post was created manually / by the importer - Ekta case), log it so we can see how often; the
    copy then stays without a Podio item and without a webhook (Blagojche 01.10)."""
    if not DEFER_LISTING_POSTED or not post_id:
        return
    try:
        sib = (ParsedListing.objects(post_id=post_id, buyer_matching_podio_item_id__ne=None,
                                     id__ne=pl.id)
               .only("buyer_matching_podio_item_id").order_by("-updated_at").first())
        if sib and getattr(sib, "buyer_matching_podio_item_id", None):
            pl.update(set__buyer_matching_podio_item_id=int(sib.buyer_matching_podio_item_id))
            _defer_maybe_rematch_price_drop(pl, post_id)  # #26 (option B): re-email on a real drop, same item
        else:
            logging.info("defer: no sibling item post=%s id=%s", post_id, getattr(pl, "id", None))
    except Exception:
        logging.exception("defer link existing podio failed id=%s post=%s", getattr(pl, "id", None), post_id)


def _pl_sender_email(pl) -> str:
    """Normalised sender email of a ParsedListing (from_info, else the source_email ref)."""
    try:
        e = (getattr(getattr(pl, "from_info", None), "email", "") or "").strip().lower()
        if e:
            return e
    except Exception:
        pass
    try:
        doc = getattr(pl, "source_email", None)
        return (getattr(getattr(doc, "from_info", None), "email", "") or "").strip().lower()
    except Exception:
        return ""


def _streetcity_price_dup(pl, addr, city):
    """An already-POSTED ParsedListing with the same canonical street+city AND the same price."""
    price = getattr(pl, "price", None)
    key = _dedup_key(addr, city)
    if price is None or not key or len(key) < 6:
        return None
    first = key.split(" ", 1)[0]
    for cand in ParsedListing.objects(post_id__ne=None, address__istartswith=first, id__ne=pl.id).only(
            "id", "address", "city", "price", "post_id"):
        if _dedup_key(getattr(cand, "address", None), getattr(cand, "city", None)) == key \
                and getattr(cand, "price", None) == price:
            return cand
    return None
WP_BASE  = os.getenv("WP_API_BASE", "https://inventory.joinbuyerslist.com/wp-json/addproperty/v1")

GET_URL  = f"{WP_BASE}/getproperty"
POST_URL = f"{WP_BASE}/create"

REQUEST_TIMEOUT = 20  # seconds

def _trim(s: Optional[str]) -> Optional[str]:
    if s is None:
        return None
    s2 = str(s).strip()
    return s2 if s2 else None

def _first(lst: Optional[List[Any]]) -> Optional[Any]:
    if isinstance(lst, list) and lst:
        return lst[0]
    return None

def _clean_featured_image_url(url: str) -> str:
    """Remove rdr=true from query; if it's the only param, drop the whole query."""
    try:
        p = urlparse(url)
        if not p.query:
            return url
        qs = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k.lower() != "rdr"]
        new_query = urlencode(qs, doseq=True)
        return urlunparse((p.scheme, p.netloc, p.path, p.params, new_query, p.fragment))
    except Exception:
        # On any parsing error, fall back to original
        return url

def _compose_full_address(pl: ParsedListing) -> Optional[str]:
    """
    posttitle/address format:
    "<address>, <city>, <state> <zip> USA"
    If any piece is missing, omit gracefully. If address+city missing, return None.
    """
    addr  = _trim(resolve_street_address(pl))
    city  = _trim(getattr(pl, "city", None))    or _trim((getattr(pl, "complete_info", {}) or {}).get("city"))
    state = _trim(getattr(pl, "state", None))   or _trim((getattr(pl, "complete_info", {}) or {}).get("state"))
    zip_  = _trim(getattr(pl, "zip", None))     or _trim((getattr(pl, "complete_info", {}) or {}).get("zip"))

    # Require at least street+city
    if not addr or not city:
        return None

    parts = [addr, city]
    tail = " ".join([p for p in [state, zip_] if _trim(p)])
    if tail:
        parts.append(tail)
    parts.append("USA")
    return ", ".join(parts)

def _main_search_key(pl: ParsedListing) -> Optional[str]:
    """
    First GET should use "<address>, <city>" (no state/zip)
    """
    addr = _trim(resolve_street_address(pl))
    city = _trim(getattr(pl, "city", None))    or _trim((getattr(pl, "complete_info", {}) or {}).get("city"))
    if not addr or not city:
        return None
    return f"{addr}, {city}"

def _wp_get(address_city: str) -> Optional[Dict[str, Any]]:
    """
    Call WP GET search. Returns parsed JSON on 200, else None.
    """
    try:
        resp = requests.get(
            GET_URL,
            params={"address": address_city, "token": WP_TOKEN},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception:
        return None

def _build_post_body(pl: ParsedListing) -> Dict[str, Any]:
    """
    Build POST payload from whatever we have.
    Only include fields that exist. Always include token.
    Static fields:
      deal_type = ["MLS Deals"]
      newest_deals = ["Daily Deal Email"]
    """
    # B (28.09): do NOT hardcode the Today tag here - /create upserts by address, so an old
    # post that matches gets re-tagged (1417 Walter). The tag is added only in the real create.
    body: Dict[str, Any] = {"token": WP_TOKEN}

    # title/address lines
    full_addr_line = _compose_full_address(pl)
    if full_addr_line:
        body["posttitle"] = full_addr_line
        body["address"]   = full_addr_line

    # description (HTML) from wp_property_description
    desc = _trim(getattr(pl, "wp_property_description", None))
    if desc:
        body["postdesc"] = desc

    # featured image (first) — remirror to S3 and persist images + images_s3
    orig_imgs = list(getattr(pl, "images", None) or [])
    imgs = mirror_images_to_s3(orig_imgs)
    persist = _image_mirror_updates(orig_imgs, imgs)
    if persist:
        try:
            persist["set__updated_at"] = datetime.utcnow()
            ParsedListing.objects(id=pl.id).update_one(**persist)
            if persist.get("set__images"):
                pl.images = persist["set__images"]
            if persist.get("set__images_s3"):
                pl.images_s3 = persist["set__images_s3"]
        except Exception:
            logging.exception("Failed to persist S3-mirrored images | listing_id=%s", pl.id)
    img0 = _first(imgs)
    if _trim(img0):
        body["featured_image"] = _clean_featured_image_url(img0)

    # price
    price = getattr(pl, "price", None)
    if price is not None:
        try:
            # WP expects string number
            body["asking_price"] = str(int(price)) if float(price).is_integer() else str(float(price))
        except Exception:
            pass

    zip_code= getattr(pl, "zip", None)
    if desc:
        body["zip_code"] = zip_code

    # taxonomy keys from wp_parsed_data
    wp_pd = getattr(pl, "wp_parsed_data", None) or {}
    # country_deals (prefer exact; else proposed)
    country_deals = wp_pd.get("country_deals") or []
    if not country_deals:
        country_deals = wp_pd.get("proposed_country_deals") or []
    if country_deals:
        body["country_deals"] = [cd for cd in country_deals if _trim(cd)]

    # region (prefer exact; else proposed)
    region = wp_pd.get("region") or []
    if not region:
        region = wp_pd.get("proposed_region") or []
    if region:
        body["region"] = [r for r in region if _trim(r)]

    # property_name (array if present and non-empty/non-null)
    prop_name = wp_pd.get("property_name", None)
    if isinstance(prop_name, str) and _trim(prop_name):
        body["property_name"] = [prop_name]
    elif isinstance(prop_name, list):
        kept = [p for p in prop_name if _trim(p)]
        if kept:
            body["property_name"] = kept

    # other_images_source -> picture_button_url
    other_src = _trim(getattr(pl, "other_images_dropbox_link", None))
    if other_src:
        body["picture_button_url"] = other_src

    # # static
    # body.setdefault("deal_type", ["MLS Deals"])
    # body.setdefault("newest_deals", ["Todays Deal"])

    return body

def _wp_post_create(body: Dict[str, Any]) -> Optional[int]:
    """
    Calls WP create endpoint. Returns post_id on success, else None.
    """
    try:
        resp = requests.post(POST_URL, json=body, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            logging.error(
                "WP POST failed | status=%s | response=%s",
                resp.status_code,
                resp.text[:1000]
            )
            return None
        try:
            data = resp.json()
        except Exception as e:
            logging.error(
                "WP invalid JSON response | error=%s | response=%s",
                str(e),
                resp.text[:1000]
            )
            return None
        # data = resp.json()
        # Expecting WP to return something with new post_id; common patterns:
        # { success: true, post_id: 123, ... } OR data/post_id inside data.
        if isinstance(data, dict):
            if "post_id" in data and isinstance(data["post_id"], int):
                return data["post_id"]
            # Sometimes API returns under data
            d2 = data.get("data")
            if isinstance(d2, dict) and isinstance(d2.get("post_id"), int):
                return d2["post_id"]
        logging.error("WP unexpected response format: %s", data)
        return None
    except Exception:
        logging.exception("WP request crashed (exception in _wp_post_create)")
        return None

def _extract_first_post_id(get_json: Dict[str, Any]) -> Optional[int]:
    """
    From GET response shape (sample provided), grab first result post_id if available.
    """
    try:
        if not get_json.get("success"):
            return None
        arr = get_json.get("data") or []
        first = _first(arr)
        pid = first.get("post_id") if isinstance(first, dict) else None
        return pid if isinstance(pid, int) else None
    except Exception:
        return None

# --- B.2 price guard (Blagojche/Rich 01.10); env-gated OFF by default ---
# A no-house-number / masked address can only be matched by street+city, so an
# exact-address guarantee is absent: 2**0 NW 91st St ($399,900) wrongly linked to
# 2493 NW 91st St = post 51290 ($685,000). When the found post's price is far from
# this listing's price, route to review instead of linking. Only for weak (no-number)
# addresses: a full-address match still links even on a legit price change.
PRICE_MATCH_GUARD = os.getenv("PRICE_MATCH_GUARD", "0").strip().lower() not in ("0", "false", "no", "")
try:
    PRICE_MATCH_TOL = float(os.getenv("PRICE_MATCH_TOL", "0.10"))
except Exception:
    PRICE_MATCH_TOL = 0.10


def _to_price(v):
    """Parse "$685,000" / 685000 / "685000.0" -> float, else None."""
    if v is None:
        return None
    try:
        if isinstance(v, (int, float)):
            return float(v)
        import re as _re
        t = _re.sub(r"[^0-9.]", "", str(v))
        return float(t) if t else None
    except Exception:
        return None


def _addr_has_no_house_number(pl) -> bool:
    """True when this listing's street address is numberless / masked (the only case
    the price guard applies to). Fail-safe: on any error return False (do not block)."""
    try:
        from integrations.wordpress.address_dedup import review_reason
        return review_reason(resolve_street_address(pl)) in ("no_house_number", "masked")
    except Exception:
        return False


def _street_only_key(address, city=None, zip_=None):
    """(c-i) Street+city+zip key with the house number / mask REMOVED. Unlike canonical_key (which
    KEEPS the number, so a masked item never matches a numbered one), this lets us spot a masked
    listing that sits on the SAME STREET as a numbered live post. Loose by design - only ever used to
    raise a "possible dup, check" flag, never to merge."""
    import re as _re
    from integrations.wordpress.address_dedup import _SUF
    street = (address or "").split(",")[0]
    street = _re.sub(r"^\s*[\dxX*\u2013\-]+\s+", "", street)   # drop leading house number / mask token
    base = (street + " " + (city or "") + " " + (str(zip_) if zip_ else "")).lower()
    base = _re.sub(r"\bfl\b|\bflorida\b|,?\s*usa", " ", base)
    base = _re.sub(r"[^a-z0-9 ]+", " ", base)
    return " ".join(_SUF.get(w, w) for w in base.split()).strip()


_CI_MASK = "xX*\u2013"  # mask chars in masked house numbers (x, X, *, en-dash)

def _ci_num_token(addr):
    """(c-i tighten) leading house-number token of the street part (digits + mask chars)."""
    import re as _re
    street = (addr or "").split(",")[0].strip()
    m = _re.match(r"^\s*([0-9xX*\u2013\-]+)", street)
    return m.group(1) if m else ""

def _ci_masked_number_consistent(masked_addr, cand_addr):
    """(c-i tighten) True if the masked listing's visible digits are consistent with the candidate
    live post's house number: *4421 -> candidate ends 4421; 38XX -> starts 38; full number -> exact."""
    import re as _re
    mt = _ci_num_token(masked_addr)
    cand_digits = _re.sub(r"[^0-9]", "", _ci_num_token(cand_addr))
    if not cand_digits:
        return False
    has_mask = any(c in _CI_MASK for c in mt)
    pre = _re.match(r"^([0-9]+)", mt)
    prefix = pre.group(1) if pre else ""
    suf = _re.search(r"([0-9]+)$", mt)
    suffix = suf.group(1) if suf else ""
    if not has_mask:
        md = _re.sub(r"[^0-9]", "", mt)
        return bool(md) and md == cand_digits
    if not prefix and not suffix:
        return False
    if prefix and not cand_digits.startswith(prefix):
        return False
    if suffix and not cand_digits.endswith(suffix):
        return False
    return len(cand_digits) >= (len(prefix) + len(suffix))


def _possible_dup_live_post_id(pl):
    """(c-i) For a MASKED listing, return the post_id of a LIVE posted listing on the same
    street+city+zip that HAS a real house number - a POSSIBLE dup for Rich to check, never merged.
    Best-effort; returns None on anything uncertain."""
    try:
        from pipeline.address_utils import has_house_number
        addr = resolve_street_address(pl)
        key = _street_only_key(addr, getattr(pl, "city", None), getattr(pl, "zip", None))
        if not key or len(key) < 6:
            return None
        q = dict(status="posted", wp_status__in=["posted", "already_found"], post_id__ne=None)
        _city = (getattr(pl, "city", "") or "").strip()
        if _city:
            q["city__iexact"] = _city
        for cand in ParsedListing.objects(**q).only("address", "city", "zip", "post_id").limit(200):
            caddr = getattr(cand, "address", "") or ""
            if not has_house_number(caddr):
                continue
            if _street_only_key(caddr, getattr(cand, "city", None), getattr(cand, "zip", None)) != key:
                continue
            # (c-i tighten 20.10): require same zip AND masked-digit consistency - advisory note only
            _mzip = (str(getattr(pl, "zip", "") or "")).strip()[:5]
            _czip = (str(getattr(cand, "zip", "") or "")).strip()[:5]
            if not (_mzip and _czip and _mzip == _czip):
                continue
            if not _ci_masked_number_consistent(addr, caddr):
                continue
            return cand.post_id
    except Exception:
        return None
    return None


def _price_mismatch(pl, found_post):
    """None => do not block (within tolerance, or a price is unknown on either side =>
    FAIL-OPEN, keep the current link behaviour). Otherwise (pct_str, found_price)."""
    lp = _to_price(getattr(pl, "price", None))
    fp = _to_price((found_post or {}).get("asking_price"))
    if not lp or not fp or lp <= 0 or fp <= 0:
        return None
    diff = abs(lp - fp) / max(lp, fp)
    if diff > PRICE_MATCH_TOL:
        return ("%.0f%%" % (diff * 100.0), fp)
    return None


def _try_search_in_wp(pl: ParsedListing):
    """
    Try main "<address>, <city>" first, then each address_search_keys variant.

    Returns (post_id, state, detail). UNREACHABLE means WordPress never
    answered - the caller must NOT create a post then, because we cannot tell
    whether one is already there. Creating on a failed check is what produced
    the duplicate posts.
    """
    keys = []
    main_key = _main_search_key(pl)
    if main_key:
        keys.append(main_key)
    for key in (getattr(pl, "address_search_keys", None) or []):
        key = _trim(key)
        if key:
            keys.append(key)
    state, pid, _item, detail = search_keys(GET_URL, WP_TOKEN, keys, REQUEST_TIMEOUT)
    return pid, state, detail, _item  # B.2: expose found post (asking_price) to caller

# --- Out-of-window price reduction (Blagojche 14.10) ------------------------------------------
# When a listing re-arrives OLDER than the 30-day dup window it never gets price_drop_pass, so the
# "Reduced from $X" line was never written (bug: 524 NE 71st St - re-sent lower, no reduction line).
# These helpers write the REDUCED!! line via the Podio-free addproperty/create upsert, upserting by
# the existing post's EXACT title (no second post). Guards: only when the post is currently PUBLISH
# (never revive a private/hidden post - direct-sender rule), tag Today's Deals only on a drop >= 6%,
# hold a > 50% drop (misread), skip if the post already shows REDUCED. No Podio, no buyer re-match.
_REDUCED_TITLE_PREFIX = "<strong><span style='color: #ff6600;'>REDUCED!!</span> </strong>"
_TODAYS_DEAL_MIN_DROP = float(os.getenv("TODAYS_DEAL_MIN_DROP", "0.06"))
_PRICE_DROP_MAX_AUTO = float(os.getenv("PRICE_DROP_MAX_AUTO", "0.50"))


def _listing_price(pl):
    """New arrival price: top-level price, else complete_info.list_price_usd."""
    v = getattr(pl, "price", None)
    if v is None:
        ci = getattr(pl, "complete_info", None) or {}
        v = ci.get("list_price_usd") if isinstance(ci, dict) else None
    try:
        return float(v) if v is not None and str(v) != "" else None
    except (TypeError, ValueError):
        return None


def _item_price(item):
    if not isinstance(item, dict):
        return None
    for k in ("asking_price", "price", "asking"):
        if item.get(k) not in (None, ""):
            try:
                return float(str(item.get(k)).replace(",", "").replace("$", "").strip())
            except (TypeError, ValueError):
                continue
    return None


def _getproperty_item(address):
    """Direct getproperty lookup by full address. Returns the first item dict (carries post_status,
    posttitle, asking_price, custom_title) or None. search_keys can miss where a direct address hits."""
    addr = _trim(address)
    if not addr:
        return None
    try:
        r = requests.get(GET_URL, params={"address": addr, "token": WP_TOKEN}, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            return None
        data = (r.json() or {}).get("data") or []
        return data[0] if data and isinstance(data[0], dict) else None
    except Exception:
        logging.exception("getproperty item lookup failed addr=%r", address)
        return None


def _reduction_decision(old_price, new_price):
    """Pure decision: returns dict(action, drop, todays_deal) or None when not a reduction."""
    try:
        op = float(old_price); np_ = float(new_price)
    except (TypeError, ValueError):
        return None
    if not (op > 0 and np_ > 0 and np_ < op):
        return None
    drop = (op - np_) / op
    return {
        "action": "hold_over_50pct" if drop > _PRICE_DROP_MAX_AUTO else "reduce",
        "drop": drop,
        "todays_deal": drop >= _TODAYS_DEAL_MIN_DROP,
    }


def _apply_out_of_window_reduction(pl, item, new_price):
    """Found-branch reduction writer. `item` is a getproperty item for the matched post. Writes the
    REDUCED!! line + new price via set_wp_post_status ONLY when: the post is publish, the new price is
    lower, the drop <= 50%, and the post is not already REDUCED. Returns the decision dict or None."""
    if not isinstance(item, dict):
        return None
    status = str(item.get("post_status") or "").strip().lower()
    match_title = _trim(item.get("posttitle"))
    match_pid = item.get("post_id")
    d = _reduction_decision(_item_price(item), new_price)
    if not d or not match_title:
        return d
    if status != "publish":
        logging.info("reduction skipped: post private (status=%s) id=%s title=%r",
                     status or "unknown", getattr(pl, "id", None), match_title)
        d["action"] = "skip_private"
        return d
    if d["action"] != "reduce":
        logging.warning("reduction HOLD >50%%: id=%s title=%r old=%s new=%s drop=%.2f",
                        getattr(pl, "id", None), match_title, _item_price(item), new_price, d["drop"])
        return d
    if "REDUCED" in str(item.get("custom_title") or "").upper():
        logging.info("reduction skipped: post already REDUCED id=%s title=%r",
                     getattr(pl, "id", None), match_title)
        d["action"] = "already_reduced"
        return d
    from integrations.wordpress.post_status import set_wp_post_status
    np_ = float(new_price)
    asking = str(int(np_)) if float(np_).is_integer() else str(np_)
    custom_title = "%s %s" % (_REDUCED_TITLE_PREFIX, match_title)
    ok, scode, payload = set_wp_post_status(
        match_title, "publish",
        asking_price=asking,
        custom_title=custom_title,
        address=match_title,
        newest_deals=(["Todays Deal"] if d["todays_deal"] else None),  # set_wp_post_status drops falsy
    )
    if ok and isinstance(payload, dict) and match_pid:
        _ret = payload.get("post_id")
        if _ret is not None and int(_ret) != int(match_pid):
            logging.error("reduction ALARM: /create post_id=%s != expected %s id=%s title=%r",
                          _ret, match_pid, getattr(pl, "id", None), match_title)
    if not ok:
        logging.error("reduction write failed id=%s status=%s detail=%s",
                      getattr(pl, "id", None), scode, str(payload)[:200])
    d["wp_ok"] = bool(ok)
    return d


def sync_wp_for_descriptions(
    *,
    limit: Optional[int] = None,
    per_item_sleep_s: float = 0.0,
    gmail_message_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Process all ParsedListing with:
      - wp_status == "des_generated"
      - wp_property_description exists and is non-empty

    For each:
      - search in WP (main "<addr, city>" then variants)
        - if found: set wp_status="already_found", set post_id
        - else: POST create; on success set wp_status="posted", set post_id

    Optional gmail_message_id scopes sync to one source email (catch-up).
    """
    if not WP_TOKEN:
        raise RuntimeError("WP_API_TOKEN is not set in environment")

    filters: Dict[str, Any] = {"wp_status": "des_generated"}
    # #39 (05.10 outage): the docstring contract is "description exists and is non-empty", but it
    # was only checked inside the loop. With limit=5 and oldest-first order, five des_generated
    # listings WITHOUT a description filled every batch and nothing was posted from Fri 02.10
    # 18:21 UTC to Mon 05.10 16:31 UTC (99 deals waiting). Enforce the contract in the query.
    filters["wp_property_description__nin"] = [None, ""]
    if gmail_message_id:
        filters["gmail_message_id"] = gmail_message_id
    else:
        filters["gmail_message_id__not__startswith"] = "test_"
        filters["input_source__ne"] = "new_email"

    q = ParsedListing.objects(**filters).only(
        "address", "city", "state", "zip", "images", "price",
        "wp_property_description", "wp_parsed_data",
        "other_images_dropbox_link", "address_search_keys",
        "price_drop_pass", "post_id"
    ).order_by("+_id")

    if limit is not None:
        q = q.limit(limit)

    processed = 0
    posted = 0
    already = 0
    errors = 0
    results: List[Dict[str, Any]] = []

    for pl in q:
        try:
            # A1 (28.09, fixed 30.09 per Blagojche): the price-drop path owns this record and
            # publishes/updates the EXISTING post itself, so the poster must not create a second
            # page for it. The old guard used price_drop_activated+post_id, but activate never
            # writes post_id -> always False. price_drop_pass (set by dedup on a real drop) is the
            # real ownership signal.
            if getattr(pl, "price_drop_pass", False):
                pl.update(set__wp_status="already_found", set__updated_at=datetime.utcnow())
                results.append({"id": str(pl.id), "ok": True, "status": "skip_price_drop_owned",
                                "post_id": getattr(pl, "post_id", None)})
                processed += 1; already += 1
                continue
            desc = _trim(getattr(pl, "wp_property_description", None))
            if not desc:
                logging.warning("Skipping listing (no description) | id=%s", pl.id)
                # Skip if no description (contract says must exist)
                results.append({"id": str(pl.id), "ok": False, "reason": "no_description"})
                continue

            # search
            found_id, _state, _detail, _found_post = _try_search_in_wp(pl)

            if _state == UNREACHABLE:
                # Never create on a failed check. wp_status stays des_generated,
                # so the listing is picked up again on the next run.
                logging.warning("WP unreachable, not creating | id=%s | %s",
                                pl.id, _detail)
                results.append({
                    "id": str(pl.id),
                    "ok": False,
                    "status": "wp_unreachable",
                    "reason": str(_detail)[:200],
                })
                continue

            if found_id and _post_is_live(found_id) is False:
                # getproperty's mapping can hold a stale post_id whose WP post
                # was deleted (root cause of POST_LOST). Do not link to a dead
                # post - treat as not found so the dup-gate / create path runs.
                logging.warning("WP returned stale post_id %s (post not live) | id=%s - will re-create",
                                found_id, pl.id)
                found_id = None

            # B.2 (Blagojche/Rich 01.10): guard a weak (no-number) match against linking to
            # a DIFFERENT property on the same street. If the found post's price is far from
            # this listing's price, route to review instead of linking. Env-gated; FAIL-OPEN
            # when a price is unknown; only for numberless/masked addresses.
            if found_id and PRICE_MATCH_GUARD and _addr_has_no_house_number(pl):
                _pm = _price_mismatch(pl, _found_post)
                if _pm is not None:
                    logging.warning(
                        "B.2 price-mismatch: id=%s price=%s vs post %s asking=%s (%s) -> review",
                        pl.id, getattr(pl, "price", None), found_id, _pm[1], _pm[0])
                    pl.update(set__wp_status="needs_address_review",
                              set__address_review="price_mismatch",
                              set__updated_at=datetime.utcnow())
                    _pid = getattr(pl, "buyer_matching_podio_item_id", None)
                    if _pid:
                        try:
                            from buyers.matching_api import podio_set_address_review_needs
                            podio_set_address_review_needs(int(_pid),
                                comment=f"Cloud A B.2: a numberless address matched a post with a very "
                                        f"different price ({_pm[0]} apart). Routed to review instead of linking.")
                        except Exception:
                            logging.exception("podio price-mismatch write failed listing=%s", pl.id)
                    results.append({"id": str(pl.id), "ok": False,
                                    "status": "needs_address_review", "reason": "price_mismatch"})
                    processed += 1
                    continue

            if found_id:
                # Out-of-window reduction (Blagojche 14.10): matched an existing post via getproperty;
                # the already_found branch never updated price. If the new price is lower AND the post
                # is currently PUBLISH (never revive a private post), write price + REDUCED!! (Podio-free).
                try:
                    _red_item = _found_post if (isinstance(_found_post, dict) and _found_post.get("post_status")) \
                                else _getproperty_item(_main_search_key(pl))
                    _apply_out_of_window_reduction(pl, _red_item, _listing_price(pl))
                except Exception:
                    logging.exception("out-of-window reduction (found branch) failed id=%s", pl.id)
                pl.update(
                    set__wp_status="already_found",
                    set__post_id=found_id,
                    set__updated_at=datetime.utcnow(),
                )
                _defer_link_existing_podio(pl, found_id)  # #2: reuse existing Podio item
                try:
                    from observability.pipeline_metrics import record_listing_stage
                    record_listing_stage(str(pl.id), "wp_already_found", wp_status="already_found")
                except Exception:
                    pass

                # Cloud A (2026-09-24): listing came back via /listings/address-fixed -> tell Podio
                    try:
                        _fx = ParsedListing.objects(id=pl.id).only("address_review", "buyer_matching_podio_item_id").first()
                        if _fx and getattr(_fx, "address_review", None) == "fixed" and getattr(_fx, "buyer_matching_podio_item_id", None):
                            from buyers.matching_api import podio_set_address_review
                            podio_set_address_review(_fx.buyer_matching_podio_item_id, 4,
                                "Address fixed and published: WordPress post %s (%s)" % (found_id, "duplicate of an existing post"))
                            pl.update(set__address_review="published")
                    except Exception:
                        logging.exception("address-fixed podio hook failed id=%s", pl.id)
                results.append({"id": str(pl.id), "ok": True, "status": "already_found", "post_id": found_id})
                processed += 1
                already += 1
            else:
                # --- Cloud A dup-gate (2026-09-04): normalised-address check before
                # creating. Catches duplicates the exact WP search missed, and routes
                # non-standard addresses. Option (b): masked -> post + flag; no house
                # number / unusable -> review (do not post). See address_dedup.py.
                _dg_addr = resolve_street_address(pl)
                _dg_city = getattr(pl, "city", None)
                _dg_status, _dg_detail = _dedup_classify(
                    _dg_addr, _dg_city, find_existing=_dedup_finder(exclude_id=pl.id))
                if _dg_status == _DEDUP_DUP:
                    _dg_pid = getattr(_dg_detail, "post_id", None)
                    pl.update(set__wp_status="already_found", set__post_id=_dg_pid,
                              set__updated_at=datetime.utcnow())
                    _defer_link_existing_podio(pl, _dg_pid)  # #2: reuse existing Podio item
                    results.append({"id": str(pl.id), "ok": True, "status": "dedup_linked",
                                    "post_id": _dg_pid})
                    processed += 1
                    already += 1
                    continue
                # Ruben/ZCG (Rich 29.09): exempt these direct-wholesaler senders from the
                # no-house-number review BLOCK - publish, de-duped by street+city+price. Only for
                # a REAL street (Blagojche 30.09 junk filter) - junk stays in review.
                if (_dg_status == _DEDUP_REVIEW and _dg_detail == "no_house_number"
                        and _pl_sender_email(pl) in ADDRESS_REVIEW_EXEMPT_SENDERS
                        and _is_real_street(_dg_addr, _dg_city)):
                    _xdup = _streetcity_price_dup(pl, _dg_addr, _dg_city)
                    if _xdup is not None:
                        _xpid = getattr(_xdup, "post_id", None)
                        pl.update(set__wp_status="already_found", set__post_id=_xpid,
                                  set__address_review="exempt_dup", set__updated_at=datetime.utcnow())
                        _defer_link_existing_podio(pl, _xpid)  # #2: reuse existing Podio item
                        results.append({"id": str(pl.id), "ok": True,
                                        "status": "dedup_linked_exempt", "post_id": _xpid})
                        processed += 1
                        already += 1
                        continue
                    pl.update(set__address_review="exempt_no_house_number")
                    _dg_status = _DEDUP_NEW   # bypass the review block; fall through to create
                if _dg_status == _DEDUP_REVIEW and _dg_detail != "masked":
                    pl.update(set__wp_status="needs_address_review",
                              set__address_review=str(_dg_detail),
                              set__updated_at=datetime.utcnow())
                    # Cloud A (2026-09-24, Blagojche/Rich): mirror the flag to Podio so the
                    # team sees it. Fires once (listing leaves the des_generated query). Best-effort.
                    _pid = getattr(pl, "buyer_matching_podio_item_id", None)
                    if _pid:
                        try:
                            from buyers.matching_api import podio_set_address_review_needs
                            podio_set_address_review_needs(int(_pid),
                                comment=f"Cloud A dup-gate flagged this address for review (reason: {_dg_detail}). Address Review set to 'Needs review'.")
                        except Exception:
                            logging.exception("podio address-review write failed listing=%s", pl.id)
                    _defer_fire_listing_posted(pl)  # #2: review listing still gets a Podio item
                    results.append({"id": str(pl.id), "ok": False,
                                    "status": "needs_address_review", "reason": str(_dg_detail)})
                    processed += 1
                    continue
                if _dg_status == _DEDUP_REVIEW:  # masked -> post but flag
                    pl.update(set__address_review="masked")
                    # (c-i) Blagojche 20.10 #6: if this masked address sits on the same street+city+zip
                    # as a numbered LIVE post, flag it as a POSSIBLE dup for Rich to check - never merged
                    # (different house numbers stay distinct properties). Podio write rides the token
                    # bucket once #1 lands.
                    try:
                        _dup_pid = _possible_dup_live_post_id(pl)
                        _pid = getattr(pl, "buyer_matching_podio_item_id", None)
                        if _dup_pid and _pid:
                            from buyers.matching_api import podio_set_address_review_needs
                            podio_set_address_review_needs(int(_pid),
                                comment=f"POSSIBLE DUP of post {_dup_pid} (masked address) - check. "
                                        f"Not auto-linked; the house number is masked so this is a street match only.")
                    except Exception:
                        logging.exception("(c-i) possible-dup note failed listing=%s", getattr(pl, "id", None))
                # create
                body = _build_post_body(pl)
                # fail early if posttitle is missing
                if not _trim(body.get("posttitle")):
                    logging.error("Skipping listing (missing posttitle in WP body) | listing_id=%s", pl.id)
                    pl.update(
                        set__wp_status="failed",
                        set__updated_at=datetime.utcnow(),
                    )
                    try:
                        from observability.pipeline_metrics import record_listing_stage
                        record_listing_stage(str(pl.id), "wp_failed", wp_status="failed", detail="missing_posttitle")
                    except Exception:
                        pass
                    results.append({
                        "id": str(pl.id),
                        "ok": False,
                        "status": "failed",
                        "reason": "missing_posttitle",
                    })
                    processed += 1
                    errors += 1
                    continue
                # pl.update(
                #     set__wp_status="posted_temp",
                #     set__updated_at=datetime.utcnow(),
                # )
                # processed += 1
                # posted += 1
                # B (28.09): tag Today only on a genuine new post (this is the not-found/create path)
                body["newest_deals"] = ["Todays Deal"]
                # Out-of-window reduction (Blagojche 14.10): getproperty search can miss where a direct
                # address lookup still finds the post (/create upserts by exact title). If a currently
                # PUBLISH post exists at a HIGHER price, make this a REDUCED!! upsert; gate the tag on
                # >= 6% and NEVER send newest_deals=null (would clear the plugin's tags). Private post
                # or already-REDUCED -> leave as a normal create (do not revive, do not double-write).
                try:
                    _ci = _getproperty_item(_compose_full_address(pl) or body.get("posttitle"))
                    if isinstance(_ci, dict) and str(_ci.get("post_status") or "").strip().lower() == "publish" \
                            and "REDUCED" not in str(_ci.get("custom_title") or "").upper():
                        _rd = _reduction_decision(_item_price(_ci), _listing_price(pl))
                        if _rd and _rd["action"] == "reduce":
                            body["posttitle"] = _trim(_ci.get("posttitle")) or body["posttitle"]
                            body["custom_title"] = "%s %s" % (_REDUCED_TITLE_PREFIX, body["posttitle"])
                            if _rd["todays_deal"]:
                                body["newest_deals"] = ["Todays Deal"]
                            else:
                                body.pop("newest_deals", None)  # FIX2: drop key, never send null
                                # Blagojche 19.10 (option a): also strip a STALE Todays-Deal term on a sub-6% drop
                                try:
                                    from integrations.wordpress.price_media_updates import _remove_todays_deal_tag
                                    _remove_todays_deal_tag(_ci.get("id") or _ci.get("post_id"), drop=_rd.get("drop"))
                                except Exception:
                                    logging.exception("todays-deal remove (create path) failed id=%s", pl.id)
                        elif _rd and _rd["action"] == "hold_over_50pct":
                            logging.warning("create-path reduction HOLD >50%%: id=%s title=%r old=%s new=%s",
                                            pl.id, body.get("posttitle"), _item_price(_ci), _listing_price(pl))
                except Exception:
                    logging.exception("out-of-window reduction (create path) failed id=%s", pl.id)
                post_id = _wp_post_create(body)
                if post_id:
                    pl.update(
                        set__wp_status="posted",
                        set__post_id=post_id,
                        set__updated_at=datetime.utcnow(),
                    )
                    _defer_fire_listing_posted(pl)  # #2: new listing -> Podio item here
                    try:
                        from observability.pipeline_metrics import record_listing_stage
                        record_listing_stage(str(pl.id), "wp_synced", wp_status="posted")
                    except Exception:
                        pass

                    # Cloud A (2026-09-24): listing came back via /listings/address-fixed -> tell Podio
                    try:
                        _fx = ParsedListing.objects(id=pl.id).only("address_review", "buyer_matching_podio_item_id").first()
                        if _fx and getattr(_fx, "address_review", None) == "fixed" and getattr(_fx, "buyer_matching_podio_item_id", None):
                            from buyers.matching_api import podio_set_address_review
                            podio_set_address_review(_fx.buyer_matching_podio_item_id, 4,
                                "Address fixed and published: WordPress post %s (%s)" % (post_id, "new post"))
                            pl.update(set__address_review="published")
                    except Exception:
                        logging.exception("address-fixed podio hook failed id=%s", pl.id)
                    results.append({"id": str(pl.id), "ok": True, "status": "posted", "post_id": post_id})
                    processed += 1
                    posted += 1
                else:
                    logging.error("WP POST failed | listing_id=%s", pl.id)
                    results.append({"id": str(pl.id), "ok": False, "reason": "post_failed"})
                    errors += 1

        except Exception as e:
            logging.exception("Unexpected error processing listing_id=%s", pl.id)
            results.append({"id": str(pl.id), "ok": False, "error": f"{type(e).__name__}: {e}"})
            errors += 1

        if per_item_sleep_s > 0:
            time.sleep(per_item_sleep_s)

    return {
        "processed": processed,
        "posted": posted,
        "already_found": already,
        "errors": errors,
        "results": results
    }
