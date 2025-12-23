# buyer_submissions_api.py
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Dict, Any, Optional

from models import WebFormBuyerSubmission, BuyerContact, BuyerLocation, BuyerPropertyPrefs
from buyer_submissions_formatter import build_all_property_html
from podio_web_form_submissions import create_web_form_submission_item

router = APIRouter(prefix="/api", tags=["buyer-submissions"])


class ContactModel(BaseModel):
    name: str
    company: str
    email: str
    textNumber: str
    phoneCall: str


class PropertyModel(BaseModel):
    enabled: bool = False
    type: str = ""
    priceRange: str = ""
    preferences: Dict[str, str] = Field(default_factory=dict)


class PropertiesModel(BaseModel):
    multiFamily: PropertyModel
    condo: PropertyModel
    land: PropertyModel
    commercial: PropertyModel
    singleFamily: PropertyModel
    townhouse: PropertyModel


class LocationModel(BaseModel):
    county: str
    city: str


class BuyerSubmissionPayload(BaseModel):
    contact: ContactModel
    properties: PropertiesModel
    location: LocationModel


def _to_embedded(p: PropertyModel) -> BuyerPropertyPrefs:
    return BuyerPropertyPrefs(
        enabled=bool(p.enabled),
        type=(p.type or "").strip(),
        price_range=(p.priceRange or "").strip(),
        preferences=p.preferences or {},
    )


@router.post("/buyer-submissions")
def create_buyer_submission(payload: BuyerSubmissionPayload):
    try:
        raw = payload.model_dump() if hasattr(payload, "model_dump") else payload.dict()
    except Exception:
        raw = payload.dict()

    # Build readable HTML for Podio fields
    props_dict = raw.get("properties") or {}
    property_html = build_all_property_html(props_dict)

    # Save to Mongo first (always)
    doc = WebFormBuyerSubmission(
        contact=BuyerContact(
            name=payload.contact.name.strip(),
            company=payload.contact.company.strip(),
            email=payload.contact.email.strip(),
            text_number=payload.contact.textNumber.strip(),
            phone_call=payload.contact.phoneCall.strip(),
        ),
        location=BuyerLocation(
            county=payload.location.county.strip(),
            city=payload.location.city.strip(),
        ),
        multi_family=_to_embedded(payload.properties.multiFamily),
        condo=_to_embedded(payload.properties.condo),
        land=_to_embedded(payload.properties.land),
        commercial=_to_embedded(payload.properties.commercial),
        single_family=_to_embedded(payload.properties.singleFamily),
        townhouse=_to_embedded(payload.properties.townhouse),
        raw_payload=raw,
        podio_property_html=property_html,
    )

    doc.save()  # creates Mongo ObjectId

    mongo_id = str(doc.id)

    # Push to Podio (phase 1 requirement)
    try:
        item_id = create_web_form_submission_item(
            name=doc.contact.name,
            company=doc.contact.company,
            email=doc.contact.email,
            phone_call=doc.contact.phone_call,
            text_number=doc.contact.text_number,
            county=doc.location.county,
            city=doc.location.city,
            mongo_object_id=mongo_id,
            property_html=property_html,
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
        # IMPORTANT: we still return ok since Mongo is saved (no data loss)
        return {"ok": True, "mongo_id": mongo_id, "podio_ok": False, "error": str(e)}
