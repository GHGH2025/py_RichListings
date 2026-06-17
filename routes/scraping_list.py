from typing import List, Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from models.scraping_list import ScrapingList
from services.scraping_list_service import (
  create_entry,
  delete_entry,
  doc_to_response,
  get_by_id,
  get_patterns_for_account,
  import_from_json,
  update_entry,
)

router = APIRouter(prefix="/api", tags=["scraping-list"])


class ScrapingListCreate(BaseModel):
  account_label: str
  sender_pattern: str
  list_type: Literal["allow", "skip"] = "allow"
  active: bool = True


class ScrapingListUpdate(BaseModel):
  account_label: Optional[str] = None
  sender_pattern: Optional[str] = None
  list_type: Optional[Literal["allow", "skip"]] = None
  active: Optional[bool] = None


class ScrapingListResponse(BaseModel):
  id: str
  account_label: str
  sender_pattern: str
  list_type: str
  active: bool
  created_at: Optional[str] = None
  updated_at: Optional[str] = None


class ScrapingListPatternsResponse(BaseModel):
  account_label: str
  allow: List[str]
  skip: List[str]


class ImportJsonResponse(BaseModel):
  ok: bool = True
  created: int
  updated: int
  skipped: int
  total: int


@router.get("/scraping-list", response_model=List[ScrapingListResponse])
def list_scraping_entries(
  account_label: Optional[str] = None,
  list_type: Optional[Literal["allow", "skip"]] = None,
  active: Optional[bool] = None,
):
  qs = ScrapingList.objects
  if account_label:
    qs = qs.filter(account_label=account_label.strip())
  if list_type:
    qs = qs.filter(list_type=list_type)
  if active is not None:
    qs = qs.filter(active=active)
  return [
    doc_to_response(doc)
    for doc in qs.order_by("account_label", "list_type", "sender_pattern")
  ]


@router.get(
  "/scraping-list/patterns/{account_label}",
  response_model=ScrapingListPatternsResponse,
)
def get_scraping_patterns(account_label: str):
  label = (account_label or "").strip()
  if not label:
    raise HTTPException(status_code=400, detail="account_label is required")
  allow, skip = get_patterns_for_account(label, force_refresh=True)
  return ScrapingListPatternsResponse(account_label=label, allow=allow, skip=skip)


@router.get("/scraping-list/{doc_id}", response_model=ScrapingListResponse)
def get_scraping_entry(doc_id: str):
  doc = get_by_id(doc_id)
  if not doc:
    raise HTTPException(status_code=404, detail="Scraping list entry not found")
  return doc_to_response(doc)


@router.post("/scraping-list", response_model=ScrapingListResponse, status_code=201)
def create_scraping_entry(payload: ScrapingListCreate):
  try:
    doc = create_entry(
      account_label=payload.account_label,
      sender_pattern=payload.sender_pattern,
      list_type=payload.list_type,
      active=payload.active,
    )
  except ValueError as e:
    msg = str(e)
    if "already exists" in msg:
      raise HTTPException(status_code=409, detail=msg)
    raise HTTPException(status_code=400, detail=msg)
  return doc_to_response(doc)


@router.put("/scraping-list/{doc_id}", response_model=ScrapingListResponse)
def replace_scraping_entry(doc_id: str, payload: ScrapingListCreate):
  doc = get_by_id(doc_id)
  if not doc:
    raise HTTPException(status_code=404, detail="Scraping list entry not found")
  try:
    doc = update_entry(
      doc,
      account_label=payload.account_label,
      sender_pattern=payload.sender_pattern,
      list_type=payload.list_type,
      active=payload.active,
    )
  except ValueError as e:
    msg = str(e)
    if "already exists" in msg:
      raise HTTPException(status_code=409, detail=msg)
    raise HTTPException(status_code=400, detail=msg)
  return doc_to_response(doc)


@router.patch("/scraping-list/{doc_id}", response_model=ScrapingListResponse)
def patch_scraping_entry(doc_id: str, payload: ScrapingListUpdate):
  doc = get_by_id(doc_id)
  if not doc:
    raise HTTPException(status_code=404, detail="Scraping list entry not found")
  try:
    doc = update_entry(
      doc,
      account_label=payload.account_label,
      sender_pattern=payload.sender_pattern,
      list_type=payload.list_type,
      active=payload.active,
    )
  except ValueError as e:
    msg = str(e)
    if "already exists" in msg:
      raise HTTPException(status_code=409, detail=msg)
    raise HTTPException(status_code=400, detail=msg)
  return doc_to_response(doc)


@router.delete("/scraping-list/{doc_id}")
def remove_scraping_entry(doc_id: str):
  doc = get_by_id(doc_id)
  if not doc:
    raise HTTPException(status_code=404, detail="Scraping list entry not found")
  delete_entry(doc)
  return {"ok": True, "deleted_id": doc_id}


@router.post("/scraping-list/import-seed", response_model=ImportJsonResponse)
def import_scraping_list_from_seed():
  try:
    result = import_from_json()
  except FileNotFoundError:
    raise HTTPException(status_code=404, detail="scraping_list_seed.json not found")
  except ValueError as e:
    raise HTTPException(status_code=400, detail=str(e))
  except Exception as e:
    raise HTTPException(status_code=500, detail=str(e))
  return ImportJsonResponse(**result)
