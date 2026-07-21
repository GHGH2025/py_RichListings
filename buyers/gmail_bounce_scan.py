"""Scan Gmail inboxes for delivery-failure / bounce notification messages."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Dict, List, Set

from buyers.email_delivery_utils import (
    extract_invalid_recipients_from_bounce_body,
    is_delivery_failure_notification,
    normalize_email,
)
from ingestion.email_extract import extract_email_body_simple
from ingestion.gmail import ACCOUNTS, _ensure_paths, _get_message, _gmail_search, _gmail_service, _header

# Deal emails are sent from rich@wholesaledealfinder.ai (RingCentral /rich_ai_deal_Email).
# Bounce/DSN messages land in THAT mailbox — not the listing scrapers (acct1/acct2).
# Keep this list separate from ingestion.gmail.ACCOUNTS so listing fetch never touches it.
BOUNCE_ACCOUNTS = [
    {
        "label": "richai_deal",
        "base_dir": os.path.join("accounts", "richai_deal"),
        "only_inbox": False,
        "fallback_lookback_min": 60,
        "credentials_filename": "credentials.json",
        "token_filename": "token.json",
        "state_filename": "state.json",
    },
]

# Comma-separated labels. Default: richai_deal only (not acct1/acct2).
BUYER_EMAIL_BOUNCE_GMAIL_ACCOUNTS = os.getenv(
    "BUYER_EMAIL_BOUNCE_GMAIL_ACCOUNTS",
    "richai_deal",
).strip()


def _utc_epoch_seconds(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return int(dt.timestamp())


def _build_bounce_search_query(after_epoch: int, before_epoch: int) -> str:
    """Gmail query for generic delivery-failure / mailer-daemon messages."""
    time_query = f"after:{after_epoch} before:{before_epoch}"
    bounce_query = (
        "from:(mailer-daemon OR postmaster OR "
        '"Mail Delivery Subsystem" OR "Mail Delivery" OR noreply) '
        'OR subject:("Delivery Status Notification" OR "Address not found" OR '
        '"Undelivered Mail" OR "Mail Delivery Failed" OR "Returned mail" OR '
        '"Delivery failure" OR "Message blocked")'
    )
    return f"{time_query} ({bounce_query})"


def _resolve_account_labels() -> List[str]:
    if BUYER_EMAIL_BOUNCE_GMAIL_ACCOUNTS:
        return [label.strip() for label in BUYER_EMAIL_BOUNCE_GMAIL_ACCOUNTS.split(",") if label.strip()]
    return [acct["label"] for acct in BOUNCE_ACCOUNTS]


def _account_registry() -> Dict[str, dict]:
    """Bounce accounts first; listing ACCOUNTS only for explicit env overrides."""
    registry: Dict[str, dict] = {acct["label"]: acct for acct in BOUNCE_ACCOUNTS}
    for acct in ACCOUNTS:
        registry.setdefault(acct["label"], acct)
    return registry


def _message_text(message: dict) -> str:
    content = extract_email_body_simple(message)
    plain = (content.get("text") or "").strip()
    html = (content.get("html_full") or "").strip()
    return plain if len(plain) >= len(html) else html


def scan_gmail_for_bounced_emails(
    *,
    window_start: datetime,
    window_end: datetime,
    candidate_emails: Set[str],
) -> Set[str]:
    """
    Search the deal-sender Gmail mailbox for bounce/DSN messages and
    extract invalid recipient addresses that match candidate_emails.
    """
    if not candidate_emails:
        return set()

    after_epoch = _utc_epoch_seconds(window_start)
    before_epoch = _utc_epoch_seconds(window_end)
    query = _build_bounce_search_query(after_epoch, before_epoch)
    labels = _resolve_account_labels()
    label_to_raw = _account_registry()

    bounced: Set[str] = set()
    scanned_messages = 0

    for label in labels:
        raw = label_to_raw.get(label)
        if not raw:
            logging.warning("gmail_bounce_scan: unknown account label %s", label)
            continue

        acct = _ensure_paths(raw)
        try:
            service = _gmail_service(acct.credentials_path, acct.token_path)
        except Exception:
            logging.exception("gmail_bounce_scan: failed to init Gmail service for %s", label)
            continue

        try:
            msg_ids = _gmail_search(service, query, only_inbox=False)
        except Exception:
            logging.exception("gmail_bounce_scan: Gmail search failed for %s query=%s", label, query)
            continue

        logging.info(
            "gmail_bounce_scan: account=%s query=%s candidate_messages=%s",
            label,
            query,
            len(msg_ids),
        )

        for msg_id in msg_ids:
            try:
                message = _get_message(service, msg_id)
            except Exception:
                logging.exception("gmail_bounce_scan: failed to fetch message %s on %s", msg_id, label)
                continue

            scanned_messages += 1
            payload = message.get("payload", {}) or {}
            headers = payload.get("headers", []) or []
            subject = _header(headers, "Subject") or ""
            from_header = _header(headers, "From") or ""
            body = _message_text(message)

            if not is_delivery_failure_notification(subject=subject, from_header=from_header, body=body):
                continue

            found = extract_invalid_recipients_from_bounce_body(body, candidate_emails)
            if found:
                logging.info(
                    "gmail_bounce_scan: account=%s msg=%s subject=%r found=%s",
                    label,
                    msg_id,
                    subject[:120],
                    sorted(found),
                )
                bounced |= found

    logging.info(
        "gmail_bounce_scan: scanned_messages=%s bounced_emails=%s",
        scanned_messages,
        len(bounced),
    )
    return bounced
