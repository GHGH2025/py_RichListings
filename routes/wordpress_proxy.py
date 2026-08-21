# routes/wordpress_proxy.py
"""Public proxy for WordPress addproperty create (URL-encoded JSON body)."""
from __future__ import annotations

import json
import logging
from copy import deepcopy
from typing import Any, Optional
from urllib.parse import unquote_plus

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from integrations.wordpress.post_status import set_wp_post_status
from models.wp_proxy_request_log import WpProxyRequestLog

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


def _redact_body(data: dict) -> dict:
    """Copy body for Mongo; never store the raw WP token."""
    body = deepcopy(data)
    if "token" in body and body["token"] is not None:
        body["token"] = "***REDACTED***"
    return body


def _client_ip(request: Request) -> str:
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if forwarded:
        return forwarded
    if request.client and request.client.host:
        return request.client.host
    return ""


def _save_request_log(
    *,
    request: Request,
    request_body: Optional[dict],
    posttitle: str = "",
    post_status: str = "",
    wp_ok: Optional[bool] = None,
    wp_status_code: Optional[int] = None,
    wp_response: Any = None,
    error: str = "",
) -> None:
    try:
        WpProxyRequestLog(
            client_ip=_client_ip(request),
            request_body=_redact_body(request_body) if request_body else {},
            posttitle=(posttitle or "").strip(),
            post_status=(post_status or "").strip(),
            wp_ok=wp_ok,
            wp_status_code=wp_status_code,
            wp_response=wp_response,
            error=(error or "")[:2000],
        ).save()
    except Exception:
        logging.exception("Failed to save wp_proxy_request_logs entry")


@router.post("/public/wp/create")
async def public_wp_create(request: Request):
    """
    Accept URL-encoded JSON in the request body, decode it, and POST to
    WordPress addproperty/v1/create.

    Example body (URL-encoded):
      %7B%0D%0A++++%22token%22%3A+%22...%22%2C%0D%0A++++%22posttitle%22%3A+%22...%22%2C%0D%0A++++%22post_status%22%3A+%22private%22%0D%0A++%7D
    """
    raw = (await request.body()).decode("utf-8", errors="replace")
    try:
        data = _parse_encoded_body(raw)
    except HTTPException as e:
        _save_request_log(
            request=request,
            request_body={"raw": (raw or "")[:2000]},
            error=str(e.detail),
            wp_ok=False,
            wp_status_code=e.status_code,
        )
        raise

    missing = [k for k in REQUIRED_FIELDS if not str(data.get(k) or "").strip()]
    if missing:
        detail = f"Missing required fields: {', '.join(missing)}"
        _save_request_log(
            request=request,
            request_body=data,
            posttitle=str(data.get("posttitle") or ""),
            post_status=str(data.get("post_status") or ""),
            error=detail,
            wp_ok=False,
            wp_status_code=400,
        )
        raise HTTPException(status_code=400, detail=detail)

    posttitle = str(data["posttitle"]).strip()
    post_status = str(data["post_status"]).strip()

    _ok, status_code, payload = set_wp_post_status(
        posttitle,
        post_status,
        token=str(data["token"]).strip(),
    )

    if status_code == 0:
        logging.error("WP create proxy failed: %s", payload)
        _save_request_log(
            request=request,
            request_body=data,
            posttitle=posttitle,
            post_status=post_status,
            wp_ok=False,
            wp_status_code=502,
            wp_response={"error": str(payload)},
            error=f"WordPress request failed: {payload}",
        )
        raise HTTPException(status_code=502, detail=f"WordPress request failed: {payload}")

    if not isinstance(payload, (dict, list)):
        payload = {"raw": payload}

    _save_request_log(
        request=request,
        request_body=data,
        posttitle=posttitle,
        post_status=post_status,
        wp_ok=_ok,
        wp_status_code=status_code,
        wp_response=payload,
    )

    return JSONResponse(status_code=status_code, content=payload)
