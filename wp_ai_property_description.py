# wp_ai_property_description.py  (UPDATED)
import os
import time
import json
from typing import Dict, Any, List, Optional, Iterable
from openai import OpenAI

from bson import ObjectId
from models import ParsedListing

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY)

# ---------- Strict JSON response format ----------
def _response_format() -> Dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "wp_property_description_out",
            "strict": True,
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "property_description_html": {"type": "string"},
                    "notes": {"type": "array", "items": {"type": "string"}}
                },
                "required": ["property_description_html", "notes"]
            }
        }
    }

# # ---------- System prompt (tight and explicit) ----------
# _SYSTEM_PROMPT = """\
# You generate a short property description HTML fragment from two sources:
# (1) complete_info: structured, machine-extracted fields for the property
# (2) post_content: human-friendly text used for WhatsApp (may include address, contact info, links)

# Rules (non-negotiable):
# 1) NO hallucinations. Use only facts present in complete_info or clearly in post_content.
# 2) EXCLUDE the following entirely, even if present:
#    - Street address or neighborhood/address hints
#    - Any phone numbers, emails, links, QR/Dropbox/Drive/MLS URLs
#    - Company names, personal names, contact instructions, “call/text”, showing notes/appointments
#    - Asking price/ARV unless explicitly included as a property feature line (you may omit prices entirely)
#    - Any estimated prices, valuations, “estimated ARV”, “value range”, or price guesses of any kind
# 3) DO include factual FEATURES ONLY, e.g.:
#    - Beds, baths (e.g., “2.5 Baths” if full/half are present), living area, lot size (sq ft/acres), year built
#    - Build material (CBS/concrete block), condition (“clean/outdated”, “needs everything”), unit mix (e.g., “2 units both 2/1”)
#    - Occupancy (vacant/occupied), rents IF explicitly stated; special flags (teardown/redevelopment, assumable financing)
# 4) Format: one fact per line, each wrapped in <p>…</p>. You can use <strong> labels </strong> (e.g., <p><strong>Beds:</strong> 4</p>).
# 5) Keep it concise: usually 6–16 lines; omit unknown/empty values.
# 6) Do not contradict complete_info. If complete_info provides a value, prefer it over raw text.
# 7) Return ONLY a fragment (no <html> or <body>).
# """

# # ---------- User message (now passes complete_info + post_content) ----------
# _USER_TEMPLATE = """\
# COMPLETE_INFO (authoritative structured fields):
# {complete_info_json}

# POST_CONTENT (secondary source; may contain address/links/contact—IGNORE those; use only property features):
# {post_content_str}

# TASK:
# Build the property description HTML fragment per the rules. One fact per line (<p>…</p>), optional <strong> labels.
# Return JSON with:
# - "property_description_html"
# - "notes" (warnings/ambiguities; else empty)
# """

