"""
image_step – AI image curation followed by primary image verification.

Combines the two separate v1 jobs into one synchronous call:
    1. process_listings_ready_for_image_processing  → ready_for_primary_image_check
    2. process_primary_image_verification           → ready_to_post | primary_image_failed

A listing that fails the primary check does NOT exit the pipeline – it still
moves to ready_to_post (with no primary image) so the ad can be composed.
This matches the v1 behaviour where primary_image_failed listings are not
retried automatically.

Outcomes:
    CONTINUE  – always (curation and primary check are best-effort; errors set
                 status to image_curation_failed / primary_image_failed but the
                 orchestrator will EXIT on those statuses since compose-post
                 requires ready_to_post)
"""
import logging
from datetime import datetime

from models import ParsedListing
from ai.image_curation import (
    _invoke_vision_model,
    _dedupe_preserve_order,
    classify_primary_image,
    MIDDLEWARE_STATUS_PRIMARY,
    PRIMARY_PASS_STATUS,
    PRIMARY_FAIL_STATUS,
)
from observability.pipeline_metrics import record_listing_stage
from pipeline_v2.steps import StepResult

logger = logging.getLogger(__name__)

_PRIMARY_MODEL = "gpt-5.1"
_SECONDARY_MODEL = "gpt-5-mini"


def _curate(pl: ParsedListing, now: datetime) -> bool:
    """
    Run image curation for a single listing.
    Returns True if curation succeeded (status → ready_for_primary_image_check).
    """
    images = [u.strip() for u in (pl.images or []) if isinstance(u, str) and u.strip()]

    if not images:
        pl.update(
            set__status=MIDDLEWARE_STATUS_PRIMARY,
            set__updated_at=now,
        )
        try:
            record_listing_stage(str(pl.id), "image_curation", listing_status=MIDDLEWARE_STATUS_PRIMARY)
        except Exception:
            pass
        return True

    result = _invoke_vision_model(images)
    kept = _dedupe_preserve_order(result.get("kept_ordered") or [])
    skipped_items = result.get("skipped") or []

    norm_skipped = []
    for item in skipped_items:
        if isinstance(item, dict) and item.get("url"):
            norm_skipped.append({"url": item["url"], "reason": (item.get("reason") or "").strip()})
        elif isinstance(item, str):
            norm_skipped.append({"url": item, "reason": ""})

    pl.update(
        set__images=kept,
        set__skipped_images=norm_skipped,
        set__status=MIDDLEWARE_STATUS_PRIMARY,
        set__updated_at=now,
    )
    try:
        record_listing_stage(str(pl.id), "image_curation", listing_status=MIDDLEWARE_STATUS_PRIMARY)
    except Exception:
        pass
    return True


def _verify_primary(pl: ParsedListing, now: datetime) -> None:
    """
    Run primary image verification for a listing already in ready_for_primary_image_check.
    Updates status to ready_to_post or primary_image_failed.
    """
    # Re-fetch to get the updated images list after curation
    pl.reload()
    images = [u.strip() for u in (pl.images or []) if isinstance(u, str) and u.strip()]

    if not images:
        pl.update(
            set__primary_image_check={"url": None, "keep": False, "reason": "no_images_after_curation"},
            set__status=PRIMARY_PASS_STATUS,
            set__updated_at=now,
        )
        try:
            record_listing_stage(str(pl.id), "primary_image", listing_status=PRIMARY_PASS_STATUS)
        except Exception:
            pass
        return

    primary_url = images[0]
    result_1 = classify_primary_image(primary_url, model=_PRIMARY_MODEL)
    keep_1 = bool(result_1.get("keep", False))
    reason_1 = (result_1.get("reason") or "").strip()

    result_2 = classify_primary_image(primary_url, model=_SECONDARY_MODEL)
    keep_2 = bool(result_2.get("keep", False))
    reason_2 = (result_2.get("reason") or "").strip()

    if keep_1 and keep_2:
        pl.update(
            set__primary_image_check={
                "url": primary_url,
                "keep": True,
                "reason": "both_models_keep_true",
                "model_primary": {"name": _PRIMARY_MODEL, "keep": keep_1, "reason": reason_1},
                "model_secondary": {"name": _SECONDARY_MODEL, "keep": keep_2, "reason": reason_2},
            },
            set__status=PRIMARY_PASS_STATUS,
            set__updated_at=now,
        )
        try:
            record_listing_stage(str(pl.id), "primary_image", listing_status=PRIMARY_PASS_STATUS)
        except Exception:
            pass
    else:
        pl.update(
            set__primary_image_check={
                "url": primary_url,
                "keep": False,
                "reason": "one_or_both_models_rejected",
                "model_primary": {"name": _PRIMARY_MODEL, "keep": keep_1, "reason": reason_1},
                "model_secondary": {"name": _SECONDARY_MODEL, "keep": keep_2, "reason": reason_2},
            },
            set__status=PRIMARY_FAIL_STATUS,
            set__updated_at=now,
        )
        try:
            record_listing_stage(str(pl.id), "primary_image_failed", listing_status=PRIMARY_FAIL_STATUS)
        except Exception:
            pass


def run(pl: ParsedListing) -> StepResult:
    """
    Run image curation then primary check for a single listing.

    Always returns StepResult.CONTINUE – the post_step handles the case where
    primary_image_failed listings should not be composed.
    """
    now = datetime.utcnow()

    try:
        curated = _curate(pl, now)
        if curated:
            try:
                _verify_primary(pl, now)
            except Exception as exc:
                logger.warning(
                    "image_step: primary check failed for listing=%s (non-blocking): %s",
                    pl.id,
                    exc,
                )
                try:
                    ParsedListing.objects(id=pl.id).update_one(
                        set__primary_image_check={"url": None, "keep": False, "reason": f"primary_check_exception: {exc}"},
                        set__status=PRIMARY_FAIL_STATUS,
                        set__updated_at=now,
                    )
                    record_listing_stage(str(pl.id), "primary_image_failed", listing_status=PRIMARY_FAIL_STATUS)
                except Exception:
                    pass
    except Exception as exc:
        logger.warning("image_step: curation failed for listing=%s: %s", pl.id, exc)
        try:
            ParsedListing.objects(id=pl.id).update_one(
                set__rules_ai_reason=f"image_curation_failed: {exc}",
                set__status="image_curation_failed",
                set__updated_at=now,
            )
            record_listing_stage(str(pl.id), "image_curation_failed", listing_status="image_curation_failed", skip_reason=str(exc))
        except Exception:
            pass

    logger.info("image_step: listing=%s CONTINUE", pl.id)
    return StepResult.CONTINUE
