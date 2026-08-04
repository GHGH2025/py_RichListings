"""
Catch-up runner for listings stuck at status=verified.

Finds verified listings since a timestamp and runs the live pipeline forward:
dedup → AI rules → post selection → image curation → primary image →
WhatsApp ad copy (Podio webhook) → WordPress AI → WP sync → WhatsApp send.
"""
from __future__ import annotations

import logging
import traceback
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from models import ParsedListing
from pipeline.dedup import process_not_processed_with_duplicate_rule
from ai.rules_runner import apply_ai_english_rules
from pipeline.post_selection import select_passed_listings_for_post
from ai.image_curation import (
    process_listings_ready_for_image_processing,
    process_primary_image_verification,
)
from ai.whatsapp_posts import make_whatsapp_posts_from_ready_to_post
from integrations.wordpress.ai_mapper import ai_build_wp_payload_for_posted
from integrations.wordpress.ai_property_description import (
    ai_build_wp_property_description_for_posted,
)
from integrations.wordpress.sync_poster import sync_wp_for_descriptions
from whatsapp.sender import process_whatsapp_queue
from core.paths import data_path

logger = logging.getLogger(__name__)


def _parse_since(since: str) -> datetime:
    raw = (since or "").strip()
    if not raw:
        raise ValueError("since is required (ISO-8601 UTC timestamp)")
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if not dt:
        return None
    try:
        return dt.isoformat() + "Z"
    except Exception:
        return str(dt)


def _listing_preview(pl: ParsedListing) -> Dict[str, Any]:
    return {
        "id": str(pl.id),
        "address": getattr(pl, "address", None),
        "city": getattr(pl, "city", None),
        "state": getattr(pl, "state", None),
        "created_at": _iso(getattr(pl, "created_at", None)),
        "updated_at": _iso(getattr(pl, "updated_at", None)),
        "gmail_message_id": getattr(pl, "gmail_message_id", None),
        "account_label": getattr(pl, "account_label", None),
        "status": getattr(pl, "status", None),
    }


def find_verified_since(since: str, limit: int = 100) -> Dict[str, Any]:
    """
    Preview verified listings with updated_at >= since.

    Note: many production ParsedListing docs have created_at null/missing, so
    the catch-up window is keyed on updated_at (set when status becomes verified).
    """
    since_dt = _parse_since(since)
    limit = max(1, int(limit or 100))

    qs = (
        ParsedListing.objects(
            status="verified",
            updated_at__gte=since_dt,
            gmail_message_id__not__startswith="test_",
        )
        .order_by("+updated_at")
        .limit(limit)
        .only(
            "id",
            "address",
            "city",
            "state",
            "created_at",
            "updated_at",
            "gmail_message_id",
            "account_label",
            "status",
        )
    )
    listings = [_listing_preview(pl) for pl in qs]
    msg_ids = sorted(
        {m for m in (row.get("gmail_message_id") for row in listings) if m}
    )
    return {
        "ok": True,
        "dry_run": True,
        "since": _iso(since_dt),
        "limit": limit,
        "listing_count": len(listings),
        "message_count": len(msg_ids),
        "gmail_message_ids": msg_ids,
        "listings": listings,
    }


def _run_stage(name: str, fn, stages: List[Dict[str, Any]]) -> Any:
    entry: Dict[str, Any] = {"stage": name, "ok": True}
    try:
        result = fn()
        entry["result"] = result
        stages.append(entry)
        return result
    except Exception as e:
        entry["ok"] = False
        entry["error"] = str(e)
        entry["traceback"] = traceback.format_exc()[-2000:]
        stages.append(entry)
        logger.exception("catchup stage failed: %s", name)
        return None


