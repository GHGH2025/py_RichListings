import os
import time
import json
import re
import logging

from html import unescape
from typing import Any, Dict, List, Optional, Tuple
from mongoengine.queryset.visitor import Q
import difflib
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

PROPERTIES_SPECIAL_PREFERENCES_FIELD_ID = int(os.getenv("PODIO_PROPERTIES_SPECIAL_PREFERENCES_FIELD_ID", "275389745"))


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
# Phase 1 enhancement START
# -------------------------------------------------------------------
def podio_richtext_to_plain(s: str) -> str:
    s = unescape((s or "").strip())

    # convert common html breaks to newlines
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</p\s*>", "\n", s)
    s = re.sub(r"(?i)<p\s*>", "", s)

    # strip any remaining tags
    s = re.sub(r"<[^>]+>", " ", s)

    # normalize whitespace/newlines
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    return s.strip()

def normalize_manual_prefs_text(text: str) -> List[str]:
    """
    Normalizes Podio comma/newline separated manual prefs so whitespace/case/reorder doesn't trigger rerun.
    Keeps symbols like '+' (unlike _norm_text which strips many chars).
    """
    raw = podio_richtext_to_plain(text)  # ✅ strip html first
    raw = (raw or "").strip()
    if not raw:
        return []

    parts = re.split(r"[,\n;]+", raw)
    out: List[str] = []
    for p in parts:
        s = (p or "").strip().lower()
        s = re.sub(r"\s+", " ", s).strip(" \t\r\n-_.")
        if s:
            out.append(s)

    # de-dup stable
    return sorted(list(set(out)))


def podio_get_item(item_id: int) -> Optional[Dict[str, Any]]:
    token = get_podio_access_token()
    data = _podio_request("GET", f"/item/{int(item_id)}", token=token)
    return data if isinstance(data, dict) else None


def podio_extract_text_field(item: Dict[str, Any], field_id: int) -> str:
    """
    Extract value for a Podio text/multi-line text field from item payload.
    """
    if not item or not field_id:
        return ""

    fields = item.get("fields") or []
    for f in fields:
        try:
            if int(f.get("field_id") or 0) != int(field_id):
                continue
        except Exception:
            continue

        values = f.get("values") or []
        chunks = []
        for v in values:
            val = v.get("value")
            if val is None:
                continue
            chunks.append(str(val))
        return "\n".join([c for c in chunks if c.strip()]).strip()

    return ""


def _safe_float(x) -> Optional[float]:
    try:
        if x is None:
            return None
        if isinstance(x, (int, float)):
            return float(x)
        s = str(x).strip().replace(",", "")
        if not s:
            return None
        return float(s)
    except Exception:
        return None


def _county_variants(raw: str) -> List[str]:
    """
    Variants to help match array-stored counties (case-sensitive in Mongo __in),
    plus python-side normalization matching.
    """
    r = (raw or "").strip()
    if not r:
        return []
    r2 = re.sub(r"\bcounty\b", "", r, flags=re.I).strip()
    variants = {r, r2, r.title(), r2.title(), r.upper(), r2.upper(), r.lower(), r2.lower()}
    # Miami-Dade vs Dade helper
    tokens = [t for t in re.split(r"[\s\-]+", r2) if t]
    for t in tokens:
        variants.add(t)
        variants.add(t.title())
        variants.add(t.lower())
    return [v for v in variants if v]

# -----------------------------
# NEW: list/string normalizers
# -----------------------------
def _clean_list_any(x: Any) -> List[str]:
    """
    Accepts list[str] OR a legacy string, returns clean list[str].
    """
    if not x:
        return []
    if isinstance(x, list):
        return [str(v).strip() for v in x if str(v).strip()]
    s = str(x).strip()
    if not s:
        return []
    # allow comma-separated legacy strings
    if "," in s:
        return [p.strip() for p in s.split(",") if p.strip()]
    return [s]

def _case_variants(s: str) -> List[str]:
    s = (s or "").strip()
    if not s:
        return []
    return list({s, s.title(), s.upper(), s.lower()})


# -----------------------------
# NEW: location matching v3 (uses global counties/cities arrays)
# -----------------------------
SOUTH_FLORIDA_COUNTY_TOKENS = {"miami", "dade", "broward", "palm", "beach"}

def _is_south_florida_listing(listing_ci: Dict[str, Any], listing_city: str, listing_county: str) -> bool:
    # if parsed region fields exist, trust them
    tri = _norm_text(str(listing_ci.get("tri_county_name") or ""))
    if tri in {"miami_dade", "broward", "palm_beach"}:
        return True

    # fallback token match on county name
    lc = _norm_text(listing_county)
    tokens = {t for t in re.split(r"[\s\-]+", lc) if t and len(t) >= 3}
    return bool(tokens & SOUTH_FLORIDA_COUNTY_TOKENS)

def _city_list_match(listing_city: str, buyer_cities: List[str]) -> bool:
    if not listing_city or not buyer_cities:
        return False

    listing_opts = {_norm_text(listing_city)}
    # also support st/saint variants on listing side
    for x in _city_equivalents(listing_city):
        listing_opts.add(_norm_text(x))

    for bc in buyer_cities:
        bc_norm = _norm_text(bc)
        if not bc_norm:
            continue
        # exact or token-boundary containment
        for lo in listing_opts:
            if bc_norm == lo or re.search(rf"\b{re.escape(bc_norm)}\b", lo):
                return True
    return False

def buyer_location_match_v3(
    listing_city: str,
    listing_county: str,
    listing_ci: Dict[str, Any],
    b: WebFormBuyerSubmission,
    bucket: str
) -> bool:
    """
    NEW RULES:
    - Use bucket location if present (scope + counties).
    - Use GLOBAL location arrays: location.counties[] / location.cities[].
    - If buyer has cities[] => city must match (most precise).
    - Else if buyer has counties[] => county must match.
    - Else use scope presets (all_florida => pass, south_florida => tri-county check).
    - Else fallback to legacy strict county+city.
    """
    bucket_doc = getattr(b, bucket, None)
    bucket_loc = getattr(bucket_doc, "location", None) if bucket_doc else None

    global_loc = getattr(b, "location", None)

    bucket_scope = _scope_norm(getattr(bucket_loc, "scope", "") or "") if bucket_loc else ""
    bucket_counties = _clean_list_any(getattr(bucket_loc, "counties", []) if bucket_loc else [])

    global_scope = _scope_norm(getattr(global_loc, "scope", "") or "") if global_loc else ""
    global_counties = _clean_list_any(getattr(global_loc, "counties", []) if global_loc else [])
    global_cities = _clean_list_any(getattr(global_loc, "cities", []) if global_loc else [])

    scope = bucket_scope or global_scope or ""

    # effective counties: prefer bucket counties if provided, else global counties
    eff_counties = bucket_counties if bucket_counties else global_counties
    eff_cities = global_cities  # only global has cities in your current schema

    # 1) cities list => strongest filter
    if eff_cities:
        return _city_list_match(listing_city, eff_cities)

    # 2) counties list => next best
    if eff_counties:
        return _county_list_match(listing_county, eff_counties)

    # 3) scope preset fallback
    if scope == "all_florida":
        return True
    if scope == "south_florida":
        return _is_south_florida_listing(listing_ci, listing_city, listing_county)

    # 4) legacy strict fallback (older docs)
    b_city = (global_loc.city if global_loc else "") or ""
    b_county = (global_loc.county if global_loc else "") or ""
    return stage1_location_match(listing_city, listing_county, b_city, b_county)


