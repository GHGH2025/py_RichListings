"""
Cloud A - address canonicalisation, duplicate detection, needs-review flagging.

The duplicate-prevention core proven on 2026-09-04 (the 3,410 / 117 analysis),
packaged for the live create path. classify() is the single entry point; it takes
a raw address (+ optional city) and returns one of three verdicts:

  NEEDS_REVIEW  the address does not standardise (masked 2xxx, no house number,
                junk like "4 Beds / 3 Bath") - never auto-matched, sent to review.
  DUPLICATE     the canonical address already exists as a posted listing.
  NEW           safe to create.

find_existing(key) is injected so this module is unit-testable without a DB, and
so the DB query strategy can change without touching the logic.
"""
from __future__ import annotations

import os
import re
import logging
from typing import Any, Callable, Optional, Tuple

NEW = "new"
DUPLICATE = "duplicate"
NEEDS_REVIEW = "needs_review"

_WP_SITE_BASE = os.getenv("WP_SITE_BASE_URL", "https://inventory.joinbuyerslist.com").rstrip("/")
_WP_LIVE_TIMEOUT = float(os.getenv("WP_LIVE_CHECK_TIMEOUT", "8"))


def post_is_live(post_id: Any) -> Optional[bool]:
    """Is this WordPress post_id a real, visible post right now?

    Returns True (HTTP 200 on its permalink), False (a definitive non-200 such
    as 404/410 - the post was deleted, or is trashed/private and thus not a
    visible listing), or None (we could not reach the site, so we do not know).

    Why: getproperty and the Mongo finder can both hand back a stale post_id
    whose WP post was deleted (the root cause of POST_LOST measured 2026-09-04:
    3 of 10 re-posts were linked to non-existent posts 50041/47674/49262 and
    silently never went live). Linking a listing to a dead post is worse than
    creating it, so callers suppress creation only on a CONFIRMED-live post.
    """
    if not post_id:
        return False
    url = _WP_SITE_BASE + "/?p=" + str(int(post_id))
    try:
        import requests
        r = requests.head(url, allow_redirects=True, timeout=_WP_LIVE_TIMEOUT)
        if r.status_code in (403, 405):  # host blocks HEAD - confirm with GET
            r = requests.get(url, allow_redirects=True, timeout=_WP_LIVE_TIMEOUT)
        return True if r.status_code == 200 else False
    except Exception:
        return None

_SUF = {"street": "st", "avenue": "ave", "road": "rd", "drive": "dr", "court": "ct",
        "terrace": "ter", "place": "pl", "boulevard": "blvd", "lane": "ln",
        "circle": "cir", "parkway": "pkwy", "highway": "hwy",
        # Blagojche/Claude 30.09 (Rich RED address request): Google writes directions and a few
        # types out in full ("Northwest ... Court") while emails/Ekta use "NW ... Ct". 60-day proof:
        # 14 duplicate WP post pairs differed ONLY in this (e.g. 1502 E Linebaugh Ave / East ... Avenue).
        "trail": "trl", "point": "pt", "square": "sq", "crossing": "xing", "cove": "cv",
        "north": "n", "south": "s", "east": "e", "west": "w",
        "northeast": "ne", "northwest": "nw", "southeast": "se", "southwest": "sw"}

_JUNK = re.compile(r"parking space|vacant lot|^\s*lot\b|\bacres?\b|beds?\s*/|\bbath\b|sqft|for lease", re.I)


def canonical_key(address: Optional[str], city: Optional[str] = None) -> str:
    """Normalised street+city key: lower, drop state/zip/USA, canonical suffixes."""
    t = ((address or "") + " " + (city or "")).lower()
    t = re.sub(r",?\s*usa\s*$", "", t)
    t = re.sub(r"\bfl\b|\bflorida\b|\b\d{5}(-\d{4})?\b", "", t)
    t = re.sub(r"[^a-z0-9x* ]+", " ", t)
    return " ".join(_SUF.get(w, w) for w in t.split()).strip()


