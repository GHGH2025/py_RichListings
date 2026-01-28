# buyer_submissions_api.py
from fastapi import APIRouter
from pydantic import BaseModel, Field
from typing import Dict, Any, Optional, List

from models import (
    WebFormBuyerSubmission,
    BuyerContact,
    BuyerLocation,
    BuyerPropertyPrefs,
    BuyerPropertyLocation,
)
from buyer_submissions_formatter import build_all_property_html, build_all_counties_html
from podio_web_form_submissions import create_web_form_submission_item

router = APIRouter(prefix="/api", tags=["buyer-submissions"])

import json
from html import escape  # add once at top (or keep near the block)

_DOLLAR_PREFIX = "__DOLLAR__"
_DOT_TOKEN = "__DOT__"

def mongo_safe_key(key: str) -> str:
    """
    MongoEngine DictField does not allow keys starting with '$' or containing '.'
    We'll make it reversible.
    """
    if not isinstance(key, str):
        key = str(key)

    if key.startswith("$"):
        key = _DOLLAR_PREFIX + key[1:]

    if "." in key:
        key = key.replace(".", _DOT_TOKEN)

    return key

def mongo_safe_obj(obj: Any) -> Any:
    """
    Deep-sanitize dict keys to be Mongo-safe.
    Lists are preserved.
    """
    if isinstance(obj, dict):
        out: Dict[str, Any] = {}
        for k, v in obj.items():
            safe_k = mongo_safe_key(k)
            # handle rare collisions
            if safe_k in out:
                i = 2
                new_k = f"{safe_k}_{i}"
                while new_k in out:
                    i += 1
                    new_k = f"{safe_k}_{i}"
                safe_k = new_k

            out[safe_k] = mongo_safe_obj(v)
        return out
    if isinstance(obj, list):
        return [mongo_safe_obj(x) for x in obj]
    return obj



# -------------------------
# Pydantic payload models
# (supports NEW payload + legacy fields)
# -------------------------
from typing import Dict, Any, Optional, List
from pydantic import BaseModel, Field

class ContactModel(BaseModel):
    name: str = ""
    email: str = ""
    callWhatsapp: str = ""

    # ✅ NEW (multi-select) but keep flexible
    communicationPreference: Any = Field(default_factory=list)

    # LEGACY
    company: str = ""
    textNumber: str = ""
    phoneCall: str = ""


class PropertyLocationModel(BaseModel):
    scope: str = ""
    counties: List[str] = Field(default_factory=list)  # legacy support (may be empty)
    cities: List[str] = Field(default_factory=list)  # ✅ NEW


class PropertyModel(BaseModel):
    enabled: bool = False

    # ✅ NEW: list[str] (but allow legacy string)
    type: Any = Field(default_factory=list)

    priceRanges: List[str] = Field(default_factory=list)
    priceRange: str = ""

    beds: List[str] = Field(default_factory=list)
    baths: List[str] = Field(default_factory=list)

    location: PropertyLocationModel = Field(default_factory=PropertyLocationModel)

    preferences: Dict[str, str] = Field(default_factory=dict)
    otherType: str = ""


class PropertiesModel(BaseModel):
    multiFamily: PropertyModel = Field(default_factory=PropertyModel)
    condo: PropertyModel = Field(default_factory=PropertyModel)
    land: PropertyModel = Field(default_factory=PropertyModel)
    commercial: PropertyModel = Field(default_factory=PropertyModel)
    singleFamily: PropertyModel = Field(default_factory=PropertyModel)
    townhouse: PropertyModel = Field(default_factory=PropertyModel)


class LocationModel(BaseModel):
    # ✅ NEW global location from frontend
    scope: str = ""
    counties: List[str] = Field(default_factory=list)
    cities: List[str] = Field(default_factory=list)

    # LEGACY
    county: str = ""
    city: str = ""


class BuyerSubmissionPayload(BaseModel):
    contact: ContactModel
    properties: PropertiesModel
    location: Optional[LocationModel] = None


def _clean_list(vals: Any) -> List[str]:
    if not vals:
        return []
    if isinstance(vals, list):
        return [str(v).strip() for v in vals if str(v).strip()]
    s = str(vals).strip()
    # allow comma-separated legacy strings
    if "," in s:
        parts = [x.strip() for x in s.split(",")]
        return [p for p in parts if p]
    return [s] if s else []


def _normalized_types(p: PropertyModel) -> List[str]:
    return _clean_list(getattr(p, "type", None))


