# api_app.py
import os
import time
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Literal

from config_runtime import (
    set_whatsapp_send_mode,
    get_whatsapp_send_mode,
)
# START_TIME is exposed via env so both runners share a consistent uptime
START_TIME = float(os.getenv("APP_START_TIME", str(time.time())))

app = FastAPI(title="Worker API", version="1.0.0")

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
