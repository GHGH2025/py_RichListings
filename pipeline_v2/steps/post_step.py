"""
post_step – Compose WhatsApp ad post and mark listing as 'posted'.

Loads ad_post_rules.txt once at import time.
Only operates on listings with status='ready_to_post'.
If the listing ended up as 'primary_image_failed' after image_step, it is skipped
(returns EXIT) so the listing stays in that terminal state for human review.

Outcomes:
    CONTINUE  – post composed; status → 'posted', whatsapp_status → 'pending',
                 wp_status → 'ready_to_process'
    EXIT      – listing not in ready_to_post (e.g. primary_image_failed); no-op
"""
import logging
from datetime import datetime
from functools import lru_cache

from models import ParsedListing
from ai.whatsapp_posts import (
    _listing_payload,
    _compose_post,
    _post_listing_to_webhook,
)
from observability.pipeline_metrics import record_listing_stage
from core.paths import data_path
from pipeline_v2.steps import StepResult

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _load_rules_text() -> str:
    path = data_path("ad_post_rules.txt")
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read().strip()


def run(pl: ParsedListing) -> StepResult:
    """
    Compose a WhatsApp post for a single listing and mark it posted.

    Returns StepResult.CONTINUE on success, StepResult.EXIT if the listing
    is not in 'ready_to_post' status (e.g. primary_image_failed).
    """
    # Re-read status from DB so we have the value set by image_step
    fresh = ParsedListing.objects(id=pl.id).only("status").first()
    if not fresh or fresh.status != "ready_to_post":
        logger.info(
            "post_step: listing=%s EXIT (status=%s, not ready_to_post)",
            pl.id,
            fresh.status if fresh else "not_found",
        )
        return StepResult.EXIT

    now = datetime.utcnow()
    rules_text = _load_rules_text()

    try:
        listing_obj = _listing_payload(pl)
        post_text = _compose_post(rules_text, listing_obj)

        pl.update(
            set__post_content=post_text,
            set__status="posted",
            set__wp_status="ready_to_process",
            set__skipped_or_posted_at=now,
            set__updated_at=now,
            set__rules_ai_reason=None,
            set__whatsapp_status="pending",
        )

        try:
            record_listing_stage(
                str(pl.id),
                "posted",
                listing_status="posted",
                wp_status="ready_to_process",
                whatsapp_status="pending",
            )
            record_listing_stage(str(pl.id), "podio_webhook")
        except Exception:
            pass

        try:
            _post_listing_to_webhook(pl.id)
        except Exception:
            pass

        logger.info("post_step: listing=%s CONTINUE (posted)", pl.id)
        return StepResult.CONTINUE

    except Exception as exc:
        pl.update(
            set__rules_ai_reason=f"post_generation_failed: {exc}",
            set__updated_at=now,
        )
        logger.warning("post_step: listing=%s post generation failed: %s", pl.id, exc)
        return StepResult.EXIT
