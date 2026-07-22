# routes/wordpress_proxy.py
"""Public proxy for WordPress addproperty create (URL-encoded JSON body)."""
from __future__ import annotations

import json
import logging
import os
from typing import Optional
from urllib.parse import unquote_plus

import requests
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

WP_BASE = os.getenv("WP_API_BASE", "https://inventory.joinbuyerslist.com/wp-json/addproperty/v1")
WP_CREATE_URL = f"{WP_BASE}/create"
REQUEST_TIMEOUT = 25

router = APIRouter(tags=["wordpress-proxy"])

REQUIRED_FIELDS = ("token", "posttitle", "post_status")


def _parse_encoded_body(raw: str) -> dict:
    """Decode URL-encoded body (or plain JSON) into a dict."""
    text = (raw or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Empty request body")

    # Body may be URL-encoded JSON, or already plain JSON.
    candidates = [text]
    decoded = unquote_plus(text)
    if decoded != text:
        candidates.append(decoded)

    last_err: Optional[Exception] = None
    for candidate in candidates:
        try:
            data = json.loads(candidate)
            if isinstance(data, dict):
                return data
            raise HTTPException(status_code=400, detail="JSON body must be an object")
        except json.JSONDecodeError as e:
            last_err = e
            continue

    raise HTTPException(
        status_code=400,
        detail=f"Invalid JSON body: {last_err}",
    )


@router.post("/public/wp/create")
async def public_wp_create(request: Request):
    """
    Accept URL-encoded JSON in the request body, decode it, and POST to
    WordPress addproperty/v1/create.

    Example body (URL-encoded):
      %7B%0D%0A++++%22token%22%3A+%22...%22%2C%0D%0A++++%22posttitle%22%3A+%22...%22%2C%0D%0A++++%22post_status%22%3A+%22private%22%0D%0A++%7D
    """
    raw = (await request.body()).decode("utf-8", errors="replace")
    data = _parse_encoded_body(raw)

    missing = [k for k in REQUIRED_FIELDS if not str(data.get(k) or "").strip()]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required fields: {', '.join(missing)}",
        )

    body = {
        "token": str(data["token"]).strip(),
        "posttitle": str(data["posttitle"]).strip(),
        "post_status": str(data["post_status"]).strip(),
    }

    try:
        resp = requests.post(
            WP_CREATE_URL,
            json=body,
            headers={"Content-Type": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as e:
        logging.exception("WP create proxy request failed")
        raise HTTPException(status_code=502, detail=f"WordPress request failed: {e}") from e

    try:
        payload = resp.json()
    except ValueError:
        payload = {"raw": resp.text}

    return JSONResponse(status_code=resp.status_code, content=payload)
