"""Shared buyer deal-email delivery helpers (bounce detection, invalid marking)."""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, List, Optional, Set

from models import WebFormBuyerSubmission

_BOUNCE_TEXT_HINTS = re.compile(
    r"(address not found|user unknown|mailbox not found|mailbox unavailable|"
    r"recipient address rejected|invalid recipient|invalid email|no such user|"
    r"does not exist|undeliverable|permanent.?fail|hard.?bounce|"
    r"email.?bounce|\bbounced\b|550[\s-]|551[\s-]|553[\s-]|554[\s-]|"
    r"wasn'?t delivered|was not delivered|could not be delivered|"
    r"delivery status notification|account that you tried to reach)",
    re.IGNORECASE,
)

_SOFT_BOUNCE_HINTS = re.compile(
    r"(mailbox full|quota exceeded|try again later|temporarily unavailable|"
    r"temporary failure|deferred|452[\s-]|421[\s-]|4\.2\.\d|over quota)",
    re.IGNORECASE,
)

_EMAIL_TOKEN_RE = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")

_RECIPIENT_EXTRACT_PATTERNS = (
    re.compile(r"wasn'?t delivered to\s+<?([\w.+-]+@[\w.-]+\.\w+)>?", re.IGNORECASE),
    re.compile(r"was not delivered to\s+<?([\w.+-]+@[\w.-]+\.\w+)>?", re.IGNORECASE),
    re.compile(r"could not be delivered to\s+<?([\w.+-]+@[\w.-]+\.\w+)>?", re.IGNORECASE),
    re.compile(r"delivery to the following recipient failed[^\n]*\n[^\n]*<?([\w.+-]+@[\w.-]+\.\w+)>?", re.IGNORECASE),
    re.compile(r"final-recipient:\s*rfc822;\s*([\w.+-]+@[\w.-]+\.\w+)", re.IGNORECASE),
    re.compile(r"original-recipient:\s*rfc822;\s*([\w.+-]+@[\w.-]+\.\w+)", re.IGNORECASE),
    re.compile(r"\bto:\s*([\w.+-]+@[\w.-]+\.\w+)", re.IGNORECASE),
    re.compile(r"address not found[^\n]*<?([\w.+-]+@[\w.-]+\.\w+)>?", re.IGNORECASE),
    re.compile(r"the email account that you tried to reach[^\n]*<?([\w.+-]+@[\w.-]+\.\w+)>?", re.IGNORECASE),
)

_SYSTEM_EMAIL_PREFIXES = (
    "mailer-daemon@",
    "postmaster@",
    "noreply@",
    "no-reply@",
    "mail-delivery@",
)


def text_indicates_invalid_email(text: str) -> bool:
    s = (text or "").strip()
    if not s:
        return False
    return bool(_BOUNCE_TEXT_HINTS.search(s))


def is_soft_bounce_text(text: str) -> bool:
    s = (text or "").strip()
    if not s:
        return False
    return bool(_SOFT_BOUNCE_HINTS.search(s))


def is_system_or_sender_email(email: str) -> bool:
    em = normalize_email(email)
    if not em:
        return True
    return any(em.startswith(prefix) for prefix in _SYSTEM_EMAIL_PREFIXES)


def is_delivery_failure_notification(*, subject: str, from_header: str, body: str) -> bool:
    """True when a Gmail/DSN message looks like a permanent delivery failure."""
    combined = "\n".join(part for part in (subject, from_header, body) if part)
    if not combined.strip():
        return False

    if is_soft_bounce_text(combined) and not text_indicates_invalid_email(combined):
        return False

    if text_indicates_invalid_email(combined):
        return True

    from_lower = (from_header or "").lower()
    subj_lower = (subject or "").lower()
    if any(
        token in from_lower
        for token in ("mailer-daemon", "postmaster", "mail delivery subsystem", "mail delivery")
    ):
        return True

    return any(
        token in subj_lower
        for token in (
            "delivery status notification",
            "address not found",
            "undelivered mail",
            "returned mail",
            "mail delivery failed",
            "delivery failure",
            "message blocked",
            "failure notice",
        )
    )


