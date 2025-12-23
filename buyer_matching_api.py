import os
import time
import json
import re
import logging
from typing import Any, Dict, List, Optional, Tuple

import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from dotenv import load_dotenv
from bson import ObjectId

from mongo_engine_conn import init_db
from models import ParsedListing, WebFormBuyerSubmission

from datetime import datetime
from datetime import timedelta
import traceback

load_dotenv()
logger = logging.getLogger(__name__)
router = APIRouter(prefix="/buyer-matching", tags=["buyer-matching"])

# -----------------------------
# Podio constants (from your message)
# -----------------------------
PODIO_BASE_URL = "https://api.podio.com"

PROPERTIES_APP_ID = int(os.getenv("PODIO_PROPERTIES_APP_ID", "18339388"))
BUYERS_APP_ID = int(os.getenv("PODIO_BUYERS_APP_ID", "30585451"))

PROPERTIES_MONGO_ID_FIELD_ID = 275206418      # Properties -> MongoDB Object ID
PROPERTIES_MATCHING_BUYERS_FIELD_ID = 275206477  # Properties -> Matching buyers (app reference)

BUYERS_MONGO_ID_FIELD_ID = 275184431          # Web Form Buyer Submissions -> Mongo Object ID (text)

# Podio auth env vars (same ones you already use)
PODIO_CLIENT_ID = os.getenv("PodioClientId")
PODIO_CLIENT_SECRET = os.getenv("PodioClientSecret")
PODIO_USERNAME = os.getenv("podioUsername")
PODIO_PASSWORD = os.getenv("podioPassword")
PODIO_REDIRECT_URI = os.getenv("redirectUri")

# for tracking
BUYER_MATCHING_BATCH_LIMIT = int(os.getenv("BUYER_MATCHING_BATCH_LIMIT", "5"))
BUYER_MATCHING_MAX_CONSECUTIVE_ERRORS = int(os.getenv("BUYER_MATCHING_MAX_CONSECUTIVE_ERRORS", "3"))
BUYER_MATCHING_PICK_MULTIPLIER = int(os.getenv("BUYER_MATCHING_PICK_MULTIPLIER", "4"))  # fetch more to allow claim skips

# Token cache (avoid hammering auth endpoint)
_PODIO_ACCESS_TOKEN: Optional[str] = None
_PODIO_ACCESS_TOKEN_EXPIRES_AT: float = 0.0

# -----------------------------
# OpenAI config
# -----------------------------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MATCHER_MODEL = os.getenv("MATCHER_MODEL", "gpt-4o-mini")  # change if you want
MATCHER_TEMPERATURE = float(os.getenv("MATCHER_TEMPERATURE", "0"))
AI_BATCH_SIZE = int(os.getenv("MATCHER_AI_BATCH_SIZE", "25"))
MIN_CONFIDENCE = float(os.getenv("MATCHER_MIN_CONFIDENCE", "0.60"))

BUYER_MATCHING_PROCESSING_STALE_MINUTES = int(os.getenv("BUYER_MATCHING_PROCESSING_STALE_MINUTES", "30"))


# -----------------------------
# Request payload
# -----------------------------
class MatchBuyersPayload(BaseModel):
    mongodb_object_id: str
    # optional: if you decide to pass property item id from Globiflow, we’ll use it
    podio_property_item_id: Optional[int] = None
    dry_run: bool = False


class EnqueueBuyerMatchingPayload(BaseModel):
    mongodb_object_id: str
    podio_property_item_id: int  # Globiflow should send this

# -------------------------------------------------------------------
# DB init (safe to call multiple times; mongoengine is idempotent)
# -------------------------------------------------------------------
init_db()


