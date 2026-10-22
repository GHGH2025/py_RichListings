#!/usr/bin/env python3
"""Buy-box fix (ii)-A, Blagojche 20.10 - STAGED, not applied.

Problem: the buy-box update page ("Update Your Buy Box") 404s for any email not in
`web_form_buyer_submissions` (304 emails). The real buyer base lives in the Podio Buyers app
(18339401 = ~39,302 items / AI-Website-Lead). The page can only work for them if a Web Form
Submissions Podio item is created for them - but doing the "does this email belong to a real buyer?"
check live on the PUBLIC /request-link endpoint would be a Podio search per request (the exact storm
we just removed in the #1 fix).

Design (Blagojche): mirror the Buyers-app emails (+ item id + contact) into a LOCAL Mongo collection
once a night, paced through the #1 token bucket; the public endpoint checks only that local set; the
Podio WRITE (create the Web Form item) happens ONLY on a local match, with a per-email-per-day limit
and a global daily cap. No live Podio read on the public endpoint.

This module holds: the mirror model, the paced nightly sync, the local lookup, and the cap helpers.
The create-item + local-doc wiring lives in buyers/submissions_api.py (where the Web Form Podio
helpers + submission models already are) and calls find_mirrored_buyer / autocreate_allowed /
record_autocreate from here.
"""
import os
import time
import logging
import requests
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List

from mongoengine import (
    Document, StringField, IntField, ListField, DateTimeField,
)

from integrations.podio.throttle import podio_allow
from integrations.podio.web_form_submissions import get_podio_access_token, PODIO_BASE_URL

log = logging.getLogger("buyers_app_mirror")

BUYERS_APP_ID = int(os.getenv("PODIO_BUYERS_APP_ID", "18339401"))
MIRROR_PAGE_SIZE = int(os.getenv("BUYERS_MIRROR_PAGE_SIZE", "500"))      # Podio filter max page
MIRROR_MAX_PAGES = int(os.getenv("BUYERS_MIRROR_MAX_PAGES", "120"))     # safety stop (~39k/500=79)
MIRROR_PAGE_SLEEP = float(os.getenv("BUYERS_MIRROR_PAGE_SLEEP", "0.3")) # gentle pacing between pages
MIRROR_HTTP_TIMEOUT = float(os.getenv("BUYERS_MIRROR_HTTP_TIMEOUT", "90")) # a 500-item Buyers page is a large payload; 30s (the _podio_request default) is too short
MIRROR_MAX_RETRIES = int(os.getenv("BUYERS_MIRROR_MAX_RETRIES", "4")) # deep-offset Podio filter pages 504/time out transiently; retry with backoff so one blip does not abort the sync

# per-request create caps (the public endpoint abuse guard)
AUTOCREATE_PER_EMAIL_PER_DAY = int(os.getenv("BUYBOX_AUTOCREATE_PER_EMAIL_PER_DAY", "1"))
AUTOCREATE_GLOBAL_PER_DAY = int(os.getenv("BUYBOX_AUTOCREATE_GLOBAL_PER_DAY", "200"))

# Buyers app (18339401) field ids (from the live schema, 2026-10-20)
F_EMAIL_TEXT = 144394672   # 'cash-buyer-emal' (text, legacy main email)
F_EMAIL_EMAIL = 239408543  # 'email' (email type)
F_NAME = 144394666         # 'title' (Name)
F_COMPANY = 144394671      # 'company'
F_PHONE = 206638025        # 'phone' (phone type)
F_PHONE_TEXT = 144394673   # 'cash-buyer-phone' (text)


class BuyersAppMirror(Document):
    """One row per Buyers-app (18339401) item: the emails + contact we need to recognise a buyer and
    create a minimal Web Form item for them, all WITHOUT a live Podio call on the public endpoint."""
    meta = {
        "collection": "buyers_app_mirror",
        "indexes": ["buyers_podio_item_id", "emails"],
    }
    buyers_podio_item_id = IntField(required=True, unique=True)
    emails = ListField(StringField())   # all normalised emails on the item (text + email fields)
    name = StringField()
    company = StringField()
    phone = StringField()
    last_synced_at = DateTimeField()


