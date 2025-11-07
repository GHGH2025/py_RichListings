# wp_ai_mapper_catalog_first.py
import os, json
from typing import Dict, Any, List, Optional
from openai import OpenAI

from bson import ObjectId
from models import ParsedListing

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY)

allowed_country_deals_options = [
    # Examples — build the full list once based on your WP terms
    "Orange County > Single Family",
    "Alachua County > Commercial",
    "Alachua County > Condo",
    "Alachua County > Land",
    "Alachua County > Multi Family",
    "Alachua County > Single Family",
    "Alachua County > Townhouse",
    "Alachua County Single Family",
    "BAY COUNTY > Single Family",
    "BAY COUNTY > Condo",
    "Dade County > Commercial",
    "Dade County > Condo",
    "Dade County > Land",
    "Dade County > Multi Family",
    "Dade County > Single Family",
    "Dade County > Townhouse",
    
    # ...
    "Broward County Deals > Single Family",
    "Broward County Deals > Multi Family",
    # ...
    "SAINT LUCIE, Single Family",
    "Multi Family",  # standalone types you keep at top-level
    "Condo",
    "Land",
    "Townhouse",
    "Commercial",
]


allowed_region_paths = [
    "Florida > Central Florida > BREVARD",
    "Florida > Central Florida > CITRUS",
    "Florida > Central Florida > ORANGE",
    "Florida > North Florida > DUVAL",
    "Florida > Southeast Florida > MIAMI DADE",
    "Florida > Southeast Florida > BROWARD",
    "Florida > Southeast Florida > PALM BEACH",
    "Florida > Southeast Florida > SAINT LUCIE",
    "Florida > Southwest Florida > LEE",
    # ...
    "Taylor",  # standalone
]


# Map internal property_type → label the model should target (helps it normalize)
WP_PROP_TYPE_LABEL = {
    "single_family": "Single Family",
    "condo": "Condo",
    "townhouse": "Townhouse",
    "multi_family": "Multi Family",
    "land": "Land",
    "mobile_home": "Mobile Home",
    "manufactured": "Manufactured",
    "other": "Other"
}

def _response_format() -> Dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "wp_taxonomy_payload",
            "strict": True,
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "country_deals": {"type": "array", "items": {"type": "string"}},
                    "region": {"type": "array", "items": {"type": "string"}},
                    "proposed_country_deals": {"type": "array", "items": {"type": "string"}},
                    "proposed_region": {"type": "array", "items": {"type": "string"}},
                    "notes": {"type": "array", "items": {"type": "string"}}
                },
                # ✅ MUST include *all* keys in properties:
                "required": [
                    "country_deals",
                    "region",
                    "proposed_country_deals",
                    "proposed_region",
                    "notes"
                ]
            }
        }
    }

_SYSTEM_PROMPT = """\
You map one real-estate listing into two WordPress taxonomy fields using the provided allowed options.

OUTPUT KEYS (exactly):
- "country_deals": array of 0–3 strings. Each MUST be an EXACT match from ALLOWED_COUNTRY_DEALS_OPTIONS if any match exists.
- "region": array of 0–3 strings. Each MUST be an EXACT match from ALLOWED_REGION_PATHS if any match exists.
- If NOTHING appropriate exists in the allowed lists, put best single suggestion(s) in:
  - "proposed_country_deals": array of 0–2 strings (format: "<County Variant> County > <PropertyTypeLabel>")
  - "proposed_region": array of 0–2 strings (format like the region paths shown)
- Do not duplicate values between allowed and proposed arrays.
- Keep arrays short and relevant (prefer just one best choice when clear).

Rules:
1) PropertyTypeLabel must be one of:
   ["Single Family","Condo","Townhouse","Multi Family","Land","Mobile Home","Manufactured","Other"].
   If listing.property_type is present, use its mapped label; else infer cautiously from the description.
2) County normalization:
   - Prefer the listing's explicit county.
   - If missing, infer from city+state+zip (Florida focus) using your knowledge.
   - Normalize variants to match how they appear inside the allowed lists (e.g., Broward vs Broward County Deals; Miami-Dade vs Dade).
3) Matching priority:
   - FIRST try to select from ALLOWED_COUNTRY_DEALS_OPTIONS and ALLOWED_REGION_PATHS (exact string match).
   - ONLY IF no adequate allowed match exists, produce a single best suggestion into "proposed_*".
4) Output ONLY the keys in the schema; keep it short and exact.
"""