# -------------------------------------------------------------------
# Podio Auth + request helper (same pattern as your production file)
# -------------------------------------------------------------------
def get_podio_access_token(force_refresh: bool = False) -> str:
    global _PODIO_ACCESS_TOKEN, _PODIO_ACCESS_TOKEN_EXPIRES_AT

    now = time.time()
    if (
        not force_refresh
        and _PODIO_ACCESS_TOKEN
        and now < _PODIO_ACCESS_TOKEN_EXPIRES_AT - 60
    ):
        return _PODIO_ACCESS_TOKEN

    if not all([PODIO_CLIENT_ID, PODIO_CLIENT_SECRET, PODIO_USERNAME, PODIO_PASSWORD, PODIO_REDIRECT_URI]):
        raise RuntimeError("Missing Podio OAuth environment variables")

    auth_url = f"{PODIO_BASE_URL}/oauth/token/v2"
    payload = {
        "grant_type": "password",
        "username": PODIO_USERNAME,
        "password": PODIO_PASSWORD,
        "client_id": PODIO_CLIENT_ID,
        "client_secret": PODIO_CLIENT_SECRET,
        "redirect_uri": PODIO_REDIRECT_URI,
    }

    resp = requests.post(auth_url, json=payload, timeout=20)
    if resp.status_code != 200:
        raise RuntimeError(f"Podio token fetch failed: {resp.status_code} {resp.text}")

    data = resp.json()
    token = data.get("access_token")
    if not token:
        raise RuntimeError(f"Podio auth response missing access_token: {data}")

    expires_in = data.get("expires_in", 3600)
    _PODIO_ACCESS_TOKEN = token
    _PODIO_ACCESS_TOKEN_EXPIRES_AT = now + float(expires_in)
    return token


def _podio_request(
    method: str,
    path: str,
    *,
    token: Optional[str] = None,
    retry_on_401: bool = True,
    **kwargs,
) -> Optional[Any]:
    if token is None:
        token = get_podio_access_token()

    headers = kwargs.pop("headers", {})
    headers.setdefault("Authorization", f"Bearer {token}")
    headers.setdefault("Content-Type", "application/json")

    url = f"{PODIO_BASE_URL}{path}"
    resp = requests.request(method, url, headers=headers, timeout=30, **kwargs)

    if resp.status_code == 401 and retry_on_401:
        token = get_podio_access_token(force_refresh=True)
        headers["Authorization"] = f"Bearer {token}"
        resp = requests.request(method, url, headers=headers, timeout=30, **kwargs)

    if not resp.ok:
        logger.warning("Podio request failed: %s %s -> %s %s", method, path, resp.status_code, resp.text)
        return None

    if resp.status_code == 204 or not resp.text.strip():
        return {}

    try:
        return resp.json()
    except ValueError:
        return {"raw": resp.text}


def podio_search_app_items(app_id: int, query: str, search_fields: Optional[List[int]] = None, limit: int = 10) -> List[int]:
    token = get_podio_access_token()
    payload: Dict[str, Any] = {
        "query": query,
        "ref_type": "item",
        "limit": limit,
        "offset": 0,
    }
    if search_fields:
        payload["search_fields"] = search_fields

    data = _podio_request("POST", f"/search/app/{app_id}/", token=token, json=payload)
    if not isinstance(data, dict):
        return []

    results = data.get("results") or []
    item_ids: List[int] = []
    for r in results:
        # Podio search typically returns "id" for item id; keep fallback
        iid = r.get("id") or r.get("item_id")
        if iid:
            try:
                item_ids.append(int(iid))
            except Exception:
                pass
    return item_ids


def podio_set_app_reference_field(item_id: int, field_id: int, referenced_item_ids: List[int]) -> bool:
    token = get_podio_access_token()
    payload = [int(x) for x in referenced_item_ids]
    data = _podio_request("PUT", f"/item/{item_id}/value/{field_id}", token=token, json=payload)
    return data is not None