class BuyBoxAutoCreateLog(Document):
    """Audit + rate-limit ledger: one row per Web Form item auto-created from a mirror match."""
    meta = {
        "collection": "buybox_autocreate_log",
        "indexes": ["email", "created_at"],
    }
    email = StringField(required=True)
    buyers_podio_item_id = IntField()
    webform_podio_item_id = IntField()
    mongo_submission_id = StringField()
    created_at = DateTimeField(default=lambda: datetime.now(timezone.utc))


def _norm_email(v: Any) -> str:
    return (str(v or "")).strip().lower()


def _extract_field(item: Dict[str, Any], field_id: int) -> List[str]:
    """Return the string value(s) of a field on a Podio filter item (text/email/phone all expose
    values[].value as a string)."""
    out: List[str] = []
    for f in item.get("fields", []) or []:
        if f.get("field_id") == field_id:
            for v in f.get("values", []) or []:
                val = v.get("value")
                if isinstance(val, dict):          # some field types nest further
                    val = val.get("value") or val.get("text")
                if val:
                    out.append(str(val))
    return out


def _emails_for_item(item: Dict[str, Any]) -> List[str]:
    emails = []
    for fid in (F_EMAIL_EMAIL, F_EMAIL_TEXT):
        for raw in _extract_field(item, fid):
            # a text field can hold "a@x.com, b@y.com"
            for piece in str(raw).replace(";", ",").split(","):
                e = _norm_email(piece)
                if "@" in e and "." in e.split("@")[-1]:
                    emails.append(e)
    # de-dup, keep order
    seen = set(); uniq = []
    for e in emails:
        if e not in seen:
            seen.add(e); uniq.append(e)
    return uniq


def _filter_page(offset: int) -> Optional[Dict[str, Any]]:
    """One Buyers-app filter page with its OWN longer timeout (a 500-item payload is large; the shared
    _podio_request hard-codes 30s, which times out). Bearer auth with a single 401 refresh-retry,
    matching web_form_submissions._podio_request."""
    url = f"{PODIO_BASE_URL}/item/app/{BUYERS_APP_ID}/filter/"
    body = {"limit": MIRROR_PAGE_SIZE, "offset": offset}
    token = get_podio_access_token()
    last_err = None
    for attempt in range(1, MIRROR_MAX_RETRIES + 1):
        try:
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            resp = requests.post(url, headers=headers, json=body, timeout=MIRROR_HTTP_TIMEOUT)
        except requests.RequestException as e:
            last_err = "req_exc:%s" % e
            time.sleep(min(5 * attempt, 20))
            continue
        if resp.status_code == 401:
            token = get_podio_access_token(force_refresh=True)
            last_err = "401"
            continue
        # deep-offset Podio pages 504/time out transiently - retry these rather than aborting
        if resp.status_code in (429, 500, 502, 503, 504):
            last_err = "http_%s" % resp.status_code
            time.sleep(min(5 * attempt, 20))
            continue
        if not resp.ok:
            log.error("buyers_app_mirror: filter page offset=%s hard-failed %s %s", offset, resp.status_code, resp.text[:200])
            return None
        try:
            return resp.json()
        except ValueError:
            return None
    log.error("buyers_app_mirror: filter page offset=%s failed after %d retries: %s", offset, MIRROR_MAX_RETRIES, last_err)
    return None


