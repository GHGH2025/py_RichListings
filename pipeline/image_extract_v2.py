"""
Mechanical image extraction - the live half of what ran as `shadow_extract_v2.py`.

Why this exists: the LLM was being used to COLLECT image URLs out of the email
HTML, and it returned roughly one per listing no matter how many were there. A
plain <img src> scan finds them all. Measured on the pilot sender, the model
found 44 URLs where the scan found 1,391 in the same emails.

So the model no longer collects. It still decides which listing is which; this
module collects the URLs and assigns each one to the listing whose address
appears most recently before it in the document.

Proven in shadow over 30,518 listings: images 30,365 -> 45,728 (+51%),
6,541 listings better, **0 worse**. The no-regression rule below is what makes
that guarantee hold, and it is the reason this is safe to run live.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

MAX_IMAGES = 12  # same cap the extraction prompt used

IMG_TAG = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.I)

# Cut the obvious non-photos before anything else looks at them. Without this
# the curator would go from one vision call per listing to thirty or more.
_DROP_SUBSTRINGS = (
    "rs6.net", "/on.jsp",                          # Constant Contact click tracking
    "imgssl.constantcontact.com/letters/images",   # spacer + UI chrome (S.gif etc)
    "googleusercontent.com/proxy", "ci3.google",   # Gmail image proxy
    "/social/", "facebook", "twitter", "instagram", "linkedin", "youtube",
    "spacer", "pixel", "beacon", "1x1",
)
_DROP_SUFFIXES = (".gif", ".svg", ".ico")


def _is_candidate(url: str) -> bool:
    lu = (url or "").lower()
    if not lu.startswith("http"):
        return False
    for s in _DROP_SUBSTRINGS:
        if s in lu:
            return False
    base = lu.split("?")[0]
    for s in _DROP_SUFFIXES:
        if base.endswith(s):
            return False
    return True


def collect_images(html: str) -> List[tuple]:
    """Every plausible photo URL in document order, as (offset, url), deduped."""
    seen = set()
    out = []
    for m in IMG_TAG.finditer(html or ""):
        url = m.group(1)
        if not _is_candidate(url):
            continue
        key = url.split("?")[0]
        if key in seen:
            continue
        seen.add(key)
        out.append((m.start(), url))
    return out


def _address_needles(address: str) -> List[str]:
    """
    Terms for locating a listing inside the HTML. Addresses are often masked
    (2xxxx SW 114th Pl, *** NE 152nd Terrace), so the house number is useless -
    anchor on the street part instead.
    """
    a = (address or "").strip()
    if not a:
        return []
    street = re.sub(r"^[\*x0-9X#]+\s+", "", a).strip()
    if len(street) < 6:
        return []
    return [street, re.sub(r"\s+", " ", street)]


def _find_position(html_lower: str, address: str) -> int:
    for n in _address_needles(address):
        p = html_lower.find(n.lower())
        if p != -1:
            return p
    return -1


def assign_images_for_email(email_html: str, listings: List[Dict[str, Any]]) -> Dict[int, List[str]]:
    """
    Map 1-based listing index -> list of image URLs.

    The listing whose address appears most recently BEFORE an image owns it.
    Anything above the first address goes to the first listing rather than
    nowhere - in single-property emails the photos usually come first, and an
    earlier version of this that dropped them measured WORSE than doing nothing.
    """
    result: Dict[int, List[str]] = {i: [] for i in range(1, len(listings) + 1)}
    if not email_html or not listings:
        return result

    html_lower = email_html.lower()
    imgs = collect_images(email_html)
    if not imgs:
        return result

    anchored = []
    for i, lst in enumerate(listings, start=1):
        pos = _find_position(html_lower, (lst or {}).get("address") or "")
        if pos != -1:
            anchored.append((pos, i))
    anchored.sort()

    # One listing, or no address could be located: the whole pool belongs to it.
    if len(listings) == 1 or not anchored:
        result[1] = [u for _, u in imgs][:MAX_IMAGES]
        return result

    first_idx = anchored[0][1]
    for ipos, url in imgs:
        owner = first_idx
        for apos, i in anchored:
            if apos <= ipos:
                owner = i
            else:
                break
        if len(result[owner]) < MAX_IMAGES:
            result[owner].append(url)
    return result


def pick_images(model_images: Any, assigned: List[str]) -> List[str]:
    """
    NO-REGRESSION RULE - this is the safety of the whole change.

    Positional assignment concentrates images on the listings it can anchor and
    starves the rest. Measured on the pilot, 1,989 listings would have dropped
    from one image to none. So: use the assignment only when it found at least
    as many as the model did; otherwise keep what the model gave.

    With this in place the shadow run produced 0 listings worse out of 30,518.
    """
    model_list = []
    for u in (model_images or []):
        if isinstance(u, str):
            u2 = u.strip()
            if u2.lower().startswith(("http://", "https://")):
                model_list.append(u2)
    model_list = model_list[:MAX_IMAGES]

    assigned = [u for u in (assigned or []) if isinstance(u, str) and u.strip()][:MAX_IMAGES]

    if len(assigned) >= len(model_list):
        return assigned
    return model_list
