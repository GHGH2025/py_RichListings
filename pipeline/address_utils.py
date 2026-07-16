"""
Resolve the best street address for a listing, including house numbers when present.

Structured extraction historically stored street names without house numbers, while the
verbatim email text (complete_info.complete_info) and WhatsApp post_content retain them.
This module centralizes resolution for WordPress, Podio, and webhooks.
"""
from __future__ import annotations

import re
from typing import Any, Optional, Tuple

_HAS_HOUSE_RE = re.compile(r"^\s*(\d{1,6}[xX*]{0,4}|\d{1,6})\b")
_STREET_WITH_HOUSE_RE = re.compile(
    r"(?:^|[\n\r])"
    r"\s*"
    r"(\d{1,6}[xX*]{0,4}|\d{1,6})"
    r"\s+"
    r"([^\n\r,]{2,80})"
)

# Bed/bath/size blurbs mistaken for street addresses, e.g.:
#   "3 Beds / 2 Baths, Miami, FL 33143"
#   "4 Bed/3 Bath- 2732 sqft, Lehigh Acres, FL 33936"
#   "3 bed | 2 bath | 1322 sf, Daytona Beach, FL"
_BED_BATH_DESCRIPTOR_RE = re.compile(
    r"""(?ix)
    ^\s*
    \d+(?:\.\d+)?\s*
    (?:beds?|bedrooms?|brs?|bds?)
    \s*[|/,\-–—]?\s*
    \d+(?:\.\d+)?\s*
    (?:baths?|bathrooms?|ba|bath)\b
    """
)


def has_house_number(address: Optional[str]) -> bool:
    """True when the street line begins with a house number (or mask like 137XX)."""
    return bool(_HAS_HOUSE_RE.match(address or "")) and not is_bed_bath_descriptor_address(address)


def is_bed_bath_descriptor_address(address: Optional[str]) -> bool:
    """
    True when `address` is a bed/bath (optionally sqft) summary line, not a street address.

    Keep real streets like "7926 213th St E" / "513 Kel Ave".
    Skip descriptors like "3 Beds / 2 Baths" or "4 Bed/3 Bath- 2732 sqft".
    """
    text = (address or "").strip()
    if not text:
        return False
    # Evaluate the street-ish portion before city/state when present.
    head = text.split(",", 1)[0].strip()
    return bool(_BED_BATH_DESCRIPTOR_RE.match(head) or _BED_BATH_DESCRIPTOR_RE.match(text))


def _normalize_street_key(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"^\d+[xX*]*\s+", "", s)
    return s


def _token_overlap(a: str, b: str) -> bool:
    if not a or not b:
        return False
    ta = set(a.split())
    tb = set(b.split())
    if not ta or not tb:
        return False
    overlap = ta & tb
    return len(overlap) >= min(2, len(ta), len(tb))


def _street_only_from_line(line: str) -> str:
    """Take the street portion before the first comma when a city may follow."""
    return (line or "").split(",")[0].strip()


def _extract_from_verbatim(verbatim: str, street_hint: str) -> Optional[str]:
    if not verbatim:
        return None

    hint_key = _normalize_street_key(street_hint) if street_hint else ""
    first_match: Optional[str] = None

    for m in _STREET_WITH_HOUSE_RE.finditer(verbatim):
        house, rest = m.group(1), m.group(2).strip()
        street = f"{house} {rest}".strip()
        street = _street_only_from_line(street)
        if is_bed_bath_descriptor_address(street):
            continue
        if not first_match:
            first_match = street
        if not hint_key:
            return street
        rest_key = _normalize_street_key(rest)
        if hint_key in rest_key or rest_key in hint_key or _token_overlap(hint_key, rest_key):
            return street

    return first_match


def _extract_from_whatsapp_bold(post_content: str) -> Optional[str]:
    for line in (post_content or "").splitlines():
        line = line.strip()
        m = re.match(r"^\*([^*]+)\*", line)
        if not m:
            continue
        candidate = _street_only_from_line(m.group(1).strip())
        if candidate and has_house_number(candidate):
            return candidate
    return None


def resolve_street_address_from_fields(
    address: Optional[str],
    complete_info: Optional[dict] = None,
    post_content: Optional[str] = None,
) -> str:
    """
    Return the best street line (house number + street name) from available listing fields.
    """
    ci = complete_info or {}
    struct_addr = (address or ci.get("address") or "").strip()

    if struct_addr and has_house_number(struct_addr):
        return struct_addr

    verbatim = ci.get("complete_info")
    if isinstance(verbatim, str):
        from_verbatim = _extract_from_verbatim(verbatim, struct_addr)
        if from_verbatim:
            return from_verbatim

    from_post = _extract_from_whatsapp_bold(post_content or "")
    if from_post:
        return from_post

    return struct_addr


def resolve_street_address(listing: Any) -> str:
    """Accept a ParsedListing, dict, or similar object with address fields."""
    if isinstance(listing, dict):
        return resolve_street_address_from_fields(
            listing.get("address"),
            listing.get("complete_info") or {},
            listing.get("post_content"),
        )

    ci = getattr(listing, "complete_info", None) or {}
    return resolve_street_address_from_fields(
        getattr(listing, "address", None),
        ci if isinstance(ci, dict) else {},
        getattr(listing, "post_content", None),
    )


def resolve_street_and_city(listing: Any) -> Tuple[str, str]:
    """Return (street_address, city) with house number on the street when available."""
    if isinstance(listing, dict):
        ci = listing.get("complete_info") or {}
        city = (listing.get("city") or ci.get("city") or "").strip()
    else:
        ci = getattr(listing, "complete_info", None) or {}
        city = (getattr(listing, "city", None) or ci.get("city") or "").strip()

    return resolve_street_address(listing), city
