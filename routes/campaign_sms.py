import os
import re
import threading
import time
from typing import Any, Dict, List, Tuple
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from services import sms_suppression

router = APIRouter(prefix="/campaign-sms", tags=["campaign-sms"])

_job: Dict[str, Any] = {
    "running": False,
    "sent": 0,
    "failed": 0,
    "suppressed": 0,
    "total": 0,
    "message": "",
    "last_error": None,
    "finished": False,
}
_job_lock = threading.Lock()
_last_suppressed_count = 0
_stop_event = threading.Event()


class StartPayload(BaseModel):
    message: str


# ---------------------------------------------------------------------------
# Audience source: Podio "Contacts" app -> "General Buyer" saved view.
# (Previously this campaign texted web-form submissions; Rich wants it to send
#  only to the General Buyer Prospects list, which lives in this Podio view.)
# ---------------------------------------------------------------------------
CONTACTS_APP_ID = 28669685
GENERAL_BUYER_VIEW_ID = 58032461
PHONE_EXTERNAL_ID = "phone-2"   # Contacts "Phone" field
NAME_EXTERNAL_ID = "title"      # Contacts "Name" field
_PAGE_SIZE = 100                # keep under Podio's per-request timeout


def _field_value(item: dict, external_id: str):
    for f in item.get("fields", []):
        if f.get("external_id") == external_id:
            vals = f.get("values", [])
            if vals:
                return vals[0].get("value")
    return None


def _fetch_general_buyer_recipients() -> List[Tuple[str, str]]:
    """Return [(e164_number, first_name)] for every contact in the
    Podio 'General Buyer' view that has a usable phone number."""
    from integrations.podio.direct_wholesaler import _podio_request

    global _last_suppressed_count
    _last_suppressed_count = 0

    recipients: List[Tuple[str, str]] = []
    seen = set()
    offset = 0
    while True:
        resp = _podio_request(
            "POST",
            f"/item/app/{CONTACTS_APP_ID}/filter/{GENERAL_BUYER_VIEW_ID}/",
            json={"limit": _PAGE_SIZE, "offset": offset},
        )
        items = (resp or {}).get("items", [])
        if not items:
            break
        for it in items:
            phone = _field_value(it, PHONE_EXTERNAL_ID)
            name = _field_value(it, NAME_EXTERNAL_ID)
            if not phone:
                continue
            digits = re.sub(r"\D+", "", str(phone))
            if len(digits) < 10:
                continue
            to_number = "+1" + digits[-10:]
            if to_number in seen:
                continue
            seen.add(to_number)
            # Never text anyone who replied STOP. The Podio view has no idea who
            # opted out - it kept handing us the same people after 20 July.
            if sms_suppression.is_suppressed(to_number):
                _last_suppressed_count += 1
                continue
            first_name = str(name).split()[0] if name else "Investor"
            recipients.append((to_number, first_name))
        offset += _PAGE_SIZE
        if len(items) < _PAGE_SIZE:
            break
    return recipients


def _run_campaign(message: str) -> None:
    try:
        from twilio.rest import Client

        account_sid = os.getenv("TWILIO_ACCOUNT_SID")
        auth_token = os.getenv("TWILIO_AUTH_TOKEN")
        from_number = os.getenv("TWILIO_FROM_NUMBER", "+18887791910")

        client = Client(account_sid, auth_token)

        recipients = _fetch_general_buyer_recipients()

        with _job_lock:
            _job["total"] = len(recipients)
            _job["suppressed"] = _last_suppressed_count

        for to_number, first_name in recipients:
            if _stop_event.is_set():
                break

            body = message.replace("{{name}}", first_name)

            try:
                client.messages.create(body=body, from_=from_number, to=to_number)
                with _job_lock:
                    _job["sent"] += 1
            except Exception as e:
                with _job_lock:
                    _job["failed"] += 1
                    _job["last_error"] = str(e)

            time.sleep(0.2)

    except Exception as outer_e:
        with _job_lock:
            _job["last_error"] = str(outer_e)
    finally:
        with _job_lock:
            _job["running"] = False
            _job["finished"] = True


@router.post("/start")
def start_campaign(payload: StartPayload):
    with _job_lock:
        if _job["running"]:
            raise HTTPException(status_code=409, detail="Campaign already running")
        _job.update({
            "running": True,
            "sent": 0,
            "failed": 0,
            "suppressed": 0,
            "total": 0,
            "message": payload.message,
            "last_error": None,
            "finished": False,
        })
        _stop_event.clear()

    thread = threading.Thread(target=_run_campaign, args=(payload.message,), daemon=True)
    thread.start()
    return {"ok": True, "status": "started"}


@router.post("/stop")
def stop_campaign():
    _stop_event.set()
    with _job_lock:
        _job["running"] = False
    return {"ok": True, "status": "stopped"}


@router.get("/status")
def get_status():
    with _job_lock:
        return dict(_job)


@router.get("/preview")
def preview_recipients():
    """Read-only, fast: size of the 'General Buyer' audience (no sending)."""
    from integrations.podio.direct_wholesaler import _podio_request

    resp = _podio_request(
        "POST",
        f"/item/app/{CONTACTS_APP_ID}/filter/{GENERAL_BUYER_VIEW_ID}/",
        json={"limit": 1, "offset": 0},
    )
    count = (resp or {}).get("filtered", 0)
    return {"count": count}
