import json
from typing import Dict, Any, Optional, List
from bson import ObjectId
import os
import re
import logging
import requests
import mimetypes
from openai import OpenAI
from datetime import datetime
from ringcentral_auth import rc_auth_header
from models import ParsedListing, WebFormBuyerSubmission
from buyers.deal_page import create_deal_page

BUYER_NON_TEXT_EMAIL_WEBHOOK_URL = os.getenv("BUYER_NON_TEXT_EMAIL_WEBHOOK_URL", "").strip()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY)

DEFAULT_BUYER_DESC_MODEL = "gpt-4.1"  # or reuse OPENAI_MODEL

BUYER_TEMPLATE_PATH = os.getenv("BUYER_TEMPLATE_PATH", "buyer_notification_templates.json")

_PLACEHOLDER_RE = re.compile(r"{{\s*(\w+)\s*}}")
POF_EMAIL_API_URL = os.getenv(
    "POF_EMAIL_API_URL",
    "http://ec2-3-90-20-111.compute-1.amazonaws.com:8000/rich_ai_deal_Email",
)

RC_SERVER_URL = os.getenv("RC_SERVER_URL", "https://platform.ringcentral.com") 

# Standard RingCentral SMS/MMS endpoint
RC_SMS_URL = f"{RC_SERVER_URL}/restapi/v1.0/account/~/extension/~/sms" 




_SYSTEM_PROMPT_SMS = """\
You generate a SHORT SMS-FRIENDLY property description from two sources:
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
   - Any ARV / After Repair Value, rehab/repair cost, estimated repair costs, repair budgets
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

D) SMS formatting requirements:
   - Output must be a SINGLE line of plain text (no line breaks).
   - Max ~240 characters; be concise, but clear.
   - No HTML tags at all.
   - No emojis, no ALL CAPS shouting.
   - A natural, investor-focused marketing sentence is fine, but keep it factual and simple.

E) Consistency:
   - Do not contradict complete_info. If complete_info has a value, prefer it over post_content.

Output:
Return ONLY JSON with:
- "sms_text": the SMS description text (single line, <= ~240 chars)
- "notes": list of warnings/ambiguities or empty list
"""


_SYSTEM_PROMPT_EMAIL = """\
You generate a SHORT EMAIL-FRIENDLY property description from two sources:
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
   - Any ARV / After Repair Value, rehab/repair cost, estimated repair costs, repair budgets
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

D) Email formatting requirements:
   - Output must be PLAIN TEXT (no HTML).
   - 2–4 short sentences, easy to read in an email body.
   - No greetings, no signature, no call-to-action like “call me” or “reply if interested”.
   - No emojis.
   - Tone: professional, clear, investor-focused.

E) Consistency:
   - Do not contradict complete_info. If complete_info has a value, prefer it over post_content.

Output:
Return ONLY JSON with:
- "email_text": the email description text (2–4 short sentences, plain text)
- "notes": list of warnings/ambiguities or empty list
"""


_USER_TEMPLATE_SMS = """\
COMPLETE_INFO (authoritative structured fields):
{complete_info_json}

POST_CONTENT (secondary source; may contain address/links/contact/dates—IGNORE those; use only property features):
{post_content_str}

TASK:
Build a SHORT SMS-FRIENDLY property description per the rules.
Return JSON with:
- "sms_text"
- "notes"
"""


_USER_TEMPLATE_EMAIL = """\
COMPLETE_INFO (authoritative structured fields):
{complete_info_json}

POST_CONTENT (secondary source; may contain address/links/contact/dates—IGNORE those; use only property features):
{post_content_str}

TASK:
Build a SHORT EMAIL-FRIENDLY property description per the rules.
Return JSON with:
- "email_text"
- "notes"
"""


