"""Daily buyer deal-email bounce reconciliation (API + optional AI parsing)."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set
from zoneinfo import ZoneInfo

import requests
from openai import OpenAI

from buyers.email_delivery_utils import (
    extract_bounced_emails_from_payload,
    extract_message_id_from_send_result,
    mark_buyer_email_invalid,
    normalize_email,
)
from models import BuyerDealEmailSend, BuyerEmailBounceJobRun, WebFormBuyerSubmission

EASTERN = ZoneInfo("America/New_York")

POF_EMAIL_API_URL = os.getenv(
    "POF_EMAIL_API_URL",
    "http://ec2-3-90-20-111.compute-1.amazonaws.com:8000/rich_ai_deal_Email",
).strip()

BUYER_EMAIL_BOUNCE_CHECK_API_URL = os.getenv("BUYER_EMAIL_BOUNCE_CHECK_API_URL", "").strip()
BUYER_EMAIL_BOUNCE_CHECK_USE_AI = os.getenv("BUYER_EMAIL_BOUNCE_CHECK_USE_AI", "true").strip().lower() in (
    "1",
    "true",
    "yes",
)
BUYER_EMAIL_BOUNCE_CHECK_MODEL = os.getenv("BUYER_EMAIL_BOUNCE_CHECK_MODEL", "gpt-4.1-mini")
BUYER_EMAIL_BOUNCE_CHECK_TIMEOUT = int(os.getenv("BUYER_EMAIL_BOUNCE_CHECK_TIMEOUT", "60"))

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
_openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

_AI_BOUNCE_SYSTEM_PROMPT = """\
You analyze email delivery / bounce reports for a real-estate buyer notification system.

You will receive:
1) candidate_emails: emails we sent deal notifications to
2) bounce_report: raw text or JSON from an email provider bounce/delivery report

Identify ONLY emails with a PERMANENT delivery failure (hard bounce), such as:
- address not found / user unknown / mailbox not found
- invalid recipient / no such user
- permanent failure / hard bounce

Do NOT flag:
- successful deliveries
- soft/temporary bounces (mailbox full, try again later)
- complaints/spam reports unless the address is permanently invalid
- emails not in candidate_emails