# -------------------------------------------------------------------
# Matching helpers (Stage 1: NO AI)
# -------------------------------------------------------------------
def _norm_text(s: Optional[str]) -> str:
    if not s:
        return ""
    s = s.strip().lower()
    s = re.sub(r"\bcounty\b", "", s, flags=re.I)
    s = re.sub(r"[^a-z0-9\s\-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _city_equivalents(city: str) -> List[str]:
    """
    Handle common "St"/"Saint" differences and punctuation.
    """
    c = _norm_text(city)
    if not c:
        return []
    equiv = {c}
    # st <-> saint
    if c.startswith("st "):
        equiv.add("saint " + c[3:].strip())
    if c.startswith("st-"):
        equiv.add("saint " + c[3:].strip())
    if c.startswith("saint "):
        equiv.add("st " + c[6:].strip())
    return list(equiv)


def _county_equivalents(county: str) -> List[str]:
    """
    Make small-form vs full-form matches work (Miami-Dade vs Dade, etc.)
    without hardcoding a single county.
    """
    c = _norm_text(county)
    if not c:
        return []
    parts = re.split(r"[\s\-]+", c)
    parts = [p for p in parts if p]
    equiv = {c}
    # include each token as a possible short form (e.g., "dade")
    for p in parts:
        equiv.add(p)
    # include joined without spaces (rare but safe)
    equiv.add("".join(parts))
    return list(equiv)

def stage1_location_match(listing_city: str, listing_county: str, buyer_city: str, buyer_county: str) -> bool:
    # STRICT: buyer must have BOTH county and city (per your requirement)
    bc = _norm_text(buyer_county)
    bcity = _norm_text(buyer_city)
    if not bc or not bcity:
        return False

    lc_norm = _norm_text(listing_county)
    lcity_opts = _city_equivalents(listing_city)

    # County: token-aware match (avoid overly permissive substring matches)
    buyer_tokens = set([t for t in re.split(r"[\s\-]+", bc) if t and len(t) >= 3])
    listing_tokens = set([t for t in re.split(r"[\s\-]+", lc_norm) if t and len(t) >= 3])
    if not (buyer_tokens & listing_tokens):
        return False

    # City: st/saint normalization + exact/contains (still strict)
    city_ok = any(
        bcity == _norm_text(x) or
        re.search(rf"\b{re.escape(bcity)}\b", _norm_text(x))
        for x in lcity_opts
    )
    return bool(city_ok)

def get_listing_property_bucket(listing_complete_info: Dict[str, Any]) -> str:
    """
    Map listing to one of:
    single_family, multi_family, condo, commercial, land, townhouse
    based primarily on complete_info.property_type and fallback flags.
    """
    pt = _norm_text(str(listing_complete_info.get("property_type") or ""))
    # normalize common variants
    if "single" in pt:
        return "single_family"
    if "multi" in pt or "duplex" in pt or "triplex" in pt or "fourplex" in pt:
        return "multi_family"
    if "condo" in pt:
        return "condo"
    if "town" in pt:
        return "townhouse"
    if "land" in pt or listing_complete_info.get("is_land_only") is True:
        return "land"
    if "commercial" in pt:
        return "commercial"

    # fallback based on flags if any
    if listing_complete_info.get("is_condo") is True:
        return "condo"
    return "single_family"


# -------------------------------------------------------------------
# Price parsing (used inside AI step)
# -------------------------------------------------------------------
def parse_price_range(price_range: str) -> Tuple[Optional[float], Optional[float]]:
    """
    Supports:
    - "$300,000 - $600,000"
    - "$1,000,000+"
    - "" (no filter)
    Returns (min, max). max=None means open-ended.
    """
    s = (price_range or "").strip()
    if not s:
        return (None, None)

    s = s.replace(",", "")
    # open ended: 1000000+
    m = re.search(r"\$?\s*(\d+(?:\.\d+)?)\s*\+\s*$", s)
    if m:
        return (float(m.group(1)), None)

    # range: a - b
    m = re.search(r"\$?\s*(\d+(?:\.\d+)?)\s*-\s*\$?\s*(\d+(?:\.\d+)?)", s)
    if m:
        return (float(m.group(1)), float(m.group(2)))

    # single number fallback
    m = re.search(r"(\d+(?:\.\d+)?)", s)
    if m:
        v = float(m.group(1))
        return (v, v)

    return (None, None)


# -------------------------------------------------------------------
# OpenAI JSON call (robust JSON extraction)
# -------------------------------------------------------------------
def _extract_json_obj(text: str) -> Dict[str, Any]:
    text = (text or "").strip()
    # first try direct parse
    try:
        return json.loads(text)
    except Exception:
        pass
    # fallback: find first {...}
    m = re.search(r"\{.*\}", text, flags=re.S)
    if not m:
        raise ValueError("No JSON object found in model response")
    return json.loads(m.group(0))


def call_ai_matcher(property_payload: Dict[str, Any], candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY missing in environment")

    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)

        system = (
    "You are a strict property-to-buyer matching engine.\n"
    "Use ONLY the evidence provided. Do NOT guess. Do NOT assume.\n\n"

    "Evidence you may use:\n"
    "- structured property fields (booleans, numeric, tags)\n"
    "- evidence_text_excerpt\n\n"

    "Price rule:\n"
    "- If buyer price_range empty => PASS.\n"
    "- Else if property price_usd is known => must fall in range inclusive.\n"
    "- Else (unknown price) => FAIL (cannot confirm).\n\n"

    "Buyer selected_type rule:\n"
    "- If selected_type is generic (e.g., just interest confirmation) => PASS.\n"
    "- If selected_type contains any concrete constraints and you cannot confirm from evidence => FAIL.\n\n"

    "Special preferences rule (per label):\n"
    "- Determine status as PRESENT, ABSENT, or UNKNOWN based on evidence.\n"
    "- Yes => must be PRESENT.\n"
    "- Only => must be PRESENT.\n"
    "- No => must be ABSENT.\n"
    "- Maybe => ignore (always pass that preference).\n"
    "- If status is UNKNOWN for Yes/Only/No => FAIL.\n\n"

    "Return ONLY JSON:\n"
    "{\n"
    '  "matched_buyer_mongo_ids": string[],\n'
    '  "evaluations": [{buyer_mongo_id, match, confidence_0_to_1, reasons: string[], failed_checks: string[]}]\n'
    "}\n"
)


        user = {
            "property": property_payload,
            "candidates": candidates,
        }

        resp = client.chat.completions.create(
            model=MATCHER_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
            ],
            temperature=MATCHER_TEMPERATURE,
        )

        content = resp.choices[0].message.content
        return _extract_json_obj(content)

    except Exception as e:
        raise RuntimeError(f"AI matcher failed: {str(e)}")


