"""Buyer deal page snapshots and public short-link URLs."""

import os

from pymongo import ReturnDocument

from models import BuyerDealPage

DEAL_PAGE_BASE_URL = os.getenv(
    "DEAL_PAGE_BASE_URL",
    "https://wholesaledealfinder.ai/deals",
)
_COUNTER_KEY = "buyer_deal_page_public_id"


def next_buyer_deal_page_public_id() -> int:
    coll = BuyerDealPage._get_collection().database["counters"]
    doc = coll.find_one_and_update(
        {"_id": _COUNTER_KEY},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return int(doc["seq"])


def create_deal_page(listing, buyer, context: dict) -> str:
    """
    Persist a BuyerDealPage snapshot for this listing+buyer pair.
    Returns the full public URL: DEAL_PAGE_BASE_URL/<public_id>
    """
    public_id = next_buyer_deal_page_public_id()
    doc = BuyerDealPage(
        public_id=public_id,
        listing_id=str(listing.id),
        buyer_id=str(buyer.id),
        first_name=context.get("first_name", ""),
        address=context.get("address", ""),
        price=context.get("price", ""),
        description=context.get("description", ""),
        pics_link=context.get("pics_link", ""),
        image_urls=list(getattr(listing, "images", None) or []),
        complete_info=dict(getattr(listing, "complete_info", None) or {}),
    )
    doc.save()
    return f"{DEAL_PAGE_BASE_URL}/{public_id}"