def _normalized_contact_prefs(c: ContactModel) -> List[str]:
    return _clean_list(getattr(c, "communicationPreference", None))



def _normalized_price_ranges(p: PropertyModel) -> List[str]:
    """
    Prefer new array field. If empty but legacy string exists, convert to list.
    """
    arr = list(p.priceRanges or [])
    if not arr and (p.priceRange or "").strip():
        arr = [(p.priceRange or "").strip()]
    return [x for x in arr if (x or "").strip()]

def _prefs_kv(original: Dict[str, str]) -> list:
    # keep only filled values (optional)
    return [{"label": k, "value": v} for k, v in (original or {}).items() if (v or "").strip()]


def _to_embedded(p: PropertyModel) -> BuyerPropertyPrefs:
    original_prefs = p.preferences or {}
    safe_prefs = mongo_safe_obj(original_prefs)

    price_ranges = _normalized_price_ranges(p)

    # ✅ NEW type list
    types = _normalized_types(p)
    legacy_type = (types[0] if types else "").strip()

    scope = (p.location.scope or "").strip()
    counties = [c.strip() for c in (p.location.counties or []) if (c or "").strip()]
    cities = [c.strip() for c in (p.location.cities or []) if (c or "").strip()]

    # ✅ enforce new frontend behavior
    if scope in ("all_florida", "south_florida"):
        counties = []
        cities = []
    elif scope == "counties":
        cities = []
    elif scope == "cities":
        counties = []

    loc = BuyerPropertyLocation(
        scope=scope,
        counties=counties,
        cities=cities,
    )

    return BuyerPropertyPrefs(
        enabled=bool(p.enabled),

        # ✅ keep old single field populated
        type=legacy_type,

        # ✅ store full list for AI logic
        types=[t.strip() for t in types if (t or "").strip()],

        price_ranges=[x.strip() for x in price_ranges if (x or "").strip()],
        beds=[x.strip() for x in (p.beds or []) if (x or "").strip()],
        baths=[x.strip() for x in (p.baths or []) if (x or "").strip()],
        location=loc,
        other_type=(p.otherType or "").strip(),

        preferences=safe_prefs,
        preferences_kv=_prefs_kv(original_prefs),

        price_range=(price_ranges[0].strip() if price_ranges else "").strip(),
    )

def _extract_prop_loc(state: dict):
    loc = (state or {}).get("location") or {}
    scope = str(loc.get("scope") or "").strip()
    counties = _clean_list(loc.get("counties") or loc.get("county"))
    cities = _clean_list(loc.get("cities") or loc.get("city"))
    return scope, counties, cities


def _aggregate_location_from_properties(props_dict: dict):
    scopes = []
    counties_set = set()
    cities_set = set()

    for st in (props_dict or {}).values():
        if not isinstance(st, dict) or not st.get("enabled"):
            continue
        scope, counties, cities = _extract_prop_loc(st)
        if scope:
            scopes.append(scope)
        for c in counties:
            counties_set.add(c)
        for c in cities:
            cities_set.add(c)

    # keep legacy behavior: prefer more “broad” scopes first if mixed
    scope_priority = ["all_florida", "south_florida", "counties", "cities"]
    final_scope = ""
    for s in scope_priority:
        if s in scopes:
            final_scope = s
            break

    return final_scope, sorted(list(counties_set)), sorted(list(cities_set))