_SYSTEM_PROMPT = """\
You generate a short property description HTML fragment for WordPress from two sources:
(1) complete_info: structured, machine-extracted fields for the property (authoritative)
(2) post_content: human-friendly text used for WhatsApp (secondary; may include address/contact/links/dates)

Rules (NON-NEGOTIABLE):
A) Zero hallucinations:
   - Use ONLY facts that exist in complete_info or are clearly stated in post_content.

B) Exclude entirely (even if present in either source):
   - Street address, neighborhood/address hints, unit number
   - Any phone numbers, emails, URLs/links (MLS/Dropbox/Drive/QR/etc.)
   - Company names, personal names, contact instructions (“call/text”), showing notes, appointments
   - Closing/escrow details: COE, closing date, “close of escrow”, title company name, “closing at…”, escrow amount
   - Any deposit/earnest money/escrow deposit terms or amounts: “deposit”, “deposit amount”, “earnest money”, “EMD”, “escrow deposit”
   - Any “under contract / ready for assignment” language (assignment of contract, ready for assignment, under contract, etc.)
   - Any ARV / After Repair Value, rehab/repair cost, estimate repair costs
   - Any “estimated” rental income
   - Emojis
   - Fluff/opinions: school zones, close to shopping, “great for investors”, “amazing”, “best”, etc.

C) Include ONLY factual property features (omit unknown/empty):
   - Beds, baths, living area, lot size (sq ft and/or acres), year built
   - Construction/material (CBS/concrete block/etc.), condition ONLY if explicitly stated (e.g., “needs updates”, “needs full rehab”)
   - Occupancy (vacant/occupied) if explicitly stated
   - Unit mix if explicitly stated (e.g., duplex 2/1 + 2/1)
   - Rental income ONLY if explicitly stated as actual rent (do NOT include anything labeled “estimated”)
   - Comps ONLY if explicitly stated (never compute/infer comps)

D) Formatting requirements:
   - One fact per line, each wrapped in <p>…</p>
   - You may use <strong> labels </strong> (example: <p><strong>Beds:</strong> 3</p>)
   - Fix obvious spelling/grammar issues in the generated text (professional tone).
   - If you encounter shorthand like “3/2”, render it as “3 Bed / 2 Bath” (never “3/2”).

E) Orange highlighting (WordPress inline style):
   - Wrap ONLY the highlighted value/phrase using EXACTLY:
     <strong><span style="color: #ff6600;">…</span></strong>
   - Do not highlight anything else.

   Highlight in orange when (and only when) explicitly supported by the sources:
   1) Lot size OVER 1 acre:
      - Treat 1 acre as 40,000 sqft.
      - If lot size sqft > 40,000 OR acres > 1.0, highlight the lot size value.
      - If you have both sqft and acres explicitly stated, you may show both (e.g., “43,560 sqft (1.0 acres)”),
        but do not convert or invent missing numbers.
   2) Water features:
      - If text indicates ocean access, waterfront, on lake, lakefront, canal, intracoastal, water view, “waterfront”, etc.,
        highlight the water feature phrase/value.
   3) Extra unit / suite:
      - If “mother-in-law suite”, “efficiency”, “attached 1/1”, “detached 1/1” (or equivalent) is explicitly stated,
        highlight that phrase/value.

F) Special Preferences (MANDATORY INCLUSION):
   - You MUST identify any special preferences or additional information based on keywords in the listing (e.g. price $1M+, 55+ mentioned, wood frame, water/flood damage).
   - If the property matches any condition, you MUST include the exact phrase provided below as a feature line in the final output. Do not skip them if present.

   Exact phrases to use if applicable:
   - "Property Needs a Full Rehab"
   - "Property has Code Violations / Liens / Fines"
   - "Property is Fire Damaged"
   - "Need to Buy Property Sight Unseen (Bad Tenants, Other Access Issues) - Videos or Pictures might be available case by case."
   - "Property with Ocean Access / Intracoastal"
   - "$1 Million Dollar Houses and Up" (Use this if the price is $1,000,000 or more)
   - "55 Plus Communities" (Use this if 55+ or senior living is mentioned)
   - "Frame Construction" (Use this if wood or frame construction is mentioned)
   - "Bulk Property Packages"
   - "Mold Remediation Needed"
   - "Property has Foundation / Structural Issues"
   - "Water/ Flood Damage"
   - "Tear-downs / Land Value Only"
   - "Unpermitted Additions"
   - "Eviction Needed/ In Progress"
   - "Post Occupancy Required (with escrow holdback and/ or rent)"
   - "Pool"
   - "Garage"
   - "40/10 Year Inspection Certificate Failed"
   - "40/10 Year Inspection Certificate Passed"
   - "Property has Rental Restrictions"
   - "Property has Special Assessments"
   - "Located on Ocean Access / Intracoastal Way Only"
   - "Located on Water Front Only"
   - "Located on Golf Course Only"
   - "40 Year Inspection Failed"
   - "NO HOA"
   - "Mobile Homes"

G) Consistency:
   - Do not contradict complete_info. If complete_info has a value, prefer it over post_content.

Output:
Return ONLY JSON with:
- "property_description_html": the HTML fragment (no <html> or <body>)
- "notes": list of warnings/ambiguities or empty list
"""


# ---------- User template (unchanged structure; still good) ----------
_USER_TEMPLATE = """\
COMPLETE_INFO (authoritative structured fields):
{complete_info_json}

POST_CONTENT (secondary source; may contain address/links/contact/dates—IGNORE those; use only property features):
{post_content_str}

TASK:
Build the property description HTML fragment per the rules.
- One fact per line (<p>…</p>), optional <strong> labels.
- Apply orange highlights using <font color="orange">…</font> when rules match.
Return JSON with:
- "property_description_html"
- "notes"
"""

