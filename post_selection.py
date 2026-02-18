# With AI

# post_selection.py
import math
import re
import json
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any

from mongo_engine_conn import init_db
from models import ParsedListing, DailyBaseCount
from dropboxImageUpload import handle_Link

from dotenv import load_dotenv
from openai import OpenAI
import requests
from bson import ObjectId
load_dotenv()

# Initialize OpenAI client using API key from .env
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
SKIPPED_LISTING_WEBHOOK_URL = os.getenv("SKIPPED_LISTING_WEBHOOK_URL")


# Allowed regions we will post from
ALLOWED_REGIONS = {
    "south_florida_tri_county",
    "st_lucie",
    "fort_pierce",
    "rest_of_florida",
}

# Reason strings
REASON_BAD_REGION = "unsupported_region_for_posting"
REASON_OVER_CAP   = "rest_of_florida_cap_exceeded_35_percent_policy"


def _load_do_not_post_cities():
    """
    Load cities from do_not_post_city.json.

    Returns:
        (raw_list, normalized_set)
    """
    path = os.path.join(os.path.dirname(__file__), "do_not_post_city.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            raw_list = [str(x).strip() for x in data if str(x).strip()]
            norm_set = {x.lower() for x in raw_list}
            return raw_list, norm_set
    except Exception as e:
        print(f"Warning: unable to load do_not_post_city.json: {e}")
    return [], set()


DO_NOT_POST_CITIES_RAW, DO_NOT_POST_CITIES_SET = _load_do_not_post_cities()

# Cache AI decisions so we don't call the API multiple times for the same city
_AI_CITY_CACHE: Dict[str, bool] = {}


def _get_region(pl: ParsedListing) -> Optional[str]:
    # Prefer extracted blob, fallback to any top-level field if you later add one
    ci = pl.complete_info or {}
    region = ci.get("region_bucket")
    if isinstance(region, str):
        return region.strip()
    return None


def _get_city(pl: ParsedListing) -> Optional[str]:
    """
    Get city name from ParsedListing, preferring top-level field,
    falling back to complete_info["city"] if needed.
    """
    if getattr(pl, "city", None):
        return (pl.city or "").strip()
    ci = pl.complete_info or {}
    city = ci.get("city") or ci.get("City")
    if isinstance(city, str):
        return city.strip()
    return None


def _ai_city_in_do_not_post(city: str) -> bool:
    """
    Use AI to determine whether the listing city matches ANY city
    in the do-not-post list, even with:
      - Abbreviations (St. vs Saint)
      - Minor typos
      - Extra context in the listing city

    We still do a quick deterministic check first, then fall back to AI.
    Result is cached.
    """
    if not city:
        return False

    key = city.strip().lower()
    if key in _AI_CITY_CACHE:
        return _AI_CITY_CACHE[key]

    # If we have no config, don't block anything
    if not DO_NOT_POST_CITIES_RAW:
        _AI_CITY_CACHE[key] = False
        return False

    # Fast deterministic check first (exact normalized match)
    if key in DO_NOT_POST_CITIES_SET:
        _AI_CITY_CACHE[key] = True
        return True

    # Slight normalization for things like dots / spacing (still before AI)
    simple_norm = key.replace(".", "").replace(" saint ", " st ").replace("saint ", "st ")
    if simple_norm in DO_NOT_POST_CITIES_SET:
        _AI_CITY_CACHE[key] = True
        return True

    # Build AI prompt with clear YES/NO contract
    city_list_text = ", ".join(sorted(DO_NOT_POST_CITIES_RAW))

    user_content = f"""
You are a strict matching engine for US city names.

TASK:
Given:
1) A listing city name.
2) A list of "do not post" city names.

You must answer ONLY with "YES" or "NO".

Return "YES" if the listing city clearly refers to the same city as any entry
in the do-not-post list, even if:
- The spelling has minor typos,
- It uses abbreviations (e.g. "St." vs "Saint"),
- It has extra context like neighborhoods or county names,
- It has different capitalization or spacing.

If you are NOT confident that it refers to the same city, return "NO".

Listing city: "{city}"

Do-not-post cities:
{city_list_text}
""".strip()

    result = False
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": "You are a precise, deterministic city-name matcher. You must only answer YES or NO."
                },
                {
                    "role": "user",
                    "content": user_content,
                },
            ],
            max_tokens=3,
            temperature=0,  # deterministic
        )
        # answer = (resp.choices[0].message.content or "").strip().upper()
        # result = answer.startswith("YES")
        
        answer_raw = resp.choices[0].message.content or ""
        answer = answer_raw.strip().upper()

        # Extract word-like tokens (A–Z only), e.g. "NO, DO NOT TREAT THIS AS YES"
        tokens = re.findall(r"[A-Z]+", answer)

        result = False  # default: NOT a do-not-post city (fail-safe against over-blocking)

        if tokens:
            first = tokens[0]  # trust the first word most

            if first == "YES":
                result = True
            elif first == "NO":
                result = False
            else:
                # If first token is something else, fall back to presence logic
                has_yes = "YES" in tokens
                has_no  = "NO" in tokens

                if has_yes and not has_no:
                    result = True
                elif has_no and not has_yes:
                    result = False
                else:
                    # Both YES and NO present or neither: treat as NO / safe default
                    result = False
        else:
            # No tokens at all → weird output → treat as NO
            result = False
    except Exception as e:
        # Fail-safe: if AI fails, we DO NOT block posting
        print(f"AI city check failed for '{city}': {e}")
        result = False

    _AI_CITY_CACHE[key] = result
    return result