_USER_TEMPLATE = """\
LISTING_JSON:
{listing_json}

ALLOWED_COUNTRY_DEALS_OPTIONS (use exact strings if they fit):
{allowed_country_deals_json}

ALLOWED_REGION_PATHS (use exact strings if they fit):
{allowed_region_paths_json}

Hints:
- property_type label hint: {ptype_hint}
"""

def ai_build_wp_payload_catalog_first(
    listing: Dict[str, Any],
    model: Optional[str] = None
) -> Dict[str, Any]:
    """
    listing: ParsedListing document or dict (must include location + property_type within listing or listing['complete_info'])
    allowed_country_deals_options: flat list of exact strings (e.g., "Brevard County > Single Family",
                                     "Broward County Deals > Multi Family", "Alachua County Single Family")
    allowed_region_paths: flat list of exact region strings (e.g., "Florida > Central Florida > BREVARD", "Taylor")
    """
    # determine property_type label hint from listing
    ci = (listing.get("complete_info") or {})
    prop_key = ci.get("property_type") or listing.get("property_type") or ""
    ptype_hint = WP_PROP_TYPE_LABEL.get(str(prop_key).strip().lower(), "Other")

    # keep payload small but complete
    listing_for_ai = {
        "address": listing.get("address") or ci.get("address"),
        "city": listing.get("city") or ci.get("city"),
        "county": ci.get("county") or listing.get("county"),
        "state": listing.get("state") or ci.get("state"),
        "zip": listing.get("zip") or ci.get("zip"),
        "property_type": prop_key,
        "complete_info_excerpt": (ci.get("complete_info") or "")[:1200]
    }

    print("listing_for_ai",listing_for_ai)

    msg = _USER_TEMPLATE.format(
        listing_json=json.dumps(listing_for_ai, ensure_ascii=False),
        allowed_country_deals_json=json.dumps(allowed_country_deals_options, ensure_ascii=False, indent=2),
        allowed_region_paths_json=json.dumps(allowed_region_paths, ensure_ascii=False, indent=2),
        ptype_hint=ptype_hint
    )

    chat = client.chat.completions.create(
        model=(model or OPENAI_MODEL),
        messages=[{"role": "system", "content": _SYSTEM_PROMPT},
                  {"role": "user", "content": msg}],
        temperature=0.1,
        response_format=_response_format()
    )

    data = json.loads(chat.choices[0].message.content)

    # sanitize arrays
    for k in ["country_deals", "region", "proposed_country_deals", "proposed_region"]:
        if k in data and isinstance(data[k], list):
            data[k] = [s for s in data[k] if isinstance(s, string_types := str) and s.strip()]

    return data


# NEW: ID-based wrapper
def ai_build_wp_payload_by_id(
    listing_id: str,
    model: Optional[str] = None
) -> Dict[str, Any]:
    """
    Fetches ParsedListing by _id and runs the same AI mapping.
    Returns a dict with the AI payload or an error structure if not found/invalid id.
    """
    try:
        oid = ObjectId(listing_id)
    except Exception:
        return {"error": "invalid_listing_id", "listing_id": listing_id}

    pl = ParsedListing.objects(id=oid).first()
    if not pl:
        return {"error": "listing_not_found", "listing_id": listing_id}

    # Convert to a plain dict suitable for the AI function
    listing_dict = {
        "address": getattr(pl, "address", None),
        "city": getattr(pl, "city", None),
        "county": getattr(pl, "county", None),
        "state": getattr(pl, "state", None),
        "zip": getattr(pl, "zip", None),
        "property_type": (getattr(pl, "complete_info", {}) or {}).get("property_type")
                          or getattr(pl, "property_type", None),
        "complete_info": getattr(pl, "complete_info", {}) or {},
    }

    return ai_build_wp_payload_catalog_first(listing_dict, model=model)
