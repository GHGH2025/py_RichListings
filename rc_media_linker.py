# rc_media_linker.py
import os
import re
import json
import time
from typing import Dict, Any, List, Optional, Tuple

import requests
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from openai import OpenAI

from models import RCMediaLinkLog  # NEW

# at top of rc_media_linker.py imports
from datetime import datetime, timedelta, timezone

from ringcentral_auth import rc_request, RC_API_BASE


# === ENV ===
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

WP_TOKEN = os.getenv("WP_API_TOKEN")
WP_GET_URL  = "https://inventory.joinbuyerslist.com/wp-json/addproperty/v1/getproperty"
WP_POST_URL = "https://inventory.joinbuyerslist.com/wp-json/addproperty/v1/create"
REQUEST_TIMEOUT = 25

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY missing in .env")
if not WP_TOKEN:
    raise RuntimeError("WP_API_TOKEN missing in .env")

# Dropbox helpers you already have
from dropboxImageUpload import handle_Link
from post_selection import _slugify_for_folder

# ---------- FastAPI router ----------
router = APIRouter(prefix="/rc", tags=["ringcentral-media-linker"])

# ---------- OpenAI client ----------
oai = OpenAI(api_key=OPENAI_API_KEY)

# ---------- Models ----------
class RCMsg(BaseModel):
    id: str
    conversation_id: str
    direction: str  # Inbound / Outbound
    from_phone: Optional[str] = ""
    to_phones: List[str] = []
    text: str
    creation_time: str
    raw: Dict[str, Any] = {}

# ---------- RC helpers ----------

def _iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

# add this helper near other RC helpers
def _attachment_text(rec: Dict[str, Any]) -> Optional[str]:
    """
    Fetch text from the 'text/plain' attachment when subject is missing/truncated.
    """
    for att in rec.get("attachments", []) or []:
        if att.get("type") == "Text" and str(att.get("contentType", "")).startswith("text/"):
            uri = att.get("uri")
            if uri:
                resp = rc_request("GET", uri, timeout=15)
                if resp.status_code == 200:
                    # RingCentral returns raw utf-8 text here
                    return (resp.text or "").strip()
    return None


