# whatsapp_sender.py
import os
import logging
from typing import List, Optional, Dict, Any, Tuple
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bson import ObjectId
import time, random
import json
from mongoengine.queryset.visitor import Q

from models import ParsedListing  # your MongoEngine model
from config_runtime import get_whatsapp_send_mode, get_group_jids

TEAM_NUMBERS = [n.strip() for n in os.getenv("TEAM_WHATSAPP_NUMBERS","").split(",") if n.strip()]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# GATEWAY_URL = os.getenv("WHATSAPP_GATEWAY_URL", "http://54.197.208.138:3001/send")
# --- NEW: config + parsing helpers ---
# SEND_MODE = os.getenv("WHATSAPP_SEND_MODE", "dm").strip().lower()  # "dm" | "group"

GATEWAY_URL_DM    = os.getenv("WHATSAPP_GATEWAY_URL_DM",    "").strip()
GATEWAY_URL_GROUP = os.getenv("WHATSAPP_GATEWAY_URL_GROUP", "").strip()

GATEWAY_TIMEOUT = float(os.getenv("WHATSAPP_GATEWAY_TIMEOUT_SEC", "20"))

# Optional auth header if your gateway requires it (leave empty if not used)
GATEWAY_AUTH_KEY = os.getenv("WHATSAPP_GATEWAY_AUTH_KEY", "").strip()

def _team_numbers() -> List[str]:
    if not TEAM_NUMBERS_ENV:
        return []
    try:
        val = json.loads(TEAM_NUMBERS_ENV)
        if isinstance(val, list):
            return [str(x).strip() for x in val if str(x).strip()]
    except Exception:
        pass
    return [p.strip() for p in TEAM_NUMBERS_ENV.split(",") if p.strip()]

def _parse_group_jids_env() -> List[str]:
    raw = os.getenv("WHATSAPP_GROUP_JIDS", "").strip()
    if not raw:
        return []
    # allow JSON array OR comma-separated
    try:
        val = json.loads(raw)
        if isinstance(val, list):
            return [str(x).strip() for x in val if str(x).strip()]
    except Exception:
        pass
    # fallback: comma-separated
    return [j.strip() for j in raw.split(",") if j.strip()]

GROUP_JIDS = _parse_group_jids_env()

def _session() -> requests.Session:
    """Requests session with basic retries for transient network hiccups."""
    s = requests.Session()
    retry = Retry(
        total=1,
        backoff_factor=0.6,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["POST"],
    )
    s.mount("http://", HTTPAdapter(max_retries=retry))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s

def _first_image_url(urls: List[str]) -> Optional[str]:
    if not urls:
        return None
    for u in urls:
        if isinstance(u, str) and u.startswith(("http://", "https://")):
            return u.strip()
    return None

def _headers() -> Dict[str, Any]:
    h = {"Content-Type": "application/json"}
    if GATEWAY_AUTH_KEY:
        h["Authorization"] = f"Bearer {GATEWAY_AUTH_KEY}"
    return h

def _parse_sent(resp: requests.Response) -> Tuple[bool, str]:
    """
    Returns (is_sent, debug_text).
    Accepts both /send and /group/send response shapes.
    Expected happy path: {"result":[{"status":"sent", ...}], ...} OR {"status":"sent", ...}
    """
    txt = (resp.text or "")[:1000]
    try:
        data = resp.json()
    except Exception:
        return (False, f"non_json_response:{resp.status_code}:{txt}")

    # common patterns
    if isinstance(data, dict):
        # direct status
        st = str(data.get("status", "")).lower()
        if st == "sent":
            return (True, "status=sent")
        # array under result
        res = data.get("results")
        if isinstance(res, list) and res:
            st2 = str((res[0] or {}).get("status", "")).lower()
            return (st2 == "sent", f"results[0].status={st2}")
    return (False, f"unrecognized_payload:{data}")