def _process_message(msg_id: str, per_msg_limit: int = 50) -> Dict[str, Any]:
    msg_kw = {"gmail_message_id": msg_id}
    stages: List[Dict[str, Any]] = []

    _run_stage(
        "dedup_30d",
        lambda: process_not_processed_with_duplicate_rule(
            limit=per_msg_limit, **msg_kw
        ),
        stages,
    )
    _run_stage(
        "ai_rules",
        lambda: apply_ai_english_rules(
            str(data_path("ai_listing_rules.yaml")),
            limit=per_msg_limit,
            **msg_kw,
        ),
        stages,
    )
    _run_stage(
        "post_selection",
        lambda: select_passed_listings_for_post(
            limit=per_msg_limit,
            sort_by="created_at",
            mark_ready_status=None,
            skip_webhooks=False,
            **msg_kw,
        ),
        stages,
    )
    _run_stage(
        "image_curation",
        lambda: process_listings_ready_for_image_processing(
            limit=per_msg_limit, **msg_kw
        ),
        stages,
    )
    _run_stage(
        "primary_image",
        lambda: process_primary_image_verification(
            limit=per_msg_limit, **msg_kw
        ),
        stages,
    )
    _run_stage(
        "whatsapp_ad_copy",
        lambda: make_whatsapp_posts_from_ready_to_post(
            str(data_path("ad_post_rules.txt")),
            limit=per_msg_limit,
            skip_webhook=False,
            **msg_kw,
        ),
        stages,
    )
    _run_stage(
        "wordpress_keys",
        lambda: ai_build_wp_payload_for_posted(
            limit=per_msg_limit, batch_size=10, **msg_kw
        ),
        stages,
    )
    _run_stage(
        "wordpress_description",
        lambda: ai_build_wp_property_description_for_posted(
            limit=per_msg_limit,
            batch_size=10,
            per_item_sleep_s=0.0,
            **msg_kw,
        ),
        stages,
    )
    _run_stage(
        "wordpress_sync",
        lambda: sync_wp_for_descriptions(
            limit=per_msg_limit,
            per_item_sleep_s=0.0,
            gmail_message_id=msg_id,
        ),
        stages,
    )
    _run_stage(
        "whatsapp_send",
        lambda: process_whatsapp_queue(
            limit=per_msg_limit,
            gmail_message_id=msg_id,
        ),
        stages,
    )

    final_listings = list(
        ParsedListing.objects(gmail_message_id=msg_id)
        .only(
            "id",
            "status",
            "whatsapp_status",
            "wp_status",
            "address",
            "rules_ai_reason",
        )
        .order_by("+list_index")
    )
    return {
        "gmail_message_id": msg_id,
        "stages": stages,
        "stages_ok": sum(1 for s in stages if s.get("ok")),
        "stages_failed": sum(1 for s in stages if not s.get("ok")),
        "listings": [
            {
                "id": str(pl.id),
                "address": getattr(pl, "address", None),
                "status": getattr(pl, "status", None),
                "whatsapp_status": getattr(pl, "whatsapp_status", None),
                "wp_status": getattr(pl, "wp_status", None),
                "rules_ai_reason": getattr(pl, "rules_ai_reason", None),
            }
            for pl in final_listings
        ],
    }


def run_catchup_from_verified(
    since: str,
    limit: int = 100,
    gmail_message_ids: Optional[List[str]] = None,
    listing_count: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Live catch-up: process listings for the given source emails.

    Prefer passing gmail_message_ids captured at request time so a concurrent
    cron that moves listings out of verified cannot empty the work set.
    """
    since_dt = _parse_since(since)
    limit = max(1, int(limit or 100))

    msg_order: "OrderedDict[str, None]" = OrderedDict()
    if gmail_message_ids:
        for mid in gmail_message_ids:
            mid = (mid or "").strip()
            if mid and not mid.startswith("test_"):
                msg_order[mid] = None
    else:
        preview = find_verified_since(since, limit=limit)
        for row in preview["listings"]:
            mid = row.get("gmail_message_id")
            if mid:
                msg_order[mid] = None
        listing_count = preview["listing_count"]

    if not msg_order:
        return {
            "ok": True,
            "dry_run": False,
            "since": _iso(since_dt),
            "limit": limit,
            "listing_count": 0,
            "message_count": 0,
            "messages": [],
        }

    messages: List[Dict[str, Any]] = []
    for msg_id in msg_order.keys():
        logger.info("catchup-from-verified: processing gmail_message_id=%s", msg_id)
        try:
            messages.append(_process_message(msg_id))
        except Exception as e:
            logger.exception("catchup message failed: %s", msg_id)
            messages.append(
                {
                    "gmail_message_id": msg_id,
                    "ok": False,
                    "error": str(e),
                    "traceback": traceback.format_exc()[-2000:],
                    "stages": [],
                    "listings": [],
                }
            )

    return {
        "ok": True,
        "dry_run": False,
        "since": _iso(since_dt),
        "limit": limit,
        "listing_count": listing_count if listing_count is not None else len(msg_order),
        "message_count": len(messages),
        "messages": messages,
    }
