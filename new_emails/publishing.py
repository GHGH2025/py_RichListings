"""Durable Podio handoff using the existing listing_posted payload contract."""
import os
from datetime import datetime


def publish_podio(*, gmail_message_id, limit=100):
    import requests
    from models import ParsedListing
    from ai.whatsapp_posts import _serialize_listing_full
    query = ParsedListing.objects(gmail_message_id=gmail_message_id, status="posted",
                                   new_email_podio_sent_at=None).limit(limit)
    sent = 0
    for listing in query:
        url = os.getenv("NEW_EMAILS_PODIO_WEBHOOK_URL") or os.getenv("POSTED_LISTING_WEBHOOK_URL")
        if not url:
            raise RuntimeError("Set NEW_EMAILS_PODIO_WEBHOOK_URL or POSTED_LISTING_WEBHOOK_URL")
        response = requests.post(url, json={"event": "listing_posted", "listing": _serialize_listing_full(listing)},
                                 headers={"Idempotency-Key": f"new-email-podio-{listing.id}"}, timeout=30)
        response.raise_for_status()
        listing.update(set__new_email_podio_sent_at=datetime.utcnow())
        sent += 1
    return {"sent": sent}


def sync_wordpress(*, gmail_message_id, limit=100):
    from models import ParsedListing
    from integrations.wordpress.sync_poster import sync_wp_for_descriptions
    # The existing sync queue only picks des_generated. Recover failures for
    # this message so a transient publication error is actually retried.
    ParsedListing.objects(gmail_message_id=gmail_message_id, wp_status="failed").update(
        set__wp_status="des_generated")
    return sync_wp_for_descriptions(gmail_message_id=gmail_message_id, limit=limit)