def _send_dm(pl: ParsedListing, to_numbers: List[str]) -> bool:
    if not to_numbers:
        logging.info("DM mode: no recipients; skipping send")
        return False

    text = (getattr(pl, "post_content", "") or "").strip()
    if not text:
        logging.warning("DM mode: empty post_content; skipping")
        return False

    img = _first_image_url(getattr(pl, "images", []) or [])
    s = _session()
    headers = _headers()

    ok_any = False
    for raw_to in to_numbers:
        to = (raw_to or "").strip()
        if not to:
            continue
        payload: Dict[str, Any] = {"to": to, "text": text}
        if img:
            payload["imageUrl"] = img

        try:
            print("DM>>",GATEWAY_URL_DM,payload)
            resp = s.post(GATEWAY_URL_DM, json=payload, headers=headers, timeout=GATEWAY_TIMEOUT)
            sent, dbg = _parse_sent(resp)
            if sent:
                ok_any = True
                logging.info("DM OK: to=%s id=%s %s", to, pl.id, dbg)
            else:
                logging.warning("DM not-sent: to=%s id=%s %s body=%s", to, pl.id, dbg, resp.text[:500])
        except requests.RequestException as e:
            logging.exception("DM request failed: to=%s id=%s err=%s", to, pl.id, e)

        # small jitter to be polite
        time.sleep(random.uniform(2, 4))

    return ok_any

def _send_group(pl: ParsedListing) -> bool:
    jids = get_group_jids()  # from config_runtime or .env adapter
    if not jids:
        logging.warning("Group mode selected but no JIDs configured; skipping")
        return False

    text = (getattr(pl, "post_content", "") or "").strip()
    if not text:
        logging.warning("Group mode: empty post_content; skipping")
        return False

    img = _first_image_url(getattr(pl, "images", []) or [])
    payload: Dict[str, Any] = {"jids": jids, "text": text}
    if img:
        payload["imageUrl"] = img

    s = _session()
    headers = _headers()

    try:
        print("Group>>",GATEWAY_URL_GROUP,payload)
        resp = s.post(GATEWAY_URL_GROUP, json=payload, headers=headers, timeout=GATEWAY_TIMEOUT)
        sent, dbg = _parse_sent(resp)
        if sent:
            logging.info("GROUP OK: jids=%s id=%s %s", len(jids), pl.id, dbg)
            return True
        logging.warning("GROUP not-sent: id=%s %s body=%s", pl.id, dbg, resp.text[:500])
    except requests.RequestException as e:
        logging.exception("GROUP request failed: id=%s err=%s", pl.id, e)
    return False

def send_listing_to_whatsapp(listing_id, to_numbers: Optional[List[str]] = None) -> bool:
    """
    Single-send helper (kept for any direct/manual calls).
    Returns True if at least one successful 'sent'.
    """
    pl = ParsedListing.objects(id=ObjectId(str(listing_id))).first()
    if not pl:
        raise ValueError(f"No listing found with ID: {listing_id}")

    mode = get_whatsapp_send_mode()  # "dm" | "group"
    if mode == "group":
        return _send_group(pl)
    # default dm
    to_numbers = TEAM_NUMBERS
    return _send_dm(pl, to_numbers)

def process_whatsapp_queue(limit: int = 100, dm_sleep_range: Tuple[float, float] = (10, 15)) -> Dict[str, int]:
    """
    Pulls ParsedListing where whatsapp_status in {"pending","failed"} and attempts delivery.
    - Before sending: whatsapp_status -> "sending"
    - After sending:  "sent" or "failed"
    """
    mode = get_whatsapp_send_mode()
    to_numbers = TEAM_NUMBERS if mode == "dm" else []

    qs = ParsedListing.objects(
        Q(whatsapp_status__in=["pending", "failed"])
    ).only("id", "post_content", "images", "whatsapp_status").limit(limit)

    total = sent = failed = 0

    for pl in qs:
        total += 1

        # mark sending
        ParsedListing.objects(id=pl.id).update_one(
            set__whatsapp_status="sending"
        )

        ok = False
        try:
            if mode == "group":
                ok = _send_group(pl)
            else:
                ok = _send_dm(pl, to_numbers)
        except Exception as e:
            logging.exception("send error id=%s: %s", pl.id, e)
            ok = False

        ParsedListing.objects(id=pl.id).update_one(
            set__whatsapp_status=("sent" if ok else "failed")
        )

        if mode == "dm" and to_numbers:
            # honor your earlier pacing: 10–15s between *listings*
            time.sleep(random.uniform(*dm_sleep_range))

        sent += int(ok)
        failed += int(not ok)

    return {"total": total, "sent": sent, "failed": failed}

# def send_listing_to_whatsapp(listing_id, to_numbers: List[str]) -> None:
#     """
#     Sends the listing via your external WhatsApp gateway.

