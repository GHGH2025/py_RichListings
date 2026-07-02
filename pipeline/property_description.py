"""
Helpers for the verbatim property description extracted from email.

Used to append full listing text to WhatsApp posts without changing AI-generated headers.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional

_TEXT_SEPARATOR = "\n\n—\n\n"


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
    """Append verbatim property description to plain-text post content (WhatsApp)."""
    verbatim = get_verbatim_property_description(complete_info)
    if not verbatim:
        return (post_content or "").strip()

    base = (post_content or "").strip()
    if _already_contains_verbatim(base, verbatim):
        return base

    if not base:
        return verbatim
    return f"{base}{_TEXT_SEPARATOR}{verbatim}"
