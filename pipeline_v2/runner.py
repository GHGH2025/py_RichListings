"""
runner – PipelineV2 worker pool.

Responsibilities:
    • Poll FilteredListingEmail for status='not_processed' rows.
    • Atomically claim each row (status → 'processing') so multiple workers
      cannot pick up the same email.
    • Call orchestrator.process_one_email() in a thread pool worker.
    • Mark the email 'processed' on success.
    • On unhandled exception: allow exactly ONE retry by resetting the email
      back to 'not_processed' (pipeline_attempts < _MAX_ATTEMPTS). After that,
      mark as 'error' so the email is not retried indefinitely.

The runner runs as a long-lived daemon thread started from server_runner.py when
PIPELINE_VERSION=v2. It runs alongside the Gmail fetch cron (which is the producer
of not_processed emails) and the auxiliary crons (WordPress, Podio, etc.) which
remain active in both modes.

Config (from pipeline_v2.config):
    PIPELINE_V2_WORKERS=2       Concurrent email workers (default 2)
    PIPELINE_V2_POLL_SECONDS=5  Polling interval when queue is empty (default 5s)
"""
import logging
import time
from concurrent.futures import ThreadPoolExecutor, Future
from datetime import datetime
from typing import Dict

from models import FilteredListingEmail
from pipeline_v2 import orchestrator
from pipeline_v2.config import get_v2_workers, get_v2_poll_seconds

logger = logging.getLogger(__name__)

_BATCH_SIZE = 10   # Max emails claimed per poll cycle
_MAX_ATTEMPTS = 2  # 1 original attempt + 1 retry


def _claim_email(fe: FilteredListingEmail) -> bool:
    """
    Atomically transition email status from not_processed → processing.
    Returns True if this worker successfully claimed it.
    """
    updated = FilteredListingEmail.objects(
        id=fe.id, status="not_processed"
    ).update_one(
        set__status="processing",
        set__updated_at=datetime.utcnow(),
    )
    return updated == 1


def _mark_email_done(email_id: str, success: bool) -> None:
    status = "processed" if success else "error"
    try:
        FilteredListingEmail.objects(id=email_id).update_one(
            set__status=status,
            set__updated_at=datetime.utcnow(),
        )
    except Exception:
        logger.exception("runner: failed to mark email=%s as %s", email_id, status)


def _process_email_worker(email_id: str) -> None:
    """
    Worker function executed inside the thread pool.

    Increments pipeline_attempts before processing so the count is durable even
    if the process crashes mid-run. On success the email is marked 'processed'.
    On unhandled exception:
      - attempt < _MAX_ATTEMPTS  → reset to 'not_processed' so the poll loop
                                    picks it up again (one retry).
      - attempt >= _MAX_ATTEMPTS → mark 'error' (no further retries).
    """
    fe = FilteredListingEmail.objects(id=email_id).first()
    if not fe:
        logger.warning("runner: email=%s disappeared before processing", email_id)
        return

    attempt = (fe.pipeline_attempts or 0) + 1
    FilteredListingEmail.objects(id=email_id).update_one(
        set__pipeline_attempts=attempt,
        set__updated_at=datetime.utcnow(),
    )

    success = False
    try:
        orchestrator.process_one_email(fe)
        success = True
    except Exception:
        logger.exception(
            "runner: unhandled error processing email=%s (attempt %d/%d)",
            email_id,
            attempt,
            _MAX_ATTEMPTS,
        )
    finally:
        if success:
            _mark_email_done(email_id, success=True)
        elif attempt < _MAX_ATTEMPTS:
            # Reset for one retry – the poll loop will claim it again
            try:
                FilteredListingEmail.objects(id=email_id).update_one(
                    set__status="not_processed",
                    set__updated_at=datetime.utcnow(),
                )
                logger.warning(
                    "runner: email=%s failed on attempt %d – reset to not_processed for retry",
                    email_id,
                    attempt,
                )
            except Exception:
                logger.exception(
                    "runner: could not reset email=%s for retry – marking error", email_id
                )
                _mark_email_done(email_id, success=False)
        else:
            logger.error(
                "runner: email=%s exhausted %d/%d attempts – marking error",
                email_id,
                attempt,
                _MAX_ATTEMPTS,
            )
            _mark_email_done(email_id, success=False)


def run_forever() -> None:
    """
    Main loop: poll for emails and dispatch them to the thread pool.

    This function blocks forever and should be called from a daemon thread.
    """
    num_workers = get_v2_workers()
    poll_seconds = get_v2_poll_seconds()

    logger.info(
        "pipeline_v2 runner started: workers=%d poll_interval=%.1fs",
        num_workers,
        poll_seconds,
    )

    # Track in-flight futures so we don't over-submit
    in_flight: Dict[str, Future] = {}

    with ThreadPoolExecutor(max_workers=num_workers, thread_name_prefix="v2-worker") as pool:
        while True:
            try:
                # Prune completed futures
                done_ids = [eid for eid, fut in in_flight.items() if fut.done()]
                for eid in done_ids:
                    fut = in_flight.pop(eid)
                    if fut.exception():
                        logger.error(
                            "runner: worker for email=%s raised %s",
                            eid,
                            fut.exception(),
                        )

                # How many slots are free?
                free_slots = num_workers - len(in_flight)
                if free_slots <= 0:
                    time.sleep(1)
                    continue

                # Poll for new emails
                candidates = (
                    FilteredListingEmail.objects(status="not_processed")
                    .order_by("+created_at")
                    .limit(min(free_slots, _BATCH_SIZE))
                )

                claimed = 0
                for fe in candidates:
                    if str(fe.id) in in_flight:
                        continue
                    if _claim_email(fe):
                        future = pool.submit(_process_email_worker, str(fe.id))
                        in_flight[str(fe.id)] = future
                        claimed += 1

                if claimed == 0:
                    # Nothing to do – wait before polling again
                    time.sleep(poll_seconds)

            except Exception:
                logger.exception("runner: unexpected error in poll loop – continuing")
                time.sleep(poll_seconds)