def ai_build_buyer_sms_description_for_listing(
    complete_info: Dict[str, Any],
    post_content: str = "",
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Generate a short SMS-friendly description from complete_info + post_content.
    Returns: {"sms_text": "...", "notes": [...]}
    """
    msg = _USER_TEMPLATE_SMS.format(
        complete_info_json=json.dumps(complete_info or {}, ensure_ascii=False, indent=2),
        post_content_str=(post_content or "")[:2000],
    )

    chat = client.chat.completions.create(
        model=(model or DEFAULT_BUYER_DESC_MODEL),
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT_SMS},
            {"role": "user",   "content": msg},
        ],
        temperature=0,
        response_format={"type": "json_object"},
    )

    data = json.loads(chat.choices[0].message.content)
    sms_text = (data.get("sms_text") or "").strip()
    notes = data.get("notes") or []
    return {"sms_text": sms_text, "notes": notes}


def ai_build_buyer_email_description_for_listing(
    complete_info: Dict[str, Any],
    post_content: str = "",
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Generate a short email-friendly description from complete_info + post_content.
    Returns: {"email_text": "...", "notes": [...]}
    """
    msg = _USER_TEMPLATE_EMAIL.format(
        complete_info_json=json.dumps(complete_info or {}, ensure_ascii=False, indent=2),
        post_content_str=(post_content or "")[:2000],
    )

    chat = client.chat.completions.create(
        model=(model or DEFAULT_BUYER_DESC_MODEL),
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT_EMAIL},
            {"role": "user",   "content": msg},
        ],
        temperature=0,
        response_format={"type": "json_object"},
    )

    data = json.loads(chat.choices[0].message.content)
    email_text = (data.get("email_text") or "").strip()
    notes = data.get("notes") or []
    return {"email_text": email_text, "notes": notes}


