# wp_ai_mapper_catalog_first.py
import os, json
from typing import Dict, Any, List, Optional, Iterable
from openai import OpenAI

from bson import ObjectId
from models import ParsedListing

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY)

allowed_country_deals_options = [
  "Orange County > Single Family",
  "Alachua County > Commercial",
  "Alachua County > Condo",
  "Alachua County > Land",
  "Alachua County > Multi Family",
  "Alachua County > Single Family",
  "Alachua County > Townhouse",
  "Baker County > Single Family",
  "BAY COUNTY > Commercial",
  "BAY COUNTY > Condo",
  "BAY COUNTY > Land",
  "BAY COUNTY > Multi Family",
  "BAY COUNTY > Single Family",
  "BAY COUNTY > Townhouse",
  "Bradford County > Commercial",
  "Bradford County > Condo",
  "Bradford County > Land",
  "Bradford County > Multi Family",
  "Bradford County > Single Family",
  "Bradford County > Townhouse",
  "Brevard County > Commercial",
  "Brevard County > Condo",
  "Brevard County > Land",
  "Brevard County > Multi Family",
  "Brevard County > Single Family",
  "Brevard County > Townhouse",
  "Broward County Deals > Commercial",
  "Broward County Deals > Condo",
  "Broward County Deals > Land",
  "Broward County Deals > Multi Family",
  "Broward County Deals > Single Family",
  "Broward County Deals > Townhouse",
  "Charlotte County > Commercial",
  "Charlotte County > Condo",
  "Charlotte County > Land",
  "Charlotte County > Multi Family",
  "Charlotte County > Single Family",
  "Charlotte County > Townhouse",
  "Citrus County > Commercial",
  "Citrus County > Condo",
  "Citrus County > Land",
  "Citrus County > Multi Family",
  "Citrus County > Single Family",
  "Citrus County > Townhouse",
  "Clay County > Commercial",
  "Clay County > Condo",
  "Clay County > Land",
  "Clay County > Multi Family",
  "Clay County > Single Family",
  "Clay County > Townhouse",
  "Collier County > Commercial",
  "Collier County > Land",
  "Collier County > Multi Family",
  "Collier County > Single Family",
  "COLUMBIA > Land",
  "COLUMBIA > Multi Family",
  "COLUMBIA > Single Family",
  "Dade County > Commercial",
  "Dade County > Condo",
  "Dade County > Land",
  "Dade County > Multi Family",
  "Dade County > Single Family",
  "Dade County > Townhouse",
  "Desoto County > Commercial",
  "Desoto County > Condo",
  "Desoto County > Land",
  "Desoto County > Multi Family",
  "Desoto County > Single Family",
  "Desoto County > Townhouse",
  "DIXIE County > Land",
  "DIXIE County > Single Family",
  "Duval County > Commercial",
  "Duval County > Condo",
  "Duval County > Land",
  "Duval County > Multi Family",
  "Duval County > Single Family",
  "Duval County > Townhouse",
  "Escambia County > Commercial",
  "Escambia County > Land",
  "Escambia County > Multi Family",
  "Escambia County > Single Family",
  "Escambia County > Townhouse",
  "Flagler County > Commercial",
  "Flagler County > Condo",
  "Flagler County > Land",
  "Flagler County > Multi Family",
  "Flagler County > Single Family",
  "Flagler County > Townhouse",
  "GILCHRIST > Land",
  "GILCHRIST > Multi Family",
  "HAMILTON > Single Family",
  "Hamilton County > Single Family",
  "HARDEE > Single Family",
  "HENDRY County > land",
  "HENDRY County > Single Family",
  "Hernando County > Land",
  "Hernando County > Multi Family",
  "Hernando County > Single Family",
  "Highlands County > Commercial",
  "Highlands County > Condo",
  "Highlands County > Land",
  "Highlands County > Multi Family",
  "Highlands County > Single Family",
  "Highlands County > Townhouse",
  "Hillsborough County > Commercial",
  "Hillsborough County > Condo",
  "Hillsborough County > Land",
  "Hillsborough County > Multi Family",
  "Hillsborough County > Single Family",
  "Hillsborough County > Townhouse",
  "Indian River > Commercial",
  "Indian River > Condo",
  "Indian River > Land",
  "Indian River > Multi Family",
  "Indian River > Single Family",
  "Indian River > Townhouse",
  "Jackson County > Land",
  "Jackson County > Multi Family",
  "Jackson County > Single Family",
  "Lake County > Commercial",
  "Lake County > Condo",
  "Lake County > Land",
  "Lake County > Multi Family",
  "Lake County > Single Family",
  "Lake County > Townhouse",
  "Lee County > Commercial",
  "Lee County > Condo",
  "Lee County > Gas Station",
  "Lee County > Land",
  "Lee County > Multi Family",
  "Lee County > Single Family",
  "Lee County > Townhouse",
  "LEON COUNTY > Commercial",
  "LEON COUNTY > Condo",
  "LEON COUNTY > Land",
  "LEON COUNTY > Multi Family",
  "LEON COUNTY > Single Family",
  "LEON COUNTY > Townhouse",
  "LEVY County > Land",
  "LEVY County > Single Family",
  "Madison > Land",
  "Manatee County > Commercial",
  "Manatee County > Condo",
  "Manatee County > Land",
  "Manatee County > Multi Family",
  "Manatee County > Single Family",
  "Manatee County > Townhouse",
  "Marion County > Commercial",
  "Marion County > Condo",
  "Marion County > Land",
  "Marion County > Multi Family",
  "Marion County > Single Family",
  "Marion County > Townhouse",
  "Martin County > Commercial",
  "Martin County > Condo",
  "Martin County > Land",
  "Martin County > Multi Family",
  "Martin County > Single Family",
  "Martin County > Townhouse",
  "Monroe County > Land",
  "Monroe County > Multi Family",
  "Monroe County > Single Family",
  "Nassau County > Land",
  "Nassau County > Single Family",
  "Okaloosa > Land",
  "Okaloosa > Multi Family",
  "Okaloosa > Single Family",
  "OKEECHOBEE > Land",
  "OKEECHOBEE > Multi Family",
  "OKEECHOBEE > Single Family",
  "Osceola County > Commercial",
  "Osceola County > Condo",
  "Osceola County > Land",
  "Osceola County > Multi Family",
  "Osceola County > Single Family",
  "Osceola County > Townhouse",
  "Palm Beach County > Commercial",
  "Palm Beach County > Condo",
  "Palm Beach County > Land",
  "Palm Beach County > Multi Family",
  "Palm Beach County > Single Family",
  "Palm Beach County > Townhouse",
  "Pasco County > Commercial",
  "Pasco County > Condo",
  "Pasco County > Land",
  "Pasco County > Multi Family",
  "Pasco County > Single Family",
  "Pasco County > Townhouse",
  "Pinellas County > Commercial",
  "Pinellas County > Condo",
  "Pinellas County > Land",
  "Pinellas County > Multi Family",
  "Pinellas County > Single Family",
  "Pinellas County > Townhouse",
  "Polk County > Commercial",
  "Polk County > Condo",
  "Polk County > Land",
  "Polk County > Multi Family",
  "Polk County > Single Family",
  "Polk County > Townhouse",
  "PUTNAM > Land",
  "PUTNAM > Multi Family",
  "PUTNAM > Single Family",
  "SAINT JOHNS > Land",
  "SAINT JOHNS > Single Family",
  "SAINT LUCIE > Commercial",
  "SAINT LUCIE > Condo",
  "SAINT LUCIE > Land",
  "SAINT LUCIE > Multi Family",
  "SAINT LUCIE > Single Family",
  "SAINT LUCIE > Townhouse",
  "SANTA ROSA > Land",
  "SANTA ROSA > Multi Family",
  "SANTA ROSA > Single Family",
  "Santa Rosa County > Single Family",
  "Sarasota County > Commercial",
  "Sarasota County > Condo",
  "Sarasota County > Land",
  "Sarasota County > Multi Family",
  "Sarasota County > Single Family",
  "Sarasota County > Townhouse",
  "Seminole > Commercial",
  "Seminole > Condo",
  "Seminole > Land",
  "Seminole > Multi Family",
  "Seminole > Single Family",
  "Seminole > Townhouse",
  "St. Johns > Commercial",
  "St. Johns > Condo",
  "St. Johns > Land",
  "St. Johns > Multi Family",
  "St. Johns > Single Family",
  "St. Johns > Townhouse",
  "SUMTER County > Land",
  "SUMTER County > Multi Family",
  "SUMTER County > Single Family",
  "Suwannee County > Commercial",
  "Suwannee County > Condo",
  "Suwannee County > Land",
  "Suwannee County > Multi Family",
  "Suwannee County > Single Family",
  "Suwannee County > Townhouse",
  "Taylor County > Commercial",
  "Taylor County > land",
  "Taylor County > Multi Family",
  "Taylor County > Single Family",
  "Volusia County > Commercial",
  "Volusia County > Condo",
  "Volusia County > Land",
  "Volusia County > Multi Family",
  "Volusia County > Single Family",
  "Volusia County > Townhouse",
  "Walton > Land",
  "Walton > Single Family",
  "Washington County > Single Family",
  "SAINT LUCIE, Single Family",
  "St Johns",
  "Commercial",
  "Condo",
  "Land",
  "Multi Family",
  "Townhouse"
]


