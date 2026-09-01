"""WhatsApp inbound ingestion → FilteredListingEmail."""

from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, List

import emoji
import requests

from ingestion.email_extract import _strip_for_ai
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

# Link-only deals: fetch the page HTML, then the normal pipeline parses it.
# Image scrape / Dropbox happens later, and only if the listing is not a duplicate.
JG_EQUITY_GROUP_NAME = "jg equity direct deals"

_HTTP_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
DEAL_HOST_NEEDLES = ("conta.cc", "constantcontact.com", "rs6.net", "ccsend.com")
_FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Arrows / bullets used in WA deal blasts that are not always in emoji data (e.g. →).
_DECORATIVE_ARROWS_RE = re.compile(
    "["
    "\U00002190-\U000021FF"
    "\U000027A1"
    "\U00002B05-\U00002B07"
    "]+",
    flags=re.UNICODE,
)


def _strip_emojis(text: str) -> str:
    """
    Remove all Unicode/WhatsApp emojis from inbound deal text.

    Uses the `emoji` package (full Unicode emoji list, including ZWJ sequences,
    skin tones, and flags). Also strips common decorative arrows used as bullets.
    """
    if not text:
        return ""
    cleaned = emoji.replace_emoji(text, replace="")
    cleaned = _DECORATIVE_ARROWS_RE.sub("", cleaned)
    # Orphaned variation selectors / ZWJ left after partial sequences.
    cleaned = cleaned.replace("\uFE0F", "").replace("\u200D", "")
    # Drop spaces left behind by emoji removal; preserve newlines.
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n[ \t]+", "\n", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned.strip()


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


def is_jg_equity_group(name: str) -> bool:
    return JG_EQUITY_GROUP_NAME in (name or "").strip().lower()


def http_urls(text: str) -> List[str]:
    seen = set()
    urls: List[str] = []
    for match in _HTTP_URL_RE.finditer(text or ""):
        url = match.group(0).rstrip(").,]>\"'")
        if not url or url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def first_http_url(text: str) -> str:
    urls = http_urls(text)
    return urls[0] if urls else ""


def deal_page_urls(text: str) -> List[str]:
    """Prefer conta.cc / Constant Contact links; otherwise all http(s) URLs."""
    urls = http_urls(text)
    deal = [u for u in urls if any(n in u.lower() for n in DEAL_HOST_NEEDLES)]
    return deal or urls


def fetch_deal_page_html(url: str) -> str:
    try:
        response = requests.get(
            url,
            headers=_FETCH_HEADERS,
            timeout=25,
            allow_redirects=True,
        )
        response.raise_for_status()
        body = (response.text or "").strip()
        if not body:
            return ""
        ctype = (response.headers.get("Content-Type") or "").lower()
        looks_html = (
            "html" in ctype
            or "text/" in ctype
            or body[:200].lower().lstrip().startswith(("<!", "<html", "<body"))
        )
        return body if looks_html else ""
    except Exception:
        logger.exception("jg equity page fetch failed url=%s", url)
        return ""


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


def _wrap_body_from_page(
    text: str,
    page_html: str,
    media_urls: List[str] | None = None,
    page_url: str = "",
) -> Bodies:
    plain_parts = []
    if page_url:
        plain_parts.append(page_url)
    cleaned = (text or "").strip()
    if cleaned and cleaned not in plain_parts:
        plain_parts.append(cleaned)
    for u in media_urls or []:
        if u not in plain_parts:
            plain_parts.append(u)

    prefix = ""
    if page_url:
        safe_url = html.escape(page_url, quote=True)
        prefix = f'<p><a href="{safe_url}">More Pictures</a></p>\n'
    html_full = f"{prefix}{page_html}"
    extra_imgs = "".join(
        f'<img src="{html.escape(u, quote=True)}" />\n' for u in (media_urls or [])
    )
    if extra_imgs:
        html_full = f"{html_full}\n{extra_imgs}"

    return Bodies(
        text="\n".join(plain_parts),
        html_full=html_full,
        html_ai=_strip_for_ai(html_full),
    )


