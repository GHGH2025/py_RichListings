"""Absolute rule: WhatsApp post text may contain only our Dropbox gallery URL."""

from __future__ import annotations

import re
from typing import Any, Optional
from urllib.parse import urlparse

_URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>\"'`]+", re.IGNORECASE)

_PICTURE_CTA_RE = re.compile(
    r"^\s*(?:"
    r"click\s+(?:here\s+)?for\s+(?:pictures?|photos?|pics)|"
    r"(?:view|see|more)\s+(?:pictures?|photos?|pics)|"
    r"(?:pictures?|photos?|photo\s+gallery)\s*"
    r")\s*:?\s*$",
    re.IGNORECASE,
)

_PRICE_LINE_RE = re.compile(r"^\*?Price\b", re.IGNORECASE)

_SOURCE_URL_KEYS = frozenset(
    {"other_images_source", "listing_url", "images"}
)


def _hostname(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    if raw.lower().startswith("www."):
        raw = "https://" + raw
    try:
        host = (urlparse(raw).netloc or "").lower()
    except Exception:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host


def is_our_dropbox_url(url: str) -> bool:
    host = _hostname(url)
    return host == "dropbox.com" or host.endswith(".dropbox.com") or host == "dl.dropboxusercontent.com"


def canonical_dropbox_link(url: Optional[str]) -> Optional[str]:
    value = (url or "").strip()
    return value if value and is_our_dropbox_url(value) else None


def _trim_url_match(raw: str) -> tuple[str, str]:
    url = raw
    trailing = ""
    while url and url[-1] in ".,);]}>\"'":
        trailing = url[-1] + trailing
        url = url[:-1]
    return url, trailing


def extract_urls(text: str) -> list[str]:
    found = []
    for match in _URL_RE.finditer(text or ""):
        url, _ = _trim_url_match(match.group(0))
        if url:
            found.append(url)
    return found


def strip_non_dropbox_urls(text: str) -> str:
    def _replace(match: re.Match) -> str:
        url, trailing = _trim_url_match(match.group(0))
        if is_our_dropbox_url(url):
            return url + trailing
        return ""

    return _URL_RE.sub(_replace, text or "")


def redact_non_dropbox_urls(value: Any) -> Any:
    """Strip non-Dropbox URLs from nested listing JSON before it reaches the model."""
    if isinstance(value, str):
        return strip_non_dropbox_urls(value)
    if isinstance(value, dict):
        return {
            key: redact_non_dropbox_urls(item)
            for key, item in value.items()
            if key not in _SOURCE_URL_KEYS
        }
    if isinstance(value, list):
        return [redact_non_dropbox_urls(item) for item in value]
    return value


def _collapse_blank_lines(lines: list[str]) -> list[str]:
    out: list[str] = []
    blank = False
    for line in lines:
        is_blank = not line.strip()
        if is_blank and blank:
            continue
        out.append(line)
        blank = is_blank
    return out


def _insert_dropbox_after_price(lines: list[str], dropbox_link: str) -> list[str]:
    out: list[str] = []
    inserted = False
    for line in lines:
        out.append(line)
        if not inserted and _PRICE_LINE_RE.match(line.strip()):
            out.append(dropbox_link)
            inserted = True
    if not inserted:
        if out and out[-1].strip():
            out.append("")
        out.append(dropbox_link)
    return out


def leftover_non_dropbox_urls(text: str) -> list[str]:
    return [url for url in extract_urls(text) if not is_our_dropbox_url(url)]


def sanitize_whatsapp_post_text(
    text: str,
    dropbox_link: Optional[str] = None,
) -> str:
    """
    The only URL allowed in WhatsApp post text is our Dropbox gallery link.
    Any other URL is stripped. If a Dropbox link exists and is missing from
    the post, it is inserted unlabeled under the price line.
    """
    allowed = canonical_dropbox_link(dropbox_link)
    cleaned = strip_non_dropbox_urls(text or "")

    lines = []
    for line in cleaned.splitlines():
        if _PICTURE_CTA_RE.match(line) and not _URL_RE.search(line):
            continue
        lines.append(line.rstrip())

    lines = _collapse_blank_lines(lines)
    body = "\n".join(lines).strip()

    if allowed:
        already_has_dropbox = any(is_our_dropbox_url(url) for url in extract_urls(body))
        if not already_has_dropbox:
            lines = _insert_dropbox_after_price(lines, allowed)
            body = "\n".join(_collapse_blank_lines(lines)).strip()

    leftover = leftover_non_dropbox_urls(body)
    if leftover:
        body = strip_non_dropbox_urls(body).strip()
        leftover = leftover_non_dropbox_urls(body)
        if leftover:
            raise ValueError(
                f"non-dropbox URL blocked from WhatsApp post: {leftover[0][:120]}"
            )

    return body