def ai_build_wp_property_description_for_listing(
    complete_info: Dict[str, Any],
    post_content: str = "",
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Generate an HTML fragment for property features only using your existing extracted data.
    - complete_info: the full dict you already store under ParsedListing.complete_info
    - post_content: the WhatsApp text you already generate (may include address/contact—model is told to ignore them)
    """
    msg = _USER_TEMPLATE.format(
        complete_info_json=json.dumps(complete_info or {}, ensure_ascii=False, indent=2),
        post_content_str=(post_content or "")[:2000],  # cap to keep prompts lean
    )

    chat = client.chat.completions.create(
        model=(model or OPENAI_MODEL),
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user",   "content": msg},
        ],
        temperature=0,
        response_format=_response_format(),
    )
    data = json.loads(chat.choices[0].message.content)
    html = (data.get("property_description_html") or "").strip()
    notes = data.get("notes") or []
    return {"property_description_html": html, "notes": notes}

# ---------- Mongo helpers ----------
def ai_build_wp_property_description_by_id(listing_id: str, model: Optional[str] = None) -> Dict[str, Any]:
    try:
        oid = ObjectId(listing_id)
    except Exception:
        return {"error": "invalid_listing_id", "listing_id": listing_id}

    pl = ParsedListing.objects(id=oid).first()
    if not pl:
        return {"error": "listing_not_found", "listing_id": listing_id}

    complete_info = getattr(pl, "complete_info", {}) or {}
    post_content  = getattr(pl, "post_content", "") or ""

    payload = ai_build_wp_property_description_for_listing(
        complete_info=complete_info,
        post_content=post_content,
        model=model,
    )

    # Persist to same doc
    try:
        ParsedListing.objects(id=oid).update_one(set__wp_property_description=payload["property_description_html"])
    except Exception as e:
        return {"error": f"save_failed: {e}", "listing_id": listing_id, "payload": payload}

    return {"ok": True, "listing_id": listing_id, "payload": payload}

def ai_build_wp_property_description_for_posted(
    model: Optional[str] = None,
    *,
    limit: Optional[int] = None,
    skip: int = 0,
    batch_size: int = 25,
    per_item_sleep_s: float = 0.0,
    only_missing: bool = True,  # skip if already present
) -> Dict[str, Any]:
    """
    Build & save HTML descriptions for all ParsedListings with status='posted'.
    Uses complete_info + post_content; saves to wp_property_description.
    """
    q = ParsedListing.objects(wp_status="keys_generated").order_by("+created_at")
    # if only_missing:
    #     q = q.filter(wp_property_description__exists=False)
    if skip:
        q = q.skip(skip)
    if limit is not None:
        q = q.limit(limit)

    processed = 0
    ok = 0
    errors = 0
    results: List[Dict[str, Any]] = []

    batch: List[ParsedListing] = []
    for pl in q:
        batch.append(pl)
        if len(batch) >= batch_size:
            _process_batch(batch, results, model, per_item_sleep_s)
            processed += len(batch)
            batch = []
    if batch:
        _process_batch(batch, results, model, per_item_sleep_s)
        processed += len(batch)

    for r in results:
        if r.get("ok"):
            ok += 1
        else:
            errors += 1

    return {"processed": processed, "ok": ok, "errors": errors, "results": results}

def _process_batch(
    docs: Iterable[ParsedListing],
    results_accum: List[Dict[str, Any]],
    model: Optional[str],
    per_item_sleep_s: float,
) -> None:
    for pl in docs:
        try:
            complete_info = getattr(pl, "complete_info", {}) or {}
            post_content  = getattr(pl, "post_content", "") or ""
            payload = ai_build_wp_property_description_for_listing(
                complete_info=complete_info,
                post_content=post_content,
                model=model,
            )
            ParsedListing.objects(id=pl.id).update_one(
                set__wp_property_description=payload["property_description_html"],
                set__wp_status="des_generated"
            )
            results_accum.append({"id": str(pl.id), "ok": True})
        except Exception as e:
            results_accum.append({"id": str(getattr(pl, "id", "")), "ok": False, "error": f"{type(e).__name__}: {e}"})
        if per_item_sleep_s > 0:
            time.sleep(per_item_sleep_s)