def sync_buyers_app_mirror(force: bool = False) -> Dict[str, Any]:
    """Paced nightly sync: page the Buyers app via POST /item/app/{id}/filter/ (limit 500), upsert each
    item's emails + contact into buyers_app_mirror. Every page goes through podio_allow (the #1 token
    bucket); if the bucket is exhausted the sync stops cleanly and resumes next run. ~79 pages for 39k
    items = ~79 reads once a night, well under the 200/hr cap."""
    started = datetime.now(timezone.utc)
    offset = 0
    pages = 0
    upserts = 0
    podio_calls = 0
    stopped_reason = "complete"
    total = None
    try:
        while pages < MIRROR_MAX_PAGES:
            if not podio_allow(f"/item/app/{BUYERS_APP_ID}/filter"):
                stopped_reason = "token_bucket_exhausted"
                break
            podio_calls += 1
            data = _filter_page(offset)
            if not isinstance(data, dict):
                stopped_reason = "podio_error"
                break
            if total is None:
                total = data.get("total")
            items = data.get("items", []) or []
            if not items:
                break
            for item in items:
                iid = item.get("item_id")
                if not iid:
                    continue
                emails = _emails_for_item(item)
                name = (_extract_field(item, F_NAME) or [""])[0]
                company = (_extract_field(item, F_COMPANY) or [""])[0]
                phone = (_extract_field(item, F_PHONE) or _extract_field(item, F_PHONE_TEXT) or [""])[0]
                try:
                    BuyersAppMirror.objects(buyers_podio_item_id=int(iid)).update_one(
                        set__emails=emails,
                        set__name=name,
                        set__company=company,
                        set__phone=phone,
                        set__last_synced_at=datetime.now(timezone.utc),
                        upsert=True,
                    )
                    upserts += 1
                except Exception:
                    log.exception("buyers_app_mirror: upsert failed item_id=%s", iid)
            pages += 1
            offset += MIRROR_PAGE_SIZE
            if total is not None and offset >= int(total):
                break
            if MIRROR_PAGE_SLEEP:
                time.sleep(MIRROR_PAGE_SLEEP)
    except Exception as exc:
        stopped_reason = f"exception: {exc}"
        log.exception("buyers_app_mirror: sync failed")

    result = {
        "ok": stopped_reason in ("complete",),
        "stopped_reason": stopped_reason,
        "pages": pages,
        "podio_calls": podio_calls,
        "upserts": upserts,
        "podio_total": total,
        "elapsed_s": round((datetime.now(timezone.utc) - started).total_seconds(), 1),
    }
    log.info("buyers_app_mirror: sync result=%s", result)
    return result


def find_mirrored_buyer(email: str) -> Optional[Dict[str, Any]]:
    """Local-only lookup (no Podio): does this email belong to a real Buyers-app item?"""
    e = _norm_email(email)
    if not e:
        return None
    row = BuyersAppMirror.objects(emails=e).first()
    if not row:
        return None
    return {
        "buyers_podio_item_id": row.buyers_podio_item_id,
        "email": e,
        "name": row.name or "",
        "company": row.company or "",
        "phone": row.phone or "",
    }


def autocreate_allowed(email: str) -> (bool, str):
    """Rate-limit guard for the public endpoint: per-email/day + global/day caps."""
    e = _norm_email(email)
    now = datetime.now(timezone.utc)
    day_ago = now - timedelta(days=1)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        if BuyBoxAutoCreateLog.objects(email=e, created_at__gte=day_ago).count() >= AUTOCREATE_PER_EMAIL_PER_DAY:
            return (False, "per_email_daily_cap")
        if BuyBoxAutoCreateLog.objects(created_at__gte=day_start).count() >= AUTOCREATE_GLOBAL_PER_DAY:
            return (False, "global_daily_cap")
    except Exception:
        log.exception("buyers_app_mirror: autocreate_allowed check failed; failing closed")
        return (False, "cap_check_error")
    return (True, "ok")


def record_autocreate(email: str, buyers_podio_item_id: Optional[int],
                      webform_podio_item_id: Optional[int], mongo_submission_id: Optional[str]) -> None:
    try:
        BuyBoxAutoCreateLog(
            email=_norm_email(email),
            buyers_podio_item_id=buyers_podio_item_id,
            webform_podio_item_id=webform_podio_item_id,
            mongo_submission_id=mongo_submission_id,
        ).save()
    except Exception:
        log.exception("buyers_app_mirror: record_autocreate failed email=%s", email)
