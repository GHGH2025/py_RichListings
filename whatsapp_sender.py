# whatsapp_sender.py
import os
from typing import List
from twilio.rest import Client

def _twilio():
    sid = os.getenv("TWILIO_ACCOUNT_SID")
    tok = os.getenv("TWILIO_AUTH_TOKEN")
    if not sid or not tok:
        raise RuntimeError("Missing TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN")
    return Client(sid, tok)

def send_listing_to_whatsapp(pl, to_numbers: List[str]) -> None:
    """
    Sends one WhatsApp message per recipient:
      - body = pl.post_content (your WhatsApp-formatted text)
      - media_url = FIRST direct image URL from pl.images (if any)
    """
    body = (getattr(pl, "post_content", "") or "").strip()
    if not body:
        raise ValueError("post_content is empty; nothing to send")

    # pick the first direct http(s) image
    first_img = None
    for u in (getattr(pl, "images", []) or []):
        if isinstance(u, str) and u.startswith(("http://", "https://")):
            first_img = u
            break

    from_id = os.getenv("TWILIO_WHATSAPP_FROM")  # e.g. +14155238886 (your Twilio WA number)
    if not from_id:
        raise RuntimeError("TWILIO_WHATSAPP_FROM not set")

    client = _twilio()
    from_whatsapp = f"whatsapp:{from_id}"

    for raw_to in to_numbers:
        to = f"whatsapp:{raw_to.strip()}"
        kwargs = {"from_": from_whatsapp, "to": to, "body": body}
        if first_img:
            kwargs["media_url"] = [first_img]   # single image sent with text
        client.messages.create(**kwargs)