def buyer_location_match_v4(
    listing_city: str,
    listing_county: str,
    listing_ci: Dict[str, Any],
    b: WebFormBuyerSubmission,
    bucket: str
) -> bool:
    """
    Bucket-first location matching (NEW schema):
    - Use bucket.location.scope/counties/cities if present.
    - If scope == cities => listing city must match one of bucket cities.
    - If scope == counties => listing county must match one of bucket counties.
    - If scope == all_florida => pass.
    - If scope == south_florida => tri-county check.
    - Fallback to legacy global location (older submissions).
    """

    bucket_doc = getattr(b, bucket, None)
    bucket_loc = getattr(bucket_doc, "location", None) if bucket_doc else None

    # legacy global location (older docs)
    global_loc = getattr(b, "location", None)

    # Prefer bucket scope/counties/cities
    scope = _scope_norm(getattr(bucket_loc, "scope", "") or "") if bucket_loc else ""
    counties = _clean_list_any(getattr(bucket_loc, "counties", []) if bucket_loc else [])
    cities = _clean_list_any(getattr(bucket_loc, "cities", []) if bucket_loc else [])

    # If bucket has nothing (older docs), fallback to global arrays if they exist
    if not scope and not counties and not cities and global_loc:
        scope = _scope_norm(getattr(global_loc, "scope", "") or "")
        counties = _clean_list_any(getattr(global_loc, "counties", []) or [])
        cities = _clean_list_any(getattr(global_loc, "cities", []) or [])

    # 1) scope=cities => must match one of cities (fail closed if empty)
    if scope == "cities":
        if not cities:
            return False
        return _city_list_match(listing_city, cities)

    # 2) scope=counties => must match one of counties (fail closed if empty)
    if scope == "counties":
        if not counties:
            return False
        return _county_list_match(listing_county, counties)

    # 3) scope presets
    if scope == "all_florida":
        return True

    if scope == "south_florida":
        return _is_south_florida_listing(listing_ci, listing_city, listing_county)

    # 4) If scope is empty but lists exist, treat lists as intent
    if cities:
        return _city_list_match(listing_city, cities)
    if counties:
        return _county_list_match(listing_county, counties)

    # 5) Legacy strict fallback (very old docs with location.city/location.county)
    b_city = (getattr(global_loc, "city", "") if global_loc else "") or ""
    b_county = (getattr(global_loc, "county", "") if global_loc else "") or ""
    return stage1_location_match(listing_city, listing_county, b_city, b_county)


# -----------------------------
# NEW: AI type matching
# -----------------------------
def _is_generic_type_label(label: str) -> bool:
    s = _norm_text(label)
    if not s:
        return False
    # broad options that should not restrict matching
    broad_hints = [
        "in general", "any location", "any locations", "all locations",
        "yes i am interested", "all florida", "all of florida"
    ]
    return any(h in s for h in broad_hints)

def type_match_deterministic(
    listing_ci: Dict[str, Any],
    property_payload: Dict[str, Any],
    bucket: str,
    selected_types: List[str],
    other_type: str
) -> Tuple[Optional[bool], str]:
    """
    Returns (decision, reason):
    - decision True/False => deterministic
    - decision None => need AI
    """
    types = [t.strip() for t in (selected_types or []) if (t or "").strip()]
    if not types:
        return (True, "no types selected (legacy)")

    if any(_is_generic_type_label(t) for t in types):
        return (True, "generic type selection")

    # quick deterministic signals (when types clearly imply a feature)
    evidence = (property_payload.get("evidence_text_excerpt") or "").lower()
    tags = [str(x).lower() for x in (listing_ci.get("marketing_tags") or [])]

    for t in types:
        tl = t.lower()

        # "Other" => must use other_type text; deterministic only if other_type clearly appears
        if "other" == _norm_text(t) or tl.strip() == "other":
            if other_type and other_type.lower() in evidence:
                return (True, "other_type matched in evidence")
            return (None, "other needs AI or missing evidence")

        # Beach / waterfront style filters
        if ("beach" in tl) or ("waterfront" in tl) or ("ocean" in tl):
            is_on_water = bool(listing_ci.get("is_on_water") is True)
            wf = str(listing_ci.get("water_feature") or "").lower()
            if is_on_water or (wf and wf != "none") or ("beach" in evidence) or any("beach" in x for x in tags):
                return (True, "beach/waterfront implied and supported")
            return (None, "beach/waterfront needs AI")

        # Tear-down / redevelopment
        if ("tear" in tl and "down" in tl) or ("teardown" in tl) or ("redevelop" in tl):
            if listing_ci.get("is_teardown_or_redevelopment") is True:
                return (True, "teardown flag true")
            if "tear down" in evidence or "teardown" in evidence or "redevelop" in evidence:
                return (True, "teardown implied in evidence")
            return (None, "teardown needs AI")

        # Units / duplex / triplex / fourplex -> usually needs AI (data may be in text)
        if any(k in tl for k in ["duplex", "triplex", "fourplex", "units", "unit"]):
            return (None, "unit-count style subtype needs AI")

    # default: subtype selected but no deterministic mapping => AI required
    return (None, "subtype selected; needs AI")