@router.post("/buyer-submissions")
def create_buyer_submission(payload: BuyerSubmissionPayload):
    raw_original = payload.model_dump() if hasattr(payload, "model_dump") else payload.dict()
    raw_sanitized = mongo_safe_obj(raw_original)

    props_dict = raw_original.get("properties") or {}
    # global_loc_dict = raw_original.get("location") or {}

    property_html = build_all_property_html(props_dict)

    # # Use your existing _clean_list so it's consistent and safe
    # global_counties = _clean_list(global_loc_dict.get("counties") or global_loc_dict.get("county"))
    # global_cities = _clean_list(global_loc_dict.get("cities") or global_loc_dict.get("city"))
    # global_scope = str(global_loc_dict.get("scope") or "").strip()

    # global_loc_lines = []
    # if global_scope:
    #     global_loc_lines.append(f"<p><b>Global scope:</b> {escape(global_scope)}</p>")
    # if global_counties:
    #     global_loc_lines.append(f"<p><b>Global counties:</b> {escape(', '.join(global_counties))}</p>")
    # if global_cities:
    #     global_loc_lines.append(f"<p><b>Global cities:</b> {escape(', '.join(global_cities))}</p>")

    # global_loc_html = "\n".join(global_loc_lines).strip()

    # if global_loc_html:
    #     for k, v in property_html.items():
    #         if v:
    #             property_html[k] = v + "\n" + global_loc_html


    # ✅ UPDATED: include global location to avoid losing counties in Podio HTML
    counties_html = build_all_counties_html(props_dict, None)

    # prefer new number, fallback to legacy
    number = (payload.contact.callWhatsapp or payload.contact.textNumber or payload.contact.phoneCall or "").strip()

    # ✅ Contact multi-select preferences
    contact_prefs = _normalized_contact_prefs(payload.contact)
    contact_pref_primary = (contact_prefs[0].strip().lower() if contact_prefs else "")

    # ✅ Global location arrays
    loc_scope = ""
    loc_counties: List[str] = []
    loc_cities: List[str] = []
    loc_county_legacy = ""
    loc_city_legacy = ""

    # if payload.location:
    #     loc_scope = (payload.location.scope or "").strip()
    #     loc_counties = [c.strip() for c in (payload.location.counties or []) if (c or "").strip()]
    #     loc_cities = [c.strip() for c in (payload.location.cities or []) if (c or "").strip()]

    #     # legacy strings (keep indexes working)
    #     loc_county_legacy = (payload.location.county or "").strip() or (loc_counties[0] if loc_counties else "")
    #     loc_city_legacy = (payload.location.city or "").strip() or (loc_cities[0] if loc_cities else "")

    # ✅ NEW: derive from property-level locations
    loc_scope, loc_counties, loc_cities = _aggregate_location_from_properties(props_dict)

    # legacy strings (keep indexes working)
    loc_county_legacy = (loc_counties[0] if loc_counties else "")
    loc_city_legacy = (loc_cities[0] if loc_cities else "")


    doc = WebFormBuyerSubmission(
        contact=BuyerContact(
            name=(payload.contact.name or "").strip(),
            company=(payload.contact.company or "").strip(),
            email=(payload.contact.email or "").strip(),

            text_number=number,
            phone_call=number,

            call_whatsapp=number,

            # ✅ keep legacy single preference populated (best effort)
            preference=contact_pref_primary,

            # ✅ NEW: store full array
            preferences=contact_prefs,
        ),

        location=BuyerLocation(
            # legacy strings
            county=loc_county_legacy,
            city=loc_city_legacy,

            # ✅ NEW global arrays
            scope=loc_scope,
            counties=loc_counties,
            cities=loc_cities,
        ),

        multi_family=_to_embedded(payload.properties.multiFamily),
        condo=_to_embedded(payload.properties.condo),
        land=_to_embedded(payload.properties.land),
        commercial=_to_embedded(payload.properties.commercial),
        single_family=_to_embedded(payload.properties.singleFamily),
        townhouse=_to_embedded(payload.properties.townhouse),

        raw_payload=raw_sanitized,
        raw_payload_json=json.dumps(raw_original, ensure_ascii=False),

        podio_property_html=property_html,
        podio_counties_html=counties_html,
    )

    doc.save()
    mongo_id = str(doc.id)

    # ✅ Podio City field: send all cities as a readable string (no data loss)
    podio_city_str = ", ".join(loc_cities) if loc_cities else doc.location.city

    # Send full preference list to Podio (field is text/html, so string is perfect)
    podio_contact_pref_str = ", ".join(contact_prefs) if contact_prefs else doc.contact.preference


    try:
        item_id = create_web_form_submission_item(
            name=doc.contact.name,
            company=doc.contact.company,
            email=doc.contact.email,

            phone_call=doc.contact.phone_call,
            text_number=doc.contact.text_number,

            # keep existing field behavior
            contact_preference=podio_contact_pref_str,

            city=podio_city_str,
            mongo_object_id=mongo_id,
            property_html=property_html,
            counties_html=counties_html,
        )

        if not item_id:
            doc.podio_status = "failed"
            doc.podio_error = "Podio create item returned None"
            doc.save()
            return {"ok": True, "mongo_id": mongo_id, "podio_ok": False, "podio_item_id": None}

        doc.podio_status = "sent"
        doc.podio_item_id = int(item_id)
        doc.podio_error = None
        doc.save()

        return {"ok": True, "mongo_id": mongo_id, "podio_ok": True, "podio_item_id": doc.podio_item_id}

    except Exception as e:
        doc.podio_status = "failed"
        doc.podio_error = str(e)
        doc.save()
        return {"ok": True, "mongo_id": mongo_id, "podio_ok": False, "error": str(e)}
