"""Shared WordPress addproperty create helper for post_status updates."""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional, Tuple

import requests

WP_TOKEN = os.getenv("WP_API_TOKEN")
WP_BASE = os.getenv("WP_API_BASE", "https://inventory.joinbuyerslist.com/wp-json/addproperty/v1")
WP_CREATE_URL = f"{WP_BASE.rstrip('/')}/create"
REQUEST_TIMEOUT = int(os.getenv("WP_CREATE_TIMEOUT", "25"))


def set_wp_post_status(
    posttitle: str,
    post_status: str,
    *,
    token: Optional[str] = None,
) -> Tuple[bool, int, Any]:
    """
    POST to WordPress addproperty/v1/create with token, posttitle, post_status.

    Returns (ok, status_code, payload_or_error_text).
    """
    auth = (token or WP_TOKEN or "").strip()
    title = (posttitle or "").strip()
    status = (post_status or "").strip()

    if not auth:
        return False, 0, "WP_API_TOKEN is not set"
    if not title:
        return False, 0, "posttitle is empty"
    if not status:
        return False, 0, "post_status is empty"

    body: Dict[str, str] = {
        "token": auth,
        "posttitle": title,
        "post_status": status,
    }

    try:
        resp = requests.post(
            WP_CREATE_URL,
            json=body,
            headers={"Content-Type": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as e:
        logging.exception("set_wp_post_status: request failed title=%s", title)
        return False, 0, str(e)

    try:
        payload: Any = resp.json()
    except ValueError:
        payload = (resp.text or "")[:500]

    ok = 200 <= resp.status_code < 300
    if not ok:
        logging.warning(
            "set_wp_post_status: non-2xx status=%s title=%s body=%s",
            resp.status_code,
            title,
            str(payload)[:300],
        )
    return ok, resp.status_code, payload