def _is_do_not_post_city(pl: ParsedListing) -> bool:
    """
    Main entry for checking if a listing should be skipped
    due to Do Not Post City rule, using AI-based matching.
    """
    city = _get_city(pl)
    if not city:
        return False
    return _ai_city_in_do_not_post(city)


def _now():
    return datetime.utcnow()


def _slugify_for_folder(name: str, fallback: str) -> str:
    """
    Create a Dropbox-safe folder slug from an address.
    - strip weird chars
    - remove spaces
    - cap length
    """
    s = (name or "").strip()
    if not s:
        s = fallback
    # keep letters, digits, space, dot, dash, underscore; turn the rest into underscore
    s = re.sub(r"[^A-Za-z0-9 ._-]", "_", s)
    s = s.replace(" ", "")        # compact spaces so path is tidy
    s = s.strip("._-") or fallback
    return s[:80]


def _update_and_get_daily_base_count(base_count: int, now: datetime) -> int:
    """
    Maintain a rolling base count per day in the DailyBaseCount collection.

    - Looks up today's document (UTC day).
    - final_base_count = existing_daily_base_count + base_count
    - Persists final_base_count back to the document (creating it if needed).
    - Returns final_base_count.
    """
    day_start = datetime(now.year, now.month, now.day)
    next_day_start = day_start + timedelta(days=1)

    # Fetch today's record (there should be at most one per day)
    daily_doc = DailyBaseCount.objects(
        current_date__gte=day_start,
        current_date__lt=next_day_start,
    ).first()

    if not daily_doc:
        if base_count <= 0:
            # Nothing to add and no prior record -> effectively 0 base so far today.
            return 0
        final_base_count = base_count
        daily_doc = DailyBaseCount(
            current_date=day_start,
            daily_base_count=final_base_count,
        )
        daily_doc.save()
        return final_base_count

    # We have an existing doc for today
    existing = daily_doc.daily_base_count or 0
    final_base_count = existing + max(base_count, 0)

    daily_doc.update(
        set__daily_base_count=final_base_count,
        # keep date tied to day_start so it stays in this day's bucket
        set__current_date=day_start,
    )
    return final_base_count


def _to_jsonable(obj):
    """
    Recursively convert MongoEngine / BSON types into JSON-serializable values.
    - datetime -> ISO string
    - ObjectId -> str
    - DBRef -> simple dict
    - dict / list / tuple -> walk recursively
    """
    # datetime -> ISO 8601
    if isinstance(obj, datetime):
        return obj.isoformat()

    # ObjectId -> string
    if isinstance(obj, ObjectId):
        return str(obj)

    # dict -> recurse
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}

    # list / tuple -> recurse
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v) for v in obj]

    # anything else: leave as-is (must already be JSON-safe: str/int/float/bool/None)
    return obj


def _serialize_parsed_listing_for_webhook(pl: ParsedListing) -> Dict[str, Any]:
    """
    Turn a ParsedListing document into a JSON-serializable dict for webhooks.
    Includes the full Mongo document (complete_info etc.), with ObjectIds → str.
    """
    raw = pl.to_mongo().to_dict()
    jsonable = _to_jsonable(raw)
    return jsonable