def call_ai_type_matcher(property_payload: Dict[str, Any], candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    AI decides whether listing matches ANY of buyer selected_types[] for that bucket.
    Returns JSON:
    { "evaluations": [ { "buyer_mongo_id": "...", "type_match": true/false, "confidence_0_to_1": 0.0, "evidence": "..." } ] }
    """
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY missing in environment")

    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY)

    system = (
        "You are a strict real estate subtype matcher.\n"
        "For each candidate buyer, decide if the property matches ANY of their selected subtype labels.\n\n"
        "Rules:\n"
        "- Use semantic matching (synonyms/paraphrases). Do not require exact strings.\n"
        "- If a selected type is broad/generic (e.g. 'in general', 'any location', 'yes i am interested'), treat as MATCH.\n"
        "- If a selected type is 'Other', use candidate.other_type as the intended subtype.\n"
        "- If unsure, return type_match=false with low confidence.\n\n"
        "Return ONLY JSON:\n"
        "{\n"
        '  "evaluations": [\n'
        "    {\n"
        '      "buyer_mongo_id": "string",\n'
        '      "type_match": true,\n'
        '      "confidence_0_to_1": 0.0,\n'
        '      "evidence": "short reason"\n'
        "    }\n"
        "  ]\n"
        "}\n"
    )

    user = {"property": property_payload, "candidates": candidates}

    resp = client.chat.completions.create(
        model=MATCHER_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
        ],
        temperature=0,
    )

    return _extract_json_obj(resp.choices[0].message.content)


def _county_list_match(listing_county: str, buyer_counties: List[str]) -> bool:
    """
    Python-side robust county match: compare normalized token overlap.
    """
    lc = _norm_text(listing_county)
    if not lc:
        return False
    lc_tokens = set([t for t in re.split(r"[\s\-]+", lc) if t and len(t) >= 3])

    for bc in (buyer_counties or []):
        bc_norm = _norm_text(bc)
        bc_tokens = set([t for t in re.split(r"[\s\-]+", bc_norm) if t and len(t) >= 3])
        if lc_tokens & bc_tokens:
            return True
    return False


def _scope_norm(s: str) -> str:
    return _norm_text(s).replace(" ", "_")


def buyer_location_match_v2(listing_city: str, listing_county: str, b: WebFormBuyerSubmission, bucket: str) -> bool:
    """
    NEW logic:
    - If buyer bucket location.scope = all_florida => match
    - If south_florida => listing county must be within buyer counties
    - Else fallback to legacy top-level location matching (county+city strict)
    """
    bucket_doc = getattr(b, bucket, None)
    if bucket_doc and getattr(bucket_doc, "location", None):
        loc = bucket_doc.location
        scope = _scope_norm(getattr(loc, "scope", "") or "")
        if scope == "all_florida":
            return True
        if scope == "south_florida":
            return _county_list_match(listing_county, list(getattr(loc, "counties", []) or []))
        # if scope is set but unknown, fail closed
        if scope:
            return False

    # legacy fallback (old submissions)
    b_city = (b.location.city if b.location else "") or ""
    b_county = (b.location.county if b.location else "") or ""
    return stage1_location_match(listing_city, listing_county, b_city, b_county)


def parse_price_range_extended(label: str) -> Tuple[Optional[float], Optional[float]]:
    """
    Extends parse_price_range() to support labels like:
    '$1 Million Dollar Houses and Up'
    """
    s = (label or "").strip()
    if not s:
        return (None, None)

    s_clean = s.replace(",", "").strip()
    s_low = s_clean.lower()

    if "all price" in s_low:
        return (None, None)

    # handle "1 million ... up"
    if "million" in s_low:
        m = re.search(r"(\d+(?:\.\d+)?)\s*million", s_low)
        if m:
            base = float(m.group(1)) * 1_000_000.0
            # treat as open ended
            return (base, None)

    # fallback to your existing numeric parser ($300000 - $600000, $1000000+ etc.)
    return parse_price_range(s_clean)


def price_match_v2(listing_price_usd: Any, selected_ranges: List[str]) -> bool:
    """
    Buyer ranges are multi-select.
    - empty => PASS
    - includes 'All price ranges' => PASS
    - else listing price must be known and fall into at least one selected range
    """
    ranges = [str(x).strip() for x in (selected_ranges or []) if str(x or "").strip()]
    if not ranges:
        return True
    if any("all price" in r.lower() for r in ranges):
        return True

    p = _safe_float(listing_price_usd)
    if p is None:
        return False

    for r in ranges:
        mn, mx = parse_price_range_extended(r)
        if mn is None and mx is None:
            return True
        if mn is not None and mx is None and p >= mn:
            return True
        if mn is not None and mx is not None and mn <= p <= mx:
            return True
        if mn is None and mx is not None and p <= mx:
            return True
    return False


def _multi_min_match(value: Any, selections: List[str]) -> bool:
    """
    selections like: ['Any', '1+', '2+', '3+'] or ['2'].
    Multi-select interpreted as OR: if any selection matches, pass.
    """
    sel = [str(x).strip() for x in (selections or []) if str(x or "").strip()]
    if not sel:
        return True
    if any(s.lower() == "any" for s in sel):
        return True

    v = _safe_float(value)
    if v is None:
        return False

    for s in sel:
        s_low = s.lower().strip()
        m = re.match(r"^(\d+(?:\.\d+)?)\s*\+\s*$", s_low)
        if m:
            if v >= float(m.group(1)):
                return True
            continue
        # exact numeric
        m2 = re.match(r"^(\d+(?:\.\d+)?)$", s_low)
        if m2:
            if v == float(m2.group(1)):
                return True
            continue

    return False


def normalize_preferences_kv(bucket_doc: Any) -> List[Dict[str, str]]:
    """
    Prefer preferences_kv (original labels preserved).
    Fallback: convert dict if needed.
    Returns list of {label, value}.
    """
    kv = []
    if bucket_doc:
        kv = list(getattr(bucket_doc, "preferences_kv", []) or [])
        if not kv:
            d = getattr(bucket_doc, "preferences", {}) or {}
            kv = [{"label": str(k), "value": str(v)} for k, v in d.items()]
    # keep only filled
    out = []
    for x in kv:
        lab = str((x or {}).get("label") or "").strip()
        val = str((x or {}).get("value") or "").strip()
        if lab and val:
            out.append({"label": lab, "value": val})
    return out


# def build_property_evidence_text(listing_ci: Dict[str, Any], listing: Any, manual_prefs_norm: List[str]) -> str:
#     """
#     Build a strong evidence text block for AI to semantically match preferences.
#     Keeps it bounded to avoid prompt bloat.
#     """
#     parts: List[str] = []

#     def add(x: Any, prefix: str = ""):
#         if x is None:
#             return
#         s = str(x).strip()
#         if not s:
#             return
#         if prefix:
#             parts.append(f"{prefix}{s}")
#         else:
#             parts.append(s)

#     # common description keys (your source data may vary)
#     for k in [
#         "raw_description_excerpt",
#         "raw_description",
#         "description",
#         "public_remarks",
#         "remarks",
#         "listing_description",
#         "property_description",
#         "agent_remarks",
#         "private_remarks",
#     ]:
#         if isinstance(listing_ci, dict) and listing_ci.get(k):
#             add(listing_ci.get(k), prefix=f"{k}: ")

#     # also check top-level listing fields (safe, optional)
#     add(getattr(listing, "raw_description", None), prefix="listing.raw_description: ")
#     add(getattr(listing, "description", None), prefix="listing.description: ")

#     # marketing tags
#     tags = listing_ci.get("marketing_tags") or []
#     if isinstance(tags, list) and tags:
#         tag_str = ", ".join([str(t).strip() for t in tags if str(t).strip()])
#         if tag_str.strip():
#             add(tag_str, prefix="Marketing tags: ")

#     # manual special prefs (raw + normalized)
#     raw_manual = (getattr(listing, "manual_special_preferences_raw", None) or "").strip()
#     if raw_manual:
#         add(raw_manual, prefix="Manual special preferences (raw): ")

#     if manual_prefs_norm:
#         add(", ".join(manual_prefs_norm[:100]), prefix="Manual special preferences (normalized): ")

#     # a compact structured highlight block
#     highlight_keys = [
#         "property_type", "water_feature", "is_on_water", "is_condo", "is_land_only",
#         "build_material", "is_frame_or_wood", "year_built",
#     ]
#     highlights = {}
#     for k in highlight_keys:
#         v = listing_ci.get(k)
#         if v is not None and str(v).strip() != "":
#             highlights[k] = v
#     if highlights:
#         add(json.dumps(highlights, ensure_ascii=False), prefix="Structured highlights: ")

#     text = "\n".join(parts).strip()
#     # keep prompt stable
#     return text[:3500]


def build_property_evidence_text(listing_ci: Dict[str, Any], listing: Any, manual_prefs_norm: List[str]) -> str:
    """
    Build a strong evidence text block for AI to semantically match preferences.
    Keeps it bounded to avoid prompt bloat.
    """
    parts: List[str] = []

    def add(x: Any, prefix: str = ""):
        if x is None:
            return
        s = str(x).strip()
        if not s:
            return
        if prefix:
            parts.append(f"{prefix}{s}")
        else:
            parts.append(s)

    # ✅ NEW: include listing.complete_info.complete_info (full raw listing blob text)
    # This is ONLY used for AI evidence (special preference classifier + subtype AI).
    try:
        blob = None
        outer_ci = getattr(listing, "complete_info", None)

        if isinstance(outer_ci, dict):
            blob = outer_ci.get("complete_info")
            # sometimes complete_info itself can be nested dict that again contains "complete_info"
            if isinstance(blob, dict):
                blob = blob.get("complete_info")

        blob_s = str(blob).strip() if blob else ""

        # avoid duplicating if listing_ci already contains the same complete_info string
        existing = ""
        if isinstance(listing_ci, dict):
            existing = str(listing_ci.get("complete_info") or "").strip()

        if blob_s and blob_s != existing:
            add(blob_s, prefix="Complete info (raw full text): ")
        elif blob_s:
            # even if same, still include once via listing_ci path below (keeps behavior stable)
            pass
    except Exception:
        pass

    # common description keys (your source data may vary)
    # ✅ NOTE: we keep your existing behavior intact
    for k in [
        # OPTIONAL: if listing_ci carries the same raw blob, it will be included here too
        # (safe and does not affect non-AI logic)
        "complete_info",
        "raw_description_excerpt",
        "raw_description",
        "description",
        "public_remarks",
        "remarks",
        "listing_description",
        "property_description",
        "agent_remarks",
        "private_remarks",
      
    ]:
        if isinstance(listing_ci, dict) and listing_ci.get(k):
            add(listing_ci.get(k), prefix=f"{k}: ")

    # also check top-level listing fields (safe, optional)
    add(getattr(listing, "raw_description", None), prefix="listing.raw_description: ")
    add(getattr(listing, "description", None), prefix="listing.description: ")

    # marketing tags
    tags = listing_ci.get("marketing_tags") or []
    if isinstance(tags, list) and tags:
        tag_str = ", ".join([str(t).strip() for t in tags if str(t).strip()])
        if tag_str.strip():
            add(tag_str, prefix="Marketing tags: ")

    # manual special prefs (raw + normalized)
    raw_manual = (getattr(listing, "manual_special_preferences_raw", None) or "").strip()
    if raw_manual:
        add(raw_manual, prefix="Manual special preferences (raw): ")

    if manual_prefs_norm:
        add(", ".join(manual_prefs_norm[:100]), prefix="Manual special preferences (normalized): ")

    # a compact structured highlight block
    highlight_keys = [
        "property_type", "water_feature", "is_on_water", "is_condo", "is_land_only",
        "build_material", "is_frame_or_wood", "year_built",
    ]
    highlights = {}
    for k in highlight_keys:
        v = listing_ci.get(k)
        if v is not None and str(v).strip() != "":
            highlights[k] = v
    if highlights:
        add(json.dumps(highlights, ensure_ascii=False), prefix="Structured highlights: ")

    text = "\n".join(parts).strip()
    # keep prompt stable
    return text[:3500]


def _find_best_ai_check_for_label(expected_label: str, ai_by_label: Dict[str, Dict[str, Any]], threshold: float = 0.88) -> Optional[Dict[str, Any]]:
    """
    If AI returns label slightly different, pick the closest normalized label.
    """
    target = _norm_label(expected_label)
    if not target:
        return None
    if target in ai_by_label:
        return ai_by_label[target]

    best_key = None
    best_ratio = 0.0
    for k in ai_by_label.keys():
        r = difflib.SequenceMatcher(None, target, k).ratio()
        if r > best_ratio:
            best_ratio = r
            best_key = k

    if best_key and best_ratio >= threshold:
        return ai_by_label[best_key]
    return None

def apply_special_preference_rules(pref_checks: List[Dict[str, Any]]) -> Tuple[bool, float, List[str], List[str]]:
    """
    Deterministic evaluation using AI-produced status.
    Implements your rules:

    - No: if feature/concept is PRESENT => mismatch. Otherwise pass (ABSENT/UNKNOWN pass).
    - Yes: must be PRESENT.
    - Maybe: never blocks.
    - Only:
        * If exactly one Only => it must be PRESENT.
        * If multiple Only => at least ONE of the Only preferences must be PRESENT.
      (Still respecting any Yes/No rules.)
    Returns: (match, confidence, reasons, failed_checks)
    """
    checks = pref_checks or []

    def sel(c) -> str:
        return str(c.get("selection") or "").strip().lower()

    def status(c) -> str:
        return str(c.get("status") or "").strip().upper()

    def conf(c) -> float:
        try:
            v = float(c.get("confidence_0_to_1") or 0.0)
            # clamp
            if v < 0: v = 0.0
            if v > 1: v = 1.0
            return v
        except Exception:
            return 0.0

    no_checks = [c for c in checks if sel(c) == "no"]
    yes_checks = [c for c in checks if sel(c) == "yes"]
    maybe_checks = [c for c in checks if sel(c) == "maybe"]
    only_checks = [c for c in checks if sel(c) == "only"]

    reasons: List[str] = []
    failed: List[str] = []

    # confidence aggregation: keep it conservative only when something is positively confirmed PRESENT
    overall_conf = 1.0

    # NO: only fails if PRESENT
    for c in no_checks:
        st = status(c)
        if st == "PRESENT":
            failed.append(f"No selected but feature present: {c.get('label')}")
        # ABSENT/UNKNOWN => pass (do not block)

    # YES: must be PRESENT
    for c in yes_checks:
        st = status(c)
        if st != "PRESENT":
            failed.append(f"Yes selected but not present/unknown: {c.get('label')}")
        else:
            overall_conf = min(overall_conf, conf(c) or 1.0)

    # ONLY logic
    if len(only_checks) == 1:
        c = only_checks[0]
        st = status(c)
        if st != "PRESENT":
            failed.append(f"Only selected (single) but not present/unknown: {c.get('label')}")
        else:
            overall_conf = min(overall_conf, conf(c) or 1.0)

    elif len(only_checks) > 1:
        present_only = [c for c in only_checks if status(c) == "PRESENT"]
        if not present_only:
            failed.append("Multiple 'Only' selected but none are present")
        else:
            # at least one is present: confidence driven by best present-only evidence
            best_present = max(conf(c) for c in present_only)
            overall_conf = min(overall_conf, best_present if best_present > 0 else 1.0)

    # MAYBE: never blocks (ignored completely)
    _ = maybe_checks  # intentionally unused

    if failed:
        return (False, float(overall_conf), reasons, failed)

    reasons.append("Special preferences passed")
    return (True, float(overall_conf), reasons, failed)


# -------------------------------------------------------------------
# Phase 1 enhancement END
# -------------------------------------------------------------------


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





# -------------------------------------------------------------------
# Phase 1 enhancement START
# -------------------------------------------------------------------


def call_ai_matcher(property_payload: Dict[str, Any], candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    AI ONLY classifies special preference labels as PRESENT/ABSENT/UNKNOWN.
    All other matching rules are enforced deterministically in backend.
    """
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY missing in environment")

    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)

        system = (
    "You are a strict real estate preference classifier.\n"
    "For each buyer, evaluate EACH preference label against the property's evidence.\n\n"
    "IMPORTANT:\n"
    "- The preference label may NOT match the listing text exactly. Use semantic matching (synonyms/paraphrases).\n"
    "  Examples: 'pool' ~ 'swimming pool'; 'waterfront' ~ 'on canal'/'intracoastal'; 'no hoa' ~ 'no homeowners association'.\n"
    "- Treat the LABEL as the concept/claim to check. If the label is negated (e.g., 'No HOA'), then PRESENT means evidence supports 'No HOA'.\n"
    "- Use ONLY the provided structured fields and evidence_text_excerpt (including manual special preferences). Do NOT guess.\n\n"
    "Definitions:\n"
    "- PRESENT: clearly supported by evidence text/fields (including strong paraphrase)\n"
    "- ABSENT: clearly contradicted by evidence (explicitly says the opposite)\n"
    "- UNKNOWN: not enough evidence either way\n\n"
    "Output requirements:\n"
    "- For every candidate buyer, you MUST return a preference_checks entry for every preferences_kv label.\n"
    "- Copy the preference label EXACTLY as provided in the candidate.\n"
    "- Return ONLY JSON in this schema:\n"
    "{\n"
    '  \"evaluations\": [\n'
    "    {\n"
    '      \"buyer_mongo_id\": \"string\",\n'
    '      \"preference_checks\": [\n'
    "        {\n"
    '          \"label\": \"string\",\n'
    '          \"selection\": \"No|Yes|Maybe|Only\",\n'
    '          \"status\": \"PRESENT|ABSENT|UNKNOWN\",\n'
    '          \"confidence_0_to_1\": 0.0,\n'
    '          \"evidence\": \"short text\"\n'
    "        }\n"
    "      ]\n"
    "    }\n"
    "  ]\n"
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


# -------------------------------------------------------------------
# Phase 1 enhancement END
# -------------------------------------------------------------------


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

            needs_rerun_now = False
            try:
                curr2 = ParsedListing.objects(id=l.id).only(
                    "manual_special_preferences_rematch_at",
                    "buyer_matching_last_attempt_at"
                ).first()

                if curr2 and curr2.manual_special_preferences_rematch_at and curr2.buyer_matching_last_attempt_at:
                    if curr2.manual_special_preferences_rematch_at > curr2.buyer_matching_last_attempt_at:
                        needs_rerun_now = True
            except Exception:
                pass

            # IMPORTANT: if buyers matched but podio update failed, treat as retryable error
            if (
                isinstance(result, dict)
                and int(result.get("matched_buyers_count") or 0) > 0
                and result.get("podio_updated") is not True
            ):
                raise RuntimeError("Podio update failed while buyers matched; will retry")


            # Success: mark matched (even if zero buyers matched — job completed cleanly)
            if needs_rerun_now or (isinstance(result, dict) and result.get("needs_rerun") is True):
                ParsedListing.objects(id=l.id).update_one(
                    set__buyer_matching_status="pending",
                    set__buyer_matching_consecutive_errors=0,
                    set__buyer_matching_last_error_sig=None,
                    set__buyer_matching_last_error=None,
                    unset__buyer_send_status=1,  # ensure sending restarts cleanly
                )
            else:
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

    # ✅ derive property_type bucket from listing.complete_info.property_type
    complete_info = listing.complete_info or {}
    listing_ci = complete_info.get("complete_info") if isinstance(complete_info.get("complete_info"), dict) else complete_info
    bucket = get_listing_property_bucket(listing_ci)

        # ✅ SKIP: do not enqueue matching for land or commercial
    if bucket in ("land", "commercial"):
        # optional: store podio item id + mark as skipped so cron never touches it
        ParsedListing.objects(id=listing_oid).update_one(
            set__buyer_matching_status="skipped",
            set__buyer_matching_podio_item_id=int(payload.podio_property_item_id),
            set__updated_at=datetime.utcnow()
        )

        return {
            "ok": True,
            "queued": False,
            "reason": f"skipped_{bucket}",
            "bucket": bucket,
            "mongodb_object_id": payload.mongodb_object_id,
            "buyer_matching_status": "skipped",
            "podio_property_item_id": int(payload.podio_property_item_id),
        }

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


class ManualSpecialPrefsPayload(BaseModel):
    mongodb_object_id: str
    podio_property_item_id: Optional[int] = None

    # If Globiflow can pass the field value, send it here (best).
    special_preferences_text: Optional[str] = None

    # If True, rerun on ANY meaningful change (add/remove), not just additions.
    # Default matches your requirement: only rerun on new additions.
    rematch_on_any_change: bool = False

    dry_run: bool = False


@router.post("/manual-special-preferences")
def manual_special_preferences(payload: ManualSpecialPrefsPayload):
    if not payload.special_preferences_text and not payload.podio_property_item_id:
        raise HTTPException(status_code=400, detail="Provide special_preferences_text or podio_property_item_id")

    # 1) Validate listing ObjectId
    try:
        listing_oid = ObjectId(payload.mongodb_object_id.strip())
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid mongodb_object_id (not a valid ObjectId)")

    listing: Optional[ParsedListing] = ParsedListing.objects(id=listing_oid).first()
    if not listing:
        raise HTTPException(status_code=404, detail="Listing not found in parsed_listings for given ObjectId")

    # 2) Determine the new text
    new_text = (payload.special_preferences_text or "").strip()

    # If not provided, fetch from Podio using item id + field id
    if (not new_text) and payload.podio_property_item_id:
        if not PROPERTIES_SPECIAL_PREFERENCES_FIELD_ID:
            raise HTTPException(
                status_code=400,
                detail="PODIO_PROPERTIES_SPECIAL_PREFERENCES_FIELD_ID not set in env, cannot fetch from Podio"
            )
        item = podio_get_item(int(payload.podio_property_item_id))
        if not item:
            raise HTTPException(status_code=502, detail="Failed to fetch Podio item")
        new_text = podio_extract_text_field(item, PROPERTIES_SPECIAL_PREFERENCES_FIELD_ID)

    # 3) Normalize + compare
    old_norm = list(getattr(listing, "manual_special_preferences_norm", []) or [])
    # if old list empty but raw exists, normalize raw to ensure stable behavior
    if not old_norm and (getattr(listing, "manual_special_preferences_raw", None) or ""):
        old_norm = normalize_manual_prefs_text(getattr(listing, "manual_special_preferences_raw", "") or "")

    new_text = podio_richtext_to_plain(new_text)
    new_norm = normalize_manual_prefs_text(new_text)

    old_set = set(old_norm or [])
    new_set = set(new_norm or [])

    added = sorted(list(new_set - old_set))
    removed = sorted(list(old_set - new_set))

 

    any_meaningful_change = bool(added or removed)
    # NO-OP change (spaces/case/reorder only) => do nothing
    if not any_meaningful_change:
        return {
            "ok": True,
            "mongodb_object_id": payload.mongodb_object_id,
            "podio_property_item_id": payload.podio_property_item_id,
            "added": [],
            "removed": [],
            "should_rematch": False,
            "note": "No meaningful preference change (normalized). No DB update performed."
        }
    
    added_only_change = bool(added)

    should_rematch = (any_meaningful_change if payload.rematch_on_any_change else added_only_change)

    now = datetime.utcnow()

    update_fields = {
        "set__manual_special_preferences_raw": new_text,
        "set__manual_special_preferences_norm": new_norm,
        "set__manual_special_preferences_saved_at": now,   # NEW: always update
        "set__updated_at": now,
    }
    # keep podio item id in listing (optional but useful)
    if payload.podio_property_item_id:
        update_fields["set__buyer_matching_podio_item_id"] = int(payload.podio_property_item_id)

    # Always clear send status if we’re going to rematch (per requirement)
    if should_rematch:
        update_fields["set__manual_special_preferences_rematch_at"] = now  # NEW: only when should_rematch
        update_fields["unset__buyer_send_status"] = 1

        # ✅ NEW: mark rematch + reset new-id storage for upcoming run
        update_fields["set__rematch"] = True
        update_fields["set__re_matched_buyer_ids"] = []

        if (listing.buyer_matching_status or "") != "processing":
            update_fields["set__buyer_matching_status"] = "pending"

    if payload.dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "mongodb_object_id": payload.mongodb_object_id,
            "podio_property_item_id": payload.podio_property_item_id,
            "old_norm": old_norm,
            "new_norm": new_norm,
            "added": added,
            "removed": removed,
            "should_rematch": should_rematch,
            "note": "No DB updates performed (dry_run)",
        }

    ParsedListing.objects(id=listing_oid).update_one(**update_fields)

    return {
        "ok": True,
        "mongodb_object_id": payload.mongodb_object_id,
        "podio_property_item_id": payload.podio_property_item_id,
        "added": added,
        "removed": removed,
        "should_rematch": should_rematch,
        "buyer_matching_status_after": ("pending" if should_rematch and (listing.buyer_matching_status or "") != "processing" else (listing.buyer_matching_status or "none")),
        "buyer_send_status_cleared": bool(should_rematch),
    }

def _norm_label(s: str) -> str:
    s = (s or "").strip().lower()
    s = s.replace("__dollar__", "$")
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[^\w\s$/\-\+\.]", "", s)  # keep $, /, -, +, .
    return s



def manual_pref_present(label: str, manual_prefs_norm: List[str]) -> bool:
    """
    If a label matches (or very closely matches) one of the manual prefs, treat it as PRESENT.
    Manual prefs are user-supplied on the listing, so they override AI ambiguity.
    """
    if not label:
        return False

    manual_set = {_norm_label(x) for x in (manual_prefs_norm or []) if str(x or "").strip()}
    if not manual_set:
        return False

    ln = _norm_label(label)
    if not ln:
        return False

    # Exact normalized match
    if ln in manual_set:
        return True

    # High-precision token subset match (keeps false positives low)
    ltoks = {t for t in re.split(r"[\s/\-]+", ln) if len(t) >= 4}
    if ltoks:
        for m in manual_set:
            mtoks = {t for t in re.split(r"[\s/\-]+", m) if len(t) >= 4}
            if ltoks.issubset(mtoks):
                return True

    # Very high fuzzy match (only as last resort)
    for m in manual_set:
        if difflib.SequenceMatcher(None, ln, m).ratio() >= 0.93:
            return True

    return False

def type_match_deterministic(
    *,
    listing_ci: Dict[str, Any],
    property_payload: Dict[str, Any],
    bucket: str,
    selected_types: List[str],
    other_type: str,
) -> Tuple[Optional[bool], str]:
    """
    Returns:
      (True, reason)  => type/subtype matched deterministically
      (False, reason) => mismatch deterministically
      (None, reason)  => not confident; requires AI subtype check
    Notes:
    - Multi-select selected_types is treated as OR: listing matches if it fits ANY selected type.
    - Backward safe: if buyer has no types (legacy), do NOT block.
    """

    # Legacy/no selection: do not block old data
    norm_types = [_norm_text(t) for t in (selected_types or []) if str(t or "").strip()]
    if not norm_types:
        return True, "No subtype selected (legacy) => do not block"

    # "Other" usually needs AI because it depends on free-text other_type
    if any(t == "other" for t in norm_types):
        if other_type.strip():
            return None, "Selected 'Other' with other_type => requires AI"
        # If 'Other' selected but no other_type provided, we can’t validate => AI
        return None, "Selected 'Other' without other_type => requires AI"

    # If selection contains "in general" / "any location" / "any" etc => pass
    if any(("in general" in t) or ("any location" in t) or (t == "any") for t in norm_types):
        return True, "General subtype selected"

    # Some deterministic signals
    is_on_water = bool(listing_ci.get("is_on_water") is True)
    water_feature = _norm_text(str(listing_ci.get("water_feature") or ""))
    tags = " ".join([_norm_text(str(x)) for x in (listing_ci.get("marketing_tags") or []) if str(x).strip()])
    excerpt = _norm_text(str(listing_ci.get("raw_description_excerpt") or ""))

    # Condo subtype: beachfront
    # (If buyer selected beachfront-only and listing clearly not on/near water => mismatch for that subtype)
    def matches_condo_subtype(t: str) -> Optional[bool]:
        if "beach" in t or "beachfront" in t or "beach front" in t:
            # definite present signals
            if is_on_water or ("ocean" in water_feature) or ("intracoastal" in water_feature) or ("beach" in tags) or ("ocean" in tags):
                return True
            # definite absent signals
            if (listing_ci.get("is_on_water") is False) and (water_feature in ("none", "", "unknown")):
                return False
            return None
        return None

    # Land subtype: teardown
    def matches_land_subtype(t: str) -> Optional[bool]:
        if "tear" in t:
            if listing_ci.get("is_teardown_or_redevelopment") is True:
                return True
            if "tear down" in excerpt or "teardown" in excerpt or "tear down" in tags:
                return True
            # if explicitly says not teardown? usually not present in data => None
            return None
        return None

    # Multi-family subtype: duplex/triplex/fourplex vs 5-25 units (usually needs AI)
    def matches_multifamily_subtype(t: str) -> Optional[bool]:
        if "duplex" in t or "triplex" in t or "fourplex" in t:
            if "duplex" in excerpt or "triplex" in excerpt or "fourplex" in excerpt:
                return True
            return None
        if "5" in t and "unit" in t:
            # if listing mentions units count
            if re.search(r"\b(\d+)\s*units?\b", excerpt):
                m = re.search(r"\b(\d+)\s*units?\b", excerpt)
                if m:
                    try:
                        n = int(m.group(1))
                        return True if 5 <= n <= 25 else False
                    except Exception:
                        return None
            return None
        return None

    # Evaluate OR across selected types: if ANY is True => match
    any_unknown = False
    for t in norm_types:
        verdict: Optional[bool] = None

        if bucket == "condo":
            verdict = matches_condo_subtype(t)
        elif bucket == "land":
            verdict = matches_land_subtype(t)
        elif bucket == "multi_family":
            verdict = matches_multifamily_subtype(t)

        if verdict is True:
            return True, f"Subtype matched deterministically: {t}"
        if verdict is None:
            any_unknown = True

    # If we can deterministically say none match and no unknowns => mismatch
    if not any_unknown:
        return False, "No selected subtype matched deterministically"

    # Otherwise require AI
    return None, "Subtype uncertain => requires AI"


def call_ai_type_matcher(property_payload: Dict[str, Any], candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    AI only decides: does this listing match ANY of buyer.selected_types (plus other_type if provided)?
    Output schema:
    {
      "evaluations": [
        {"buyer_mongo_id":"...", "type_match": true/false, "confidence_0_to_1": 0.0-1.0, "evidence":"..."}
      ]
    }
    """
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY missing in environment")

    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY)

    system = (
        "You are a strict real estate property subtype matcher.\n"
        "Given ONE property (listing) and multiple buyer candidates:\n"
        "- Each candidate has selected_types (array). Treat it as OR: match if the listing fits ANY selected type.\n"
        "- If candidate includes other_type text, use it to interpret 'Other'.\n"
        "- Use semantic matching (synonyms/paraphrases). Do NOT guess.\n"
        "- Output ONLY JSON in this schema:\n"
        "{\n"
        '  "evaluations": [\n'
        '    {"buyer_mongo_id":"string","type_match":true,"confidence_0_to_1":0.0,"evidence":"short"}\n'
        "  ]\n"
        "}\n"
    )

    user = {"property": property_payload, "candidates": candidates}

    resp = client.chat.completions.create(
        model=MATCHER_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
        ],
        temperature=MATCHER_TEMPERATURE,
    )

    return _extract_json_obj(resp.choices[0].message.content)


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
    
    # ✅ capture rematch state + previously matched ids (must be before recompute)
    is_rematch = bool(getattr(listing, "rematch", False))
    prev_matched_buyer_ids = list(getattr(listing, "matched_buyer_ids", []) or [])

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


    