def build_loose_regex_from_text(s: str) -> str:
    # Create alternatives for "Miami-Dade" => (miami\s*-\s*dade|miami\s+dade|dade)
    n = _norm_text(s)
    if not n:
        return r"^$"  # match nothing
    tokens = [t for t in re.split(r"[\s\-]+", n) if t and len(t) >= 3]
    alts = set()
    alts.add(re.escape(n).replace(r"\ ", r"\s+").replace(r"\-", r"\s*-\s*"))
    for t in tokens:
        alts.add(rf"\b{re.escape(t)}\b")
    return "(" + "|".join(sorted(alts, key=len, reverse=True)) + ")"

def extract_county_from_geocode(geo: Dict[str, Any]) -> str:
    comps = (geo or {}).get("address_components") or []
    for c in comps:
        if "administrative_area_level_2" in (c.get("types") or []):
            return c.get("long_name") or c.get("short_name") or ""
    return ""

def extract_city_from_geocode(geo: Dict[str, Any]) -> str:
    comps = (geo or {}).get("address_components") or []
    for c in comps:
        if "locality" in (c.get("types") or []):
            return c.get("long_name") or c.get("short_name") or ""
    return ""


# For Tracking and flag handling

def _normalize_error_sig(msg: str) -> str:
    """
    Make an error signature stable so 'same error again and again' is detectable.
    Removes object ids, numbers, extra whitespace.
    """
    s = (msg or "").strip().lower()
    s = re.sub(r"[0-9a-f]{24}", "<oid>", s)   # mongodb oid
    s = re.sub(r"\d+", "<n>", s)             # numbers
    s = re.sub(r"\s+", " ", s).strip()
    return s[:300]


def _claim_pending_listing(listing_id: ObjectId) -> bool:
    """
    Atomic-ish claim: only claim if status is still pending.
    Also increments attempts and sets attempt timestamp.
    """
    updated = ParsedListing.objects(
        id=listing_id,
        buyer_matching_status="pending",
    ).update_one(
        set__buyer_matching_status="processing",
        set__buyer_matching_last_attempt_at=datetime.utcnow(),
        inc__buyer_matching_attempts=1,
    )
    return updated == 1

