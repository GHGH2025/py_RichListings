"""
parse_step – Parse a FilteredListingEmail into ParsedListing documents.

Returns a list of listing IDs that were created/upserted with status=not_processed.
IDs with status=bypassed (sender-slice filtered) are excluded so the orchestrator
only processes listings that are meant to flow through the pipeline.
"""
import logging
from typing import List

from models import FilteredListingEmail, ParsedListing
from pipeline.listing_details import upsert_parsed_listings_from_html
from pipeline.process_email import _sender_slice_for

logger = logging.getLogger(__name__)


def run(fe: FilteredListingEmail) -> List[str]:
    """
    Parse email HTML into ParsedListings.

    Returns list of listing IDs with status='not_processed' (i.e. not bypassed).
    Records pipeline metrics via upsert_parsed_listings_from_html (calls
    record_listing_created internally).
    """
    html = ""
    if fe.bodies:
        html = fe.bodies.html_ai or fe.bodies.html_full or ""

    if not html.strip():
        logger.info("parse_step: empty HTML for email %s – nothing to parse", fe.id)
        return []

    sender_email = getattr(fe.from_info, "email", None) if fe.from_info else None
    slice_range = _sender_slice_for(fe.account_label, sender_email)

    result = upsert_parsed_listings_from_html(
        email_html=html,
        account_label=fe.account_label,
        gmail_message_id=fe.gmail_message_id,
        source_email_doc=fe,
        list_slice=slice_range,
    )

    all_ids: List[str] = result.get("ids", [])

    # Only return IDs for listings that should flow through the pipeline.
    # Bypassed listings (outside the sender slice) have status='bypassed'.
    active_ids: List[str] = []
    for listing_id in all_ids:
        pl = ParsedListing.objects(id=listing_id).only("status").first()
        if pl and pl.status == "not_processed":
            active_ids.append(listing_id)

    logger.info(
        "parse_step: email=%s → %d listings total, %d active",
        fe.id,
        len(all_ids),
        len(active_ids),
    )
    return active_ids
