from __future__ import annotations

import json
import os
import time
from typing import Dict, List, Optional, Tuple

from bson import ObjectId

from models.scraping_list import ScrapingList
from core.paths import data_path

DEFAULT_SEED_PATH = str(data_path("scraping_list_seed.json"))

_CACHE_TTL_SECONDS = 60
_cache: Dict[str, Tuple[List[str], List[str]]] = {}
_cache_at: float = 0.0


def normalize_pattern(value: str) -> str:
  return (value or "").strip()


def invalidate_cache() -> None:
  global _cache, _cache_at
  _cache = {}
  _cache_at = 0.0


def get_patterns_for_account(
  account_label: str,
  *,
  force_refresh: bool = False,
) -> Tuple[List[str], List[str]]:
  label = (account_label or "").strip()
  if not label:
    return [], []

  global _cache, _cache_at
  now = time.time()
  if (
    not force_refresh
    and label in _cache
    and (now - _cache_at) < _CACHE_TTL_SECONDS
  ):
    return _cache[label]

  allow: List[str] = []
  skip: List[str] = []
  for doc in ScrapingList.objects(account_label=label, active=True).only(
    "sender_pattern", "list_type"
  ):
    pattern = normalize_pattern(doc.sender_pattern)
    if not pattern:
      continue
    if doc.list_type == "skip":
      skip.append(pattern)
    else:
      allow.append(pattern)

  _cache[label] = (allow, skip)
  _cache_at = now
  return allow, skip


def doc_to_response(doc: ScrapingList) -> dict:
  return {
    "id": str(doc.id),
    "account_label": doc.account_label,
    "sender_pattern": doc.sender_pattern,
    "list_type": doc.list_type,
    "active": bool(doc.active),
    "created_at": doc.created_at.isoformat() if doc.created_at else None,
    "updated_at": doc.updated_at.isoformat() if doc.updated_at else None,
  }


def get_by_id(doc_id: str) -> Optional[ScrapingList]:
  try:
    oid = ObjectId(str(doc_id))
  except Exception:
    return None
  return ScrapingList.objects(id=oid).first()


def create_entry(
  *,
  account_label: str,
  sender_pattern: str,
  list_type: str = "allow",
  active: bool = True,
) -> ScrapingList:
  label = (account_label or "").strip()
  pattern = normalize_pattern(sender_pattern)
  if not label or not pattern:
    raise ValueError("account_label and sender_pattern are required")

  list_type = (list_type or "allow").strip().lower()
  if list_type not in ("allow", "skip"):
    raise ValueError("list_type must be 'allow' or 'skip'")

  existing = ScrapingList.objects(
    account_label=label,
    sender_pattern=pattern,
    list_type=list_type,
  ).only("id").first()
  if existing:
    raise ValueError(
      f"entry already exists for account={label}, pattern={pattern}, list_type={list_type}"
    )

  doc = ScrapingList(
    account_label=label,
    sender_pattern=pattern,
    list_type=list_type,
    active=active,
  )
  doc.save()
  invalidate_cache()
  return doc


def update_entry(
  doc: ScrapingList,
  *,
  account_label: Optional[str] = None,
  sender_pattern: Optional[str] = None,
  list_type: Optional[str] = None,
  active: Optional[bool] = None,
) -> ScrapingList:
  label = doc.account_label
  pattern = doc.sender_pattern
  entry_type = doc.list_type

  if account_label is not None:
    label = (account_label or "").strip()
    if not label:
      raise ValueError("account_label cannot be empty")
    doc.account_label = label

  if sender_pattern is not None:
    pattern = normalize_pattern(sender_pattern)
    if not pattern:
      raise ValueError("sender_pattern cannot be empty")
    doc.sender_pattern = pattern

  if list_type is not None:
    entry_type = (list_type or "allow").strip().lower()
    if entry_type not in ("allow", "skip"):
      raise ValueError("list_type must be 'allow' or 'skip'")
    doc.list_type = entry_type

  if active is not None:
    doc.active = active

  existing = ScrapingList.objects(
    account_label=label,
    sender_pattern=pattern,
    list_type=entry_type,
    id__ne=doc.id,
  ).only("id").first()
  if existing:
    raise ValueError(
      f"entry already exists for account={label}, pattern={pattern}, list_type={entry_type}"
    )

  doc.touch()
  doc.save()
  invalidate_cache()
  return doc


def delete_entry(doc: ScrapingList) -> None:
  doc.delete()
  invalidate_cache()


def import_from_json(json_path: Optional[str] = None) -> Dict[str, int]:
  path = json_path or DEFAULT_SEED_PATH
  with open(path, "r", encoding="utf-8") as f:
    raw = json.load(f)

  if not isinstance(raw, dict):
    raise ValueError("JSON root must be an object mapping account_label -> {allow, skip}")

  created = 0
  updated = 0
  skipped = 0

  for account_label, cfg in raw.items():
    label = (account_label or "").strip()
    if not label or not isinstance(cfg, dict):
      skipped += 1
      continue

    for list_type, patterns in (("allow", cfg.get("allow", [])), ("skip", cfg.get("skip", []))):
      if not isinstance(patterns, list):
        continue
      for raw_pattern in patterns:
        pattern = normalize_pattern(str(raw_pattern))
        if not pattern:
          skipped += 1
          continue

        existing = ScrapingList.objects(
          account_label=label,
          sender_pattern=pattern,
          list_type=list_type,
        ).first()
        if existing:
          existing.active = True
          existing.touch()
          existing.save()
          updated += 1
        else:
          ScrapingList(
            account_label=label,
            sender_pattern=pattern,
            list_type=list_type,
            active=True,
          ).save()
          created += 1

  invalidate_cache()
  return {
    "created": created,
    "updated": updated,
    "skipped": skipped,
    "total": created + updated,
  }
