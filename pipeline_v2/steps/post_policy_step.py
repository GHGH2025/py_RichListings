"""
post_policy_step – Region / city / 35%-cap post-policy gate.

In v1 this runs as `select_passed_listings_for_post` over the full 'passed' batch.
In v2 it runs per-listing (streaming), using a threading.Lock to guard the
daily counters that implement the 35% rest-of-florida cap.

Decisions (same logic as v1):
    1. Do-Not-Post city → EXIT / skipped
    2. Unsupported region → EXIT / skipped
    3. rest_of_florida over daily 35% cap → EXIT / skipped_quota
    4. Admitted → CONTINUE / ready_for_image_processing  (+ Dropbox upload if needed)

Thread safety:
    The _update_and_get_daily_base_count call from post_selection.py increments the
    DailyBaseCount collection atomically (MongoDB upsert), so it is safe from multiple
    threads. The in-memory _rest_lock / _rest_day / _rest_admitted counter guards the
    rest_admitted check-and-increment so two threads cannot simultaneously admit a
    rest listing that would push the count over the cap.
"""
import logging
import math
import threading
from datetime import datetime
from typing import Optional, Tuple

from models import ParsedListing
from observability.pipeline_metrics import record_listing_stage
from pipeline.post_selection import (
    ALLOWED_REGIONS,
    REASON_BAD_REGION,
    REASON_OVER_CAP,
    _is_do_not_post_city,
    _get_region,
    _update_and_get_daily_base_count,
    _send_skipped_listing_to_webhook,
)
from media.dropbox_upload import handle_Link
from media.slugify import slugify_for_folder
from pipeline_v2.steps import StepResult

logger = logging.getLogger(__name__)

# ── in-memory rest-of-florida admitted counter ───────────────────────────────
# Tracks how many rest_of_florida listings have been admitted (past this gate)
# today within THIS process. The counter is re-initialised on a new UTC day.
# The base count (non-rest admitted) is persisted to DailyBaseCount via
# _update_and_get_daily_base_count, so it survives restarts.  The rest_admitted
# counter is reconstructed from DB on first use each day.

_rest_lock: threading.Lock = threading.Lock()
_rest_day: Optional[Tuple[int, int, int]] = None
_rest_admitted_today: int = 0


def _get_today_rest_admitted(now: datetime) -> int:
    """
    Return number of rest_of_florida listings admitted today.
    Reconstructed from DB on the first call of each UTC day.
    """
    global _rest_day, _rest_admitted_today

    today = (now.year, now.month, now.day)
    if _rest_day == today:
        return _rest_admitted_today

    # New day – reconstruct from DB
    day_start = datetime(now.year, now.month, now.day)
    from mongoengine.queryset.visitor import Q

    admitted = ParsedListing.objects(
        Q(complete_info__region_bucket="rest_of_florida")
        & Q(updated_at__gte=day_start)
        & Q(status__nin=["not_processed", "processing", "bypassed", "skipped", "skipped_quota", "error"])
    ).count()

    _rest_day = today
    _rest_admitted_today = admitted
    return _rest_admitted_today


