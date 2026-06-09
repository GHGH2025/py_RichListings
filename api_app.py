# # api_app.py
# import os
# import time
# from fastapi import FastAPI, HTTPException
# from pydantic import BaseModel
# from typing import Literal

# from rc_media_linker import router as rc_media_router
# from config_runtime import set_whatsapp_send_mode, get_whatsapp_send_mode
# from buyer_submissions_api import router as buyer_submissions_router



# START_TIME = float(os.getenv("APP_START_TIME", str(time.time())))


# app = FastAPI(title="Worker API", version="1.0.0")
# app.include_router(rc_media_router)
# app.include_router(buyer_submissions_router)

# from buyer_matching_api import router as buyer_matching_router

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

from rc_media_linker import router as rc_media_router
from config_runtime import set_whatsapp_send_mode, get_whatsapp_send_mode

from buyer_submissions_api import router as buyer_submissions_router
from buyer_matching_api import router as buyer_matching_router
from routes.direct_wholesaler import router as direct_wholesaler_router

from mongo_engine_conn import init_db
from special_avails import snapshot_yesterday_special_avail, process_manny_special_avails


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


class ModePayload(BaseModel):
    mode: Literal["dm", "group"]


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
