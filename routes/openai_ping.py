"""Minimal OpenAI connectivity probe: send 'hi', return raw API success or error."""
from __future__ import annotations

import os

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from openai import APIConnectionError, APIStatusError, OpenAI

router = APIRouter(tags=["openai-ping"])


@router.get("/openai-hi")
def openai_hi():
    """
    Call OpenAI chat completions with the user message "hi".
    Returns the OpenAI response body on success, or the OpenAI error body on failure.
    """
    api_key = os.getenv("OPENAI_API_KEY")
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    client = OpenAI(api_key=api_key)

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "hi"}],
        )
        return JSONResponse(content=resp.model_dump(), status_code=200)
    except APIStatusError as e:
        body = e.body if isinstance(e.body, dict) else {"error": e.body or str(e)}
        return JSONResponse(content=body, status_code=int(e.status_code or 500))
    except APIConnectionError as e:
        return JSONResponse(
            content={"error": {"message": str(e), "type": "api_connection_error"}},
            status_code=502,
        )
    except Exception as e:
        return JSONResponse(
            content={"error": {"message": str(e), "type": type(e).__name__}},
            status_code=500,
        )
