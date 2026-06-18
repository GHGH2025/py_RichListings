# routes/direct_wholesaler.py
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from models.direct_wholesaler import DirectWholesaler
from services.direct_wholesaler_service import (
    create_wholesaler,
    delete_wholesaler,
    doc_to_response,
    get_by_id,
    get_by_sender_email,
    import_from_json,
    update_wholesaler,
)

router = APIRouter(prefix="/api", tags=["direct-wholesalers"])


class DirectWholesalerCreate(BaseModel):
    sender_email: str
    email: str
    name: str
    phone: str = ""
    updateFlagForPodio: bool = True


class DirectWholesalerUpdate(BaseModel):
    sender_email: Optional[str] = None
    email: Optional[str] = None
    name: Optional[str] = None
    phone: Optional[str] = None
    updateFlagForPodio: Optional[bool] = None


class DirectWholesalerResponse(BaseModel):
    id: str
    sender_email: str
    email: str
    name: str
    phone: str
    updateFlagForPodio: bool
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class ImportJsonResponse(BaseModel):
    ok: bool = True
    created: int
    updated: int
    skipped: int
    total: int


@router.get("/direct-wholesalers", response_model=List[DirectWholesalerResponse])
def list_direct_wholesalers(updateFlagForPodio: Optional[bool] = None):
    qs = DirectWholesaler.objects
    if updateFlagForPodio is not None:
        qs = qs.filter(updateFlagForPodio=updateFlagForPodio)
    return [doc_to_response(doc) for doc in qs.order_by("sender_email")]


@router.get("/direct-wholesalers/by-sender/{sender_email}", response_model=DirectWholesalerResponse)
def get_direct_wholesaler_by_sender(sender_email: str):
    doc = get_by_sender_email(sender_email)
    if not doc:
        raise HTTPException(status_code=404, detail="Direct wholesaler not found")
    return doc_to_response(doc)


@router.get("/direct-wholesalers/{doc_id}", response_model=DirectWholesalerResponse)
def get_direct_wholesaler(doc_id: str):
    doc = get_by_id(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Direct wholesaler not found")
    return doc_to_response(doc)


@router.post("/direct-wholesalers", response_model=DirectWholesalerResponse, status_code=201)
def create_direct_wholesaler(payload: DirectWholesalerCreate):
    try:
        doc = create_wholesaler(
            sender_email=payload.sender_email,
            email=payload.email,
            name=payload.name,
            phone=payload.phone,
            update_flag_for_podio=payload.updateFlagForPodio,
        )
    except ValueError as e:
        msg = str(e)
        if "already exists" in msg:
            raise HTTPException(status_code=409, detail=msg)
        raise HTTPException(status_code=400, detail=msg)
    return doc_to_response(doc)


@router.put("/direct-wholesalers/{doc_id}", response_model=DirectWholesalerResponse)
def replace_direct_wholesaler(doc_id: str, payload: DirectWholesalerCreate):
    doc = get_by_id(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Direct wholesaler not found")
    try:
        doc = update_wholesaler(
            doc,
            sender_email=payload.sender_email,
            email=payload.email,
            name=payload.name,
            phone=payload.phone,
            update_flag_for_podio=payload.updateFlagForPodio,
        )
    except ValueError as e:
        msg = str(e)
        if "already exists" in msg:
            raise HTTPException(status_code=409, detail=msg)
        raise HTTPException(status_code=400, detail=msg)
    return doc_to_response(doc)


@router.patch("/direct-wholesalers/{doc_id}", response_model=DirectWholesalerResponse)
def patch_direct_wholesaler(doc_id: str, payload: DirectWholesalerUpdate):
    doc = get_by_id(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Direct wholesaler not found")
    try:
        doc = update_wholesaler(
            doc,
            sender_email=payload.sender_email,
            email=payload.email,
            name=payload.name,
            phone=payload.phone,
            update_flag_for_podio=payload.updateFlagForPodio,
        )
    except ValueError as e:
        msg = str(e)
        if "already exists" in msg:
            raise HTTPException(status_code=409, detail=msg)
        raise HTTPException(status_code=400, detail=msg)
    return doc_to_response(doc)


@router.delete("/direct-wholesalers/{doc_id}")
def remove_direct_wholesaler(doc_id: str):
    doc = get_by_id(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Direct wholesaler not found")
    delete_wholesaler(doc)
    return {"ok": True, "deleted_id": doc_id}


@router.post("/direct-wholesalers/import-json", response_model=ImportJsonResponse)
def import_direct_wholesalers_from_json():
    try:
        result = import_from_json()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="direct_wholeseller.json not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return ImportJsonResponse(**result)
