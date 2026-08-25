# # api_app.py
# import os
# import time
# from fastapi import FastAPI, HTTPException
# from pydantic import BaseModel
# from typing import Literal

# from media.rc_linker import router as rc_media_router
# from config.runtime import set_whatsapp_send_mode, get_whatsapp_send_mode
# from buyers.submissions_api import router as buyer_submissions_router



# START_TIME = float(os.getenv("APP_START_TIME", str(time.time())))


# app = FastAPI(title="Worker API", version="1.0.0")
# app.include_router(rc_media_router)
# app.include_router(buyer_submissions_router)

# from buyers.matching_api import router as buyer_matching_router

# app.include_router(buyer_matching_router)


# class ModePayload(BaseModel):
#     mode: Literal["dm", "group"]

# @app.get("/server-status")
# def server_status():
#     return {
#         "status": "working",
#         "uptime_seconds": int(time.time() - START_TIME),
#         "whatsapp_send_mode": get_whatsapp_send_mode(),
#     }

# @app.post("/config/whatsapp-mode")
# def set_mode(payload: ModePayload):
#     try:
#         set_whatsapp_send_mode(payload.mode)
#         return {"ok": True, "mode": get_whatsapp_send_mode()}
#     except ValueError as ve:
#         raise HTTPException(status_code=400, detail=str(ve))
#     except Exception as e:
#         raise HTTPException(status_code=500, detail=str(e))





# api_app.py
import os
import time
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware  # ✅ add
from pydantic import BaseModel
from typing import Literal

from media.rc_linker import router as rc_media_router
from config.runtime import set_whatsapp_send_mode, get_whatsapp_send_mode

from buyers.submissions_api import router as buyer_submissions_router
from buyers.matching_api import router as buyer_matching_router
from routes.direct_wholesaler import router as direct_wholesaler_router
from routes.podio_wholeseller import router as podio_wholeseller_router
from routes.scraping_list import router as scraping_list_router
from routes.special_avail_list import router as special_avail_list_router
from routes.wordpress_proxy import router as wordpress_proxy_router
from routes.openai_ping import router as openai_ping_router

try:
    # Present on rich-ai EC2; optional in local checkouts that lack this module.
    from routes.campaign_sms import router as campaign_sms_router
except ImportError:
    campaign_sms_router = None

from db.mongo_engine_conn import init_db
from special_avails.processor import snapshot_yesterday_special_avail, process_manny_special_avails
from pipeline.catchup_from_verified import (
    find_verified_since,
    run_catchup_from_verified,
)

try:
    # Not always present on EC2 deploys; catch-up must still boot without it.
    from pipeline.test_pipeline import run_test_email_pipeline
except ImportError:
    run_test_email_pipeline = None


START_TIME = float(os.getenv("APP_START_TIME", str(time.time())))

app = FastAPI(title="Worker API", version="1.0.0")


@app.on_event("startup")
def on_startup():
    init_db()