Return ONLY JSON:
{
  "bounced_emails": ["email1@example.com"],
  "notes": ["short reason per email or empty list"]
}
"""


def _default_bounce_check_api_url() -> str:
    if BUYER_EMAIL_BOUNCE_CHECK_API_URL:
        return BUYER_EMAIL_BOUNCE_CHECK_API_URL
    base = POF_EMAIL_API_URL.rstrip("/")
    if base.endswith("/rich_ai_deal_Email"):
        return base.replace("/rich_ai_deal_Email", "/rich_ai_deal_Email_bounces")
    return f"{base}/bounces"


def log_buyer_deal_email_send(
    *,
    buyer_id: str,
    to_email: str,
    listing_id: str,
    subject: str,
    send_result: dict,
) -> None:
    """Persist a deal-email send attempt for later daily bounce reconciliation."""
    try:
        BuyerDealEmailSend(
            buyer_id=str(buyer_id),
            to_email=(to_email or "").strip(),
            listing_id=str(listing_id or ""),
            subject=(subject or "").strip(),
            sent_at=datetime.utcnow(),
            send_ok=bool(send_result.get("ok")),
            message_id=extract_message_id_from_send_result(send_result),
            provider_status_code=send_result.get("status_code"),
            provider_response={
                "error": send_result.get("error"),
                "response_text": (send_result.get("response_text") or "")[:4000],
                "response_json": send_result.get("response_json"),
            },
            bounce_check_status="pending",
        ).save()
    except Exception:
        logging.exception(
            "log_buyer_deal_email_send failed buyer_id=%s email=%s listing_id=%s",
            buyer_id,
            to_email,
            listing_id,
        )


def _yesterday_window_utc() -> tuple[datetime, datetime, str]:
    """Return UTC start/end for yesterday in America/New_York, plus YYYY-MM-DD label."""
    now_et = datetime.now(EASTERN)
    yesterday_et = (now_et - timedelta(days=1)).date()
    start_et = datetime(yesterday_et.year, yesterday_et.month, yesterday_et.day, 0, 0, 0, tzinfo=EASTERN)
    end_et = datetime(yesterday_et.year, yesterday_et.month, yesterday_et.day, 23, 59, 59, 999999, tzinfo=EASTERN)
    return start_et.astimezone(ZoneInfo("UTC")).replace(tzinfo=None), end_et.astimezone(ZoneInfo("UTC")).replace(tzinfo=None), yesterday_et.isoformat()


def _fetch_bounce_report(
    *,
    date_label: str,
    window_start: datetime,
    window_end: datetime,
    sends: List[BuyerDealEmailSend],
) -> Optional[Any]:
    url = _default_bounce_check_api_url()
    if not url:
        logging.warning("check_yesterday_buyer_email_bounces: no bounce check API URL configured")
        return None

    payload = {
        "date": date_label,
        "date_from": window_start.isoformat() + "Z",
        "date_to": window_end.isoformat() + "Z",
        "timezone": "America/New_York",
        "sends": [
            {
                "send_id": str(s.id),
                "buyer_id": s.buyer_id,
                "email": s.to_email,
                "message_id": s.message_id or "",
                "listing_id": s.listing_id or "",
                "subject": s.subject or "",
                "sent_at": s.sent_at.isoformat() + "Z" if s.sent_at else "",
            }
            for s in sends
        ],
    }

    try:
        resp = requests.post(url, json=payload, timeout=BUYER_EMAIL_BOUNCE_CHECK_TIMEOUT)
        if resp.status_code >= 400:
            logging.warning(
                "Bounce check API non-2xx %s url=%s body=%s",
                resp.status_code,
                url,
                resp.text[:500],
            )
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text
    except requests.RequestException as exc:
        logging.warning("Bounce check API request failed url=%s err=%s", url, exc)
        return None


def _ai_classify_bounced_emails(candidate_emails: List[str], bounce_report: str) -> Set[str]:
    if not _openai_client or not BUYER_EMAIL_BOUNCE_CHECK_USE_AI:
        return set()
    if not candidate_emails or not (bounce_report or "").strip():
        return set()

    user_payload = {
        "candidate_emails": candidate_emails,
        "bounce_report": bounce_report[:12000],
    }

    try:
        chat = _openai_client.chat.completions.create(
            model=BUYER_EMAIL_BOUNCE_CHECK_MODEL,
            messages=[
                {"role": "system", "content": _AI_BOUNCE_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
            response_format={"type": "json_object"},
            temperature=0,
        )
        data = json.loads(chat.choices[0].message.content or "{}")
        raw = data.get("bounced_emails") or []
        if not isinstance(raw, list):
            return set()
        allowed = {normalize_email(e) for e in candidate_emails if e}
        return {normalize_email(str(e)) for e in raw if normalize_email(str(e)) in allowed}
    except Exception:
        logging.exception("AI bounce classification failed")
        return set()


def _resolve_bounced_emails(
    bounce_report: Any,
    candidate_emails: List[str],
) -> Set[str]:
    bounced: Set[str] = set()
    allowed = {normalize_email(e) for e in candidate_emails if e}

    if isinstance(bounce_report, dict):
        bounced |= {e for e in extract_bounced_emails_from_payload(bounce_report) if e in allowed}

    report_text = bounce_report if isinstance(bounce_report, str) else json.dumps(bounce_report, ensure_ascii=False)
    report_lower = report_text.lower()
    report_suggests_bounces = any(
        token in report_lower
        for token in ("bounce", "bounced", "undeliverable", "invalid recipient", "address not found", "failed")
    )

    if report_suggests_bounces:
        bounced |= _ai_classify_bounced_emails(candidate_emails, report_text)

    return bounced


def _persist_bounce_job_run(result: Dict[str, Any]) -> None:
    try:
        BuyerEmailBounceJobRun(
            run_at=datetime.utcnow(),
            target_date=str(result.get("date") or ""),
            ok=bool(result.get("ok")),
            skipped=bool(result.get("skipped")),
            reason=str(result.get("reason") or ""),
            checked_sends=int(result.get("checked_sends") or 0),
            bounced_emails=int(result.get("bounced_emails") or 0),
            buyers_marked_invalid=int(result.get("buyers_marked_invalid") or 0),
            bounced_email_list=list(result.get("bounced_email_list") or []),
        ).save()
    except Exception:
        logging.exception("Failed to persist buyer email bounce job run: %s", result)


def _buyer_prefers_email(buyer_id: str) -> bool:
    buyer = WebFormBuyerSubmission.objects(id=buyer_id).only("contact").first()
    if not buyer or not buyer.contact:
        return False
    prefs = [p.lower().strip() for p in (buyer.contact.preferences or []) if p]
    return "email" in prefs


def check_yesterday_buyer_email_bounces() -> Dict[str, Any]:
    """
    Daily job: load deal emails sent yesterday, fetch bounce report, mark invalid buyers.

    Works together with immediate invalid detection on send (email_delivery_utils).
    """
    window_start, window_end, date_label = _yesterday_window_utc()
    backlog_start = window_start - timedelta(days=6)

    sends: List[BuyerDealEmailSend] = list(
        BuyerDealEmailSend.objects(
            sent_at__gte=backlog_start,
            sent_at__lte=window_end,
            bounce_check_status="pending",
            send_ok=True,
        ).order_by("sent_at")
    )

    if not sends:
        logging.info(
            "check_yesterday_buyer_email_bounces: no pending sends for %s (ET yesterday)",
            date_label,
        )
        result = {
            "ok": True,
            "date": date_label,
            "checked_sends": 0,
            "bounced_emails": 0,
            "buyers_marked_invalid": 0,
            "skipped": True,
            "reason": "no_pending_sends",
        }
        _persist_bounce_job_run(result)
        return result

    candidate_emails = sorted({normalize_email(s.to_email) for s in sends if s.to_email})
    bounce_report = _fetch_bounce_report(
        date_label=date_label,
        window_start=window_start,
        window_end=window_end,
        sends=sends,
    )

    if bounce_report is None:
        for s in sends:
            BuyerDealEmailSend.objects(id=s.id).update_one(
                inc__bounce_check_attempts=1,
                set__updated_at=datetime.utcnow(),
            )
        result = {
            "ok": False,
            "date": date_label,
            "checked_sends": len(sends),
            "bounced_emails": 0,
            "buyers_marked_invalid": 0,
            "skipped": True,
            "reason": "bounce_api_unavailable",
        }
        _persist_bounce_job_run(result)
        return result

    bounced_emails = _resolve_bounced_emails(bounce_report, candidate_emails)
    buyers_marked = 0
    now = datetime.utcnow()

    email_to_buyer_ids: Dict[str, Set[str]] = {}
    for s in sends:
        em = normalize_email(s.to_email)
        if em:
            email_to_buyer_ids.setdefault(em, set()).add(s.buyer_id)

    for em in bounced_emails:
        buyer_ids = email_to_buyer_ids.get(em, set())
        for buyer_id in buyer_ids:
            if not _buyer_prefers_email(buyer_id):
                continue
            mark_buyer_email_invalid(buyer_id, reason=f"daily_bounce_check:{date_label}:{em}")
            buyers_marked += 1

    for s in sends:
        em = normalize_email(s.to_email)
        status = "bounced" if em in bounced_emails else "clean"
        BuyerDealEmailSend.objects(id=s.id).update_one(
            set__bounce_check_status=status,
            set__bounce_checked_at=now,
            set__bounce_reason=(f"detected_in_daily_check:{date_label}" if status == "bounced" else ""),
            set__updated_at=now,
        )

    logging.info(
        "check_yesterday_buyer_email_bounces: date=%s sends=%s bounced=%s buyers_marked=%s",
        date_label,
        len(sends),
        len(bounced_emails),
        buyers_marked,
    )

    result = {
        "ok": True,
        "date": date_label,
        "checked_sends": len(sends),
        "bounced_emails": len(bounced_emails),
        "bounced_email_list": sorted(bounced_emails),
        "buyers_marked_invalid": buyers_marked,
    }
    _persist_bounce_job_run(result)
    return result