def process_pending_buyer_descriptions(
    limit: int = 20,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """
    1) Pull ParsedListing docs where:
         - buyer_send_status == 'pending'
         - matched_buyer_ids is non-empty
    2) For each listing:
         - Generate SMS + Email descriptions using the strict buyer prompts
         - Save them on listing
         - Set buyer_send_status = 'des_generated'
    """

    q = ParsedListing.objects(
        buyer_send_status="pending",
        matched_buyer_ids__ne=[],
    ).order_by("+created_at").limit(limit)

    listings: List[ParsedListing] = list(q)
    processed = 0
    failures: List[str] = []

    for pl in listings:
        # try:
        #     complete_info = getattr(pl, "complete_info", {}) or {}
        #     post_content = getattr(pl, "post_content", "") or ""

        #     sms_payload = ai_build_buyer_sms_description_for_listing(
        #         complete_info=complete_info,
        #         post_content=post_content,
        #         model=model,
        #     )
        #     email_payload = ai_build_buyer_email_description_for_listing(
        #         complete_info=complete_info,
        #         post_content=post_content,
        #         model=model,
        #     )

        #     sms_text = (sms_payload.get("sms_text") or "").strip()
        #     email_text = (email_payload.get("email_text") or "").strip()

        #     if not sms_text or not email_text:
        #         raise ValueError("Empty SMS or Email text generated")

        #     pl.buyer_sms_description = sms_text
        #     pl.buyer_email_description = email_text
        #     pl.buyer_send_status = "des_generated"
        #     pl.updated_at = datetime.utcnow()
        #     pl.save()

        #     processed += 1

        # except Exception as e:
        #     pl.buyer_send_status = "failed"
        #     pl.updated_at = datetime.utcnow()
        #     pl.save()
        #     failures.append(f"{pl.id}: {e}")

        try:
            complete_info = getattr(pl, "complete_info", {}) or {}
            post_content = getattr(pl, "post_content", "") or ""

            sms_payload = ai_build_buyer_sms_description_for_listing(
                complete_info=complete_info,
                post_content=post_content,
                model=model,
            )
            email_payload = ai_build_buyer_email_description_for_listing(
                complete_info=complete_info,
                post_content=post_content,
                model=model,
            )

            sms_text = (sms_payload.get("sms_text") or "").strip()
            email_text = (email_payload.get("email_text") or "").strip()

            if not sms_text or not email_text:
                raise ValueError("Empty SMS or Email text generated")

            # ✅ Use update_one instead of pl.save()
            ParsedListing.objects(id=pl.id).update_one(
                set__buyer_sms_description=sms_text,
                set__buyer_email_description=email_text,
                set__buyer_send_status="des_generated",
                set__updated_at=datetime.utcnow(),
            )

            processed += 1

        except Exception as e:
            # ❌ Mark as failed using update_one
            ParsedListing.objects(id=pl.id).update_one(
                set__buyer_send_status="failed",
                set__updated_at=datetime.utcnow(),
            )
            failures.append(f"{pl.id}: {e}")


    return {
        "ok": True,
        "processed_count": processed,
        "failed_count": len(failures),
        "failures": failures,
    }



def _render_template(template: str, context: Dict[str, Any]) -> str:
    """
    Simple {{placeholder}} replacement.
    Unknown placeholders -> empty string.
    """

    def _repl(match: re.Match) -> str:
        key = match.group(1)
        val = context.get(key, "")
        return "" if val is None else str(val)

    return _PLACEHOLDER_RE.sub(_repl, template)


SMS_SINGLE_SEGMENT_MAX_CHARS = 160


def _render_sms_body(templates: Dict[str, Any], context: Dict[str, Any]) -> str:
    """
    Prefer the full SMS template when it fits in one segment (<=160 chars).
    Otherwise fall back to body_short.
    """
    sms_templates = templates.get("sms") or {}
    long_template = sms_templates.get("body") or ""
    short_template = sms_templates.get("body_short") or long_template

    long_body = _render_template(long_template, context)
    if len(long_body) <= SMS_SINGLE_SEGMENT_MAX_CHARS:
        return long_body

    return _render_template(short_template, context)


_templates_cache: Optional[Dict[str, Any]] = None


def _load_buyer_templates(path: str = BUYER_TEMPLATE_PATH) -> Dict[str, Any]:
    global _templates_cache
    if _templates_cache is not None:
        return _templates_cache

    with open(path, "r", encoding="utf-8") as f:
        _templates_cache = json.load(f)
    return _templates_cache


def _format_full_address(addr: str, city: str, state: str, zip_code: str) -> str:
    parts: List[str] = []
    if addr:
        parts.append(addr.strip())
    city_state_zip = " ".join(
        p for p in [city.strip() if city else "", state.strip() if state else "", zip_code.strip() if zip_code else ""] 
        if p
    )
    if city_state_zip:
        parts.append(city_state_zip)
    if parts:
        parts.append("USA")
    return ", ".join(parts) if parts else ""




def send_email_to_buyer(to_email: str, subject: str, html_body: str, timeout: int = 20) -> dict:
    """
    Send an email to a buyer using the external /pofEmail API.

    API:
      URL:   http://ec2-3-90-20-111.compute-1.amazonaws.com:8000/pofEmail
      Method: POST
      JSON: { "to": <str>, "subject": <str>, "body": <str> }

    Returns:
      {
        "ok": bool,
        "status_code": int | None,
        "response_text": str | None,
        "error": str | None
      }
    """
    to_email = (to_email or "").strip()
    subject = (subject or "").strip()
    html_body = html_body or ""

    if not to_email:
        return {
            "ok": False,
            "status_code": None,
            "response_text": None,
            "error": "missing_to_email",
        }

    if not subject:
        return {
            "ok": False,
            "status_code": None,
            "response_text": None,
            "error": "missing_subject",
        }

    payload = {
        "to": to_email,
        "subject": subject,
        "body": html_body,
    }

    try:
        resp = requests.post(
            POF_EMAIL_API_URL,
            json=payload,
            timeout=timeout,
        )
        print("resp",resp)
        ok = resp.status_code in (200, 201, 202)

        if not ok:
            logging.warning(
                "send_email_to_buyer: non-2xx response %s body=%s",
                resp.status_code,
                resp.text[:500],
            )

        return {
            "ok": ok,
            "status_code": resp.status_code,
            "response_text": resp.text,
            "error": None if ok else f"non_2xx_status:{resp.status_code}",
        }

    except requests.RequestException as e:
        logging.exception("send_email_to_buyer: request failed")
        return {
            "ok": False,
            "status_code": None,
            "response_text": None,
            "error": f"request_failed:{e}",
        }

def _normalize_phone(num: str) -> str:
    """
    Very light normalization:
    - strip spaces and punctuation-ish chars
    - if it doesn't start with '+', you *may* want to assume +1 (US)
    Adjust logic as needed for your numbers.
    """
    if not num:
        return num

    digits = "".join(ch for ch in num if ch.isdigit() or ch == "+")
    if digits.startswith("+"):
        return digits
    # If you'd rather not assume country, remove this block
    if len(digits) == 10:
        return "+1" + digits
    return digits

def _download_image(image_url: str) -> Optional[Dict[str, Any]]:
    """
    Download the image bytes from a URL so we can attach it as MMS.
    Returns dict suitable for 'files' in requests if successful,
    otherwise None (so caller can fall back to plain SMS).
    """
    try:
        resp = requests.get(image_url, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[RingCentral] Failed to download image for MMS: {e}")
        return None

    content_type = resp.headers.get("Content-Type", "").split(";")[0].strip() or "image/jpeg"
    # Guess an extension; not critical but nice
    ext = mimetypes.guess_extension(content_type) or ".jpg"
    filename = "image" + ext

    return {
        "filename": filename,
        "content": resp.content,
        "content_type": content_type,
    }

def send_sms_to_buyer(
    to_number: str,
    sms_text: str,
    from_number: Optional[str] = None,
    image_url: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Send SMS or MMS (if image_url provided) to a buyer via RingCentral.

    - Uses JWT-based auth via ringcentralauth.get_access_token / rc_auth_header().
    - If image_url is provided and we can download it, sends MMS with one attachment.
    - Otherwise sends plain SMS.
    - Returns a dict with basic info and any error message.
    """

    if not to_number:
        return {"ok": False, "error": "missing_to_number"}

    if not sms_text or not sms_text.strip():
        return {"ok": False, "error": "empty_sms_text"}

    # Allow from_number override via env
    from_number = from_number or os.getenv("RC_FROM_NUMBER")
    if not from_number:
        return {"ok": False, "error": "missing_from_number_or_RC_FROM_NUMBER_env"}

    to_norm = _normalize_phone(to_number)
    from_norm = _normalize_phone(from_number)

    # Base JSON payload used for both SMS and MMS
    body_json = {
        "from": {"phoneNumber": from_norm},
        "to": [{"phoneNumber": to_norm}],
        "text": sms_text,
    }

    headers = rc_auth_header()  # {"Authorization": "Bearer <token>"}

    # ---------- Try MMS if image_url is provided ----------
    if image_url:
        img = _download_image(image_url)
        if img:
            try:
                files = {
                    # The JSON part of the MMS request
                    "json": (None, json.dumps(body_json), "application/json"),
                    # The binary attachment
                    "attachment": (img["filename"], img["content"], img["content_type"]),
                }

                resp = requests.post(
                    RC_SMS_URL,
                    headers=headers,  # do NOT set Content-Type; requests will handle multipart
                    files=files,
                    timeout=20,
                )
                print("resp",resp.json())
                ok = resp.status_code in (200, 201, 202)
                if not ok:
                    return {
                        "ok": False,
                        "error": f"mms_non_2xx:{resp.status_code}",
                        "response_text": resp.text[:500],
                    }

                return {
                    "ok": True,
                    "mode": "mms",
                    "status_code": resp.status_code,
                    "response": resp.json(),
                }
            except requests.RequestException as e:
                print(f"[RingCentral] MMS send failed, will fall back to SMS: {e}")
                # fall through to SMS

    # ---------- Plain SMS fallback ----------
    try:
        sms_headers = {
            **headers,
            "Content-Type": "application/json",
        }
        resp = requests.post(
            RC_SMS_URL,
            headers=sms_headers,
            json=body_json,
            timeout=20,
        )
        ok = resp.status_code in (200, 201, 202)
        if not ok:
            return {
                "ok": False,
                "error": f"sms_non_2xx:{resp.status_code}",
                "response_text": resp.text[:500],
            }

        return {
            "ok": True,
            "mode": "sms",
            "status_code": resp.status_code,
            "response": resp.json(),
        }

    except requests.RequestException as e:
        return {
            "ok": False,
            "error": f"sms_request_failed:{e}",
        }
def _to_jsonable(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, ObjectId):
        return str(obj)
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v) for v in obj]
    return obj

def _serialize_listing_for_webhook(pl: ParsedListing) -> Dict[str, Any]:
    raw = pl.to_mongo().to_dict()
    return _to_jsonable(raw)

def _serialize_buyer_for_webhook(buyer: WebFormBuyerSubmission) -> Dict[str, Any]:
    raw = buyer.to_mongo().to_dict()
    return _to_jsonable(raw)

def _send_non_text_email_buyer_to_webhook(
    buyer: WebFormBuyerSubmission,
    pl: ParsedListing,
    reason: str = "buyer_preferences_not_text_or_email",
) -> Dict[str, Any]:
    if not BUYER_NON_TEXT_EMAIL_WEBHOOK_URL:
        print("[buyer_webhook] BUYER_NON_TEXT_EMAIL_WEBHOOK_URL not set; skipping webhook send.")
        return {"ok": False, "reason": "no_webhook_url"}

    payload = {
        "reason": reason,
        "matched_buyer_id": str(buyer.id),
        "buyer": _serialize_buyer_for_webhook(buyer),
        "listing_id": str(pl.id),
        "listing": _serialize_listing_for_webhook(pl),
    }

    try:
        resp = requests.post(
            BUYER_NON_TEXT_EMAIL_WEBHOOK_URL,
            json=payload,
            timeout=15,
        )
        ok = resp.status_code in (200, 201, 202)
        if not ok:
            print(f"[buyer_webhook] non-2xx: {resp.status_code}, body={resp.text[:300]}")
        return {
            "ok": ok,
            "status_code": resp.status_code,
            "body": resp.text[:300],
        }
    except requests.RequestException as e:
        print(f"[buyer_webhook] request failed: {e}")
        return {"ok": False, "error": str(e)}

RINGCENTRAL_MMS_MAX_BYTES = 1_500_000  # RingCentral documented MMS attachment limit

def _get_remote_file_size_bytes(url: str, timeout: int = 10) -> Optional[int]:
    """
    Return remote file size in bytes.

    Strategy:
    1) Try HEAD and read Content-Length
    2) Fallback to streaming GET and count bytes up to the limit

    Returns:
        int  -> detected size in bytes
        None -> could not determine size
    """
    if not url:
        return None

    # First try HEAD
    try:
        resp = requests.head(url, allow_redirects=True, timeout=timeout)
        content_length = resp.headers.get("Content-Length")
        if content_length and content_length.isdigit():
            return int(content_length)
    except requests.RequestException:
        pass

    # Fallback: stream the file and count bytes
    try:
        with requests.get(url, stream=True, allow_redirects=True, timeout=timeout) as resp:
            resp.raise_for_status()

            content_length = resp.headers.get("Content-Length")
            if content_length and content_length.isdigit():
                return int(content_length)

            total = 0
            for chunk in resp.iter_content(chunk_size=8192):
                if not chunk:
                    continue
                total += len(chunk)

                # Early exit once we know it's too large
                if total > RINGCENTRAL_MMS_MAX_BYTES:
                    return total
            print("total size",total)
            return total

    except requests.RequestException:
        return None

def process_buyer_sends(limit: int = 10) -> Dict[str, Any]:
    """
    1) Pull ParsedListing where:
         - buyer_send_status == 'des_generated'
         - matched_buyer_ids is not empty
    2) For each listing:
         - Build property context (address, price, descriptions, pics, first image).
         - For each matched buyer:
              * Read BuyerContact.preference (whatsapp/call/sms/email)
              * If sms -> render SMS body and call your SMS sender.
              * If email -> render Email HTML and call your email sender.
    3) Update listing.buyer_send_status -> 'sent' or 'send_failed'.
    """

    templates = _load_buyer_templates()
    email_subject = templates["email"].get("subject", "New deal for your review")
    email_template = templates["email"]["html"]

    # listings = ParsedListing.objects(
    #     buyer_send_status="des_generated",
    #     matched_buyer_ids__ne=[]
    # ).limit(limit)

    listings = (
        ParsedListing.objects(buyer_send_status="des_generated")
        .filter(__raw__={
            "$or": [
                {"rematch": True, "re_matched_buyer_ids": {"$ne": []}},
                {"$or": [{"rematch": False}, {"rematch": {"$exists": False}}], "matched_buyer_ids": {"$ne": []}},
            ]
        })
        .limit(limit)
    )

    print("listings",listings)

    processed = 0
    failures: List[str] = []

    for pl in listings:
        try:
            # ---- property-level context ----
            addr = getattr(pl, "address", "") or ""
            city = getattr(pl, "city", "") or ""
            state = getattr(pl, "state", "") or ""
            zip_code = getattr(pl, "zip", "") or ""

            full_address = _format_full_address(addr, city, state, zip_code)

            # Descriptions we already generated earlier
            sms_desc = (getattr(pl, "buyer_sms_description", "") or "").strip()
            email_desc = (getattr(pl, "buyer_email_description", "") or "").strip()

            price_val = getattr(pl, "price", None)
            if isinstance(price_val, (int, float)):
                price_str = f"${price_val:,.0f}"
            else:
                price_str = ""

            pics_link = getattr(pl, "other_images_dropbox_link", None) or ""

            # ---------- NEW: image suppression for Do_Not_Post / quota ----------
            # If listing is a Do Not Post City or over 35% quota, do NOT include images
            over_35_flag = (getattr(pl, "over_35_percent", "") or "").strip().lower()
            dnp_city_flag = (getattr(pl, "do_not_post_city", "") or "").strip().lower()
            skip_images_for_buyer = (over_35_flag == "found") or (dnp_city_flag == "found")
            # --------------------------------------------------------------------

            images = getattr(pl, "images", None) or []

            if skip_images_for_buyer:
                # For these special cases, we force no image regardless of what we have
                first_image_url = ""
            else:
                first_image_url = images[0] if images else ""

            # first_image_url = images[0] if images else ""

            image_block_html = ""
            if first_image_url:
                image_block_html = (
                    f'<p><img src="{first_image_url}" '
                    f'alt="Property photo" style="max-width:100%;height:auto;" /></p>'
                )

            # --- NEW: conditional pics blocks ---
            if pics_link:
                pics_block_sms = f"\nLink to Pics: {pics_link}"
                pics_block_email = (
                    f'<p><strong>Link to Pics:</strong> '
                    f'<a href="{pics_link}">{pics_link}</a></p>'
                )
            else:
                pics_block_sms = ""
                pics_block_email = ""

            # ---- load matched buyers ----
            # buyer_ids = [ObjectId(bid) for bid in (pl.matched_buyer_ids or []) if bid]
            # if not buyer_ids:
            #     raise ValueError("No matched_buyer_ids present")

            source_ids = (pl.re_matched_buyer_ids if getattr(pl, "rematch", False) else pl.matched_buyer_ids) or []
            buyer_ids = [ObjectId(bid) for bid in source_ids if bid]
            if not buyer_ids:
                raise ValueError("No buyer ids present (matched or re-matched)")


            buyers = list(WebFormBuyerSubmission.objects(id__in=buyer_ids))
            print("buyers",buyers)

            # Determine once per listing whether MMS image is allowed
            sms_image_url = first_image_url
            sms_image_size_bytes = None

            if first_image_url:
                sms_image_size_bytes = _get_remote_file_size_bytes(first_image_url)

                if sms_image_size_bytes is None:
                    # Conservative behavior: if size cannot be determined, send text only
                    logging.warning(
                        "Could not determine image size for listing %s image %s. Sending text-only SMS.",
                        pl.id,
                        first_image_url,
                    )
                    sms_image_url = None

                elif sms_image_size_bytes > RINGCENTRAL_MMS_MAX_BYTES:
                    logging.info(
                        "Image too large for MMS for listing %s: %s bytes > %s. Sending text-only SMS.",
                        pl.id,
                        sms_image_size_bytes,
                        RINGCENTRAL_MMS_MAX_BYTES,
                    )
                    sms_image_url = None

            for buyer in buyers:
                contact = buyer.contact
                full_name = (contact.name or "").strip()
                first_name = full_name.split()[0] if full_name else "Investor"

                # pref = getattr(contact, "preference", None) or "sms"
                # pref = pref.lower().strip()

                prefs_raw = getattr(contact, "preferences", []) or []
                # normalize to lowercase
                prefs = [p.lower().strip() for p in prefs_raw if p]

                print("prefs", prefs)

                can_text = ("text" in prefs) and bool(contact.text_number)
                can_email = ("email" in prefs) and bool(contact.email)

                # Base context for both SMS and Email
                base_ctx = {
                    "first_name": first_name,
                    "address": full_address,
                    "price": price_str,
                    "pics_link": pics_link or "",
                    "pics_block": "",  
                    "image_url": first_image_url,
                    "image_block": image_block_html,
                }

                # ---- If neither text nor email preference is usable -> send webhook ----
                if not can_text and not can_email:
                    webhook_result = _send_non_text_email_buyer_to_webhook(
                        buyer=buyer,
                        pl=pl,
                        reason="buyer_preferences_not_text_or_email",
                    )
                    print("webhook_result", webhook_result)

                # ---- SMS send ----
                if can_text:
                    ctx_sms = dict(base_ctx)
                    ctx_sms["description"] = sms_desc or email_desc or ""
                    ctx_sms["pics_block"] = pics_block_sms

                    deal_url = create_deal_page(pl, buyer, ctx_sms)
                    ctx_sms["deal_url"] = deal_url

                    sms_body = _render_sms_body(templates, ctx_sms)

                    print("sms_body", sms_body)

                    result = send_sms_to_buyer(
                        to_number=contact.text_number,
                        sms_text=sms_body,
                        from_number="+17542001204",
                        image_url=sms_image_url,
                    )
                    

                    # TODO: plug your actual SMS sender here
                    # send_sms_to_buyer(contact.text_number, sms_body)
                    # Example:
                    # send_sms(contact.text_number, sms_body)

                # ---- Email send ----
                if can_email:
                    ctx_email = dict(base_ctx)
                    ctx_email["description"] = email_desc or sms_desc or ""
                    ctx_email["pics_block"] = pics_block_email
                    html_body = _render_template(email_template, ctx_email)

                    print("html_body",html_body)

                    email_result = send_email_to_buyer(
                        to_email=contact.email,
                        subject=email_subject,
                        html_body=html_body,
                    )
                    print("email_result",email_result)

                    if not email_result.get("ok"):
                        # handle/log failure if you want
                        logging.warning("Buyer email failed for %s: %s", contact.email, email_result)

                    # TODO: plug your actual Email sender here
                    # send_email_to_buyer(contact.email, email_subject, html_body)
                    # Example external API call:
                    # send_email_via_api(to=contact.email, subject=email_subject, html=html_body)

                # # For now, we skip "whatsapp" and "call" – can be implemented later.
                # else:
                #     # e.g., log or ignore
                #     pass

            # If we reached here without raising, mark listing as sent
            ParsedListing.objects(id=pl.id).update_one(
                set__buyer_send_status="sent",
                set__updated_at=datetime.utcnow(),
            )
            processed += 1

        except Exception as e:
            print("Exception",e)
            # ParsedListing.objects(id=pl.id).update_one(
            #     set__buyer_send_status="send_failed",
            #     set__updated_at=datetime.utcnow(),
            # )
            failures.append(f"{pl.id}: {e}")

    return {
        "ok": True,
        "processed": processed,
        "failed": failures,
    }