def _send_skipped_listing_to_webhook(
    pl: ParsedListing,
    skip_type: str,
    reason: str,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Send a skipped listing to the SKIPPED_LISTING_WEBHOOK_URL.

    skip_type: e.g. "Do_Not_Post_City" or "POST_POLICY_35PC"
    reason: short human-readable reason
    extra: optional extra fields (e.g., quota numbers)
    """
    if not SKIPPED_LISTING_WEBHOOK_URL:
        print("[skipped_listing] SKIPPED_LISTING_WEBHOOK_URL not set; skipping webhook send.")
        return {"ok": False, "reason": "no_webhook_url"}

    payload: Dict[str, Any] = {
        "listing_id": str(pl.id),
        "skip_type": skip_type,
        "reason": reason,
        "status": pl.status,
        "region_bucket": getattr(pl, "region_bucket", None),
        "rules_ai_rule_id": getattr(pl, "rules_ai_rule_id", None),
        "rules_ai_version": getattr(pl, "rules_ai_version", None),
        "rules_ai_reason": getattr(pl, "rules_ai_reason", None),
        "data": _serialize_parsed_listing_for_webhook(pl),
    }

    if extra:
        payload["extra"] = extra

    try:
        resp = requests.post(
            SKIPPED_LISTING_WEBHOOK_URL,
            json=payload,
            timeout=15,
        )
        ok = resp.status_code in (200, 201, 202)
        if not ok:
            print(
                f"[skipped_listing] Webhook non-2xx: {resp.status_code}, "
                f"body={resp.text[:300]}"
            )
        return {
            "ok": ok,
            "status_code": resp.status_code,
            "body": resp.text[:300],
        }
    except requests.RequestException as e:
        print(f"[skipped_listing] Webhook request failed: {e}")
        return {"ok": False, "error": str(e)}


def select_passed_listings_for_post(
    limit: Optional[int] = None,
    sort_by: str = "created_at",       # or "price", "updated_at", etc.
    mark_ready_status: Optional[str] = None,  # e.g., "image_processed" or None to leave "passed"
) -> Dict[str, any]:
    """
    Pull 'passed' listings, filter to allowed regions, enforce 35% cap for rest_of_florida,
    skip overflow with reason, and (optionally) advance the kept ones to next status.

    Returns summary dict with IDs of kept and skipped.
    """
    # init_db()

    # Fetch all PASSED that are candidates for posting
    q = ParsedListing.objects(status="passed")

    # Optional sort (oldest first is typical for fairness)
    if sort_by in {"created_at", "updated_at", "price"}:
        q = q.order_by(f"+{sort_by}")
    else:
        q = q.order_by("+created_at")

    if limit:
        q = q.limit(int(limit))

    candidates: List[ParsedListing] = list(q)

    kept_ids: List[str] = []
    skipped_ids: List[str] = []

    # Separate by region, dropping unsupported
    non_rest: List[ParsedListing] = []   # south_florida_tri_county + st_lucie + fort_pierce
    rest: List[ParsedListing] = []       # rest_of_florida
    bad_region: List[ParsedListing] = [] # any other region or missing

    now = _now()

    # 1) First, enforce Do-Not-Post city rule straight away (AI based)
    for pl in candidates:
        if _is_do_not_post_city(pl):
            updates = {
                "set__status": "skipped",
                "set__do_not_post_city": "found",
                "set__over_35_percent": "not_found",
                "set__rules_ai_rule_id": "Do_Not_Post_City",
                "set__rules_ai_version": "v1",
                "set__rules_ai_reason": "Skipped due to Do Not Post City rule",
                "set__updated_at": now,
            }
            if not pl.skipped_or_posted_at:
                updates["set__skipped_or_posted_at"] = now
            pl.update(**updates)
            skipped_ids.append(str(pl.id))

            # NEW: send full listing to webhook for Do_Not_Post_City
            try:
                _send_skipped_listing_to_webhook(
                    pl,
                    skip_type="Do_Not_Post_City",
                    reason="Skipped due to Do Not Post City rule",
                )
            except Exception as e:
                print(f"[skipped_listing] failed to send Do_Not_Post_City webhook for {pl.id}: {e}")

            # Do NOT consider this listing for region / 35% calculations
            continue

        region = _get_region(pl)
        if region not in ALLOWED_REGIONS:
            bad_region.append(pl)
        else:
            if region == "rest_of_florida":
                rest.append(pl)
            else:
                # south_florida_tri_county / st_lucie / fort_pierce
                non_rest.append(pl)

    # 2) skip unsupported regions
    for pl in bad_region:
        updates = {
            "set__status": "skipped",
            "set__rules_ai_rule_id": "POST_POLICY_REGION",
            "set__rules_ai_version": "v1",
            "set__rules_ai_reason": REASON_BAD_REGION,
            "set__do_not_post_city": "not_found",
            "set__over_35_percent": "not_found",
            "set__updated_at": now,
        }
        if not pl.skipped_or_posted_at:
            updates["set__skipped_or_posted_at"] = now
        pl.update(**updates)
        skipped_ids.append(str(pl.id))

    # 3) enforce 35% cap for rest_of_florida relative to NON-REST
    base_count = len(non_rest)  # current batch base count

    # Use total daily base count (previous + this batch)
    final_base_count = _update_and_get_daily_base_count(base_count, now)

    # Use final_base_count instead of base_count in the cap formula
    rest_cap = math.floor(0.35 * final_base_count)  # allowed count from rest_of_florida for today-so-far
    rest_keep = rest[:rest_cap]
    rest_overflow = rest[rest_cap:]

    for pl in rest_overflow:
        updates = {
            "set__status": "skipped_quota",
            "set__rules_ai_rule_id": "POST_POLICY_35PC",
            "set__rules_ai_version": "v1",
            "set__rules_ai_reason": f"{REASON_OVER_CAP}: allowed={rest_cap}, base_non_rest={final_base_count}",
            "set__over_35_percent": "found",
            "set__do_not_post_city": "not_found",
            "set__updated_at": now,
        }
        if not pl.skipped_or_posted_at:
            updates["set__skipped_or_posted_at"] = now
        pl.update(**updates)
        skipped_ids.append(str(pl.id))

        # NEW: send full listing to webhook for quota skip
        try:
            _send_skipped_listing_to_webhook(
                pl,
                skip_type="POST_POLICY_35PC",
                reason="Skipped due to 35% rest_of_florida daily cap",
                extra={
                    "rest_cap": rest_cap,
                    "final_base_count": final_base_count,
                },
            )
        except Exception as e:
            print(f"[skipped_listing] failed to send POST_POLICY_35PC webhook for {pl.id}: {e}")

    # 4) kept = all non_rest + up-to-cap rest → set to ready_for_image_processing
    kept = non_rest + rest_keep
    for pl in kept:

        db_updates = {
            "set__status": "ready_for_image_processing",
            "set__do_not_post_city": "not_found",
            "set__over_35_percent": "not_found",
            "set__updated_at": now,
        }

        # If we have a source gallery link and we haven't uploaded yet, push to Dropbox
        try:
            src = (pl.other_images_source or "").strip()
            already = (pl.other_images_dropbox_link or "").strip()
            print("src", src)
            print("already", already)
            if src and not already:
                # pick address from top-level field, or from the complete_info blob, or fallback to id
                addr = (pl.address or (pl.complete_info or {}).get("address") or str(pl.id)).strip()
                folder_slug = _slugify_for_folder(addr, fallback=str(pl.id))
                # IMPORTANT: pass only the slug; handle_Link prepends "/PropertyListings/"
                print("folder_slug", folder_slug)
                shared_links = handle_Link([src], folder=folder_slug)  # returns list, usually one folder link
                if shared_links:
                    # store the first link; it’s a shared link to the folder
                    db_updates["set__other_images_dropbox_link"] = shared_links[0]
        except Exception as e:
            print(f"dropbox_upload_error: {e}")
            # Don’t block posting if Dropbox fails
            pass

        pl.update(**db_updates)
        kept_ids.append(str(pl.id))

    return {
        "total_candidates": len(candidates),
        "non_rest_count": len(non_rest),
        "rest_count": len(rest),
        "rest_cap": rest_cap,
        "kept_count": len(kept),
        "kept_ids": kept_ids,
        "skipped_count": len(skipped_ids),
        "skipped_ids": skipped_ids,
    }



# # post_selection.py
# import math
# import re
# from datetime import datetime
# from typing import Dict, List, Optional, Tuple

# from mongo_engine_conn import init_db
# from models import ParsedListing
# from dropboxImageUpload import handle_Link


# # Allowed regions we will post from
# ALLOWED_REGIONS = {
#     "south_florida_tri_county",
#     "st_lucie",
#     "fort_pierce",
#     "rest_of_florida",
# }

# # Reason strings
# REASON_BAD_REGION = "unsupported_region_for_posting"
# REASON_OVER_CAP   = "rest_of_florida_cap_exceeded_35_percent_policy"

# def _get_region(pl: ParsedListing) -> Optional[str]:
#     # Prefer extracted blob, fallback to any top-level field if you later add one
#     ci = pl.complete_info or {}
#     region = ci.get("region_bucket")
#     if isinstance(region, str):
#         return region.strip()
#     return None

# def _now():
#     return datetime.utcnow()

# def _slugify_for_folder(name: str, fallback: str) -> str:
#     """
#     Create a Dropbox-safe folder slug from an address.
#     - strip weird chars
#     - remove spaces
#     - cap length
#     """
#     s = (name or "").strip()
#     if not s:
#         s = fallback
#     # keep letters, digits, space, dot, dash, underscore; turn the rest into underscore
#     s = re.sub(r"[^A-Za-z0-9 ._-]", "_", s)
#     s = s.replace(" ", "")        # compact spaces so path is tidy
#     s = s.strip("._-") or fallback
#     return s[:80]

# def select_passed_listings_for_post(
#     limit: Optional[int] = None,
#     sort_by: str = "created_at",       # or "price", "updated_at", etc.
#     mark_ready_status: Optional[str] = None,  # e.g., "image_processed" or None to leave "passed"
# ) -> Dict[str, any]:
#     """
#     Pull 'passed' listings, filter to allowed regions, enforce 35% cap for rest_of_florida,
#     skip overflow with reason, and (optionally) advance the kept ones to next status.

#     Returns summary dict with IDs of kept and skipped.
#     """
#     # init_db()

#     # Fetch all PASSED that are candidates for posting
#     q = ParsedListing.objects(status="passed")

#     # Optional sort (oldest first is typical for fairness)
#     if sort_by in {"created_at", "updated_at", "price"}:
#         q = q.order_by(f"+{sort_by}")
#     else:
#         q = q.order_by("+created_at")

#     if limit:
#         q = q.limit(int(limit))

#     candidates: List[ParsedListing] = list(q)

#     kept_ids: List[str] = []
#     skipped_ids: List[str] = []

#     # Separate by region, dropping unsupported
#     non_rest: List[ParsedListing] = []   # south_florida_tri_county + st_lucie + fort_pierce
#     rest: List[ParsedListing] = []       # rest_of_florida
#     bad_region: List[ParsedListing] = [] # any other region or missing

#     for pl in candidates:
#         region = _get_region(pl)
#         if region not in ALLOWED_REGIONS:
#             bad_region.append(pl)
#         else:
#             if region == "rest_of_florida":
#                 rest.append(pl)
#             else:
#                 # south_florida_tri_county / st_lucie / fort_pierce
#                 non_rest.append(pl)

#     now = _now()
#     # 2) skip unsupported regions
#     for pl in bad_region:
#         updates = {
#             "set__status": "skipped",
#             "set__rules_ai_rule_id": "POST_POLICY_REGION",
#             "set__rules_ai_version": "v1",
#             "set__rules_ai_reason": REASON_BAD_REGION,
#             "set__updated_at": now,
#         }
#         if not pl.skipped_or_posted_at:
#             updates["set__skipped_or_posted_at"] = now
#         pl.update(**updates)
#         skipped_ids.append(str(pl.id))

#     # 3) enforce 35% cap for rest_of_florida relative to NON-REST
#     base_count = len(non_rest)
#     rest_cap = math.floor(0.35 * base_count)  # allowed count from rest_of_florida
#     rest_keep = rest[:rest_cap]
#     rest_overflow = rest[rest_cap:]


#     for pl in rest_overflow:
#         updates = {
#             "set__status": "skipped",
#             "set__rules_ai_rule_id": "POST_POLICY_35PC",
#             "set__rules_ai_version": "v1",
#             "set__rules_ai_reason": f"{REASON_OVER_CAP}: allowed={rest_cap}, base_non_rest={base_count}",
#             "set__updated_at": now,
#         }
#         if not pl.skipped_or_posted_at:
#             updates["set__skipped_or_posted_at"] = now
#         pl.update(**updates)
#         skipped_ids.append(str(pl.id))

#     # 4) kept = all non_rest + up-to-cap rest → set to ready_to_post
#     kept = non_rest + rest_keep
#     for pl in kept:

#         db_updates = {
#             "set__status": "ready_for_image_processing",
#             "set__updated_at": now,
#         }

#         # If we have a source gallery link and we haven't uploaded yet, push to Dropbox
#         try:
#             src = (pl.other_images_source or "").strip()
#             already = (pl.other_images_dropbox_link or "").strip()
#             print("src",src)
#             print("already",already)
#             if src and not already:
#                 # pick address from top-level field, or from the complete_info blob, or fallback to id
#                 addr = (pl.address or (pl.complete_info or {}).get("address") or str(pl.id)).strip()
#                 folder_slug = _slugify_for_folder(addr, fallback=str(pl.id))
#                 # IMPORTANT: pass only the slug; handle_Link prepends "/PropertyListings/"
#                 print("folder_slug",folder_slug)
#                 shared_links = handle_Link([src], folder=folder_slug)  # returns list, usually one folder link
#                 if shared_links:
#                     # store the first link; it’s a shared link to the folder
#                     db_updates["set__other_images_dropbox_link"] = shared_links[0]
#         except Exception as e:
#             print(f"dropbox_upload_error: {e}")
#             # Don’t block posting if Dropbox fails; you can optionally log or track this
#             # db_updates["set__rules_ai_reason"] = f"dropbox_upload_error: {e}"
#             pass

#         pl.update(**db_updates)
#         kept_ids.append(str(pl.id))
#         # # If already ready_to_post, this is a no-op update; if 'passed', we promote it.
#         # pl.update(
#         #     set__status="ready_to_post",
#         #     set__updated_at=now,
#         #     # We do NOT set skipped_or_posted_at here; set it when actually posted.
#         # )
#         # kept_ids.append(str(pl.id))

#     return {
#         "total_candidates": len(candidates),
#         "non_rest_count": len(non_rest),
#         "rest_count": len(rest),
#         "rest_cap": rest_cap,
#         "kept_count": len(kept),
#         "kept_ids": kept_ids,
#         "skipped_count": len(skipped_ids),
#         "skipped_ids": skipped_ids,
#     }


# # summary = select_passed_listings_for_post(limit=200, sort_by="created_at", mark_ready_status=None)
# # print(summary)


# # WITHOUT AI Start

# # post_selection.py
# import math
# import re
# import json
# import os
# from datetime import datetime, timedelta
# from typing import Dict, List, Optional, Tuple

# from mongo_engine_conn import init_db
# from models import ParsedListing, DailyBaseCount
# from dropboxImageUpload import handle_Link


# # Allowed regions we will post from
# ALLOWED_REGIONS = {
#     "south_florida_tri_county",
#     "st_lucie",
#     "fort_pierce",
#     "rest_of_florida",
# }

# # Reason strings
# REASON_BAD_REGION = "unsupported_region_for_posting"
# REASON_OVER_CAP   = "rest_of_florida_cap_exceeded_35_percent_policy"


# def _load_do_not_post_cities() -> set:
#     """
#     Load list of cities from do_not_post_city.json (lower-cased).
#     If the file is missing or invalid, we fallback to an empty set
#     so existing behaviour is not broken.
#     """
#     path = os.path.join(os.path.dirname(__file__), "do_not_post_city.json")
#     try:
#         with open(path, "r", encoding="utf-8") as f:
#             data = json.load(f)
#         if isinstance(data, list):
#             return {str(x).strip().lower() for x in data if str(x).strip()}
#     except Exception as e:
#         # Non-fatal: just log and continue with empty set
#         print(f"Warning: unable to load do_not_post_city.json: {e}")
#     return set()


# DO_NOT_POST_CITIES = _load_do_not_post_cities()


# def _get_region(pl: ParsedListing) -> Optional[str]:
#     # Prefer extracted blob, fallback to any top-level field if you later add one
#     ci = pl.complete_info or {}
#     region = ci.get("region_bucket")
#     if isinstance(region, str):
#         return region.strip()
#     return None


# def _get_city(pl: ParsedListing) -> Optional[str]:
#     """
#     Get city name from ParsedListing, preferring top-level field,
#     falling back to complete_info["city"] if needed.
#     """
#     if getattr(pl, "city", None):
#         return (pl.city or "").strip()
#     ci = pl.complete_info or {}
#     city = ci.get("city") or ci.get("City")
#     if isinstance(city, str):
#         return city.strip()
#     return None


# def _is_do_not_post_city(pl: ParsedListing) -> bool:
#     city = _get_city(pl)
#     if not city:
#         return False
#     return city.lower() in DO_NOT_POST_CITIES


# def _now():
#     return datetime.utcnow()


# def _slugify_for_folder(name: str, fallback: str) -> str:
#     """
#     Create a Dropbox-safe folder slug from an address.
#     - strip weird chars
#     - remove spaces
#     - cap length
#     """
#     s = (name or "").strip()
#     if not s:
#         s = fallback
#     # keep letters, digits, space, dot, dash, underscore; turn the rest into underscore
#     s = re.sub(r"[^A-Za-z0-9 ._-]", "_", s)
#     s = s.replace(" ", "")        # compact spaces so path is tidy
#     s = s.strip("._-") or fallback
#     return s[:80]


# def _update_and_get_daily_base_count(base_count: int, now: datetime) -> int:
#     """
#     Maintain a rolling base count per day in the DailyBaseCount collection.

#     - Looks up today's document (UTC day).
#     - final_base_count = existing_daily_base_count + base_count
#     - Persists final_base_count back to the document (creating it if needed).
#     - Returns final_base_count.

#     If base_count == 0 and no document exists yet, we simply return 0 and do
#     not create a row (so behaviour is essentially unchanged).
#     """
#     day_start = datetime(now.year, now.month, now.day)
#     next_day_start = day_start + timedelta(days=1)

#     # Fetch today's record (there should be at most one per day)
#     daily_doc = DailyBaseCount.objects(
#         current_date__gte=day_start,
#         current_date__lt=next_day_start,
#     ).first()

#     if not daily_doc:
#         if base_count <= 0:
#             # Nothing to add and no prior record -> effectively 0 base so far today.
#             return 0
#         final_base_count = base_count
#         daily_doc = DailyBaseCount(
#             current_date=day_start,
#             daily_base_count=final_base_count,
#         )
#         daily_doc.save()
#         return final_base_count

#     # We have an existing doc for today
#     existing = daily_doc.daily_base_count or 0
#     final_base_count = existing + max(base_count, 0)

#     # Update in-place (also refresh current_date timestamp if you want)
#     daily_doc.update(
#         set__daily_base_count=final_base_count,
#         set__current_date=now,
#     )
#     return final_base_count


# def select_passed_listings_for_post(
#     limit: Optional[int] = None,
#     sort_by: str = "created_at",       # or "price", "updated_at", etc.
#     mark_ready_status: Optional[str] = None,  # e.g., "image_processed" or None to leave "passed"
# ) -> Dict[str, any]:
#     """
#     Pull 'passed' listings, filter to allowed regions, enforce 35% cap for rest_of_florida,
#     skip overflow with reason, and (optionally) advance the kept ones to next status.

#     Returns summary dict with IDs of kept and skipped.
#     """
#     # init_db()

#     # Fetch all PASSED that are candidates for posting
#     q = ParsedListing.objects(status="passed")

#     # Optional sort (oldest first is typical for fairness)
#     if sort_by in {"created_at", "updated_at", "price"}:
#         q = q.order_by(f"+{sort_by}")
#     else:
#         q = q.order_by("+created_at")

#     if limit:
#         q = q.limit(int(limit))

#     candidates: List[ParsedListing] = list(q)

#     kept_ids: List[str] = []
#     skipped_ids: List[str] = []

#     # Separate by region, dropping unsupported
#     non_rest: List[ParsedListing] = []   # south_florida_tri_county + st_lucie + fort_pierce
#     rest: List[ParsedListing] = []       # rest_of_florida
#     bad_region: List[ParsedListing] = [] # any other region or missing

#     now = _now()

#     # 1) First, enforce Do-Not-Post city rule straight away
#     for pl in candidates:
#         if _is_do_not_post_city(pl):
#             updates = {
#                 "set__status": "skipped",
#                 "set__do_not_post_city": "found",
#                 "set__over_35_percent": "not_found",
#                 "set__rules_ai_rule_id": "Do_Not_Post_City",
#                 "set__rules_ai_version": "v1",
#                 "set__rules_ai_reason": "Skipped due to Do Not Post City rule",
#                 "set__updated_at": now,
#             }
#             if not pl.skipped_or_posted_at:
#                 updates["set__skipped_or_posted_at"] = now
#             pl.update(**updates)
#             skipped_ids.append(str(pl.id))
#             # Do NOT consider this listing for region / 35% calculations
#             continue

#         region = _get_region(pl)
#         if region not in ALLOWED_REGIONS:
#             bad_region.append(pl)
#         else:
#             if region == "rest_of_florida":
#                 rest.append(pl)
#             else:
#                 # south_florida_tri_county / st_lucie / fort_pierce
#                 non_rest.append(pl)

#     # 2) skip unsupported regions
#     for pl in bad_region:
#         updates = {
#             "set__status": "skipped",
#             "set__rules_ai_rule_id": "POST_POLICY_REGION",
#             "set__rules_ai_version": "v1",
#             "set__rules_ai_reason": REASON_BAD_REGION,
#             "set__do_not_post_city": "not_found",
#             "set__over_35_percent": "not_found",
#             "set__updated_at": now,
#         }
#         if not pl.skipped_or_posted_at:
#             updates["set__skipped_or_posted_at"] = now
#         pl.update(**updates)
#         skipped_ids.append(str(pl.id))

#     # 3) enforce 35% cap for rest_of_florida relative to NON-REST
#     base_count = len(non_rest)  # current batch base count

#     # NEW: use total daily base count (previous + this batch)
#     final_base_count = _update_and_get_daily_base_count(base_count, now)

#     # Use final_base_count instead of base_count in the cap formula
#     rest_cap = math.floor(0.35 * final_base_count)  # allowed count from rest_of_florida for today-so-far
#     rest_keep = rest[:rest_cap]
#     rest_overflow = rest[rest_cap:]

#     for pl in rest_overflow:
#         updates = {
#             "set__status": "skipped",
#             "set__rules_ai_rule_id": "POST_POLICY_35PC",
#             "set__rules_ai_version": "v1",
#             "set__rules_ai_reason": f"{REASON_OVER_CAP}: allowed={rest_cap}, base_non_rest={final_base_count}",
#             "set__over_35_percent": "found",
#             "set__do_not_post_city": "not_found",
#             "set__updated_at": now,
#         }
#         if not pl.skipped_or_posted_at:
#             updates["set__skipped_or_posted_at"] = now
#         pl.update(**updates)
#         skipped_ids.append(str(pl.id))

#     # 4) kept = all non_rest + up-to-cap rest → set to ready_to_post
#     kept = non_rest + rest_keep
#     for pl in kept:

#         db_updates = {
#             "set__status": "ready_for_image_processing",
#             "set__do_not_post_city": "not_found",
#             "set__over_35_percent": "not_found",
#             "set__updated_at": now,
#         }

#         # If we have a source gallery link and we haven't uploaded yet, push to Dropbox
#         try:
#             src = (pl.other_images_source or "").strip()
#             already = (pl.other_images_dropbox_link or "").strip()
#             print("src", src)
#             print("already", already)
#             if src and not already:
#                 # pick address from top-level field, or from the complete_info blob, or fallback to id
#                 addr = (pl.address or (pl.complete_info or {}).get("address") or str(pl.id)).strip()
#                 folder_slug = _slugify_for_folder(addr, fallback=str(pl.id))
#                 # IMPORTANT: pass only the slug; handle_Link prepends "/PropertyListings/"
#                 print("folder_slug", folder_slug)
#                 shared_links = handle_Link([src], folder=folder_slug)  # returns list, usually one folder link
#                 if shared_links:
#                     # store the first link; it’s a shared link to the folder
#                     db_updates["set__other_images_dropbox_link"] = shared_links[0]
#         except Exception as e:
#             print(f"dropbox_upload_error: {e}")
#             # Don’t block posting if Dropbox fails; you can optionally log or track this
#             # db_updates["set__rules_ai_reason"] = f"dropbox_upload_error: {e}"
#             pass

#         pl.update(**db_updates)
#         kept_ids.append(str(pl.id))

#     return {
#         "total_candidates": len(candidates),
#         "non_rest_count": len(non_rest),
#         "rest_count": len(rest),
#         "rest_cap": rest_cap,
#         "kept_count": len(kept),
#         "kept_ids": kept_ids,
#         "skipped_count": len(skipped_ids),
#         "skipped_ids": skipped_ids,
#     }