def reset_stale_processing_listings() -> int:
    """
    If a listing is stuck in 'processing' beyond stale window, reset it to 'pending'
    so cron can retry it.
    """
    cutoff = datetime.utcnow() - timedelta(minutes=BUYER_MATCHING_PROCESSING_STALE_MINUTES)

    # Only reset ones that have a last_attempt_at older than cutoff
    # (meaning they were claimed long ago and probably crashed mid-run)
    updated = ParsedListing.objects(
        buyer_matching_status="processing",
        buyer_matching_last_attempt_at__lte=cutoff
    ).update(
        set__buyer_matching_status="pending",
    )

    return int(updated or 0)

def process_pending_buyer_matching_batch(limit: int = BUYER_MATCHING_BATCH_LIMIT) -> Dict[str, Any]:
    """
    Runs in scheduler every 3-5 mins.
    Picks pending listings and processes them using existing /match logic (internal call),
    with retry + quarantine.
    """
    stale_reset = reset_stale_processing_listings()
    if stale_reset:
        logger.warning("Reset %s stale buyer_matching_status=processing listings back to pending", stale_reset)

    picked = 0
    processed = 0
    matched = 0
    errored = 0
    left_pending = 0

    # fetch more than limit to allow for claim collisions / skips
    to_fetch = max(limit * BUYER_MATCHING_PICK_MULTIPLIER, limit)


    pending_listings = list(
        ParsedListing.objects(buyer_matching_status="pending")
        .order_by("updated_at")
        .only("id", "buyer_matching_podio_item_id", "buyer_matching_last_error_sig", "buyer_matching_consecutive_errors")
        .limit(to_fetch)
    )

    for l in pending_listings:
        if processed >= limit:
            break

        picked += 1

        # require podio item id for best reliability; if missing, keep pending
        podio_item_id = getattr(l, "buyer_matching_podio_item_id", None)
        if not podio_item_id:
            # keep pending, do not claim
            left_pending += 1
            continue

        # claim
        if not _claim_pending_listing(l.id):
            continue

        try:
            # Run the existing matching logic (internal call, not HTTP)
            result = match_buyers(MatchBuyersPayload(
                mongodb_object_id=str(l.id),
                podio_property_item_id=int(podio_item_id),
                dry_run=False,
            ))

            # IMPORTANT: if buyers matched but podio update failed, treat as retryable error
            if (
                isinstance(result, dict)
                and int(result.get("matched_buyers_count") or 0) > 0
                and result.get("podio_updated") is not True
            ):
                raise RuntimeError("Podio update failed while buyers matched; will retry")

            # Success: mark matched (even if zero buyers matched — job completed cleanly)
            ParsedListing.objects(id=l.id).update_one(
                set__buyer_matching_status="matched",
                set__buyer_matching_consecutive_errors=0,
                set__buyer_matching_last_error_sig=None,
                set__buyer_matching_last_error=None,
            )

            processed += 1
            matched += 1
            continue

        except Exception as e:
            processed += 1
            msg = f"{type(e).__name__}: {str(e)}"
            sig = _normalize_error_sig(msg)

            # reload minimal error state (safe)
            curr = ParsedListing.objects(id=l.id).only(
                "buyer_matching_last_error_sig", "buyer_matching_consecutive_errors"
            ).first()

            prev_sig = (getattr(curr, "buyer_matching_last_error_sig", None) or "")
            prev_consec = int(getattr(curr, "buyer_matching_consecutive_errors", 0) or 0)

            if sig and sig == prev_sig:
                new_consec = prev_consec + 1
            else:
                new_consec = 1

            # Decide status
            if new_consec >= BUYER_MATCHING_MAX_CONSECUTIVE_ERRORS:
                # quarantine
                ParsedListing.objects(id=l.id).update_one(
                    set__buyer_matching_status="errored_listing",
                    set__buyer_matching_consecutive_errors=new_consec,
                    set__buyer_matching_last_error_sig=sig,
                    set__buyer_matching_last_error=msg,
                )
                errored += 1
            else:
                # keep pending for later retry
                ParsedListing.objects(id=l.id).update_one(
                    set__buyer_matching_status="pending",
                    set__buyer_matching_consecutive_errors=new_consec,
                    set__buyer_matching_last_error_sig=sig,
                    set__buyer_matching_last_error=msg,
                )
                left_pending += 1

            logger.exception("Buyer matching failed for listing %s (sig=%s consec=%s)", str(l.id), sig, new_consec)
            continue

    return {
        "ok": True,
        "picked": picked,
        "processed": processed,
        "matched": matched,
        "errored_listing": errored,
        "left_pending": left_pending,
        "batch_limit": limit,
        "max_consecutive_errors": BUYER_MATCHING_MAX_CONSECUTIVE_ERRORS,
    }



