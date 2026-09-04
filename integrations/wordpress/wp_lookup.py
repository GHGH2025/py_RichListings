"""
Three-state WordPress lookup.

Why this exists: both callers used a helper that returned None for three very
different outcomes - "WordPress answered and has no such post", "WordPress
answered with an error code", and "we never reached WordPress at all". Each
caller then treated all three the same way, as "the post is not there".

That is what drove the loop. In price_media_updates a timeout was recorded as
wp_check="not_found"; in sync_poster it meant "create it", so a listing that
was already on the site got a second post every time the endpoint hiccuped.

A failure to ask is not an answer. This module keeps the three apart.
"""
from __future__ import annotations

import requests
from typing import Any, Dict, Iterable, Optional, Tuple

FOUND = "found"
NOT_FOUND = "not_found"
UNREACHABLE = "unreachable"

# Status codes that are an ANSWER of "no such post", not a transport failure.
NO_MATCH_CODES = (404,)


def _first_post(js: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(js, dict):
        return None
    data = js.get("data")
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict) and isinstance(first.get("post_id"), int):
            return first
    return None


def wp_get(get_url: str, address_key: str, token: Optional[str],
           timeout: int = 25) -> Tuple[bool, Any, str]:
    """(answered, json, detail) - answered is False when we got no usable reply."""
    try:
        resp = requests.get(get_url,
                            params={"address": address_key, "token": token},
                            timeout=timeout)
    except Exception as exc:
        return False, None, "%s: %s" % (type(exc).__name__, str(exc)[:120])
    if resp.status_code in NO_MATCH_CODES:
        # Verified against the live endpoint on 2026-09-03: a bogus address
        # returns 404, a matching one returns 200. So 404 is WordPress telling
        # us "nothing matched" - a real answer, not a failure to reach it.
        return True, {"data": []}, "http_404_no_match"
    if resp.status_code != 200:
        return False, None, "http_%d" % resp.status_code
    try:
        return True, resp.json(), ""
    except Exception as exc:
        return False, None, "bad_json: %s" % str(exc)[:80]


def search_keys(get_url: str, token: Optional[str], keys: Iterable[str],
                timeout: int = 25):
    """
    Try each key in order. Returns (state, post_id, item, detail).

      FOUND       a post was found
      NOT_FOUND   every lookup got a clean reply and none held a post
      UNREACHABLE at least one lookup never got a reply, so "not found" is a
                  conclusion we have not earned - the caller must not act on it

    No keys at all is UNREACHABLE too, not NOT_FOUND: an address we cannot even
    compose a search key for tells us nothing about what is on the site.
    """
    first_failure = None
    tried = 0
    for key in keys:
        if not key:
            continue
        tried += 1
        answered, js, detail = wp_get(get_url, key, token, timeout)
        if not answered:
            if first_failure is None:
                first_failure = "%s [%s]" % (detail, str(key)[:60])
            continue
        post = _first_post(js)
        if post:
            return FOUND, post["post_id"], post, ""
    if tried == 0:
        return UNREACHABLE, None, None, "no_search_keys"
    if first_failure:
        return UNREACHABLE, None, None, first_failure
    return NOT_FOUND, None, None, ""
