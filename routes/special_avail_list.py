from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from models.special_avail_list import SpecialAvailList
from services.special_avail_list_service import (
  create_entry,
  delete_entry,
  doc_to_response,
  get_by_id,
  get_runtime_config,
  import_from_json,
  update_entry,
)

router = APIRouter(prefix="/api", tags=["special-avail-list"])


class SpecialAvailListCreate(BaseModel):
  wholesaler_name: str
  sender_emails: List[str]
  podio_item_ids: List[int] = []
  active: bool = True


class SpecialAvailListUpdate(BaseModel):
  wholesaler_name: Optional[str] = None
  sender_emails: Optional[List[str]] = None
  podio_item_ids: Optional[List[int]] = None
  active: Optional[bool] = None


class SpecialAvailListResponse(BaseModel):
  id: str
  wholesaler_name: str
  sender_emails: List[str]
  podio_item_ids: List[int]
  active: bool
  created_at: Optional[str] = None
  updated_at: Optional[str] = None


class SpecialAvailListConfigResponse(BaseModel):
  wholesaler_config: dict
  podio_bucket: dict
  sender_emails: List[str]


class ImportJsonResponse(BaseModel):
  ok: bool = True
  created: int
  updated: int
  skipped: int
  total: int


@router.get("/special-avail-list", response_model=List[SpecialAvailListResponse])
def list_special_avail_list(active: Optional[bool] = None):
  qs = SpecialAvailList.objects
  if active is not None:
    qs = qs.filter(active=active)
  return [doc_to_response(doc) for doc in qs.order_by("wholesaler_name")]


@router.get(
  "/special-avail-list/config",
  response_model=SpecialAvailListConfigResponse,
)
def get_special_avail_list_config():
  cfg = get_runtime_config(force_refresh=True)
  return SpecialAvailListConfigResponse(**cfg)


@router.get("/special-avail-list/{doc_id}", response_model=SpecialAvailListResponse)
def get_special_avail_list_entry(doc_id: str):
  doc = get_by_id(doc_id)
  if not doc:
    raise HTTPException(status_code=404, detail="Special avail list entry not found")
  return doc_to_response(doc)


@router.post("/special-avail-list", response_model=SpecialAvailListResponse, status_code=201)
def create_special_avail_list_entry(payload: SpecialAvailListCreate):
  try:
    doc = create_entry(
      wholesaler_name=payload.wholesaler_name,
      sender_emails=payload.sender_emails,
      podio_item_ids=payload.podio_item_ids,
      active=payload.active,
    )
  except ValueError as e:
    msg = str(e)
    if "already exists" in msg:
      raise HTTPException(status_code=409, detail=msg)
    raise HTTPException(status_code=400, detail=msg)
  return doc_to_response(doc)


@router.put("/special-avail-list/{doc_id}", response_model=SpecialAvailListResponse)
def replace_special_avail_list_entry(doc_id: str, payload: SpecialAvailListCreate):
  doc = get_by_id(doc_id)
  if not doc:
    raise HTTPException(status_code=404, detail="Special avail list entry not found")
  try:
    doc = update_entry(
      doc,
      wholesaler_name=payload.wholesaler_name,
      sender_emails=payload.sender_emails,
      podio_item_ids=payload.podio_item_ids,
      active=payload.active,
    )
  except ValueError as e:
    msg = str(e)
    if "already exists" in msg:
      raise HTTPException(status_code=409, detail=msg)
    raise HTTPException(status_code=400, detail=msg)
  return doc_to_response(doc)


@router.patch("/special-avail-list/{doc_id}", response_model=SpecialAvailListResponse)
def patch_special_avail_list_entry(doc_id: str, payload: SpecialAvailListUpdate):
  doc = get_by_id(doc_id)
  if not doc:
    raise HTTPException(status_code=404, detail="Special avail list entry not found")
  try:
    doc = update_entry(
      doc,
      wholesaler_name=payload.wholesaler_name,
      sender_emails=payload.sender_emails,
      podio_item_ids=payload.podio_item_ids,
      active=payload.active,
    )
  except ValueError as e:
    msg = str(e)
    if "already exists" in msg:
      raise HTTPException(status_code=409, detail=msg)
    raise HTTPException(status_code=400, detail=msg)
  return doc_to_response(doc)


@router.delete("/special-avail-list/{doc_id}")
def remove_special_avail_list_entry(doc_id: str):
  doc = get_by_id(doc_id)
  if not doc:
    raise HTTPException(status_code=404, detail="Special avail list entry not found")
  delete_entry(doc)
  return {"ok": True, "deleted_id": doc_id}


@router.post("/special-avail-list/import-seed", response_model=ImportJsonResponse)
def import_special_avail_list_from_seed():
  try:
    result = import_from_json()
  except FileNotFoundError:
    raise HTTPException(status_code=404, detail="special_avail_list_seed.json not found")
  except ValueError as e:
    raise HTTPException(status_code=400, detail=str(e))
  except Exception as e:
    raise HTTPException(status_code=500, detail=str(e))
  return ImportJsonResponse(**result)