# -------------------------------------------------------------------
# Main endpoints
# -------------------------------------------------------------------

@router.post("/enqueue")
def enqueue_buyer_matching(payload: EnqueueBuyerMatchingPayload):
    # validate object id
    try:
        listing_oid = ObjectId(payload.mongodb_object_id.strip())
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid mongodb_object_id (not a valid ObjectId)")

    listing: Optional[ParsedListing] = ParsedListing.objects(id=listing_oid).first()
    if not listing:
        raise HTTPException(status_code=404, detail="Listing not found in parsed_listings for given ObjectId")

    # If already matched, don't requeue (safe + idempotent).
    if (listing.buyer_matching_status or "none") == "matched":
        return {
            "ok": True,
            "queued": False,
            "reason": "already_matched",
            "mongodb_object_id": payload.mongodb_object_id,
            "buyer_matching_status": listing.buyer_matching_status,
            "podio_property_item_id": listing.buyer_matching_podio_item_id,
        }

    # If errored_listing, do not auto requeue (prevents looping)
    if (listing.buyer_matching_status or "none") == "errored_listing":
        return {
            "ok": True,
            "queued": False,
            "reason": "errored_listing_not_requeued",
            "mongodb_object_id": payload.mongodb_object_id,
            "buyer_matching_status": listing.buyer_matching_status,
            "podio_property_item_id": listing.buyer_matching_podio_item_id,
        }

    # Mark pending + save podio item id
    ParsedListing.objects(id=listing_oid).update_one(
    set__buyer_matching_status="pending",
    set__buyer_matching_podio_item_id=int(payload.podio_property_item_id),
    set__updated_at=datetime.utcnow(),
    )

    return {
        "ok": True,
        "queued": True,
        "mongodb_object_id": payload.mongodb_object_id,
        "buyer_matching_status": listing.buyer_matching_status,
        "podio_property_item_id": listing.buyer_matching_podio_item_id,
    }


