"""Run with python -m new_emails.worker; no legacy cron registration."""
import logging
import os
import signal
from threading import Event
from .ingestion import scan_account
from .pipeline import process_one


def main():
    from db.mongo_engine_conn import init_db
    from .config import seed_templates
    init_db()
    seed_templates()
    logging.basicConfig(level=logging.INFO)
    stop = Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    while not stop.is_set():
        for label in ("acct1", "acct2"):
            try:
                logging.info("new email scan: %s", scan_account(label))
            except Exception:
                logging.exception("new email scan failed for %s", label)
        # Bound each drain so Gmail is scanned regularly even with a backlog.
        for _ in range(int(os.getenv("NEW_EMAILS_BATCH_SIZE", "20"))):
            if stop.is_set():
                break
            try:
                result = process_one()
            except Exception:
                logging.exception("new email worker queue failure")
                break
            if result is None:
                break
            logging.info("new email job: %s", result)
        stop.wait(max(1, int(os.getenv("NEW_EMAILS_POLL_SECONDS", "60"))))


if __name__ == "__main__":
    main()
