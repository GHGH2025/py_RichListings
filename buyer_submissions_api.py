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

class ContactModel(BaseModel):
    # NEW
    name: str = ""
    email: str = ""
    callWhatsapp: str = ""
    communicationPreference: str = ""

    # LEGACY (kept for backward compatibility)
    company: str = ""
    textNumber: str = ""
    phoneCall: str = ""


class PropertyLocationModel(BaseModel):
    # NEW
    scope: str = ""  # e.g. "south_florida" | "all_florida" | etc (frontend enum)
    counties: List[str] = Field(default_factory=list)


class PropertyModel(BaseModel):
    enabled: bool = False

    # type is auto-set by frontend now, still kept here
    type: str = ""

    # NEW (multi select)
    priceRanges: List[str] = Field(default_factory=list)

    # LEGACY (single select string) - keep parsing if ever received
    priceRange: str = ""

    # NEW (beds/baths multi)
    beds: List[str] = Field(default_factory=list)
    baths: List[str] = Field(default_factory=list)

    # NEW location per property
    location: PropertyLocationModel = Field(default_factory=PropertyLocationModel)

    # preferences matrix
    preferences: Dict[str, str] = Field(default_factory=dict)

    # kept (frontend still sends it, even if empty)
    otherType: str = ""


class PropertiesModel(BaseModel):
    multiFamily: PropertyModel = Field(default_factory=PropertyModel)
    condo: PropertyModel = Field(default_factory=PropertyModel)
    land: PropertyModel = Field(default_factory=PropertyModel)
    commercial: PropertyModel = Field(default_factory=PropertyModel)
    singleFamily: PropertyModel = Field(default_factory=PropertyModel)
    townhouse: PropertyModel = Field(default_factory=PropertyModel)


class LocationModel(BaseModel):
    # LEGACY top-level location (old payload)
    county: str = ""
    city: str = ""


class BuyerSubmissionPayload(BaseModel):
    contact: ContactModel
    properties: PropertiesModel
    # legacy (optional)
    location: Optional[LocationModel] = None


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
    safe_prefs = mongo_safe_obj(original_prefs)  # ✅ sanitize keys ($ and .)

    price_ranges = _normalized_price_ranges(p)

    # build embedded location
    loc = BuyerPropertyLocation(
        scope=(p.location.scope or "").strip(),
        counties=[c.strip() for c in (p.location.counties or []) if (c or "").strip()],
    )

    return BuyerPropertyPrefs(
        enabled=bool(p.enabled),
        type=(p.type or "").strip(),

        # ✅ NEW
        price_ranges=[x.strip() for x in price_ranges if (x or "").strip()],
        beds=[x.strip() for x in (p.beds or []) if (x or "").strip()],
        baths=[x.strip() for x in (p.baths or []) if (x or "").strip()],
        location=loc,
        other_type=(p.otherType or "").strip(),

        # ✅ preferences
        preferences=safe_prefs,
        preferences_kv=_prefs_kv(original_prefs),

        # ✅ legacy (keep populated for older logic)
        price_range=(price_ranges[0].strip() if price_ranges else "").strip(),
    )

@router.post("/buyer-submissions")
def create_buyer_submission(payload: BuyerSubmissionPayload):
    # 1) original request
    raw_original = payload.model_dump() if hasattr(payload, "model_dump") else payload.dict()

    # 2) mongo-safe copy for DictField storage
    raw_sanitized = mongo_safe_obj(raw_original)

    # 3) Podio HTML should use ORIGINAL labels (with $ intact)
    props_dict = raw_original.get("properties") or {}
    property_html = build_all_property_html(props_dict)
    counties_html = build_all_counties_html(props_dict)  # ✅ NEW Podio county fields per property

    # prefer new number, fallback to legacy
    number = (payload.contact.callWhatsapp or payload.contact.textNumber or payload.contact.phoneCall or "").strip()

    loc_county = ""
    loc_city = ""
    if payload.location:
        loc_county = (payload.location.county or "").strip()
        loc_city = (payload.location.city or "").strip()
    # Save to Mongo first
    doc = WebFormBuyerSubmission(
        contact=BuyerContact(
        name=(payload.contact.name or "").strip(),
        company=(payload.contact.company or "").strip(),  # legacy (may be blank)
        email=(payload.contact.email or "").strip(),

        # legacy fields kept populated
        text_number=number,
        phone_call=number,

        # ✅ NEW fields
        call_whatsapp=number,
        preference=(payload.contact.communicationPreference or "").strip().lower(),
        ),
        
        location=BuyerLocation(
            county=loc_county,
            city=loc_city,
        ),

        multi_family=_to_embedded(payload.properties.multiFamily),
        condo=_to_embedded(payload.properties.condo),
        land=_to_embedded(payload.properties.land),
        commercial=_to_embedded(payload.properties.commercial),
        single_family=_to_embedded(payload.properties.singleFamily),
        townhouse=_to_embedded(payload.properties.townhouse),

        raw_payload=raw_sanitized,  # ✅ safe dict
        raw_payload_json=json.dumps(raw_original, ensure_ascii=False),  # ✅ exact original
        podio_property_html=property_html,
        podio_counties_html=counties_html,
    )

    doc.save()
    mongo_id = str(doc.id)

    # -------------------------
    # Push to Podio (best effort)
    # -------------------------
    try:
        item_id = create_web_form_submission_item(
            name=doc.contact.name,
            company=doc.contact.company,
            email=doc.contact.email,

            # keep existing Podio phone fields populated
            phone_call=doc.contact.phone_call,
            text_number=doc.contact.text_number,

            contact_preference=doc.contact.preference,

            # legacy fields still exist in Podio (City is there, County is now per-property)
            city=doc.location.city,

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
