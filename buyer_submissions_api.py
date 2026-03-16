# buyer_submissions_api.py
from fastapi import APIRouter, HTTPException  # ✅ add HTTPException
import re  # ✅ add
from mongoengine.queryset.visitor import Q  # ✅ add
from pydantic import BaseModel, Field
from typing import Dict, Any, Optional, List
import os
import jwt  # pip install PyJWT
from bson import ObjectId  # pip install pymongo (already usually present)

import time
from datetime import datetime, timedelta
import json
import requests

from models import (
    WebFormBuyerSubmission,
    BuyerContact,
    BuyerLocation,
    BuyerPropertyPrefs,
    BuyerPropertyLocation,
)
from buyer_submissions_formatter import build_all_property_html, build_all_counties_html
# from podio_web_form_submissions import create_web_form_submission_item

from podio_web_form_submissions import (
    create_web_form_submission_item,
    update_web_form_submission_item,   # ✅ NEW (you'll add below)
)

router = APIRouter(prefix="/api", tags=["buyer-submissions"])

import json
from html import escape  # add once at top (or keep near the block)

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

def _normalize_email(v: str) -> str:
    return (v or "").strip().lower()

def _normalize_us_phone(v: str) -> str:
    digits = re.sub(r"\D", "", v or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits

def _safe_object_id(v: Optional[str]) -> Optional[ObjectId]:
    try:
        if not v:
            return None
        return ObjectId(str(v))
    except Exception:
        return None

def _email_exists(email: str, exclude_mongo_id: Optional[str] = None) -> bool:
    if not email:
        return False
    qs = WebFormBuyerSubmission.objects(contact__email__iexact=email)
    ex = _safe_object_id(exclude_mongo_id)
    if ex:
        qs = qs.filter(id__ne=ex)
    return qs.only("id").limit(1).count() > 0

def _phone_exists(phone10: str, exclude_mongo_id: Optional[str] = None) -> bool:
    if not phone10:
        return False
    qs = WebFormBuyerSubmission.objects(
        Q(contact__call_whatsapp=phone10) |
        Q(contact__text_number=phone10) |
        Q(contact__phone_call=phone10)
    )
    ex = _safe_object_id(exclude_mongo_id)
    if ex:
        qs = qs.filter(id__ne=ex)
    return qs.only("id").limit(1).count() > 0

@router.get("/buyer-submissions/exists")
def buyer_submission_exists(type: str, value: str, exclude_id: Optional[str] = None):
    t = (type or "").strip().lower()
    v = (value or "").strip()

    if t not in ("email", "phone"):
        raise HTTPException(status_code=400, detail="type must be 'email' or 'phone'")

    if t == "email":
        email = _normalize_email(v)
        if not EMAIL_RE.match(email):
            raise HTTPException(status_code=400, detail="Invalid email")
        return {"ok": True, "type": "email", "exists": _email_exists(email, exclude_id)}

    phone10 = _normalize_us_phone(v)
    if len(phone10) != 10:
        raise HTTPException(status_code=400, detail="Invalid US phone number")
    return {"ok": True, "type": "phone", "exists": _phone_exists(phone10, exclude_id)}


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



# ----------------------------
# Update-link + JWT settings
# ----------------------------
BUYER_UPDATE_JWT_SECRET = os.getenv("BUYER_UPDATE_JWT_SECRET", "")
BUYER_UPDATE_LINK_BASE = os.getenv("BUYER_UPDATE_LINK_BASE", "https://wholesaledealfinder.ai/ai/")
BUYER_UPDATE_EMAIL_API_URL = os.getenv(
    "BUYER_UPDATE_EMAIL_API_URL",
    "http://ec2-3-90-20-111.compute-1.amazonaws.com:8000/rich_ai_deal_Email"
)
BUYER_UPDATE_LINK_TTL_HOURS = int(os.getenv("BUYER_UPDATE_LINK_TTL_HOURS", "24"))

def _require_update_secret():
    if not BUYER_UPDATE_JWT_SECRET or len(BUYER_UPDATE_JWT_SECRET) < 16:
        raise HTTPException(status_code=500, detail="Server misconfigured: missing BUYER_UPDATE_JWT_SECRET")

def _find_submission_by_email(email: str) -> Optional[WebFormBuyerSubmission]:
    return WebFormBuyerSubmission.objects(
        contact__email__iexact=email
    ).order_by("-created_at").first()

def _build_update_token(*, mongo_id: str, podio_item_id: int, email: str) -> str:
    _require_update_secret()
    now = int(time.time())
    exp = now + (BUYER_UPDATE_LINK_TTL_HOURS * 3600)
    payload = {
        "mongo_id": mongo_id,
        "podio_item_id": int(podio_item_id or 0),
        "email": _normalize_email(email),
        "iat": now,
        "exp": exp,
    }
    return jwt.encode(payload, BUYER_UPDATE_JWT_SECRET, algorithm="HS256")

def _decode_update_token(token: str) -> Dict[str, Any]:
    _require_update_secret()
    try:
        return jwt.decode(token, BUYER_UPDATE_JWT_SECRET, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="This update link has expired. Please request a new one.")
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid update link. Please request a new one.")

def _build_update_email_html(update_link: str) -> str:
    # simple + clean, works in email clients
    safe_link = escape(update_link)
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Update Your Buy Box</title>
</head>
<body style="margin:0;padding:0;background:#f6f6f6;font-family:Arial,Helvetica,sans-serif;color:#111;">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background:#f6f6f6;">
    <tr>
      <td align="center" style="padding:24px;">
        <table role="presentation" width="600" cellspacing="0" cellpadding="0" border="0"
               style="width:600px;max-width:100%;background:#ffffff;border:1px solid #e6e6e6;border-radius:16px;overflow:hidden;">
          <tr>
            <td style="padding:28px 28px 10px 28px;background:#0b5cff;color:#fff;">
              <div style="font-size:12px;letter-spacing:3px;font-weight:800;text-transform:uppercase;opacity:.9;">
                WholesaleDealFinder.ai
              </div>
              <div style="font-size:26px;font-weight:900;line-height:1.2;margin-top:10px;">
                Update Your Buy Box
              </div>
              <div style="font-size:13px;opacity:.95;margin-top:10px;line-height:1.5;">
                Use the secure button below to open your existing profile and save changes.
              </div>
            </td>
          </tr>

          <tr>
            <td style="padding:22px 28px 28px 28px;">
              <div style="font-size:14px;line-height:1.7;color:#333;">
                <p style="margin:0 0 14px 0;">
                  Click the button to open your saved preferences. A secure loader will appear while we fetch your profile.
                </p>

                <p style="margin:0 0 18px 0;">
                  <a href="{safe_link}"
                     style="display:inline-block;background:#0b5cff;color:#fff;text-decoration:none;
                            padding:14px 18px;border-radius:12px;font-weight:900;letter-spacing:1px;
                            text-transform:uppercase;">
                    Open My Buy Box
                  </a>
                </p>

                <p style="margin:0 0 8px 0;font-size:12px;color:#666;">
                  If the button doesn’t work, copy/paste this link:
                </p>
                <p style="margin:0;font-size:12px;word-break:break-all;">
                  <a href="{safe_link}" style="color:#0b5cff;text-decoration:underline;">{safe_link}</a>
                </p>

                <hr style="border:none;border-top:1px solid #eee;margin:22px 0;"/>

                <p style="margin:0;font-size:12px;color:#777;line-height:1.6;">
                  This link is secure and expires in {BUYER_UPDATE_LINK_TTL_HOURS} hours.
                  If you didn’t request this, you can ignore this email.
                </p>
              </div>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""

class UpdateLinkRequest(BaseModel):
    email: str = ""

class UpdateResolveRequest(BaseModel):
    token: str = ""

class UpdateSaveRequest(BaseModel):
    token: str
    payload: Any  # we’ll validate by reusing BuyerSubmissionPayload below


# ✅ reuse your existing BuyerSubmissionPayload (already defined above in your file)
# from your existing code:
# class BuyerSubmissionPayload(BaseModel): ...

@router.post("/buyer-submissions/update/request-link")
def request_update_link(body: UpdateLinkRequest):
    email = _normalize_email(body.email)

    if not EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="Invalid email")

    doc = _find_submission_by_email(email)
    if not doc:
        raise HTTPException(status_code=404, detail="No profile found for that email.")

    if not doc.podio_item_id:
        raise HTTPException(status_code=409, detail="Profile found, but Podio item_id is missing. Please contact support.")

    token = _build_update_token(
        mongo_id=str(doc.id),
        podio_item_id=int(doc.podio_item_id),
        email=doc.contact.email,
    )

    base = BUYER_UPDATE_LINK_BASE.rstrip("/") + "/"
    update_link = f"{base}?id={token}"

    subject = "Your WholesaleDealFinder.ai Buy Box Update Link"
    html = _build_update_email_html(update_link)

    # Send via your existing email API
    try:
        resp = requests.post(
            BUYER_UPDATE_EMAIL_API_URL,
            json={"to": [email], "subject": subject, "body": html},
            timeout=25,
        )
        if resp.status_code >= 300:
            raise RuntimeError(f"Email API error {resp.status_code}: {resp.text}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to send email. {str(e)}")

    return {"ok": True, "sent_to": email}


