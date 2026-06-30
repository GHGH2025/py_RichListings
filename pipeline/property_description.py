"""
Shared helpers for the full verbatim property description extracted from email.

Used by WhatsApp posts, WordPress sync, and Podio webhooks.
"""
from __future__ import annotations

import html
import re
from typing import Any, Dict, Optional

_TEXT_SEPARATOR = "\n\n—\n\n"
_HTML_SEPARATOR = "<hr />"


def get_verbatim_property_description(complete_info: Optional[Dict[str, Any]]) -> str:
    """Return the full verbatim listing text saved during email extraction."""
    ci = complete_info or {}
    verbatim = ci.get("complete_info")
    if isinstance(verbatim, str) and verbatim.strip():
        return verbatim.strip()

    excerpt = ci.get("raw_description_excerpt")
    if isinstance(excerpt, str) and excerpt.strip():
        return excerpt.strip()

    return ""


def _normalize_for_compare(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", text.strip().lower())


def _already_contains_verbatim(body: str, verbatim: str) -> bool:
    """Avoid duplicating the full description when it is already present."""
    if not body or not verbatim:
        return False
    norm_body = _normalize_for_compare(body)
    norm_verbatim = _normalize_for_compare(verbatim)
    if len(norm_verbatim) < 40:
        return norm_verbatim in norm_body
    return norm_verbatim[:120] in norm_body


def append_full_property_description(post_content: str, complete_info: Optional[Dict[str, Any]]) -> str:
    """Append verbatim property description to plain-text post content (WhatsApp / Podio)."""
    verbatim = get_verbatim_property_description(complete_info)
    if not verbatim:
        return (post_content or "").strip()

    base = (post_content or "").strip()
    if _already_contains_verbatim(base, verbatim):
        return base

    if not base:
        return verbatim
    return f"{base}{_TEXT_SEPARATOR}{verbatim}"


def verbatim_to_html(text: str) -> str:
    """Convert plain-text property description to simple HTML paragraphs."""
    if not text:
        return ""
    parts = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            parts.append(f"<p>{html.escape(stripped)}</p>")
    return "\n".join(parts)


def append_full_property_description_html(
    html_content: str,
    complete_info: Optional[Dict[str, Any]],
) -> str:
    """Append verbatim property description as HTML (WordPress postdesc)."""
    verbatim = get_verbatim_property_description(complete_info)
    if not verbatim:
        return (html_content or "").strip()

    block = verbatim_to_html(verbatim)
    if not block:
        return (html_content or "").strip()

    base = (html_content or "").strip()
    if _already_contains_verbatim(base, verbatim):
        return base

    if not base:
        return block
    return f"{base}\n{_HTML_SEPARATOR}\n{block}"
