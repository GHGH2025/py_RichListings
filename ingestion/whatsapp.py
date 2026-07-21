"""WhatsApp inbound ingestion → FilteredListingEmail."""

from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, List

from models import (
    Bodies,
    FilteredListingEmail,
    FromInfo,
    InternalDate,
    WindowRange,
)
from models.whatsapp_tracked_messages import WhatsappTrackedMessage

logger = logging.getLogger(__name__)

ACCOUNT_LABEL = "whatsapp"


def _push_name(msg: WhatsappTrackedMessage) -> str:
    raw = msg.raw or {}
    if isinstance(raw, dict):
        name = (raw.get("pushName") or "").strip()
        if name:
            return name
    return (msg.group_name or "").strip()


def _gmail_message_id(msg: WhatsappTrackedMessage) -> str:
    return f"{msg.group_jid}:{msg.message_id}"


def _subject(msg: WhatsappTrackedMessage) -> str:
    name = (msg.group_name or "").strip()
    if name:
        return f"WA {name}"
    return f"WA {msg.group_jid}"


def _message_dt(msg: WhatsappTrackedMessage) -> datetime:
    ts = msg.timestamp
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            return ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc)
    return datetime.now(tz=timezone.utc)


def _media_urls(msg: WhatsappTrackedMessage) -> List[str]:
    urls = []
    for u in list(getattr(msg, "media_urls", None) or []):
        if isinstance(u, str):
            u = u.strip()
            if u.startswith(("http://", "https://")):
                urls.append(u)
    return urls


def _wrap_body(text: str, media_urls: List[str] | None = None) -> Bodies:
    plain = (text or "").strip()
    urls = media_urls or []

    # Keep direct URLs in plain text so downstream parsers can find them
    plain_parts = [plain] if plain else []
    for u in urls:
        if u not in plain_parts:
            plain_parts.append(u)
    plain_out = "\n".join(plain_parts)

    escaped = html.escape(plain).replace("\n", "<br>\n")
    img_html = "".join(
        f'<img src="{html.escape(u, quote=True)}" />\n' for u in urls
    )
    inner = ""
    if escaped:
        inner += escaped
    if img_html:
        if inner:
            inner += "<br>\n"
        inner += img_html
    wrapped = f"<div>{inner}</div>" if inner else ""
    return Bodies(text=plain_out, html_full=wrapped, html_ai=wrapped)


def _sender_lookup_email(msg: WhatsappTrackedMessage) -> str:
    """
    Prefer configured seller email (matches direct_wholesalers.sender_email).
    Fall back to phone so older messages still ingest.
    """
    email = (getattr(msg, "sender_email", None) or "").strip().lower()
    if email and "@" in email:
        return email
    return (msg.sender_phone or "").strip().lower()


def _upsert_filtered_email(msg: WhatsappTrackedMessage) -> str:
    dt = _message_dt(msg)
    ts_ms = int(dt.timestamp() * 1000)
    epoch = int(dt.timestamp())
    push_name = _push_name(msg)
    sender_phone = (msg.sender_phone or "").strip()
    sender_lookup = _sender_lookup_email(msg)
    gmail_message_id = _gmail_message_id(msg)
    media_urls = _media_urls(msg)

    q = FilteredListingEmail.objects(
        account_label=ACCOUNT_LABEL,
        gmail_message_id=gmail_message_id,
    )

    q.update_one(
        upsert=True,
        set__subject=_subject(msg),
        set__window=WindowRange(after_epoch=epoch, before_epoch=epoch + 1),
        set__from_info=FromInfo(
            raw=(msg.sender_jid or sender_phone or ""),
            name=push_name,
            email=sender_lookup,
        ),
        set__rfc822_date=dt.isoformat(),
        set__internal_date=InternalDate(ts_ms=ts_ms, iso=dt.isoformat()),
        set__bodies=_wrap_body(msg.text or "", media_urls),
        set_on_insert__status="not_processed",
        set__updated_at=datetime.utcnow(),
        set_on_insert__created_at=datetime.utcnow(),
    )

    saved = q.only("id").first()
    if not saved:
        raise RuntimeError(f"Failed to upsert FilteredListingEmail for {gmail_message_id}")

    saved_id = str(saved.id)
    try:
        from observability.pipeline_metrics import record_email_ingested

        record_email_ingested(saved_id)
    except Exception:
        logger.exception("record_email_ingested failed for whatsapp email_id=%s", saved_id)

    return saved_id


def _mark_error(msg_id: Any, error_message: str) -> None:
    WhatsappTrackedMessage.objects(id=msg_id).update_one(
        set__status="error",
        set__errorMessage=(error_message or "")[:500],
    )


def process_pending_whatsapp(limit: int = 10) -> dict:
    """
    Claim pending WhatsApp messages, upsert FilteredListingEmail rows,
    and mark each message processed or error.
    """
    pending = (
        WhatsappTrackedMessage.objects(status="pending")
        .order_by("timestamp")
        .limit(limit)
    )

    stats = {"picked": 0, "processed": 0, "error": 0, "skipped_race": 0}

    for msg in pending:
        stats["picked"] += 1
        updated = WhatsappTrackedMessage.objects(
            id=msg.id,
            status="pending",
        ).update_one(set__status="processing")
        if updated == 0:
            stats["skipped_race"] += 1
            continue

        try:
            text = (msg.text or "").strip()
            media_urls = _media_urls(msg)
            if not text and not media_urls:
                _mark_error(msg.id, "empty_text")
                stats["error"] += 1
                continue

            email_id = _upsert_filtered_email(msg)
            WhatsappTrackedMessage.objects(id=msg.id).update_one(
                set__status="processed",
                set__errorMessage="",
            )
            stats["processed"] += 1
            logger.info(
                "whatsapp ingest processed message_id=%s → email_id=%s",
                msg.message_id,
                email_id,
            )
        except Exception as e:
            logger.exception(
                "whatsapp ingest failed group=%s message_id=%s",
                msg.group_jid,
                msg.message_id,
            )
            _mark_error(msg.id, str(e))
            stats["error"] += 1

    return stats


def reset_stale_processing_whatsapp(hours: int = 6) -> dict:
    """
    Reset WhatsApp messages stuck in 'processing' longer than `hours`
    (based on message timestamp) back to 'pending'.
    """
    now = datetime.utcnow()
    cutoff = now - timedelta(hours=hours)

    q = WhatsappTrackedMessage.objects(
        status="processing",
        timestamp__lt=cutoff,
    )
    stuck_count = q.count()
    if stuck_count == 0:
        return {
            "ok": True,
            "stuck_count": 0,
            "updated": 0,
            "cutoff_utc": cutoff.isoformat(),
        }

    updated = q.update(
        set__status="pending",
        set__errorMessage="",
    )

    return {
        "ok": True,
        "stuck_count": stuck_count,
        "updated": updated,
        "cutoff_utc": cutoff.isoformat(),
    }