def review_reason(address: Optional[str]) -> Optional[str]:
    """Why an address cannot be trusted for auto-match. None => usable.

    Option (b) routing (measured 2026-09-04 on 150 live posted listings):
      masked / garbled / partial house number  -> post but flag (never block)
      genuinely no leading house number         -> needs review (block)
    A clean number or a number range (644-646 / 644\u2013646) is a real number.
    """
    a = (address or "").strip()
    if not a:
        return "empty"
    if _JUNK.search(a):
        return "junk_description"
    street = a.split(",")[0].strip()
    parts = street.split()
    lead = parts[0] if parts else ""
    # masked / partial / garbled house number -> flag, do not block
    if re.fullmatch(r"0+", lead):
        # B.1 (Blagojche 01.10): "0 Main St" = a collapsed mask OR a genuine vacant-lot format.
        # Either way there is no usable house number -> review as no_house_number (street+city+price,
        # per Rich). Logged so we can see the daily volume of these.
        logging.getLogger(__name__).info("review_reason: zero house number -> no_house_number | %s", a)
        return "no_house_number"
    if re.search(r"[xX]{2,}", street):
        return "masked"                       # 2xxx / XXXX
    if lead and re.fullmatch(r"[*xX]+", lead):
        return "masked"                       # ***  xx
    lead_has_digit = bool(re.search(r"\d", lead))
    lead_clean_num = bool(re.match(r"^\d+([-\u2013]\d+)?$", lead))  # 644 or 644-646
    if lead_has_digit and not lead_clean_num:
        return "masked"                       # 5x / 6*0 / 1[40 - digit present but garbled
    if not lead_clean_num:
        return "no_house_number"
    if len(a) > 70 or a.count(",") > 3:
        return "description_like"
    return None


def classify(address: Optional[str], city: Optional[str] = None,
             find_existing: Optional[Callable[[str], Any]] = None) -> Tuple[str, Any]:
    """
    (status, detail):
      (NEEDS_REVIEW, reason) | (DUPLICATE, matched_record) | (NEW, None)
    find_existing(canonical_key) -> a matched existing record, or None.
    """
    reason = review_reason(address)
    if reason:
        return NEEDS_REVIEW, reason
    key = canonical_key(address, city)
    if not key or len(key) < 6:
        return NEEDS_REVIEW, "key_too_short"
    if find_existing is not None:
        hit = find_existing(key)
        if hit:
            return DUPLICATE, hit
    return NEW, None


# ---- DB-backed finder (used in production; not imported by the unit tests) ----
def mongo_finder(exclude_id: Any = None) -> Callable[[str], Any]:
    """
    Returns a find_existing that looks for an already-POSTED ParsedListing whose
    canonical address matches. City-bounded so it does not scan the whole
    collection. Only counts listings that actually reached WordPress (post_id set).
    """
    from models import ParsedListing
    from pipeline.address_utils import resolve_street_address

    def _find(key: str):
        # derive the city back out of the key is unreliable; instead the caller
        # passes address+city, so we re-query by any listing with a post_id whose
        # canonical key matches. Bound the scan with a text-ish filter on the key's
        # first token (house number) to keep it cheap.
        first = key.split(" ", 1)[0]
        qs = ParsedListing.objects(post_id__ne=None, address__istartswith=first)
        if exclude_id is not None:
            qs = qs.filter(id__ne=exclude_id)
        for pl in qs.only("address", "city", "post_id").limit(50):
            if canonical_key(getattr(pl, "address", None), getattr(pl, "city", None)) == key:
                # Only a confirmed-live post counts as a duplicate. A stale
                # post_id (deleted/hidden) or an unreachable check -> keep
                # scanning; better to create than to link to a dead post.
                if post_is_live(getattr(pl, "post_id", None)) is True:
                    return pl
        return None

    return _find
