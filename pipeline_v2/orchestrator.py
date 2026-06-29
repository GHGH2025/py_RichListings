"""
orchestrator – Drives a single FilteredListingEmail through the full v2 pipeline.

Stage order (v2):
    parse  →  per-listing:
        dedup  →  rules  →  post_policy  →  media  →  image  →  post  →  send

Cheap deterministic gates (dedup, rules, post_policy) run BEFORE media / AI image
work so listings that would be dropped exit without incurring vision API costs.
"""
import logging
from datetime import datetime

from models import FilteredListingEmail, ListingPipelineMetric, ParsedListing
from pipeline_v2.steps import StepResult
from pipeline_v2.steps import (
    parse_step,
    dedup_step,
    rules_step,
    post_policy_step,
    media_step,
    image_step,
    post_step,
    send_step,
)

logger = logging.getLogger(__name__)

# Statuses that are already terminal (set by a previous pipeline run or a parallel
# worker) – skip without re-processing.
_TERMINAL_STATUSES = frozenset({
    "posted", "skipped", "skipped_quota", "bypassed", "image_curation_failed",
})


def _run_listing_pipeline(listing_id: str) -> None:
    """
    Drive one ParsedListing through all pipeline stages.
    Each step either advances the listing or returns EXIT to stop early.
    """
    pl = ParsedListing.objects(id=listing_id).first()
    if not pl:
        logger.warning("orchestrator: listing %s not found – skipping", listing_id)
        return

    if pl.status in _TERMINAL_STATUSES:
        logger.info(
            "orchestrator: listing=%s already terminal (status=%s) – skipping",
            listing_id,
            pl.status,
        )
        return

    stages = [
        ("dedup",        dedup_step.run),
        ("rules",        rules_step.run),
        ("post_policy",  post_policy_step.run),
        ("media",        media_step.run),
        ("image",        image_step.run),
        ("post",         post_step.run),
        ("send",         send_step.run),
    ]

    for stage_name, step_fn in stages:
        try:
            result = step_fn(pl)
        except Exception as exc:
            logger.exception(
                "orchestrator: unhandled exception in stage=%s listing=%s",
                stage_name,
                listing_id,
            )
            break

        if result == StepResult.EXIT:
            logger.info(
                "orchestrator: listing=%s exited at stage=%s",
                listing_id,
                stage_name,
            )
            break

        # Refresh the in-memory listing after each DB-mutating step
        try:
            pl.reload()
        except Exception:
            # If reload fails, fetch fresh
            pl = ParsedListing.objects(id=listing_id).first()
            if not pl:
                break


def process_one_email(fe: FilteredListingEmail) -> dict:
    """
    Parse one FilteredListingEmail and drive each resulting ParsedListing through
    the full pipeline.

    Returns a summary dict for logging.
    """
    logger.info("orchestrator: processing email=%s", fe.id)
    start = datetime.utcnow()

    listing_ids = parse_step.run(fe)

    # Stamp every metric created by this parse with pipeline_version="v2" so
    # the metric dashboard can distinguish v1 and v2 runs.
    if listing_ids:
        try:
            ListingPipelineMetric.objects(
                listing_id__in=listing_ids
            ).update(set__pipeline_version="v2")
        except Exception as exc:
            logger.warning("orchestrator: failed to stamp pipeline_version: %s", exc)

    posted = 0
    skipped = 0
    failed = 0

    for listing_id in listing_ids:
        try:
            _run_listing_pipeline(listing_id)

            # Inspect final status for summary
            final = ParsedListing.objects(id=listing_id).only("status").first()
            status = final.status if final else "unknown"
            if status == "posted":
                posted += 1
            elif status in ("skipped", "skipped_quota", "bypassed", "image_curation_failed", "primary_image_failed"):
                skipped += 1
            else:
                # Still in a mid-stage status – treat as not-yet-complete
                skipped += 1
        except Exception as exc:
            logger.exception(
                "orchestrator: unhandled error for listing=%s email=%s: %s",
                listing_id,
                fe.id,
                exc,
            )
            failed += 1

    elapsed = (datetime.utcnow() - start).total_seconds()
    summary = {
        "email_id": str(fe.id),
        "listings": len(listing_ids),
        "posted": posted,
        "skipped": skipped,
        "failed": failed,
        "elapsed_sec": round(elapsed, 1),
    }
    logger.info("orchestrator: email=%s done %s", fe.id, summary)
    return summary