@router.post("/buyer-submissions/update/resolve")
def resolve_update_link(body: UpdateResolveRequest):
    token = (body.token or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="Missing token")

    claims = _decode_update_token(token)
    mongo_id = claims.get("mongo_id")
    email = _normalize_email(claims.get("email", ""))

    if not mongo_id or not email:
        raise HTTPException(status_code=401, detail="Invalid update link payload.")

    doc = WebFormBuyerSubmission.objects(id=_safe_object_id(mongo_id)).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Profile not found.")

    # extra safety: ensure token still matches record
    doc_email = _normalize_email(doc.contact.email)

    if doc_email != email:
        raise HTTPException(status_code=404, detail="Profile not found for this link.")

    # Return the original request body we stored
    payload = {}
    try:
        if doc.raw_payload_json:
            payload = json.loads(doc.raw_payload_json)
        elif doc.raw_payload:
            payload = doc.raw_payload
    except Exception:
        payload = {}

    return {
        "ok": True,
        "mongo_id": str(doc.id),
        "podio_item_id": int(doc.podio_item_id or 0),
        "submission": payload,
    }


@router.post("/buyer-submissions/update/save")
def save_update(body: UpdateSaveRequest):
    token = (body.token or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="Missing token")

    claims = _decode_update_token(token)
    mongo_id = claims.get("mongo_id")
    email = _normalize_email(claims.get("email", ""))

    if not mongo_id or not email:
        raise HTTPException(status_code=401, detail="Invalid update link payload.")

    doc = WebFormBuyerSubmission.objects(id=_safe_object_id(mongo_id)).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Profile not found.")

    doc_email = _normalize_email(doc.contact.email)

    if doc_email != email:
        raise HTTPException(status_code=404, detail="Profile not found for this link.")

    if not doc.podio_item_id:
        raise HTTPException(status_code=409, detail="Podio item_id missing. Cannot update Podio.")

    # Validate incoming payload using your existing BuyerSubmissionPayload model
    try:
        incoming = BuyerSubmissionPayload(**(body.payload or {}))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid payload: {str(e)}")

    raw_original = incoming.model_dump() if hasattr(incoming, "model_dump") else incoming.dict()
    raw_sanitized = mongo_safe_obj(raw_original)

    props_dict = raw_original.get("properties") or {}
    property_html = build_all_property_html(props_dict)
    counties_html = build_all_counties_html(props_dict, None)

    number = (incoming.contact.callWhatsapp or incoming.contact.textNumber or incoming.contact.phoneCall or "").strip()

    contact_prefs = _normalized_contact_prefs(incoming.contact)
    contact_pref_primary = (contact_prefs[0].strip().lower() if contact_prefs else "")

    loc_scope, loc_counties, loc_cities = _aggregate_location_from_properties(props_dict)
    loc_county_legacy = (loc_counties[0] if loc_counties else "")
    loc_city_legacy = (loc_cities[0] if loc_cities else "")

    # ---- Update Mongo doc (same record) ----
    doc.contact.name = (incoming.contact.name or "").strip()
    doc.contact.email = (incoming.contact.email or "").strip()

    doc.contact.text_number = number
    doc.contact.phone_call = number
    doc.contact.call_whatsapp = number
    doc.contact.preference = contact_pref_primary
    doc.contact.preferences = contact_prefs

    doc.location.scope = loc_scope
    doc.location.counties = loc_counties
    doc.location.cities = loc_cities
    doc.location.county = loc_county_legacy
    doc.location.city = loc_city_legacy

    doc.multi_family = _to_embedded(incoming.properties.multiFamily)
    doc.condo = _to_embedded(incoming.properties.condo)
    doc.land = _to_embedded(incoming.properties.land)
    doc.commercial = _to_embedded(incoming.properties.commercial)
    doc.single_family = _to_embedded(incoming.properties.singleFamily)
    doc.townhouse = _to_embedded(incoming.properties.townhouse)

    doc.raw_payload = raw_sanitized
    doc.raw_payload_json = json.dumps(raw_original, ensure_ascii=False)

    doc.podio_property_html = property_html
    doc.podio_counties_html = counties_html

    doc.touch()
    doc.save()

    podio_city_str = ", ".join(loc_cities) if loc_cities else doc.location.city
    podio_contact_pref_str = ", ".join(contact_prefs) if contact_prefs else doc.contact.preference

    # ---- Update Podio item (same item_id) ----
    ok = update_web_form_submission_item(
        item_id=int(doc.podio_item_id),
        name=doc.contact.name,
        company=doc.contact.company or "",
        email=doc.contact.email,
        phone_call=doc.contact.phone_call,
        text_number=doc.contact.text_number,
        city=podio_city_str,
        mongo_object_id=str(doc.id),
        property_html=property_html,
        contact_preference=podio_contact_pref_str,
        counties_html=counties_html,
    )

    return {"ok": True, "mongo_id": str(doc.id), "podio_ok": bool(ok), "podio_item_id": int(doc.podio_item_id)}

