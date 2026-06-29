"""
dedup_step – Duplicate-address gate (30-day window, 6 % price-drop threshold).

In v1 this step runs on listings with status='verified' (after media_verify).
In v2 we run it immediately after parsing on status='not_processed' listings
because the logic only requires address, city, zip, and price fields – none of
which depend on image verification.

Outcomes:
    CONTINUE  – no recent duplicate found (or price dropped >= 6%); status → 'processed'
    EXIT      – duplicate found / no address; status → 'skipped'
"""
import logging
from datetime import datetime, timedelta

from models import ParsedListing
from pipeline.dedup import (
    _addr_candidates,
    _find_recent_prior,
    _find_recent_prior_geo,
    _price,
    _reason,
    PRICE_DROP_THRESHOLD,
    NEXT_STATUS_ON_PASS,
)
from observability.pipeline_metrics import record_listing_stage
from pipeline_v2.steps import StepResult

logger = logging.getLogger(__name__)

_THIRTY_DAYS = timedelta(days=30)


def run(pl: ParsedListing) -> StepResult:
    """
    Apply the 30-day duplicate rule to a single listing.

    Returns StepResult.CONTINUE on pass, StepResult.EXIT on skip.
    """
    now = datetime.utcnow()
    since = now - _THIRTY_DAYS

    cand_list = _addr_candidates(pl)
    if not cand_list:
        pl.update(
            set__status="skipped",
            set__rules_ai_reason=_reason("no address available to match", "cannot dedupe"),
            set__skipped_or_posted_at=now,
            set__updated_at=now,
        )
        try:
            record_listing_stage(str(pl.id), "dedup_skipped", listing_status="skipped", skip_reason="no address")
        except Exception:
            pass
        logger.info("dedup_step: listing=%s EXIT (no address)", pl.id)
        return StepResult.EXIT

    prior = None
    for (addr, city, zip_) in cand_list:
        prior = _find_recent_prior(addr, city, zip_, since, pl.id)
        if prior:
            break

    if not prior:
        prior = _find_recent_prior_geo(pl, since)

    if not prior:
        pl.update(
            set__status=NEXT_STATUS_ON_PASS,
            set__rules_ai_reason=None,
            set__updated_at=now,
        )
        try:
            record_listing_stage(str(pl.id), "dedup", listing_status=NEXT_STATUS_ON_PASS)
        except Exception:
            pass
        logger.info("dedup_step: listing=%s CONTINUE (no prior)", pl.id)
        return StepResult.CONTINUE

    prev_price = _price(prior)
    curr_price = _price(pl)

    if prev_price is None or curr_price is None or prev_price <= 0:
        pl.update(
            set__status="skipped",
            set__rules_ai_reason=_reason(
                "duplicate found but price comparison unavailable",
                f"prev_id={prior.id} prev={prev_price}, curr={curr_price}",
            ),
            set__skipped_or_posted_at=now,
            set__updated_at=now,
        )
        try:
            record_listing_stage(
                str(pl.id),
                "dedup_skipped",
                listing_status="skipped",
                skip_reason="price comparison unavailable",
            )
        except Exception:
            pass
        logger.info("dedup_step: listing=%s EXIT (price unavailable)", pl.id)
        return StepResult.EXIT

    drop = (prev_price - curr_price) / prev_price
    if drop >= PRICE_DROP_THRESHOLD:
        pl.update(
            set__status=NEXT_STATUS_ON_PASS,
            set__rules_ai_reason=None,
            set__updated_at=now,
        )
        try:
            record_listing_stage(str(pl.id), "dedup", listing_status=NEXT_STATUS_ON_PASS)
        except Exception:
            pass
        logger.info("dedup_step: listing=%s CONTINUE (price drop %.1f%%)", pl.id, drop * 100)
        return StepResult.CONTINUE

    pl.update(
        set__status="skipped",
        set__rules_ai_reason=_reason(
            "duplicate found; price not low enough",
            f"prev_id={prior.id} drop={drop:.1%} (< 6%) prev={prev_price:.0f} -> curr={curr_price:.0f}",
        ),
        set__skipped_or_posted_at=now,
        set__updated_at=now,
    )
    try:
        record_listing_stage(
            str(pl.id),
            "dedup_skipped",
            listing_status="skipped",
            skip_reason="duplicate; price not low enough",
        )
    except Exception:
        pass
    logger.info(
        "dedup_step: listing=%s EXIT (duplicate, drop=%.1f%%)",
        pl.id,
        drop * 100,
    )
    return StepResult.EXIT