# -------------------------------------------------------------------
# Phase 1 enhancement START
# -------------------------------------------------------------------


   # 3) Stage 1: NO AI filter (UPDATED for global location arrays)
    # q_enabled = Q(**{f"{bucket}__enabled": True})

    # # bucket scope all_florida OR global scope all_florida
    # q_new_all = Q(**{f"{bucket}__location__scope": "all_florida"}) | Q(location__scope="all_florida")

    # county_re = build_loose_regex_from_text(listing_county)
    # city_re = build_loose_regex_from_text(listing_city)
    # q_legacy = Q(location__county__iregex=county_re, location__city__iregex=city_re)

    # # cheap mongo prefilter for counties-in (bucket OR global)
    # county_vars = _county_variants(listing_county)

    # q_new_sf = (
    #     Q(**{f"{bucket}__location__scope": "south_florida", f"{bucket}__location__counties__in": county_vars})
    #     | Q(location__counties__in=county_vars)
    # )

    # # optional city __in prefilter (case sensitive in mongo, but helps sometimes)
    # city_vars = _case_variants(listing_city)
    # q_city_in = Q(location__cities__in=city_vars)

    # buyers_qs = WebFormBuyerSubmission.objects(
    #     q_enabled & (q_new_all | q_new_sf | q_city_in | q_legacy)
    # ).only("id", "contact", "location", bucket, "podio_item_id")


    q_enabled = Q(**{f"{bucket}__enabled": True})

    county_vars = _county_variants(listing_county)
    city_vars = _case_variants(listing_city)

    # legacy strict (very old docs)
    county_re = build_loose_regex_from_text(listing_county)
    city_re = build_loose_regex_from_text(listing_city)
    q_legacy = Q(location__county__iregex=county_re, location__city__iregex=city_re)

    # bucket scopes (NEW)
    q_scope_all = Q(**{f"{bucket}__location__scope": "all_florida"}) | Q(location__scope="all_florida")

    # IMPORTANT: south_florida buyers have empty counties in new schema => don't require __in
    q_scope_sf = Q(**{f"{bucket}__location__scope": "south_florida"}) | Q(location__scope="south_florida")

    q_scope_counties = (
        Q(**{f"{bucket}__location__scope": "counties", f"{bucket}__location__counties__in": county_vars})
        | Q(location__counties__in=county_vars)
    )

    q_scope_cities = (
        Q(**{f"{bucket}__location__scope": "cities", f"{bucket}__location__cities__in": city_vars})
        | Q(location__cities__in=city_vars)
    )

    buyers_qs = WebFormBuyerSubmission.objects(
        q_enabled & (q_scope_all | q_scope_sf | q_scope_counties | q_scope_cities | q_legacy)
    ).only("id", "contact", "location", bucket, "podio_item_id")


    # stage1_candidates: List[WebFormBuyerSubmission] = []
    # for b in buyers_qs:
    #     if buyer_location_match_v3(listing_city, listing_county, listing_ci, b, bucket):
    #         stage1_candidates.append(b)

    stage1_candidates: List[WebFormBuyerSubmission] = []
    for b in buyers_qs:
        if buyer_location_match_v4(listing_city, listing_county, listing_ci, b, bucket):
            stage1_candidates.append(b)


    print("stage1_candidates=====", stage1_candidates)