allowed_region_paths = [
  "Florida > Central Florida > BREVARD",
  "Florida > Central Florida > CITRUS",
  "Florida > Central Florida > HERNANDO",
  "Florida > Central Florida > HILLSBOROUGH",
  "Florida > Central Florida > INDIAN RIVER",
  "Florida > Central Florida > LAKE",
  "Florida > Central Florida > LEVY",
  "Florida > Central Florida > MARION",
  "Florida > Central Florida > OKEECHOBEE",
  "Florida > Central Florida > ORANGE",
  "Florida > Central Florida > OSCEOLA",
  "Florida > Central Florida > PASCO",
  "Florida > Central Florida > PINELLAS",
  "Florida > Central Florida > POLK",
  "Florida > Central Florida > SEMINOLE",
  "Florida > Central Florida > SUMTER",
  "Florida > Central Florida > Volusia County",

  "Florida > North Florida > ALACHUA",
  "Florida > North Florida > BAKER",
  "Florida > North Florida > BAY",
  "Florida > North Florida > BRADFORD",
  "Florida > North Florida > CLAY",
  "Florida > North Florida > COLUMBIA",
  "Florida > North Florida > DIXIE",
  "Florida > North Florida > DUVAL",
  "Florida > North Florida > ESCAMBIA",
  "Florida > North Florida > FLAGLER",
  "Florida > North Florida > GILCHRIST",
  "Florida > North Florida > HAMILTON",
  "Florida > North Florida > Jackson County",
  "Florida > North Florida > LAFAYETTE",
  "Florida > North Florida > LEON",
  "Florida > North Florida > Madison",
  "Florida > North Florida > NASSAU",
  "Florida > North Florida > Okaloosa",
  "Florida > North Florida > PUTNAM",
  "Florida > North Florida > SAINT JOHNS",
  "Florida > North Florida > SANTA ROSA",
  "Florida > North Florida > SUWANNEE",
  "Florida > North Florida > UNION",
  "Florida > North Florida > Walton",
  "Florida > North Florida > Washington County",

  "Florida > Southeast Florida > BROWARD",
  "Florida > Southeast Florida > MARTIN",
  "Florida > Southeast Florida > MIAMI DADE",
  "Florida > Southeast Florida > MONROE",
  "Florida > Southeast Florida > PALM BEACH",
  "Florida > Southeast Florida > SAINT LUCIE",

  "Florida > Southwest Florida > CHARLOTTE",
  "Florida > Southwest Florida > COLLIER",
  "Florida > Southwest Florida > DE SOTO",
  "Florida > Southwest Florida > GLADES",
  "Florida > Southwest Florida > HARDEE",
  "Florida > Southwest Florida > HENDRY",
  "Florida > Southwest Florida > HIGHLANDS",
  "Florida > Southwest Florida > LEE",
  "Florida > Southwest Florida > MANATEE",
  "Florida > Southwest Florida > SARASOTA",

  "Taylor"

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
                    "property_name": {
                        "anyOf": [
                            {"type": "string", "enum": ["Commercial", "Condo", "Land", "Multi Family", "Single Family", "Townhouse"]},
                            {"type": "null"}
                        ]
                    },
                    "notes": {"type": "array", "items": {"type": "string"}}
                },
                # ✅ MUST include *all* keys in properties:
                "required": [
                    "country_deals",
                    "region",
                    "proposed_country_deals",
                    "proposed_region",
                    "property_name",
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
- "property_name": one of ["Commercial","Condo","Land","Multi Family","Single Family","Townhouse"] or null.
  • If the listing’s property_type is present, map it to the closest label:
      single_family→Single Family, condo→Condo, townhouse→Townhouse,
      multi_family→Multi Family, land→Land, mobile_home/manufactured→Single Family (only if clearly SFR),
      otherwise null.
  • If property_type is missing, infer cautiously from the description; if unclear, return null.
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

    _allowed_prop_names = {"Commercial","Condo","Land","Multi Family","Single Family","Townhouse"}
    pn = data.get("property_name", None)
    if pn is not None and (not isinstance(pn, str) or pn.strip() not in _allowed_prop_names):
        data["property_name"] = None

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


def _listing_to_dict(pl: ParsedListing) -> Dict[str, Any]:
    """Normalize a ParsedListing mongo doc into the dict expected by ai_build_wp_payload_catalog_first."""
    ci = getattr(pl, "complete_info", {}) or {}
    return {
        "address": getattr(pl, "address", None) or ci.get("address"),
        "city": getattr(pl, "city", None) or ci.get("city"),
        "county": ci.get("county") or getattr(pl, "county", None),
        "state": getattr(pl, "state", None) or ci.get("state"),
        "zip": getattr(pl, "zip", None) or ci.get("zip"),
        "property_type": (ci.get("property_type")
                          or getattr(pl, "property_type", None)),
        "complete_info": ci,
    }


def ai_build_wp_payload_for_posted(
    model: Optional[str] = None,
    *,
    limit: Optional[int] = None,         # e.g. 500; None = no hard cap
    skip: int = 0,                        # pagination offset
    batch_size: int = 25,                 # safety against rate limits
    per_item_sleep_s: float = 0.0,        # e.g. 0.2 if you want light throttling
) -> Dict[str, Any]:
    """
    Runs AI mapping for all ParsedListing with status='posted'.
    Returns a compact report with successes and errors.

    You can paginate with skip/limit, and throttle with batch_size + per_item_sleep_s.
    """
    q = ParsedListing.objects(wp_status="ready_to_process").order_by("+created_at")
    if skip:
        q = q.skip(skip)
    if limit is not None:
        q = q.limit(limit)

    processed = 0
    results: List[Dict[str, Any]] = []
    errors = 0

    batch: List[ParsedListing] = []
    for pl in q:  # mongoengine QuerySet is iterable
        batch.append(pl)
        if len(batch) >= batch_size:
            _process_batch(batch, results, model, per_item_sleep_s)
            processed += len(batch)
            batch = []
    # tail
    if batch:
        _process_batch(batch, results, model, per_item_sleep_s)
        processed += len(batch)

    # quick error count
    for r in results:
        if not r.get("ok", False):
            errors += 1

    return {
        "processed": processed,
        "ok": processed - errors,
        "errors": errors,
        "results": results,   # [{id, ok, payload or error}]
    }


def _process_batch(
    docs: Iterable[ParsedListing],
    results_accum: List[Dict[str, Any]],
    model: Optional[str],
    per_item_sleep_s: float,
) -> None:
    for pl in docs:
        try:
            listing_dict = _listing_to_dict(pl)
            payload = ai_build_wp_payload_catalog_first(listing_dict, model=model)
            # ✅ Save to this listing
            ParsedListing.objects(id=pl.id).update_one(
                set__wp_parsed_data=payload,
                set__wp_status="keys_generated"
            )
            try:
                from observability.pipeline_metrics import record_listing_stage
                record_listing_stage(str(pl.id), "wp_keys", wp_status="keys_generated")
            except Exception:
                pass
            results_accum.append({
                "id": str(getattr(pl, "id", "")),
                "ok": True,
                "payload": payload
            })
        except Exception as e:
            results_accum.append({
                "id": str(getattr(pl, "id", "")),
                "ok": False,
                "error": f"{type(e).__name__}: {e}"
            })
        if per_item_sleep_s > 0:
            time.sleep(per_item_sleep_s)
