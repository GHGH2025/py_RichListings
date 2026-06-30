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
    r"email.?bounce|\bbounced\b|550[\s-]|551[\s-]|553[\s-]|554[\s-])",
    re.IGNORECASE,
)


def text_indicates_invalid_email(text: str) -> bool:
    s = (text or "").strip()
    if not s:
        return False
    return bool(_BOUNCE_TEXT_HINTS.search(s))


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