# -------------------------------------------------------------------
# Phase 1 enhancement END
# -------------------------------------------------------------------



    # 4) Stage 2: AI filter within stage1 group
    # Build compact property payload for the model (include evidence, but keep it bounded)
    manual_prefs = list(getattr(listing, "manual_special_preferences_norm", []) or [])

    raw_excerpt = build_property_evidence_text(
        listing_ci=listing_ci,
        listing=listing,
        manual_prefs_norm=manual_prefs,
    )

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
        "manual_special_preferences": manual_prefs,
    }

    

# -------------------------------------------------------------------
# Phase 1 enhancement START
# -------------------------------------------------------------------
    
    def buyer_bucket_obj(b: WebFormBuyerSubmission) -> Dict[str, Any]:
        bucket_doc = getattr(b, bucket, None)

        price_ranges = []
        beds_sel = []
        baths_sel = []
        scope = ""
        counties = []
        cities = []
        selected_types = []
        other_type = ""
        pref_kv = []

        if bucket_doc:
            # ✅ NEW: types is array now (fallback to legacy 'type' string)
            selected_types = list(getattr(bucket_doc, "types", []) or [])
            if (not selected_types) and (getattr(bucket_doc, "type", "") or "").strip():
                selected_types = [(getattr(bucket_doc, "type", "") or "").strip()]

            other_type = (getattr(bucket_doc, "other_type", "") or "").strip()

            price_ranges = list(getattr(bucket_doc, "price_ranges", []) or [])
            # legacy fallback
            if not price_ranges and (getattr(bucket_doc, "price_range", "") or "").strip():
                price_ranges = [(getattr(bucket_doc, "price_range", "") or "").strip()]

            beds_sel = list(getattr(bucket_doc, "beds", []) or [])
            baths_sel = list(getattr(bucket_doc, "baths", []) or [])

            loc = getattr(bucket_doc, "location", None)
            if loc:
                scope = (getattr(loc, "scope", "") or "").strip()
                counties = list(getattr(loc, "counties", []) or [])
                cities = list(getattr(loc, "cities", []) or [])

            pref_kv = normalize_preferences_kv(bucket_doc)

        return {
            "buyer_mongo_id": str(b.id),
            "contact": {
                "name": (b.contact.name if b.contact else ""),
                "email": (b.contact.email if b.contact else ""),
                "company": (b.contact.company if b.contact else ""),
            },
            "bucket": bucket,

            # ✅ NEW fields Patch 4 needs
            "selected_types": selected_types,
            "other_type": other_type,

            "price_ranges": price_ranges,
            "beds": beds_sel,
            "baths": baths_sel,
            "location": {"scope": scope, "counties": counties, "cities": cities},
            "preferences_kv": pref_kv,
        }


    ai_candidates: List[Dict[str, Any]] = []
    type_ai_pending: List[Dict[str, Any]] = []  # candidates that need AI for subtype
    matched_buyer_ids: List[str] = []
    evaluations_all: List[Dict[str, Any]] = []

    for b in stage1_candidates:
        obj = buyer_bucket_obj(b)

        # FAIL-FAST: No + manual pref present => reject
        pref_kv = obj.get("preferences_kv") or []
        hard_block = False
        for p in pref_kv:
            label = (p.get("label") or "").strip()
            selection = (p.get("value") or "").strip().lower()
            if selection == "no" and manual_pref_present(label, manual_prefs):
                hard_block = True
                break
        if hard_block:
            continue

        # 1) Price
        if not price_match_v2(listing_price, obj.get("price_ranges") or []):
            continue

        # 2) Beds/Baths (only for SF/Condo/Townhouse)
        if bucket in ("single_family", "condo", "townhouse"):
            if not _multi_min_match(listing_ci.get("bedrooms"), obj.get("beds") or []):
                continue
            if not _multi_min_match(listing_ci.get("bathrooms_full"), obj.get("baths") or []):
                continue

        # 3) ✅ Type/Subtype (NEW) — must match (OR across selected_types)
        decision, reason = type_match_deterministic(
            listing_ci=listing_ci,
            property_payload=property_payload,
            bucket=bucket,
            selected_types=obj.get("selected_types") or [],
            other_type=obj.get("other_type") or "",
        )

        if decision is False:
            continue

        if decision is None:
            type_ai_pending.append(obj)
            continue

        # If we get here: type matched deterministically
        prefs_kv = obj.get("preferences_kv") or []
        if not prefs_kv:
            matched_buyer_ids.append(obj["buyer_mongo_id"])
            evaluations_all.append({
                "buyer_mongo_id": obj["buyer_mongo_id"],
                "match": True,
                "confidence_0_to_1": 1.0,
                "reasons": [f"Type matched ({reason}); no special preferences set; deterministic rules passed"],
                "failed_checks": [],
            })
            continue

        ai_candidates.append(obj)

    print("type_ai_pending=====", type_ai_pending)
    print("ai_candidates(after deterministic type)=====", ai_candidates)

    # ✅ NEW: run AI subtype checks for those pending
    for i in range(0, len(type_ai_pending), AI_BATCH_SIZE):
        batch = type_ai_pending[i:i + AI_BATCH_SIZE]
        if not batch:
            continue

        ai_type_result = call_ai_type_matcher(property_payload, batch)
        type_evals = ai_type_result.get("evaluations") or []
        by_id = {str(x.get("buyer_mongo_id")): x for x in type_evals if isinstance(x, dict) and x.get("buyer_mongo_id")}

        for cand in batch:
            bid = cand["buyer_mongo_id"]
            ev = by_id.get(bid) or {}
            ok = bool(ev.get("type_match") is True)

            if not ok:
                continue

            prefs_kv = cand.get("preferences_kv") or []
            if not prefs_kv:
                matched_buyer_ids.append(bid)
                evaluations_all.append({
                    "buyer_mongo_id": bid,
                    "match": True,
                    "confidence_0_to_1": float(ev.get("confidence_0_to_1") or 0.8),
                    "reasons": [f"Type matched by AI: {ev.get('evidence') or 'ok'}; no special preferences set"],
                    "failed_checks": [],
                })
                continue

            ai_candidates.append(cand)

    print("ai_candidates(after AI type)=====", ai_candidates)







    # # Chunk AI calls
 
    for i in range(0, len(ai_candidates), AI_BATCH_SIZE):
        batch = ai_candidates[i:i + AI_BATCH_SIZE]
        if not batch:
            continue

        ai_result = call_ai_matcher(property_payload, batch)
        batch_evals = ai_result.get("evaluations") or []

        # Index by buyer id for safety
        eval_by_id: Dict[str, Dict[str, Any]] = {}
        for ev in batch_evals:
            if isinstance(ev, dict) and ev.get("buyer_mongo_id"):
                eval_by_id[str(ev["buyer_mongo_id"])] = ev

        for cand in batch:
            bid = cand["buyer_mongo_id"]
            ev = eval_by_id.get(bid) or {}

            expected = cand.get("preferences_kv") or []
            expected_labels = [x.get("label") for x in expected if x.get("label") and x.get("value")]

            ai_checks = ev.get("preference_checks") or []
            ai_by_label = {}
            for pc in ai_checks:
                if isinstance(pc, dict) and pc.get("label"):
                    ai_by_label[_norm_label(pc["label"])] = pc

            # Build full check list (FAIL-CLOSED) + manual override to PRESENT
            pref_checks = []
            for x in expected:
                lab = str(x.get("label") or "").strip()
                sel = str(x.get("value") or "").strip()  # Yes/No/Maybe/Only
                if not lab or not sel:
                    continue

                # ✅ B) Manual prefs override: if listing manual prefs imply this concept is PRESENT,
                # force status=PRESENT regardless of AI output.
                if manual_pref_present(lab, manual_prefs):
                    pref_checks.append({
                        "label": lab,
                        "selection": sel,
                        "status": "PRESENT",
                        "confidence_0_to_1": 1.0,
                        "evidence": "Manual special preferences override (listing indicates PRESENT)",
                    })
                    continue

                pc = _find_best_ai_check_for_label(lab, ai_by_label)

                if not pc:
                    # missing from AI => UNKNOWN (so Yes/No/Only fail)
                    pref_checks.append({
                        "label": lab,
                        "selection": sel,
                        "status": "UNKNOWN",
                        "confidence_0_to_1": 0.0,
                        "evidence": "Missing from AI response",
                    })
                else:
                    # ensure selection exists + keep output stable
                    pc = dict(pc)  # avoid mutating ai_by_label contents
                    pc["label"] = lab         # keep exact label from candidate (your system prompt asks for this)
                    pc["selection"] = sel     # ensure correct selection
                    pc.setdefault("confidence_0_to_1", 0.0)
                    pc.setdefault("evidence", "")
                    pref_checks.append(pc)




            ok, conf, reasons, failed = apply_special_preference_rules(pref_checks)
            final_match = bool(ok) and float(conf or 0) >= MIN_CONFIDENCE
            


            evaluations_all.append({
                "buyer_mongo_id": bid,
                "match": final_match,
                "confidence_0_to_1": float(conf or 0),
                "reasons": reasons,
                "failed_checks": failed,
            })

            if final_match:
                matched_buyer_ids.append(bid)