# ✅ CORS must be added before routers
ALLOWED_ORIGINS = [
    "http://localhost:5173",  # Vite
    "http://localhost:3000",  # CRA
    # ✅ Live (add these)
    "http://100.51.131.116",
    "https://100.51.131.116",     # include only if you serve https on the IP (safe to keep)
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(rc_media_router)
app.include_router(buyer_submissions_router)
app.include_router(buyer_matching_router)
app.include_router(direct_wholesaler_router)
app.include_router(podio_wholeseller_router)
app.include_router(scraping_list_router)
app.include_router(special_avail_list_router)
app.include_router(wordpress_proxy_router)
app.include_router(openai_ping_router)
# Buyer-SMS campaign (Twilio 800#). Mounted under /api so nginx /api/ proxy reaches it.
if campaign_sms_router is not None:
    app.include_router(campaign_sms_router, prefix="/api")



class ModePayload(BaseModel):
    mode: Literal["dm", "group"]


class TestEmailPipelinePayload(BaseModel):
    html: str
    account_label: str = "acct1"
    from_email: str = "test-sender@example.com"
    from_name: str = "Test Sender"
    subject: str = "[test-pipeline] seeded email"
    text: str = ""
    # Default True: wipe temporary Mongo docs after the run (debug: set false to inspect).
    cleanup: bool = True


class CatchupFromVerifiedPayload(BaseModel):
    since: str
    dry_run: bool = True
    limit: int = 100


@app.get("/server-status")
def server_status():
    return {
        "status": "working",
        "uptime_seconds": int(time.time() - START_TIME),
        "whatsapp_send_mode": get_whatsapp_send_mode(),
    }


@app.post("/config/whatsapp-mode")
def set_mode(payload: ModePayload):
    try:
        set_whatsapp_send_mode(payload.mode)
        return {"ok": True, "mode": get_whatsapp_send_mode()}
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/tasks/snapshot-yesterday-special-avail")
def run_snapshot_yesterday_special_avail():
    """
    Manually trigger snapshot_yesterday_special_avail().
    Returns whatever that function returns, wrapped in {"ok": True, "result": ...}
    """
    try:
        result = snapshot_yesterday_special_avail()
        return {
            "ok": True,
            "result": result,
        }
    except Exception as e:
        # You can also log here if you want
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/tasks/run-manny-special-avails")
def run_manny_special_avails(background_tasks: BackgroundTasks):
    """
    Trigger process_manny_special_avails() for Manny in the background.
    Returns immediately without waiting for completion.
    """
    try:
        background_tasks.add_task(
            process_manny_special_avails,
            manny_podio_item_ids=[2486909239],
            sheet_urls=[
                "https://docs.google.com/spreadsheets/d/1JosEwFm0XNPUACJIE44-r7xXQopxI4Bz3jLXdRNHavg/edit?gid=1583695700#gid=1583695700"
            ],
        )
        return {
            "ok": True,
            "status": "started_process",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/tasks/test-email-pipeline")
def test_email_pipeline(payload: TestEmailPipelinePayload):
    """
    DEV dry-run for one email HTML body.

    - Seeds a FilteredListingEmail with gmail_message_id prefix `test_`
    - Runs parse → media → dedup → AI rules → post selection → images →
      WhatsApp ad copy → WordPress AI
    - Force-passes gate skips (dedup/rules/selection/image fails) so WhatsApp
      post_content is always generated when parse succeeds
    - Does NOT send WhatsApp, create WordPress posts, write Podio, bump daily
      caps, or fire webhooks
    - DOES run Dropbox gallery upload (same handle_Link path as live), including
      for listings force-advanced past post_selection
    - Deletes temporary Mongo docs afterward (cleanup=true by default)
    - Returns listings[].whatsapp.post_content and
      listings[].listing.other_images_dropbox_link
    """
    if run_test_email_pipeline is None:
        raise HTTPException(
            status_code=501,
            detail="pipeline.test_pipeline is not deployed on this host",
        )
    try:
        result = run_test_email_pipeline(
            html=payload.html,
            account_label=payload.account_label,
            from_email=payload.from_email,
            from_name=payload.from_name,
            subject=payload.subject,
            text=payload.text or "",
            cleanup=payload.cleanup,
        )
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result)
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/tasks/catchup-from-verified")
def catchup_from_verified(
    payload: CatchupFromVerifiedPayload,
    background_tasks: BackgroundTasks,
):
    """
    Catch up listings stuck at status=verified since a timestamp.

    - dry_run=true (default): preview matching listings only (no pipeline work)
    - dry_run=false: run live pipeline in background for those listings
      (dedup → rules → selection → images → WhatsApp ad copy + Podio webhook →
       WordPress → WhatsApp send)
    """
    try:
        if payload.dry_run:
            return find_verified_since(payload.since, limit=payload.limit)

        preview = find_verified_since(payload.since, limit=payload.limit)
        if preview["listing_count"] == 0:
            return {
                "ok": True,
                "status": "nothing_to_do",
                "since": preview["since"],
                "listing_count": 0,
                "message_count": 0,
            }

        background_tasks.add_task(
            run_catchup_from_verified,
            since=payload.since,
            limit=payload.limit,
            gmail_message_ids=preview["gmail_message_ids"],
            listing_count=preview["listing_count"],
        )
        return {
            "ok": True,
            "status": "started",
            "since": preview["since"],
            "listing_count": preview["listing_count"],
            "message_count": preview["message_count"],
            "gmail_message_ids": preview["gmail_message_ids"],
        }
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