def _sender_lookup_email(msg: WhatsappTrackedMessage) -> str:
    """
    Prefer configured seller email (matches direct_wholesalers.sender_email).
    Fall back to phone so older messages still ingest.
    """
    email = (getattr(msg, "sender_email", None) or "").strip().lower()
    if email and "@" in email:
        return email
    return (msg.sender_phone or "").strip().lower()


def _write_filtered_email(
    msg: WhatsappTrackedMessage,
    gmail_message_id: str,
    subject: str,
    bodies: Bodies,
) -> str:
    dt = _message_dt(msg)
    ts_ms = int(dt.timestamp() * 1000)
    epoch = int(dt.timestamp())
    push_name = _push_name(msg)
    sender_phone = (msg.sender_phone or "").strip()
    sender_lookup = _sender_lookup_email(msg)

    q = FilteredListingEmail.objects(
        account_label=ACCOUNT_LABEL,
        gmail_message_id=gmail_message_id,
    )

    q.update_one(
        upsert=True,
        set__subject=subject,
        set__window=WindowRange(after_epoch=epoch, before_epoch=epoch + 1),
        set__from_info=FromInfo(
            raw=(msg.sender_jid or sender_phone or ""),
            name=push_name,
            email=sender_lookup,
        ),
        set__rfc822_date=dt.isoformat(),
        set__internal_date=InternalDate(ts_ms=ts_ms, iso=dt.isoformat()),
        set__bodies=bodies,
        set__input_source="whatsapp",
        set__source_website=None,
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


def _upsert_filtered_emails(msg: WhatsappTrackedMessage) -> List[str]:
    media_urls = _media_urls(msg)
    text = _strip_emojis(msg.text or "")
    base_id = _gmail_message_id(msg)
    base_subject = _subject(msg)

    if not is_jg_equity_group(msg.group_name):
        return [_write_filtered_email(msg, base_id, base_subject, _wrap_body(text, media_urls))]

    page_urls = deal_page_urls(text)
    if not page_urls:
        logger.warning(
            "jg equity message has no URL group=%s message_id=%s",
            msg.group_jid,
            msg.message_id,
        )
        return [_write_filtered_email(msg, base_id, base_subject, _wrap_body(text, media_urls))]

    saved_ids: List[str] = []
    failed: List[str] = []
    total = len(page_urls)

    for i, page_url in enumerate(page_urls, start=1):
        page_html = fetch_deal_page_html(page_url)
        if not page_html:
            failed.append(page_url)
            continue

        email_id = f"{base_id}:link{i}"
        subject = f"{base_subject} [{i}/{total}]"
        bodies = _wrap_body_from_page(text, page_html, media_urls, page_url)
        saved_ids.append(_write_filtered_email(msg, email_id, subject, bodies))
        logger.info(
            "jg equity fetched page url=%s chars=%s message_id=%s email=%s",
            page_url,
            len(page_html),
            msg.message_id,
            email_id,
        )

    if failed:
        raise RuntimeError(f"jg_equity_fetch_failed: {', '.join(failed)}")
    if not saved_ids:
        raise RuntimeError("jg_equity_fetch_failed: no pages saved")
    return saved_ids


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
            text = _strip_emojis(msg.text or "")
            media_urls = _media_urls(msg)
            if not text and not media_urls:
                _mark_error(msg.id, "empty_text")
                stats["error"] += 1
                continue

            email_ids = _upsert_filtered_emails(msg)
            WhatsappTrackedMessage.objects(id=msg.id).update_one(
                set__status="processed",
                set__errorMessage="",
            )
            stats["processed"] += 1
            logger.info(
                "whatsapp ingest processed message_id=%s → email_ids=%s",
                msg.message_id,
                email_ids,
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
