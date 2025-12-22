# api_app.py
import os
import time
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Literal

from rc_media_linker import router as rc_media_router
from config_runtime import set_whatsapp_send_mode, get_whatsapp_send_mode
from special_avails import snapshot_yesterday_special_avail

START_TIME = float(os.getenv("APP_START_TIME", str(time.time())))

app = FastAPI(title="Worker API", version="1.0.0")
app.include_router(rc_media_router)

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