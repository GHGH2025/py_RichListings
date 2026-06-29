"""
send_step – Send the composed WhatsApp message.

Uses the same low-level _send_dm / _send_group helpers as the v1 queue sender,
but called inline (no queue round-trip).

Retry policy: one automatic retry on failure.  The standard pacing sleep
(_DM_SLEEP_RANGE) is applied between the original attempt and the retry so the
retry itself acts as the inter-message gap.

Note on pacing: in v1 the queue sender sleeps 10–15 s between listings to avoid
rate limits. In v2, the per-listing sleep is applied within this step so the
caller (orchestrator) does not need to know about it. When PIPELINE_V2_WORKERS > 1,
each worker thread independently paces its own sends, which naturally spreads
them out.

Outcomes:
    CONTINUE  – message sent (whatsapp_status → 'sent')
    EXIT      – both attempts failed (whatsapp_status → 'failed')
"""
import logging
import random
import time

from models import ParsedListing
from whatsapp.sender import (
    _send_dm,
    _send_group,
    get_whatsapp_send_mode,
    TEAM_NUMBERS,
)
from observability.pipeline_metrics import record_listing_stage
from pipeline_v2.steps import StepResult

logger = logging.getLogger(__name__)

_DM_SLEEP_RANGE = (10, 15)


_MAX_SEND_ATTEMPTS = 2  # 1 original + 1 retry


def _attempt_send(pl: ParsedListing, mode: str) -> bool:
    if mode == "group":
        return _send_group(pl)
    return _send_dm(pl, TEAM_NUMBERS)


def run(pl: ParsedListing) -> StepResult:
    """
    Send the WhatsApp message for a single listing, with one automatic retry.

    Returns StepResult.CONTINUE on success, StepResult.EXIT if both attempts fail.
    The pacing sleep is applied after each attempt (between attempts and after the
    final one) to avoid WhatsApp rate limits.
    """
    mode = get_whatsapp_send_mode()

    ParsedListing.objects(id=pl.id).update_one(set__whatsapp_status="sending")

    ok = False
    for attempt in range(1, _MAX_SEND_ATTEMPTS + 1):
        try:
            ok = _attempt_send(pl, mode)
        except Exception as exc:
            logger.exception(
                "send_step: send error listing=%s attempt=%d: %s", pl.id, attempt, exc
            )
            ok = False

        if ok:
            break

        if attempt < _MAX_SEND_ATTEMPTS:
            logger.warning(
                "send_step: listing=%s attempt %d failed – retrying after delay",
                pl.id,
                attempt,
            )
        # Pacing sleep between attempts and after the final attempt
        time.sleep(random.uniform(*_DM_SLEEP_RANGE))

    whatsapp_status = "sent" if ok else "failed"
    ParsedListing.objects(id=pl.id).update_one(set__whatsapp_status=whatsapp_status)

    try:
        record_listing_stage(
            str(pl.id),
            "whatsapp_sent" if ok else "whatsapp_failed",
            whatsapp_status=whatsapp_status,
        )
    except Exception:
        pass

    if ok:
        logger.info("send_step: listing=%s CONTINUE (sent)", pl.id)
    else:
        logger.warning(
            "send_step: listing=%s EXIT (send failed after %d attempts)",
            pl.id,
            _MAX_SEND_ATTEMPTS,
        )

    return StepResult.CONTINUE if ok else StepResult.EXIT