def extract_invalid_recipients_from_bounce_body(body: str, candidate_emails: Set[str]) -> Set[str]:
    """
    Extract bounced recipient addresses from a delivery-failure email body.
    Only returns emails that appear in candidate_emails (emails we sent to).
    """
    if not body or not candidate_emails:
        return set()

    found: Set[str] = set()
    for pattern in _RECIPIENT_EXTRACT_PATTERNS:
        for match in pattern.finditer(body):
            email = normalize_email(match.group(1))
            if email in candidate_emails:
                found.add(email)

    if text_indicates_invalid_email(body):
        mentioned = {
            normalize_email(token)
            for token in _EMAIL_TOKEN_RE.findall(body)
            if not is_system_or_sender_email(token)
        }
        found |= mentioned & candidate_emails

    return found


def json_indicates_invalid_email(data: Any) -> bool:
    if not isinstance(data, dict):
        return False

    for key in (
        "invalid_email",
        "isInvalidEmail",
        "is_in_valid_email",
        "is_invalid_email",
        "bounced",
        "bounce",
    ):
        val = data.get(key)
        if val is True:
            return True
        if isinstance(val, str) and val.strip().lower() in ("true", "yes", "1", "bounced", "bounce"):
            return True

    for msg_key in ("error", "message", "detail", "reason", "status", "description"):
        if text_indicates_invalid_email(str(data.get(msg_key) or "")):
            return True

    return False


def email_send_indicates_invalid_address(result: dict) -> bool:
    """True when an email API response indicates a permanent invalid/bounced recipient."""
    if not result:
        return False
    if result.get("invalid_email"):
        return True

    response_json = result.get("response_json")
    if json_indicates_invalid_email(response_json):
        return True

    combined = " ".join(str(result.get(k) or "") for k in ("response_text", "error"))
    return text_indicates_invalid_email(combined)


def extract_bounced_emails_from_payload(payload: Any) -> Set[str]:
    """Best-effort extraction of bounced email addresses from structured API payloads."""
    found: Set[str] = set()
    _collect_bounced_emails(payload, found)
    return found


def _collect_bounced_emails(node: Any, found: Set[str]) -> None:
    if node is None:
        return

    if isinstance(node, str):
        if "@" in node and text_indicates_invalid_email(node):
            for token in re.findall(r"[\w.+-]+@[\w.-]+\.\w+", node):
                found.add(token.lower())
        return

    if isinstance(node, dict):
        email_val = node.get("email") or node.get("to") or node.get("recipient")
        if isinstance(email_val, str) and "@" in email_val:
            is_bounced = False
            for key in ("bounced", "bounce", "invalid_email", "isInvalidEmail", "is_invalid_email"):
                val = node.get(key)
                if val is True or (isinstance(val, str) and val.strip().lower() in ("true", "yes", "1", "bounced")):
                    is_bounced = True
                    break
            if not is_bounced:
                for msg_key in ("reason", "error", "message", "status", "bounce_type", "type"):
                    if text_indicates_invalid_email(str(node.get(msg_key) or "")):
                        is_bounced = True
                        break
            if is_bounced:
                found.add(email_val.strip().lower())

        for key in ("bounced_emails", "invalid_emails", "failed_emails", "bounces", "results", "items", "data"):
            if key in node:
                _collect_bounced_emails(node[key], found)

        for val in node.values():
            if isinstance(val, (dict, list)):
                _collect_bounced_emails(val, found)
        return

    if isinstance(node, list):
        for item in node:
            _collect_bounced_emails(item, found)


def normalize_email(value: str) -> str:
    return (value or "").strip().lower()


def mark_buyer_email_invalid(buyer_id: Any, *, reason: str = "") -> None:
    try:
        WebFormBuyerSubmission.objects(id=buyer_id).update_one(
            set__contact__is_invalid_email=True,
            set__updated_at=datetime.utcnow(),
        )
        logging.info(
            "Marked buyer %s contact.is_invalid_email=True%s",
            buyer_id,
            f" ({reason})" if reason else "",
        )
    except Exception:
        logging.exception("Failed to mark buyer %s contact.is_invalid_email", buyer_id)


def extract_message_id_from_send_result(result: dict) -> str:
    response_json = result.get("response_json") if isinstance(result, dict) else None
    if isinstance(response_json, dict):
        for key in ("message_id", "messageId", "MessageId", "id", "ses_message_id"):
            val = response_json.get(key)
            if val:
                return str(val).strip()
    return ""