# -------------------------------------------------------------------
# Phase 1 enhancement END
# -------------------------------------------------------------------



    # de-dup
    matched_buyer_ids = sorted(list(set(matched_buyer_ids)))

    # ✅ NEW: if rematch, store only new buyer ids in re_matched_buyer_ids
    re_matched_buyer_ids: List[str] = []

    if is_rematch:
        prev_set = set(prev_matched_buyer_ids or [])
        new_set = set(matched_buyer_ids or [])

        re_matched_buyer_ids = sorted(list(new_set - prev_set))     # only NEW ids
        matched_buyer_ids = sorted(list(prev_set | new_set))        # keep ALL (old + new)

    print("matched_buyer_ids=====",matched_buyer_ids)

    # 5) Update parsed_listings with matched buyer Mongo ids

    if not payload.dry_run:
        update_fields = {
            "set__matched_buyer_ids": matched_buyer_ids,
            "set__updated_at": datetime.utcnow(),
        }

        if is_rematch:
            # ✅ store only new ids for contact step
            update_fields["set__re_matched_buyer_ids"] = re_matched_buyer_ids
            update_fields["set__rematch"] = False  # reset flag once handled

            # ✅ only trigger sending if we actually found NEW matches
            if len(re_matched_buyer_ids) > 0:
                update_fields["set__buyer_send_status"] = "pending"
            else:
                update_fields["unset__buyer_send_status"] = 1
        else:
            # existing behavior (unchanged)
            if len(matched_buyer_ids) > 0:
                update_fields["set__buyer_send_status"] = "pending"
            else:
                update_fields["unset__buyer_send_status"] = 1

        ParsedListing.objects(id=listing_oid).update_one(**update_fields)
    
    # if not payload.dry_run:
    #     update_fields = {
    #         "set__matched_buyer_ids": matched_buyer_ids,
    #         "set__updated_at": datetime.utcnow(),
    #     }

    #     # NEW: if at least one buyer matched, mark buyer_send_status as pending
    #     if len(matched_buyer_ids) > 0:
    #         update_fields["set__buyer_send_status"] = "pending"
    #     else:
    #         update_fields["unset__buyer_send_status"] = 1

    #     ParsedListing.objects(id=listing_oid).update_one(**update_fields)

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
    
    needs_rerun = False
    try:
        curr = ParsedListing.objects(id=listing_oid).only(
            "manual_special_preferences_rematch_at",
            "buyer_matching_last_attempt_at"
        ).first()

        if curr and curr.manual_special_preferences_rematch_at and curr.buyer_matching_last_attempt_at:
            if curr.manual_special_preferences_rematch_at > curr.buyer_matching_last_attempt_at:
                needs_rerun = True
    except Exception:
        pass
    

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
        "needs_rerun": needs_rerun,
        "is_rematch": is_rematch,
        "re_matched_buyers_count": len(re_matched_buyer_ids) if is_rematch else 0,
        "re_matched_buyer_mongo_ids": re_matched_buyer_ids if is_rematch else [],
    }
