# whatsapp_sender.py
import os
from typing import List
from twilio.rest import Client
import logging
from models import ParsedListing  # Make sure you import this
from bson import ObjectId  # If using ObjectId for IDs

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

def _twilio():
    sid = os.getenv("TWILIO_ACCOUNT_SID")
    tok = os.getenv("TWILIO_AUTH_TOKEN")
    logging.info(f"TWILIO_ACCOUNT_SID: {repr(sid)}")
    logging.info(f"TWILIO_AUTH_TOKEN: {repr(tok)}")
    if not sid or not tok:
        raise RuntimeError("Missing TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN")
    return Client(sid, tok)

def send_listing_to_whatsapp(listing_id, to_numbers: List[str]) -> None:
    """
    Sends one WhatsApp message per recipient:
      - body = pl.post_content (your WhatsApp-formatted text)
      - media_url = FIRST direct image URL from pl.images (if any)
    """

    pl = ParsedListing.objects(id=ObjectId(listing_id)).first()
    if not pl:
        raise ValueError(f"No listing found with ID: {listing_id}")

    print("send_listing_to_whatsapp>>",listing_id,to_numbers)
    body = (getattr(pl, "post_content", "") or "").strip()
    if not body:
        raise ValueError("post_content is empty; nothing to send")

    # pick the first direct http(s) image
    first_img = None
    for u in (getattr(pl, "images", []) or []):
        if isinstance(u, str) and u.startswith(("http://", "https://")):
            first_img = u
            break

    from_id = os.getenv("TWILIO_WHATSAPP_FROM")
    if not from_id:
        raise RuntimeError("TWILIO_WHATSAPP_FROM not set")

    client = _twilio()
    from_whatsapp = f"whatsapp:{from_id}"

    for raw_to in to_numbers:
        to = f"whatsapp:{raw_to.strip()}"
        kwargs = {"from_": from_whatsapp, "to": to, "body": body}
        if first_img:
            kwargs["media_url"] = [first_img]
        client.messages.create(**kwargs)
