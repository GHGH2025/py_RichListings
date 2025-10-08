# whatsapp_sender.py
import os
import logging
from typing import List, Optional, Dict, Any
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bson import ObjectId

from models import ParsedListing  # your MongoEngine model

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

GATEWAY_URL = os.getenv("WHATSAPP_GATEWAY_URL", "http://54.197.208.138:3001/send")
GATEWAY_TIMEOUT = float(os.getenv("WHATSAPP_GATEWAY_TIMEOUT_SEC", "20"))

# Optional auth header if your gateway requires it (leave empty if not used)
GATEWAY_AUTH_KEY = os.getenv("WHATSAPP_GATEWAY_AUTH_KEY", "").strip()

def _session() -> requests.Session:
    """Requests session with basic retries for transient network hiccups."""
    s = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.5,
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

def send_listing_to_whatsapp(listing_id, to_numbers: List[str]) -> None:
    """
    Sends the listing via your external WhatsApp gateway.

    Body sent per recipient:
      {
        "to": "<E.164 or raw phone>",
        "text": "<post_content>",
        "imageUrl": "<first image URL or ''>"
      }
    """
    if not to_numbers:
        logging.info("send_listing_to_whatsapp: no recipients provided; skipping")
        return

    pl = ParsedListing.objects(id=ObjectId(str(listing_id))).first()
    if not pl:
        raise ValueError(f"No listing found with ID: {listing_id}")

    text = (getattr(pl, "post_content", "") or "").strip()
    if not text:
        raise ValueError("post_content is empty; nothing to send")

    img = _first_image_url(getattr(pl, "images", []) or [])

    headers: Dict[str, Any] = {"Content-Type": "application/json"}
    if GATEWAY_AUTH_KEY:
        headers["Authorization"] = f"Bearer {GATEWAY_AUTH_KEY}"

    s = _session()

    for raw_to in to_numbers:
        to = (raw_to or "").strip()
        if not to:
            continue

        payload = {
            "to": to,
            "text": text,
        }
        # Only include imageUrl when present
        if img:
            payload["imageUrl"] = img

        try:
            resp = s.post(GATEWAY_URL, json=payload, headers=headers, timeout=GATEWAY_TIMEOUT)
            if 200 <= resp.status_code < 300:
                logging.info("WhatsApp gateway OK: to=%s id=%s status=%s",
                             to, listing_id, resp.status_code)
            else:
                logging.warning("WhatsApp gateway NON-2xx: to=%s id=%s status=%s body=%s",
                                to, listing_id, resp.status_code, resp.text[:500])
        except requests.RequestException as e:
            logging.exception("WhatsApp gateway request failed: to=%s id=%s err=%s", to, listing_id, e)