#     Body sent per recipient:
#       {
#         "to": "<E.164 or raw phone>",
#         "text": "<post_content>",
#         "imageUrl": "<first image URL or ''>"
#       }
#     """
#     # if not to_numbers:
#     #     logging.info("send_listing_to_whatsapp: no recipients provided; skipping")
#     #     return

#     pl = ParsedListing.objects(id=ObjectId(str(listing_id))).first()
#     if not pl:
#         raise ValueError(f"No listing found with ID: {listing_id}")

#     text = (getattr(pl, "post_content", "") or "").strip()
#     if not text:
#         raise ValueError("post_content is empty; nothing to send")

#     # img = _first_image_url(getattr(pl, "images", []) or [])

#     # headers: Dict[str, Any] = {"Content-Type": "application/json"}
#     # if GATEWAY_AUTH_KEY:
#     #     headers["Authorization"] = f"Bearer {GATEWAY_AUTH_KEY}"

#     # s = _session()

#     # for raw_to in to_numbers:
#     #     to = (raw_to or "").strip()
#     #     if not to:
#     #         continue

#     #     payload = {
#     #         "to": to,
#     #         "text": text,
#     #     }
#     #     # Only include imageUrl when present
#     #     if img:
#     #         payload["imageUrl"] = img

#     #     try:
#     #         resp = s.post(GATEWAY_URL, json=payload, headers=headers, timeout=GATEWAY_TIMEOUT)
#     #         if 200 <= resp.status_code < 300:
#     #             logging.info("WhatsApp gateway OK: to=%s id=%s status=%s",
#     #                          to, listing_id, resp.status_code)
#     #         else:
#     #             logging.warning("WhatsApp gateway NON-2xx: to=%s id=%s status=%s body=%s",
#     #                             to, listing_id, resp.status_code, resp.text[:500])
#     #     except requests.RequestException as e:
#     #         logging.exception("WhatsApp gateway request failed: to=%s id=%s err=%s", to, listing_id, e)

    
#     img = _first_image_url(getattr(pl, "images", []) or [])
#     s = _session()
#     headers = _headers()

#     mode = get_whatsapp_send_mode() 
#     if mode == "group":
#         if not GROUP_JIDS:
#             logging.warning("Group mode selected but WHATSAPP_GROUP_JIDS is empty; skipping send.")
#             return
#         payload: Dict[str, Any] = {"jids": GROUP_JIDS, "text": text}
#         if img:
#             payload["imageUrl"] = img
#         try:
#             print("Group >>",GATEWAY_URL_GROUP)
#             print("payload",payload)
#             # resp = s.post(GATEWAY_URL_GROUP, json=payload, headers=headers, timeout=GATEWAY_TIMEOUT)
#             # if 200 <= resp.status_code < 300:
#             #     logging.info("WhatsApp GROUP send OK: jids=%s id=%s status=%s",
#             #                  len(GROUP_JIDS), listing_id, resp.status_code)
#             # else:
#             #     logging.warning("WhatsApp GROUP send NON-2xx: status=%s body=%s",
#             #                     resp.status_code, resp.text[:500])
#         except requests.RequestException as e:
#             logging.exception("WhatsApp GROUP send failed: id=%s err=%s", listing_id, e)
#         return

#     # default: DM mode
#     if not to_numbers:
#         logging.info("DM mode but no recipients provided; skipping")
#         return

#     for raw_to in to_numbers:
#         to = (raw_to or "").strip()
#         if not to:
#             continue
#         payload = {"to": to, "text": text}
#         if img:
#             payload["imageUrl"] = img
#         try:
#             print("DM >>",GATEWAY_URL_DM)
#             print("payload",payload)
#             # resp = s.post(GATEWAY_URL_DM, json=payload, headers=headers, timeout=GATEWAY_TIMEOUT)
#             # if 200 <= resp.status_code < 300:
#             #     logging.info("WhatsApp DM OK: to=%s id=%s status=%s",
#             #                  to, listing_id, resp.status_code)
#             # else:
#             #     logging.warning("WhatsApp DM NON-2xx: to=%s id=%s status=%s body=%s",
#             #                     to, listing_id, resp.status_code, resp.text[:500])
#         except requests.RequestException as e:
#             logging.exception("WhatsApp DM failed: to=%s id=%s err=%s", to, listing_id, e)