@router.post("/match")
def match_buyers(payload: MatchBuyersPayload):
    # 1) Validate listing ObjectId
    try:
        listing_oid = ObjectId(payload.mongodb_object_id.strip())
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid mongodb_object_id (not a valid ObjectId)")

    # 2) Fetch listing from parsed_listings
    listing: Optional[ParsedListing] = ParsedListing.objects(id=listing_oid).first()
    if not listing:
        raise HTTPException(status_code=404, detail="Listing not found in parsed_listings for given ObjectId")

    complete_info = listing.complete_info or {}
    # Your sample stores actual fields under complete_info["complete_info"] too; handle both
    listing_ci = complete_info.get("complete_info") if isinstance(complete_info.get("complete_info"), dict) else complete_info
    print("listing_ci=====",listing_ci)

    listing_city = str(listing_ci.get("city") or listing.city or "")
    listing_county = str(listing_ci.get("county") or "")
    listing_state = str(listing_ci.get("state") or listing.state or "")
    listing_zip = str(listing_ci.get("zip") or listing.zip or "")
    listing_price = listing_ci.get("list_price_usd") or listing.price



    if not listing_county:
        listing_county = extract_county_from_geocode(listing.geo_code_response or {})
    if not listing_city:
        listing_city = extract_city_from_geocode(listing.geo_code_response or {})

    bucket = get_listing_property_bucket(listing_ci)

    # 3) Stage 1: NO AI filter — county AND city AND property type enabled
    # Fetch buyers who enabled this bucket
    enabled_filter = {f"{bucket}__enabled": True}
    county_re = build_loose_regex_from_text(listing_county)
    city_re = build_loose_regex_from_text(listing_city)

    print("county_re=====",county_re)
    print("city_re=====",city_re)

    buyers_qs = WebFormBuyerSubmission.objects(
        **enabled_filter,
        location__county__iregex=county_re,
        location__city__iregex=city_re,
    )

    print("buyers_qs=====",buyers_qs)
    
    stage1_candidates: List[WebFormBuyerSubmission] = []
    for b in buyers_qs:
        b_city = (b.location.city if b.location else "") or ""
        b_county = (b.location.county if b.location else "") or ""
        if stage1_location_match(listing_city, listing_county, b_city, b_county):
            stage1_candidates.append(b)
    print("stage1_candidates=====",stage1_candidates)
    # 4) Stage 2: AI filter within stage1 group
    # Build compact property payload for the model (include evidence, but keep it bounded)
    raw_text = ""
    try:
        raw_text = str(listing_ci.get("complete_info") or "")
    except Exception:
        raw_text = ""

    raw_excerpt = raw_text[:2000]  # keep prompt stable

    property_payload: Dict[str, Any] = {
        "mongodb_object_id": str(listing.id),
        "address": listing_ci.get("address") or listing.address,
        "city": listing_city,
        "county": listing_county,
        "state": listing_state,
        "zip": listing_zip,
        "price_usd": listing_price,
        "property_type_bucket": bucket,
        "property_type_raw": listing_ci.get("property_type"),
        "bedrooms": listing_ci.get("bedrooms"),
        "bathrooms_full": listing_ci.get("bathrooms_full"),
        "living_area_sqft": listing_ci.get("living_area_sqft"),
        "lot_size_sqft": listing_ci.get("lot_size_sqft"),
        "year_built": listing_ci.get("year_built"),
        "build_material": listing_ci.get("build_material"),
        "is_frame_or_wood": listing_ci.get("is_frame_or_wood"),
        "is_condo": listing_ci.get("is_condo"),
        "is_land_only": listing_ci.get("is_land_only"),
        "is_on_water": listing_ci.get("is_on_water"),
        "water_feature": listing_ci.get("water_feature"),
        "marketing_tags": listing_ci.get("marketing_tags") or [],
        "raw_description_excerpt": listing_ci.get("raw_description_excerpt"),
        "evidence_text_excerpt": raw_excerpt,
    }

    # Turn buyers into AI candidate objects (only the relevant bucket section)
    def buyer_bucket_obj(b: WebFormBuyerSubmission) -> Dict[str, Any]:
        bucket_doc = getattr(b, bucket, None)
        prefs = {}
        price_range = ""
        selected_type = ""
        if bucket_doc:
            prefs = bucket_doc.preferences or {}
            price_range = bucket_doc.price_range or ""
            selected_type = bucket_doc.type or ""
        return {
            "buyer_mongo_id": str(b.id),
            "contact": {
                "name": (b.contact.name if b.contact else ""),
                "email": (b.contact.email if b.contact else ""),
                "company": (b.contact.company if b.contact else ""),
            },
            "location": {
                "county": (b.location.county if b.location else ""),
                "city": (b.location.city if b.location else ""),
            },
            "bucket": bucket,
            "selected_type": selected_type,
            "price_range": price_range,
            "parsed_price_range": {
                "min": parse_price_range(price_range)[0],
                "max": parse_price_range(price_range)[1],
            },
            "preferences": prefs,
        }

    ai_candidates = [buyer_bucket_obj(b) for b in stage1_candidates]

    print("ai_candidates=====",ai_candidates)

    matched_buyer_ids: List[str] = []
    evaluations_all: List[Dict[str, Any]] = []

    # Chunk AI calls
    for i in range(0, len(ai_candidates), AI_BATCH_SIZE):
        batch = ai_candidates[i:i + AI_BATCH_SIZE]
        if not batch:
            continue

        

        ai_result = call_ai_matcher(property_payload, batch)

        print("ai_result=====",ai_result)

        batch_matches = ai_result.get("matched_buyer_mongo_ids") or []
        batch_evals = ai_result.get("evaluations") or []

        print("batch_matches=====",batch_matches)
        print("batch_evals=====",batch_evals)

        # apply confidence threshold if provided
        for ev in batch_evals:
            try:
                evaluations_all.append(ev)
            except Exception:
                pass

        for ev in batch_evals:
            if not isinstance(ev, dict):
                continue
            if ev.get("match") is True and float(ev.get("confidence_0_to_1") or 0) >= MIN_CONFIDENCE:
                mid = ev.get("buyer_mongo_id")
                if isinstance(mid, str) and mid:
                    matched_buyer_ids.append(mid)

        # fallback: if model only returned matched_buyer_mongo_ids
        if batch_matches:
            for mid in batch_matches:
                if isinstance(mid, str) and mid:
                    matched_buyer_ids.append(mid)

    # de-dup
    matched_buyer_ids = sorted(list(set(matched_buyer_ids)))

    print("matched_buyer_ids=====",matched_buyer_ids)

    # 5) Update parsed_listings with matched buyer Mongo ids
    
    if not payload.dry_run:
        ParsedListing.objects(id=listing_oid).update_one(
            set__matched_buyer_ids=matched_buyer_ids,
            set__updated_at=datetime.utcnow(),
        )

    # 6) If matches found => update Podio property reference field
    podio_updated = False
    property_item_id: Optional[int] = payload.podio_property_item_id

    # If property_item_id not provided, search Properties app by the MongoDB Object ID field
    if property_item_id is None:
        found_props = podio_search_app_items(
            PROPERTIES_APP_ID,
            payload.mongodb_object_id.strip(),
            search_fields=[PROPERTIES_MONGO_ID_FIELD_ID],
            limit=5,
        )
        if not found_props:
            # we still return matched_buyer_ids, but Podio update can’t happen
            property_item_id = None
        else:
            property_item_id = int(found_props[0])

    matched_podio_buyer_item_ids: List[int] = []
    if matched_buyer_ids:
        # Load matched buyer docs
        matched_docs = list(WebFormBuyerSubmission.objects(id__in=[ObjectId(x) for x in matched_buyer_ids if ObjectId.is_valid(x)]))

        # Prefer stored buyer.podio_item_id, otherwise search Podio by buyer mongo id field
        for b in matched_docs:
            if b.podio_item_id:
                try:
                    matched_podio_buyer_item_ids.append(int(b.podio_item_id))
                    continue
                except Exception:
                    pass

            # fallback search
            buyer_item_ids = podio_search_app_items(
                BUYERS_APP_ID,
                str(b.id),
                search_fields=[BUYERS_MONGO_ID_FIELD_ID],
                limit=3,
            )
            if buyer_item_ids:
                matched_podio_buyer_item_ids.append(int(buyer_item_ids[0]))

    matched_podio_buyer_item_ids = sorted(list(set(matched_podio_buyer_item_ids)))

    print("matched_podio_buyer_item_ids=====",matched_podio_buyer_item_ids)

    if (not payload.dry_run) and property_item_id and matched_podio_buyer_item_ids:
        podio_updated = podio_set_app_reference_field(
            item_id=int(property_item_id),
            field_id=PROPERTIES_MATCHING_BUYERS_FIELD_ID,
            referenced_item_ids=matched_podio_buyer_item_ids,
        )
    
    print("Final return=====",{
        "ok": True,
        "mongodb_object_id": payload.mongodb_object_id,
        "bucket": bucket,
        "stage1_candidates_count": len(stage1_candidates),
        "matched_buyers_count": len(matched_buyer_ids),
        "matched_buyer_mongo_ids": matched_buyer_ids,
        "property_podio_item_id": property_item_id,
        "matched_buyer_podio_item_ids": matched_podio_buyer_item_ids,
        "podio_updated": podio_updated,
        "dry_run": payload.dry_run,
        "evaluations_sample": evaluations_all[:10],  # helpful for debugging in logs
    }
)
    

    return {
        "ok": True,
        "mongodb_object_id": payload.mongodb_object_id,
        "bucket": bucket,
        "stage1_candidates_count": len(stage1_candidates),
        "matched_buyers_count": len(matched_buyer_ids),
        "matched_buyer_mongo_ids": matched_buyer_ids,
        "property_podio_item_id": property_item_id,
        "matched_buyer_podio_item_ids": matched_podio_buyer_item_ids,
        "podio_updated": podio_updated,
        "dry_run": payload.dry_run,
        "evaluations_sample": evaluations_all[:10],  # helpful for debugging in logs
    }
