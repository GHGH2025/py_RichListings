from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from integrations.podio.direct_wholesaler import (
    find_wholeseller_item_by_email,
    get_podio_access_token,
)

router = APIRouter(prefix="/api", tags=["podio-wholesellers"])


class PodioWholesellerMatch(BaseModel):
    email: str
    found: bool
    item_id: Optional[int] = None
    error: Optional[str] = None


class PodioWholesellerLookupResponse(BaseModel):
    items: List[PodioWholesellerMatch]


@router.get("/podio/wholesellers", response_model=PodioWholesellerLookupResponse)
def lookup_podio_wholesellers(
    email: Optional[str] = Query(None, description="One wholeseller email"),
    emails: Optional[str] = Query(None, description="Comma-separated emails"),
):
    keys = []
    seen = set()
    raw_values = []
    if email:
        raw_values.append(email)
    if emails:
        raw_values.extend(emails.split(","))
    for raw in raw_values:
        value = raw.strip().lower()
        if not value or "@" not in value or value in seen:
            continue
        seen.add(value)
        keys.append(value)
        if len(keys) >= 15:
            break

    if not keys:
        return PodioWholesellerLookupResponse(items=[])

    try:
        token = get_podio_access_token()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Podio auth failed: {e}")

    items: List[PodioWholesellerMatch] = []
    for lookup_email in keys:
        try:
            item_id = find_wholeseller_item_by_email(token, lookup_email)
            items.append(
                PodioWholesellerMatch(
                    email=lookup_email,
                    found=item_id is not None,
                    item_id=item_id,
                )
            )
        except Exception as e:
            items.append(
                PodioWholesellerMatch(email=lookup_email, found=False, error=str(e))
            )
    return PodioWholesellerLookupResponse(items=items)
