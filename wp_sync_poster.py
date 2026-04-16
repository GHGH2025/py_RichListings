# wp_sync_poster.py
import os
import time
from datetime import datetime
import json
import requests
from typing import Optional, Dict, Any, List
from urllib.parse import urlparse, parse_qsl, urlunparse, urlencode
from models import ParsedListing  # mongoengine document
import logging

WP_TOKEN = os.getenv("WP_API_TOKEN")  # <-- set in env
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
    addr  = _trim(getattr(pl, "address", None)) or _trim((getattr(pl, "complete_info", {}) or {}).get("address"))
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
    addr = _trim(getattr(pl, "address", None)) or _trim((getattr(pl, "complete_info", {}) or {}).get("address"))
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
    body: Dict[str, Any] = {"token": WP_TOKEN,  "newest_deals": ["Todays Deal"]}

    # title/address lines
    full_addr_line = _compose_full_address(pl)
    if full_addr_line:
        body["posttitle"] = full_addr_line
        body["address"]   = full_addr_line

    # description (HTML) from wp_property_description
    desc = _trim(getattr(pl, "wp_property_description", None))
    if desc:
        body["postdesc"] = desc

    # featured image (first)
    imgs = getattr(pl, "images", None) or []
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
            logger.error(
                "WP POST failed | status=%s | response=%s",
                resp.status_code,
                resp.text[:1000]
            )
            return None
        try:
            data = resp.json()
        except Exception as e:
            logger.error(
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
        logger.error("WP unexpected response format: %s", data)
        return None
    except Exception:
        logger.exception("WP request crashed (exception in _wp_post_create)")
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

def _try_search_in_wp(pl: ParsedListing) -> Optional[int]:
    """
    Try main "<address>, <city>" first; if not found, try each address_search_keys variant.
    Return first post_id found, else None.
    """
    # 1) main key
    main_key = _main_search_key(pl)
    if main_key:
        js = _wp_get(main_key)
        pid = _extract_first_post_id(js) if js else None
        if pid:
            return pid

    # 2) variants from address_search_keys
    variants: List[str] = getattr(pl, "address_search_keys", None) or []
    for key in variants:
        key = _trim(key)
        if not key:
            continue
        js = _wp_get(key)
        pid = _extract_first_post_id(js) if js else None
        if pid:
            return pid
    return None

def sync_wp_for_descriptions(*, limit: Optional[int] = None, per_item_sleep_s: float = 0.0) -> Dict[str, Any]:
    """
    Process all ParsedListing with:
      - wp_status == "des_generated"
      - wp_property_description exists and is non-empty

    For each:
      - search in WP (main "<addr, city>" then variants)
        - if found: set wp_status="already_found", set post_id
        - else: POST create; on success set wp_status="posted", set post_id
    """
    if not WP_TOKEN:
        raise RuntimeError("WP_API_TOKEN is not set in environment")

    q = ParsedListing.objects(wp_status="des_generated").only(
        "address", "city", "state", "zip", "images", "price",
        "wp_property_description", "wp_parsed_data",
        "other_images_dropbox_link", "address_search_keys"
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
            desc = _trim(getattr(pl, "wp_property_description", None))
            if not desc:
                logger.warning("Skipping listing (no description) | id=%s", pl.id)
                # Skip if no description (contract says must exist)
                results.append({"id": str(pl.id), "ok": False, "reason": "no_description"})
                continue

            # search
            found_id = _try_search_in_wp(pl)
            if found_id:
                pl.update(
                    set__wp_status="already_found",
                    set__post_id=found_id,
                    set__updated_at=datetime.utcnow(),
                )
                results.append({"id": str(pl.id), "ok": True, "status": "already_found", "post_id": found_id})
                processed += 1
                already += 1
            else:
                # create
                body = _build_post_body(pl)
                # pl.update(
                #     set__wp_status="posted_temp",
                #     set__updated_at=datetime.utcnow(),
                # )
                # processed += 1
                # posted += 1
                post_id = _wp_post_create(body)
                if post_id:
                    pl.update(
                        set__wp_status="posted",
                        set__post_id=post_id,
                        set__updated_at=datetime.utcnow(),
                    )
                    results.append({"id": str(pl.id), "ok": True, "status": "posted", "post_id": post_id})
                    processed += 1
                    posted += 1
                else:
                    logger.error("WP POST failed | listing_id=%s", pl.id)
                    results.append({"id": str(pl.id), "ok": False, "reason": "post_failed"})
                    errors += 1

        except Exception as e:
            logger.exception("Unexpected error processing listing_id=%s", pl.id)
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
