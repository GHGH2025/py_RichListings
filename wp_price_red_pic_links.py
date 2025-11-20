import os
import re
import requests
from typing import Dict, Any, Optional, Tuple, List
from mongoengine.queryset.visitor import Q
from models import ParsedListing  # mongoengine document
from post_selection import _slugify_for_folder
from dropboxImageUpload import handle_Link
WP_TOKEN = os.getenv("WP_API_TOKEN")
GET_URL  = "https://inventory.joinbuyerslist.com/wp-json/addproperty/v1/getproperty"
POST_URL = "https://inventory.joinbuyerslist.com/wp-json/addproperty/v1/create"
REQUEST_TIMEOUT = 25
from openai import OpenAI
import json
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1")
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

def _trim(s: Optional[str]) -> str:
    return (s or "").strip()

def _num_from_str(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    try:
        return float(re.sub(r"[^\d.]", "", str(s)))
    except Exception:
        return None

def _wp_get(address_key: str) -> Optional[Dict[str, Any]]:
    """
    Calls WP GET search endpoint with an address search key.
    Returns JSON dict on 200, else None.
    """
    try:
        resp = requests.get(
            GET_URL,
            params={"address": address_key, "token": WP_TOKEN},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception:
        return None

def _extract_first_post(js: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    Given WP GET JSON, return the first data item (contains post_id, address, asking_price, picture_button_url, etc.).
    """
    if not isinstance(js, dict):
        return None
    data = js.get("data")
    if isinstance(data, list) and data:
        first = data[0]
        return first if isinstance(first, dict) else None
    return None

def _extract_first_post_id(js: Optional[Dict[str, Any]]) -> Optional[int]:
    first = _extract_first_post(js)
    if first and isinstance(first.get("post_id"), int):
        return first["post_id"]
    return None

def _compose_main_key_from_pl(pl) -> Optional[str]:
    # "<address>, <city>" if both exist
    addr = _trim(getattr(pl, "address", None))
    city = _trim(getattr(pl, "city", None))
    if addr and city:
        return f"{addr}, {city}"
    return None

def _search_wp_with_data(pl) -> Tuple[Optional[int], Optional[Dict[str, Any]]]:
    """
    Try the main "<address>, <city>" key; if not found, iterate address_search_keys.
    Return (post_id, first_data_item) or (None, None).
    """
    # 1) main
    main_key = _compose_main_key_from_pl(pl)
    if main_key:
        js = _wp_get(main_key)
        first = _extract_first_post(js)
        if first and isinstance(first.get("post_id"), int):
            return first["post_id"], first

    # 2) variants
    for key in (getattr(pl, "address_search_keys", None) or []):
        key = _trim(key)
        if not key:
            continue
        js = _wp_get(key)
        first = _extract_first_post(js)
        if first and isinstance(first.get("post_id"), int):
            return first["post_id"], first

    return None, None

def _wp_post_create(body: Dict[str, Any]) -> Optional[int]:
    """
    Calls WP create endpoint (also used for updates) and returns post_id on success.
    """
    try:
        resp = requests.post(POST_URL, json=body, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            return None
        data = resp.json()
        if isinstance(data, dict):
            if isinstance(data.get("post_id"), int):
                return data["post_id"]
            d2 = data.get("data")
            if isinstance(d2, dict) and isinstance(d2.get("post_id"), int):
                return d2["post_id"]
        return None
    except Exception:
        return None

def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


_AI_SYS_PROMPT = """You verify whether two US property addresses refer to the SAME physical property.
Return ONLY JSON: {"match": true|false, "confidence": 0..1, "reason": "<short>"}

Heuristics (be strict but practical):
- Allow case, punctuation, spacing, ordering, presence/absence of state/ZIP/country.
- Treat street type full↔abbr as equivalent (Street/St, Avenue/Ave, Road/Rd, Drive/Dr, Court/Ct, Terrace/Ter, Place/Pl,
  Boulevard/Blvd, Lane/Ln, Circle/Cir, Parkway/Pkwy, Highway/Hwy).
- Directionals: N↔North, S↔South, E↔East, W↔West, NW↔Northwest, NE↔Northeast, SW↔Southwest, SE↔Southeast.
- Ordinal suffixes allowed on street numbers (12 vs 12th).
- City word variants allowed (Beach↔Bch, Gardens↔Gdns, Springs↔Spgs, Saint↔St, Fort↔Ft, Mount↔Mt).
- Apt/unit numbers can differ or be missing and still be SAME property.
- House number MUST match exactly unless one side has a mask like 137X/137XX, which may match the other if consistent prefix.
- Street name core must match (minor typos ok if clearly same, but different names mean NOT same).
- Do NOT rely on price, photos, or descriptions—address only.

Output strictly the JSON object with keys match, confidence, reason.
"""

def ai_verify_same_listing(
    target_key: str,
    wp_address: str,
    wp_title: Optional[str] = None,
    model: Optional[str] = None
) -> Dict[str, Any]:
    """
    target_key: our intended key, e.g. "<address>, <city>"
    wp_address: address string from WP GET result ("data[0].address")
    wp_title:   optional post title from WP result
    Returns: {"match": bool, "confidence": float, "reason": str}
    """
    model = model or "gpt-4.1-mini"
    user_msg = f"""TARGET_KEY:
{target_key}

WP_ADDRESS:
{wp_address}

WP_POSTTITLE:
{wp_title or ""}

Return ONLY JSON: {{"match": true|false, "confidence": 0..1, "reason": "<short>"}}"""
    try:
        chat = client.chat.completions.create(
            model=model,
            temperature=0.0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _AI_SYS_PROMPT},
                {"role": "user", "content": user_msg}
            ],
        )
        content = chat.choices[0].message.content
        data = json.loads(content)
        # sanity
        return {
            "match": bool(data.get("match")),
            "confidence": float(data.get("confidence", 0.0)),
            "reason": str(data.get("reason", "")[:300]),
        }
    except Exception as e:
        # Conservative: reject if AI failed
        return {"match": False, "confidence": 0.0, "reason": f"ai_error: {type(e).__name__}"}

def process_wp_price_and_media_updates(limit: int = 200) -> Dict[str, Any]:
    """
    For ParsedListing with wp_check='pending':
      1) Search in WP (main "<address>, <city>" then all 'address_search_keys').
         - If not found: set wp_check='not_found'.
         - If found:
             - Save found post_id to wp_check_post_id.
             - If found asking_price > parsed listing price, send a "reduction" update:
               {
                 "address": <WP found address>,
                 "asking_price": <parsedListing.price>,
                 "token": WP_API_TOKEN,
                 "custom_title": "<strong><span style='color: #ff6600;'>REDUCED!!</span> </strong><address>"
               }
             - If found picture_button_url is empty AND parsedListing.other_images_dropbox_link exists:
               send update:
               {
                 "address": <WP found address>,
                 "token": WP_API_TOKEN,
                 "picture_button_url": <parsedListing.other_images_dropbox_link>
               }
             - Finally set wp_check='processed'.
    """
    # Pull minimal fields required
    qs = ParsedListing.objects(wp_check="pending") \
        .only("id", "address", "city", "state", "zip", "price",
              "address_search_keys", "wp_check", "wp_check_post_id",
              "other_images_dropbox_link","other_images_source",
              "complete_info" )


    total = qs.count()
    processed = 0
    not_found = 0
    reduced_updates = 0
    media_updates = 0
    errors: List[str] = []

    for pl in qs.limit(limit):
        try:
            pid, wp_item = _search_wp_with_data(pl)

            if not pid or not wp_item:
                # Mark not found
                ParsedListing.objects(id=pl.id).update_one(
                    set__wp_check="not_found",
                    set__updated_at=_now(),
                )
                not_found += 1
                continue

            # We found a WP listing
            wp_address          = _trim(wp_item.get("address"))
            wp_title            = wp_item.get("posttitle")
            wp_post_id          = pid
            wp_price_str        = wp_item.get("asking_price")
            wp_picture_btn_url  = _trim(wp_item.get("picture_button_url"))

            target_key = _compose_main_key_from_pl(pl) or ""

            ai_result = ai_verify_same_listing(
                target_key=target_key,
                wp_address=wp_address,
                wp_title=wp_title,
                model=None  # or pass your preferred model
            )

            if not ai_result.get("match", False) or float(ai_result.get("confidence", 0.0)) < 0.80:
                # We found a WP record, but AI says it's not the same property → reject safely.
                ParsedListing.objects(id=pl.id).update_one(
                    set__wp_check="found_but_rejected",
                    set__wp_check_post_id=str(wp_post_id),          # keep for audit
                    set__updated_at=_now(),
                )
                processed += 1
                continue

            # Save the found post_id for reference
            ParsedListing.objects(id=pl.id).update_one(
                set__wp_check_post_id=str(wp_post_id),
                set__wp_check="processed",
                set__updated_at=_now(),
            )

            # 1) Price reduction (only if WP asking price is HIGHER than our parsedListing price)
            body_reduction = None
            parsed_price = getattr(pl, "price", None)
            wp_price     = _num_from_str(wp_price_str)

            if wp_address and parsed_price is not None and wp_price is not None:
                if wp_price > float(parsed_price):
                    body_reduction = {
                        "posttitle": wp_title,
                        "address": wp_address,
                        "asking_price": str(int(parsed_price)) if float(parsed_price).is_integer()
                                       else str(float(parsed_price)),
                        "token": WP_TOKEN,
                        "custom_title": f"<strong><span style='color: #ff6600;'>REDUCED!!</span> </strong> {wp_address}",
                    }
                    ParsedListing.objects(id=pl.id).update_one(
                        set__wp_check_reduced="updated"
                    )
                    
                    if _wp_post_create(body_reduction):
                        reduced_updates += 1

            # 2) Media link update (if WP missing AND we have dropbox/other link)
            body_media = None
            # other_src = _trim(getattr(pl, "other_images_dropbox_link", None))
            if wp_address and not wp_picture_btn_url:
                use_link: Optional[str] = None

                # Prefer already-generated Dropbox link if exists
                already_dbx = _trim(getattr(pl, "other_images_dropbox_link", None))
                if already_dbx:
                    use_link = already_dbx
                else:
                    # generate from other_images_source if available
                    src = _trim(getattr(pl, "other_images_source", None))
                    if src:
                        try:
                            # Choose a readable folder slug: top-level address or complete_info.address or the id
                            addr_for_slug = _trim(getattr(pl, "address", None)) \
                                            or _trim((getattr(pl, "complete_info", {}) or {}).get("address")) \
                                            or str(pl.id)
                            folder_slug = _slugify_for_folder(addr_for_slug, fallback=str(pl.id))
                            shared_links = handle_Link([src], folder=folder_slug)  # returns list; we take the first
                            if shared_links:
                                use_link = _trim(shared_links[0])
                                # persist the generated dropbox link
                                ParsedListing.objects(id=pl.id).update_one(
                                    set__other_images_dropbox_link=use_link,
                                    set__updated_at=_now(),
                                )
                        except Exception as e:
                            errors.append(f"{pl.id}: dropbox_gen_error: {e}")

                if use_link:
                    body_media = {
                        "posttitle": wp_title,
                        "address": wp_address,
                        "token": WP_TOKEN,
                        "picture_button_url": use_link,
                    }
                    if _wp_post_create(body_media):
                        media_updates += 1

        except Exception as e:
            errors.append(f"{pl.id}: {type(e).__name__}: {e}")

    return {
        "total_pending": total,
        "processed": processed,
        "not_found": not_found,
        "price_reduction_updates": reduced_updates,
        "media_link_updates": media_updates,
        "errors": errors[:20],
    }