def rc_list_messages(params: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{RC_API_BASE}/account/~/extension/~/message-store"
    r = rc_request("GET", url, params=params, timeout=30)  # auto-refresh on 401
    r.raise_for_status()
    # print("r.json()===list messages===", r.json())
    return r.json()

# --- replace the whole fetch_conversation() with this ---
from urllib.parse import urlparse, parse_qs

def fetch_conversation(conversation_id: str, per_page: int = 100, max_pages: int = 10) -> List[RCMsg]:
    """
    Fetch ALL SMS in a conversation using RingCentral's pageToken pagination.
    We follow navigation.nextPage.uri -> pageToken until exhausted or max_pages reached.
    """

    lookback_days = int(os.getenv("RC_LOOKBACK_DAYS", "30"))  # env-tunable

    all_records: List[Dict[str, Any]] = []
    params: Dict[str, Any] = {
        "messageType": "SMS",
        "conversationId": conversation_id,
        "perPage": min(per_page, 100),
        "dateFrom": _iso_utc(datetime.now(timezone.utc) - timedelta(days=lookback_days)),
    }

    pages = 0
    while pages < max_pages:
        data = rc_list_messages(params)
        recs = data.get("records", []) or []
        if not recs:
            break
        all_records.extend(recs)

        # follow nextPage.pageToken if present
        nav = (data.get("navigation") or {}).get("nextPage") or {}
        next_uri = nav.get("uri") or ""
        if not next_uri:
            break

        qs = parse_qs(urlparse(next_uri).query)
        page_token = (qs.get("pageToken") or [None])[0]
        if not page_token:
            break

        # prepare next call with the pageToken
        params = {
            "messageType": "SMS",
            "conversationId": conversation_id,
            "perPage": min(per_page, 100),
            "pageToken": page_token,
        }
        pages += 1

    out: List[RCMsg] = []
    for rec in all_records:
        text = (rec.get("subject") or "").strip()
        if not text:
            att_txt = _attachment_text(rec)
            if att_txt:
                text = att_txt

        out.append(RCMsg(
            id=str(rec.get("id")),
            conversation_id=str(rec.get("conversationId") or conversation_id),
            direction=rec.get("direction") or "",
            from_phone=((rec.get("from") or {}).get("phoneNumber") or ""),
            to_phones=[t.get("phoneNumber") for t in (rec.get("to") or []) if t.get("phoneNumber")],
            text=text,
            creation_time=rec.get("creationTime") or rec.get("lastModifiedTime") or "",
            raw=rec
        ))


    # chronological ascending by ISO8601 (safe to sort lexicographically)
    out.sort(key=lambda m: m.creation_time or "")
    return out

# ---------- Utilities ----------
_URL_RE = re.compile(r'https?://\S+', re.IGNORECASE)

def last_k(messages: List[RCMsg], k: int = 10) -> List[RCMsg]:
    return messages[-k:] if len(messages) > k else messages

def as_ai_dialog(messages: List[RCMsg]) -> List[Dict[str, str]]:
    """
    Normalize to a simple dialog the model can grok well:
    [{speaker: "buyer"|"me", text: "..."}]
    We rely on RC 'direction' to map: Inbound -> buyer, Outbound -> me.
    """
    dialog = []
    for m in messages:
        speaker = "buyer" if (m.direction or "").lower() == "inbound" else "me"
        dialog.append({"speaker": speaker, "text": m.text})
    return dialog

def wp_get(address_key: str) -> Optional[Dict[str, Any]]:
    try:
        r = requests.get(WP_GET_URL, params={"address": address_key, "token": WP_TOKEN}, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None

def wp_first(js: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(js, dict):
        return None
    data = js.get("data")
    if isinstance(data, list) and data:
        return data[0] if isinstance(data[0], dict) else None
    return None

def wp_post_update(body: Dict[str, Any]) -> Optional[int]:
    """WP create endpoint also updates. Return post_id on success."""
    try:
        r = requests.post(WP_POST_URL, json=body, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            return None
        data = r.json()
        if isinstance(data, dict):
            if isinstance(data.get("post_id"), int):
                return data["post_id"]
            d2 = data.get("data")
            if isinstance(d2, dict) and isinstance(d2.get("post_id"), int):
                return d2["post_id"]
        return None
    except Exception:
        return None

# ---------- AI prompts ----------
_EXTRACT_SYS = (
    "You extract property-related info from a short dialog between a buyer and me.\n"
    "Return STRICT JSON with keys: url, street_number, address, confidences.\n"
    "Rules:\n"
    "- url: a single best link that points to photos/media (Dropbox/Google Drive/etc). If multiple links, pick the one most likely to be the PHOTOS link for the property mentioned nearest in messages.\n"
    "- street_number: numeric house number if present (e.g., '5840'). If multiple appear, pick the one closest to the selected url.\n"
    "- address: the best full/partial address string tied to that street_number (if any). If not available, return null.\n"
    "- confidences: object with url, street_number, address in 0..1.\n"
    "Consider proximity: if two properties are mentioned, pick the one mentioned just before the url (closest in time).\n"
    "Do not invent addresses. If unsure, leave null and lower confidence."
)

def ai_extract(dialog: List[Dict[str, str]], model: Optional[str] = None) -> Dict[str, Any]:
    """Send last 9–10 messages in normalized form to AI to extract url/street/address."""
    model = model or OPENAI_MODEL
    user_payload = {"dialog": dialog}
    chat = oai.chat.completions.create(
        model=model,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _EXTRACT_SYS},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
    )
    raw = chat.choices[0].message.content
    try:
        data = json.loads(raw)
    except Exception:
        data = {"url": None, "street_number": None, "address": None, "confidences": {"url": 0.0, "street_number": 0.0, "address": 0.0}}
    # safety normalization
    def _s(x): 
        return (x or "").strip() if isinstance(x, str) else x
    data["url"] = _s(data.get("url"))
    data["street_number"] = _s(data.get("street_number"))
    data["address"] = _s(data.get("address"))
    return data

# Optional secondary AI to verify WP match (strict, address-only)
_VERIFY_SYS = """You verify whether the target address/name likely refers to the SAME property as a WP record.

Return ONLY JSON: {"match": true|false, "confidence": 0..1}

Heuristics:
- House number must match exactly unless target has a masked pattern like 137XX that aligns.
- Street core (name) must match allowing minor typos or Ave/Avenue style differences.
- City/state/ZIP can be missing or slightly varied; unit numbers may differ.
- If target is partial (only number or number+street), be conservative but allow a match if it's clearly the same.
"""

def ai_verify_same(target: str, wp_address: str, model: Optional[str] = None) -> Dict[str, Any]:
    model = model or "gpt-4o-mini"
    u = f"TARGET:\n{target or ''}\n\nWP_ADDRESS:\n{wp_address or ''}\n\nReturn ONLY JSON."
    try:
        chat = oai.chat.completions.create(
            model=model, temperature=0, response_format={"type": "json_object"},
            messages=[{"role": "system", "content": _VERIFY_SYS}, {"role": "user", "content": u}]
        )
        return json.loads(chat.choices[0].message.content)
    except Exception:
        return {"match": False, "confidence": 0.0}

# ---------- Core workflow ----------
def resolve_wp_listing(street_number: Optional[str], addr: Optional[str]) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """
    Search order:
      1) Street number (if provided) → WP GET (may return >1).
         If >1, verify with addr using AI and pick best match (>=0.8).
      2) If nothing via street number, search by addr (full/partial string).
    Returns (wp_item, debug)
    """
    debug: Dict[str, Any] = {"searches": []}

    def _try_key(k: str) -> Optional[List[Dict[str, Any]]]:
        js = wp_get(k)
        debug["searches"].append({"key": k, "raw_success": bool(js and js.get("success")), "count": (js or {}).get("results_count")})
        if not js or not js.get("success"):
            return None
        return js.get("data") or []

    # 1) Street number
    if street_number:
        candidates = _try_key(street_number) or []
        if len(candidates) == 1:
            return candidates[0], debug
        elif len(candidates) > 1 and addr:
            # pick by AI
            scored = []
            for c in candidates:
                wp_addr = (c.get("address") or "").strip()
                ver = ai_verify_same(addr, wp_addr)
                scored.append((ver.get("confidence", 0.0), ver.get("match", False), c))
            scored.sort(key=lambda t: t[0], reverse=True)
            if scored and scored[0][1] and scored[0][0] >= 0.80:
                return scored[0][2], debug

    # 2) Fallback to address search
    if addr:
        candidates = _try_key(addr) or []
        if candidates:
            # If multiple, still pick first (address string is already close)
            return candidates[0], debug

    return None, debug

def ensure_dropbox_link(source_url: str, folder_hint: str) -> Optional[str]:
    """
    Use your existing handle_Link to normalize/host the media.
    Returns a single folder shared link (first of list) or the original URL if nothing produced.
    """
    folder_slug = _slugify_for_folder(folder_hint, fallback="Property")
    links = handle_Link([source_url], folder=folder_slug)  # usually returns one folder link
    if links:
        return links[0]
    return source_url  # fallback: keep original if Dropbox processing yielded nothing


def _extract_conversation_id(payload: dict) -> Optional[str]:
    """Handle all common RC webhook shapes for conversation id."""
    def _get(d, path):
        cur = d
        for k in path:
            if isinstance(cur, dict) and k in cur:
                cur = cur[k]
            else:
                return None
        return cur

    # Common shapes
    candidates = [
        _get(payload, ["body", "conversation", "id"]),
        _get(payload, ["body", "conversationId"]),
        _get(payload, ["conversation", "id"]),
        payload.get("conversationId"),
    ]

    # Fallback: shallow recursive scan for any {"conversation": {"id": ...}}
    if not any(candidates):
        stack = [payload]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                conv = node.get("conversation")
                if isinstance(conv, dict) and "id" in conv:
                    candidates.append(conv["id"])
                    break
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)

    for c in candidates:
        if c is not None:
            return str(c)
    return None


# Only pick the newest address and link helpers

# ---------- Latest-pair extractor ----------
RC_LATEST_BACK = int(os.getenv("RC_LATEST_WINDOW_BACK", "3"))  # how many msgs to look back from a URL

def _strip_urls(text: str) -> str:
    return _URL_RE.sub(" ", text or "")

_addr_pat = re.compile(r"(\b\d{1,6}\s+[A-Za-z0-9][A-Za-z0-9 .#\-]{2,80})")

def _nearest_addr_before(messages: List[RCMsg], url_idx: int) -> Tuple[Optional[str], Optional[str]]:
    """
    Search from url_idx down to max(0, url_idx - RC_LATEST_BACK) for the closest
    address-like text. If none, try a bare street number (e.g., '1233 pic').
    Returns (street_number, address) where either can be None.
    """
    street = None
    addr = None
    start = max(0, url_idx - RC_LATEST_BACK)
    for j in range(url_idx, start - 1, -1):
        t = _strip_urls(messages[j].text or "")
        # Prefer full address (starts with number)
        m_addr = _addr_pat.search(t)
        if m_addr:
            addr = m_addr.group(1).strip()
            m_num = re.match(r"\s*(\d{1,6})\b", addr)
            if m_num:
                street = m_num.group(1)
            break
        # Otherwise accept a bare street number (e.g., "1233 pic")
        if street is None:
            m_num_only = re.search(r"\b(\d{3,6})\b", t)
            if m_num_only:
                street = m_num_only.group(1)
                # keep scanning a bit more to see if an address shows up closer
                # but if not, street alone is still good
    return street, addr

def _find_url_indexes(messages: List[RCMsg]) -> List[Tuple[int, str]]:
    idxs = []
    for i, m in enumerate(messages):
        for u in _URL_RE.findall(m.text or ""):
            idxs.append((i, u.rstrip(").,")))
    return idxs

def _find_first_index_of_url(messages: List[RCMsg], url: str) -> int:
    if not url:
        return -1
    for i, m in enumerate(messages):
        if (m.text or "").find(url) != -1:
            return i
    return -1

def extract_latest_url_addr_pair(messages: List[RCMsg]) -> Optional[Tuple[int, str, Optional[str], Optional[str]]]:
    """
    Returns the latest (by message order) candidate as (idx, url, street, addr).
    """
    latest = None
    for i, url in _find_url_indexes(messages):
        street, addr = _nearest_addr_before(messages, i)
        latest = (i, url, street, addr)  # overwrite -> newest wins
    return latest


# helpers for logging in mongo
def _safe_log_run(
    *,
    conversation_id: str,
    status: str,
    reason: str = "",
    error: str = "",
    url: str = "",
    street: str = "",
    addr: str = "",
    wp_item: Optional[Dict[str, Any]] = None,
    ai_extract: Optional[Dict[str, Any]] = None,
    search_debug: Optional[Dict[str, Any]] = None,
    recent_dialog: Optional[List[Dict[str, str]]] = None,
    message_count_considered: Optional[int] = None,
    last_message_time_iso: Optional[str] = None,
    new_picture_button_url: Optional[str] = None,
) -> None:
    """
    Fire-and-forget log writer. Never raises.
    """
    try:
        doc = RCMediaLinkLog(
            conversation_id=conversation_id,
            last_message_time_iso=last_message_time_iso,
            message_count_considered=message_count_considered or 0,
            selected_url=url or None,
            selected_street_number=street or None,
            selected_address=addr or None,
            status=status,
            reason=(reason or None),
            error=(error or None),
            ai_extract=ai_extract or None,
            search_debug=search_debug or None,
            recent_dialog=recent_dialog or None,
        )
        if wp_item:
            doc.wp_post_id            = wp_item.get("post_id")
            doc.wp_address            = (wp_item.get("address") or "").strip() or None
            doc.wp_old_picture_button = (wp_item.get("picture_button_url") or "").strip() or None
        if new_picture_button_url:
            doc.wp_new_picture_button = new_picture_button_url
        doc.save()  # don't re-raise on failure
    except Exception as e:
        # keep absolutely silent to avoid breaking the endpoint
        print(f"[RCMediaLinkLog] save failed: {type(e).__name__}: {e}")
 

# ---------- Endpoint ----------
@router.post("/webhook/media-link")
async def rc_media_linker(request: Request) -> Dict[str, Any]:
    """
    Receives inbound/outbound webhook payload, finds its conversationId, looks up recent messages,
    AI-extracts (url, street_number, address), finds the WP listing, and fills picture_button_url if empty.
    """
    payload = await request.json()
    # conversation id can appear in several places per your samples
    conv_id = _extract_conversation_id(payload)

    if not conv_id:
        raise HTTPException(400, "conversationId not found in payload")


    # 1) fetch full thread; keep last 10
    thread = fetch_conversation(conv_id, per_page=100, max_pages=10)
    if not thread:
        raise HTTPException(404, f"No messages for conversation {conv_id}")
    recent = last_k(thread, k=10)

    print("recent========10 messages=======",recent)
    dialog = as_ai_dialog(recent)

    last_time_iso = recent[-1].creation_time if recent else ""


    # 2) AI extract
    extract = ai_extract(dialog)
    url = (extract.get("url") or "").strip()
    street = (extract.get("street_number") or "").strip()
    addr = (extract.get("address") or "").strip()

    # If AI found nothing, try last-10 heuristic
    if not url:
        for m in reversed(recent):
            urls = _URL_RE.findall(m.text or "")
            if urls:
                url = urls[-1].rstrip(").,")
                break

    if not (street or addr):
        for m in reversed(recent):
            t = m.text or ""
            n = re.search(r"\b(\d{3,6})\b", t)  # street number
            if n and not street:
                street = n.group(1)
            a = re.search(r"\b(\d{3,6}\s+[A-Za-z0-9 .#\-]{3,80})", t)  # quick address snippet
            if a and not addr:
                cand = a.group(1).strip()
                addr = re.split(r"[,\n]|https?://", cand)[0].strip()
            if url and (street or addr):
                break

    ## --- Prefer the newest URL/address pair found deterministically ---
    cand = extract_latest_url_addr_pair(recent)
    if cand:
        cand_idx, cand_url, cand_street, cand_addr = cand
        ai_idx = _find_first_index_of_url(recent, url)

        # Prefer a strictly later candidate, and only if it found some address info
        if (ai_idx == -1 or cand_idx > ai_idx) and (cand_street or cand_addr):
            url = cand_url
            if cand_street:
                street = cand_street
            if cand_addr:
                addr = cand_addr




    print("ai url====",url,"  street==", street,"  address==",addr)

    # Minimal gating: (street OR address_with_streetnum) AND url
    if not url:
        _safe_log_run(
        conversation_id=conv_id, status="no_action",
        reason="No URL found by AI",
        url=url, street=street, addr=addr,
        ai_extract=extract, recent_dialog=dialog,
        message_count_considered=len(recent),
        last_message_time_iso=last_time_iso,
    )
        return {
            "conversation_id": conv_id,
            "status": "no_action",
            "reason": "No URL found by AI",
            "ai_extract": extract,
            "debug": {"message_count_considered": len(recent)},
        }

    # If AI didn’t fill street but address contains a leading number, try to capture it
    if not street and addr:
        m = re.match(r"\s*(\d{1,6})\b", addr)
        if m:
            street = m.group(1)

    if not (street or addr):
        _safe_log_run(
        conversation_id=conv_id, status="no_action",
        reason="No street number or address found",
        url=url, street=street, addr=addr,
        ai_extract=extract, recent_dialog=dialog,
        message_count_considered=len(recent),
        last_message_time_iso=last_time_iso,
    )
        return {
            "conversation_id": conv_id,
            "status": "no_action",
            "reason": "No street number or address found",
            "ai_extract": extract,
            "debug": {"message_count_considered": len(recent)},
        }

    # 3) find WP listing
    wp_item, search_debug = resolve_wp_listing(street, addr)

    print("wp_item=======",wp_item,"   search_debug======",search_debug)
    if not wp_item:
        _safe_log_run(
        conversation_id=conv_id, status="not_found_in_wp",
        url=url, street=street, addr=addr,
        ai_extract=extract, search_debug=search_debug,
        recent_dialog=dialog,
        message_count_considered=len(recent),
        last_message_time_iso=last_time_iso,
    )
        return {
            "conversation_id": conv_id,
            "status": "not_found_in_wp",
            "ai_extract": extract,
            "debug": {"search": search_debug},
        }

    wp_address = (wp_item.get("address") or "").strip()
    picture_button_url = (wp_item.get("picture_button_url") or "").strip()
    posttitle = wp_item.get("posttitle")

    print("wp_address====",wp_address,"  picture_button_url=====", picture_button_url,"  posttitle=====",posttitle)


    # 4) if already has link → stop
    if picture_button_url:
        _safe_log_run(
        conversation_id=conv_id, status="already_has_picture_button_url",
        url=url, street=street, addr=addr,
        wp_item=wp_item, ai_extract=extract,
        recent_dialog=dialog,
        message_count_considered=len(recent),
        last_message_time_iso=last_time_iso,
    )
        return {
            "conversation_id": conv_id,
            "status": "already_has_picture_button_url",
            "ai_extract": extract,
            "wp_item": {"post_id": wp_item.get("post_id"), "address": wp_address, "picture_button_url": picture_button_url},
        }

    # 5) upload/normalize to Dropbox (folder from address if possible; else street or conversation id)
    folder_hint = wp_address or addr or (street or f"conv_{conv_id}")

    print("folder_hint========",folder_hint)
    try:
        final_link = ensure_dropbox_link(url, folder_hint=folder_hint)
        print("final_link========",final_link)

    except Exception as e:
        _safe_log_run(
        conversation_id=conv_id, status="dropbox_error",
        reason="Dropbox processing failed",
        error=f"{type(e).__name__}: {e}",
        url=url, street=street, addr=addr,
        wp_item=wp_item, ai_extract=extract,
        recent_dialog=dialog,
        message_count_considered=len(recent),
        last_message_time_iso=last_time_iso,
    )
        return {
            "conversation_id": conv_id,
            "status": "dropbox_error",
            "error": f"{type(e).__name__}: {e}",
            "ai_extract": extract,
            "wp_item": {"post_id": wp_item.get("post_id"), "address": wp_address}
        }

    # 6) update WP listing
    body = {
        "posttitle": posttitle,
        "address": wp_address,
        "token": WP_TOKEN,
        "picture_button_url": final_link,
    }

    _safe_log_run(
    conversation_id=conv_id, status="prepared_update",
    url=url, street=street, addr=addr,
    wp_item=wp_item, ai_extract=extract,
    recent_dialog=dialog,
    message_count_considered=len(recent),
    last_message_time_iso=last_time_iso,
    new_picture_button_url=final_link,
)
    print("final body to update WP========",body)

    # return body
    post_id = wp_post_update(body)

    print("post_id after wp post logic==========",post_id)
    if not post_id:
        _safe_log_run(
    conversation_id=conv_id, status="wp_update_failed",  # <-- change this
    url=url, street=street, addr=addr,
    wp_item={**wp_item, "post_id": post_id},  # record the new/confirmed id
    ai_extract=extract,
    recent_dialog=dialog,
    message_count_considered=len(recent),
    last_message_time_iso=last_time_iso,
    new_picture_button_url=final_link,
)
        return {
            "conversation_id": conv_id,
            "status": "wp_update_failed",
            "ai_extract": extract,
            "wp_item": {"post_id": wp_item.get("post_id"), "address": wp_address},
            "attempted_body": body,
        }

    _safe_log_run(
    conversation_id=conv_id, status="updated",
    url=url, street=street, addr=addr,
    wp_item={**wp_item, "post_id": post_id},  # record the new/confirmed id
    ai_extract=extract,
    recent_dialog=dialog,
    message_count_considered=len(recent),
    last_message_time_iso=last_time_iso,
    new_picture_button_url=final_link,
)

    return {
        "conversation_id": conv_id,
        "status": "updated",
        "ai_extract": extract,
        "wp_item": {"post_id": wp_item.get("post_id"), "address": wp_address},
        "picture_button_url": final_link,
        "post_id": post_id,
    }