def run(pl: ParsedListing) -> StepResult:
    """
    Apply post-policy rules to a single listing.

    Returns StepResult.CONTINUE on pass, StepResult.EXIT on skip.
    """
    global _rest_admitted_today
    now = datetime.utcnow()

    # ── 1. Do-Not-Post city check ─────────────────────────────────────────────
    if _is_do_not_post_city(pl):
        pl.update(
            set__status="skipped",
            set__do_not_post_city="found",
            set__over_35_percent="not_found",
            set__rules_ai_rule_id="Do_Not_Post_City",
            set__rules_ai_version="v1",
            set__rules_ai_reason="Skipped due to Do Not Post City rule",
            set__updated_at=now,
        )
        if not pl.skipped_or_posted_at:
            ParsedListing.objects(id=pl.id).update_one(set__skipped_or_posted_at=now)

        try:
            record_listing_stage(
                str(pl.id),
                "post_selection_skipped",
                listing_status="skipped",
                skip_reason="Do Not Post City",
            )
        except Exception:
            pass
        try:
            _send_skipped_listing_to_webhook(
                pl,
                skip_type="Do_Not_Post_City",
                reason="Skipped due to Do Not Post City rule",
            )
        except Exception:
            pass
        logger.info("post_policy_step: listing=%s EXIT (do-not-post city)", pl.id)
        return StepResult.EXIT

    # ── 2. Region check ───────────────────────────────────────────────────────
    region = _get_region(pl)
    if region not in ALLOWED_REGIONS:
        pl.update(
            set__status="skipped",
            set__rules_ai_rule_id="POST_POLICY_REGION",
            set__rules_ai_version="v1",
            set__rules_ai_reason=REASON_BAD_REGION,
            set__do_not_post_city="not_found",
            set__over_35_percent="not_found",
            set__updated_at=now,
        )
        if not pl.skipped_or_posted_at:
            ParsedListing.objects(id=pl.id).update_one(set__skipped_or_posted_at=now)

        try:
            record_listing_stage(
                str(pl.id),
                "post_selection_skipped",
                listing_status="skipped",
                skip_reason=REASON_BAD_REGION,
            )
        except Exception:
            pass
        logger.info("post_policy_step: listing=%s EXIT (bad region=%s)", pl.id, region)
        return StepResult.EXIT

    # ── 3. 35% rest-of-florida cap ────────────────────────────────────────────
    if region == "rest_of_florida":
        with _rest_lock:
            rest_admitted = _get_today_rest_admitted(now)
            # Compute cap from today's non-rest base (persisted in DailyBaseCount)
            # Read without incrementing (base_count=0 does not modify the record
            # because _update_and_get_daily_base_count adds max(base_count, 0)).
            base_today = _update_and_get_daily_base_count(0, now)
            rest_cap = math.floor(0.35 * base_today)

            if rest_admitted >= rest_cap:
                # Over cap
                pl.update(
                    set__status="skipped_quota",
                    set__rules_ai_rule_id="POST_POLICY_35PC",
                    set__rules_ai_version="v1",
                    set__rules_ai_reason=(
                        f"{REASON_OVER_CAP}: allowed={rest_cap}, base_non_rest={base_today}"
                    ),
                    set__over_35_percent="found",
                    set__do_not_post_city="not_found",
                    set__updated_at=now,
                )
                if not pl.skipped_or_posted_at:
                    ParsedListing.objects(id=pl.id).update_one(set__skipped_or_posted_at=now)

                try:
                    record_listing_stage(
                        str(pl.id),
                        "post_selection_skipped",
                        listing_status="skipped_quota",
                        skip_reason="35% quota cap",
                    )
                except Exception:
                    pass
                try:
                    _send_skipped_listing_to_webhook(
                        pl,
                        skip_type="POST_POLICY_35PC",
                        reason="Skipped due to 35% rest_of_florida daily cap",
                        extra={"rest_cap": rest_cap, "final_base_count": base_today},
                    )
                except Exception:
                    pass
                logger.info(
                    "post_policy_step: listing=%s EXIT (35%% cap rest_admitted=%d cap=%d)",
                    pl.id,
                    rest_admitted,
                    rest_cap,
                )
                return StepResult.EXIT

            # Admit this rest listing – increment counter inside the lock
            _rest_admitted_today = rest_admitted + 1

    else:
        # Non-rest listing: increment the daily base count used to compute the cap
        _update_and_get_daily_base_count(1, now)

    # ── 4. Admitted – advance to ready_for_image_processing ──────────────────
    db_updates = {
        "set__status": "ready_for_image_processing",
        "set__do_not_post_city": "not_found",
        "set__over_35_percent": "not_found",
        "set__updated_at": now,
    }

    # Optional Dropbox upload for other_images_source
    try:
        src = (pl.other_images_source or "").strip()
        already = (pl.other_images_dropbox_link or "").strip()
        if src and not already:
            addr = (
                pl.address
                or (pl.complete_info or {}).get("address")
                or str(pl.id)
            ).strip()
            folder_slug = slugify_for_folder(addr, fallback=str(pl.id))
            shared_links = handle_Link([src], folder=folder_slug)
            if shared_links:
                db_updates["set__other_images_dropbox_link"] = shared_links[0]
    except Exception as exc:
        logger.warning("post_policy_step: dropbox upload failed for listing=%s: %s", pl.id, exc)

    pl.update(**db_updates)

    try:
        record_listing_stage(str(pl.id), "post_selection", listing_status="ready_for_image_processing")
    except Exception:
        pass

    logger.info("post_policy_step: listing=%s CONTINUE (region=%s)", pl.id, region)
    return StepResult.CONTINUE
