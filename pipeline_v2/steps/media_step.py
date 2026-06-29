"""
media_step – Verify and fill missing media (images / other_images_source).

Mirrors the single-listing logic from the _work() closure in
ai.media_verify.verify_and_fill_missing_media_for_not_processed.

In v2 this runs AFTER the post-policy gate, so images are only fetched for
listings that are confirmed to be posting candidates (region / city / cap passed).
This avoids expensive AI vision calls for listings that will be skipped.

Outcome:
    Always returns StepResult.CONTINUE (media verify is best-effort; failures
    do not block the listing – it will post with whatever images it already has).
    Status → 'verified'
"""
import logging
from datetime import datetime

from models import ParsedListing
from ai.media_verify import (
    ai_verify_media_for_listing,
    _fix_forbidden_images,
    _clean_images,
    _address_line_for_match,
)
from observability.pipeline_metrics import record_listing_stage
from pipeline_v2.steps import StepResult

logger = logging.getLogger(__name__)


def _html_from_source_email(pl: ParsedListing) -> str:
    se = getattr(pl, "source_email", None)
    if not se:
        return ""
    bodies = getattr(se, "bodies", None)
    if not bodies:
        return ""
    return (getattr(bodies, "html_ai", None) or getattr(bodies, "html_full", None) or "") or ""


def run(pl: ParsedListing) -> StepResult:
    """
    Fill missing images / other_images_source for a single listing, then mark verified.

    Always returns StepResult.CONTINUE.
    """
    now = datetime.utcnow()

    try:
        has_imgs = bool(pl.images) and len(pl.images) > 0
        has_other = bool(getattr(pl, "other_images_source", None))

        if has_imgs and has_other:
            # Both present – just fix any 403s and mark verified
            fixed = _fix_forbidden_images(pl.images)
            updates = {"set__status": "verified", "set__wp_check": "pending", "set__updated_at": now}
            if fixed != pl.images:
                updates["set__images"] = fixed
            ParsedListing.objects(id=pl.id).update_one(**updates)
        else:
            html_ai = _html_from_source_email(pl)
            ai_images, ai_other = [], None

            if html_ai.strip():
                anchor = _address_line_for_match(pl)
                ai = ai_verify_media_for_listing(anchor, html_ai)
                if not has_imgs:
                    ai_images = _clean_images(ai.get("images", []))
                if not has_other:
                    ai_other = ai.get("other_images_source")

            updates = {
                "set__status": "verified",
                "set__wp_check": "pending",
                "set__updated_at": now,
            }
            if not has_imgs and ai_images:
                updates["set__images"] = _fix_forbidden_images(ai_images)
            if not has_other and ai_other:
                updates["set__other_images_source"] = ai_other
            # Fix 403s in existing images even if we had them already
            if has_imgs and "set__images" not in updates:
                fixed_existing = _fix_forbidden_images(pl.images)
                if fixed_existing != pl.images:
                    updates["set__images"] = fixed_existing

            ParsedListing.objects(id=pl.id).update_one(**updates)

        try:
            record_listing_stage(str(pl.id), "verified", listing_status="verified")
        except Exception:
            pass

    except Exception as exc:
        logger.warning("media_step: listing=%s error (non-blocking): %s", pl.id, exc)
        # Mark verified anyway – listing will post with original images
        try:
            ParsedListing.objects(id=pl.id).update_one(
                set__status="verified",
                set__wp_check="pending",
                set__updated_at=now,
            )
            record_listing_stage(str(pl.id), "verified", listing_status="verified")
        except Exception:
            pass

    logger.info("media_step: listing=%s CONTINUE (verified)", pl.id)
    return StepResult.CONTINUE
