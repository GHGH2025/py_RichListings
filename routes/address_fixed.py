"""routes/address_fixed.py - Cloud A: re-run a listing after a human fixed its address in Podio.
Blagojche/Claude 2026-09-24. Called by the GlobiFlow flow on Properties when 'Address Review' (field 278194171)
changes to Fixed (option 2) or Not fixable (option 3).

POST /listings/address-fixed?t=<INTERNAL_ALERT_TOKEN>
  {"podio_item_id": 3369806714, "decision": "fixed"|"not_fixable",
   "address": "1325 Normandy Dr", "city": "Miami Beach", "zip": "33141"}   (address fields only for "fixed")

fixed:        write the new address, re-geocode (same helper the scraper uses), clear the review flag,
              set wp_status="des_generated" so the next sync_wp_for_descriptions run (every ~5 min) runs the
              duplicate gate and posts it. address_review="fixed" marks it so sync_poster can tell Podio
              'Published' (or 'Duplicate of <post_id>') afterwards - see the sync_poster hook.
not_fixable:  wp_status="review_rejected", address_review="not_fixable" -> never retried, out of the digest.
Nothing is deleted. Every call is logged. Unknown item -> 404, bad token -> 403."""
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel, ValidationError
from typing import Optional, Any, Dict
from datetime import datetime
from urllib.parse import unquote_plus, parse_qs
import os, logging, json, re

router = APIRouter(tags=["address-fixed"])
logger = logging.getLogger(__name__)


class FixReq(BaseModel):
    podio_item_id: int
    decision: str = ""                 # "fixed" | "not_fixable" (GlobiFlow sends the label: "Fixed" / "Not fixable")
    address: Optional[str] = None
    city: Optional[str] = None
    zip: Optional[str] = None
    state: Optional[str] = "FL"


def _check_token(request: Request):
    tok = request.query_params.get("t") or request.headers.get("x-alert-token") or ""
    want = os.getenv("INTERNAL_ALERT_TOKEN", "")
    if not want or tok != want:
        raise HTTPException(status_code=403, detail="forbidden")


def _parse_body(raw: str) -> Dict[str, Any]:
    """GlobiFlow's Remote HTTP Call may send the body as plain JSON, URL-encoded JSON, or form fields.
    Accept all three (same approach as routes/wordpress_proxy._parse_encoded_body)."""
    text = (raw or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty body")
    candidates = [text]
    dec = unquote_plus(text)
    if dec != text:
        candidates.append(dec)
    for c in candidates:
        try:
            d = json.loads(c)
            if isinstance(d, dict):
                return d
        except ValueError:
            pass
    if "=" in text:  # form-encoded key=value&...
        return {k: v[0] for k, v in parse_qs(text, keep_blank_values=True).items()}
    raise HTTPException(status_code=400, detail="body is not JSON, URL-encoded JSON or form data")


@router.post("/listings/address-fixed")
async def address_fixed(request: Request):
    _check_token(request)
    data = _parse_body((await request.body()).decode("utf-8", errors="replace"))
    # podio_item_id may arrive as "3,366,338,638" or "3366338638 " from a Podio text/calc field
    pid = re.sub(r"[^0-9]", "", str(data.get("podio_item_id") or ""))
    if not pid:
        logger.warning("address-fixed: bad podio_item_id in body keys=%s", sorted(data.keys()))
        raise HTTPException(status_code=422, detail={"error": "podio_item_id_missing", "keys": sorted(data.keys())})
    data["podio_item_id"] = int(pid)
    try:
        req = FixReq(**{k: v for k, v in data.items() if k in FixReq.model_fields}) if hasattr(FixReq, "model_fields") else FixReq(**{k: v for k, v in data.items() if k in FixReq.__fields__})
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=json.loads(e.json()))
    from models import ParsedListing
    pl = ParsedListing.objects(buyer_matching_podio_item_id=int(req.podio_item_id)).order_by("-id").first()
    if not pl:
        raise HTTPException(status_code=404, detail={"error": "no_listing_for_podio_item", "podio_item_id": req.podio_item_id})

    # GlobiFlow sends the Podio category label as-is ("Fixed", "Not fixable", but also "Needs review" /
    # "Published" because the flow fires on ANY change of the field). Normalise and ignore the rest.
    decision = (req.decision or "").strip().lower().replace(" ", "_").replace("-", "_")
    if decision not in ("fixed", "not_fixable"):
        logger.info("address-fixed: IGNORED decision=%r podio=%s listing=%s", req.decision, req.podio_item_id, pl.id)
        return {"ok": False, "ignored": True, "decision": req.decision, "listing_id": str(pl.id)}
    if decision == "not_fixable":
        pl.update(set__wp_status="review_rejected", set__address_review="not_fixable", set__updated_at=datetime.utcnow())
        logger.info("address-fixed: NOT FIXABLE listing=%s podio=%s", pl.id, req.podio_item_id)
        return {"ok": True, "listing_id": str(pl.id), "wp_status": "review_rejected"}

    addr = (req.address or "").strip()
    if not addr:
        raise HTTPException(status_code=422, detail={"error": "address_required_for_fixed"})
    city = (req.city or getattr(pl, "city", None) or "").strip()
    zip_ = (req.zip or getattr(pl, "zip", None) or "").strip()

    # same normalisation + geocode the scraper uses (integrations.google_formatter via scrape_ingest._geocode)
    try:
        from pipeline.scrape_ingest import _geocode
        addr, city, zip_, geo_js = _geocode(addr, city, req.state or "FL", zip_)
    except Exception:
        logger.exception("address-fixed: geocode failed listing=%s", pl.id)
        geo_js = None

    blob = dict(getattr(pl, "complete_info", None) or {})
    blob["address"] = addr
    blob["city"] = city or None
    blob["zip"] = zip_ or None
    if geo_js is not None:
        blob["geo"] = geo_js

    pl.update(
        set__address=addr, set__city=city or None, set__zip=zip_ or None,
        set__complete_info=blob,
        set__address_review="fixed",            # marker for the sync_poster Podio hook
        set__wp_status="des_generated",          # picked up by run_sync_wp_for_descriptions (dup gate -> create)
        set__updated_at=datetime.utcnow(),
    )
    logger.info("address-fixed: FIXED listing=%s podio=%s addr=%r city=%r zip=%r", pl.id, req.podio_item_id, addr, city, zip_)
    return {"ok": True, "listing_id": str(pl.id), "address": addr, "city": city, "zip": zip_, "wp_status": "des_generated"}
