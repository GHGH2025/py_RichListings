from __future__ import annotations

import json
import os
import time
from typing import Dict, List, Optional

from bson import ObjectId

from models.special_avail_list import SpecialAvailList
from core.paths import data_path

DEFAULT_SEED_PATH = str(data_path("special_avail_list_seed.json"))

_CACHE_TTL_SECONDS = 60
_config_cache: Optional[Dict[str, List[str]]] = None
_podio_cache: Optional[Dict[str, List[int]]] = None
_cache_at: float = 0.0


def normalize_wholesaler_name(value: str) -> str:
  return (value or "").strip()


def normalize_email(value: str) -> str:
  return (value or "").strip().lower()


def normalize_emails(values) -> List[str]:
  out: List[str] = []
  seen: set[str] = set()
  if not isinstance(values, list):
    values = [values]
  for raw in values:
    email = normalize_email(str(raw or ""))
    if email and email not in seen:
      seen.add(email)
      out.append(email)
  return out


def normalize_podio_ids(values) -> List[int]:
  out: List[int] = []
  seen: set[int] = set()
  if not isinstance(values, list):
    values = [values]
  for raw in values:
    try:
      item_id = int(raw)
    except (TypeError, ValueError):
      continue
    if item_id not in seen:
      seen.add(item_id)
      out.append(item_id)
  return out


def invalidate_cache() -> None:
  global _config_cache, _podio_cache, _cache_at
  _config_cache = None
  _podio_cache = None
  _cache_at = 0.0


def _refresh_cache(*, force_refresh: bool = False) -> None:
  global _config_cache, _podio_cache, _cache_at

  now = time.time()
  if (
    not force_refresh
    and _config_cache is not None
    and _podio_cache is not None
    and (now - _cache_at) < _CACHE_TTL_SECONDS
  ):
    return

  config: Dict[str, List[str]] = {}
  podio: Dict[str, List[int]] = {}

  for doc in SpecialAvailList.objects(active=True).order_by("wholesaler_name"):
    name = normalize_wholesaler_name(doc.wholesaler_name)
    if not name:
      continue
    emails = normalize_emails(doc.sender_emails or [])
    ids = normalize_podio_ids(doc.podio_item_ids or [])
    if emails:
      config[name] = emails
    if ids:
      podio[name.lower()] = ids

  _config_cache = config
  _podio_cache = podio
  _cache_at = now


def get_wholesaler_config(*, force_refresh: bool = False) -> Dict[str, List[str]]:
  """
  Returns { wholesaler_name: [sender_email, ...] } for active wholesalers.
  """
  _refresh_cache(force_refresh=force_refresh)
  return dict(_config_cache or {})


def get_wholesaler_podio_bucket(*, force_refresh: bool = False) -> Dict[str, List[int]]:
  """
  Returns { wholesaler_name_lower: [podio_item_id, ...] } for active wholesalers.
  """
  _refresh_cache(force_refresh=force_refresh)
  return dict(_podio_cache or {})


def get_all_sender_emails(*, force_refresh: bool = False) -> List[str]:
  cfg = get_wholesaler_config(force_refresh=force_refresh)
  senders: set[str] = set()
  for emails in cfg.values():
    senders.update(emails)
  return sorted(senders)


def get_runtime_config(*, force_refresh: bool = False) -> Dict[str, object]:
  return {
    "wholesaler_config": get_wholesaler_config(force_refresh=force_refresh),
    "podio_bucket": get_wholesaler_podio_bucket(force_refresh=force_refresh),
    "sender_emails": get_all_sender_emails(force_refresh=force_refresh),
  }


def doc_to_response(doc: SpecialAvailList) -> dict:
  return {
    "id": str(doc.id),
    "wholesaler_name": doc.wholesaler_name,
    "sender_emails": list(doc.sender_emails or []),
    "podio_item_ids": list(doc.podio_item_ids or []),
    "active": bool(doc.active),
    "created_at": doc.created_at.isoformat() if doc.created_at else None,
    "updated_at": doc.updated_at.isoformat() if doc.updated_at else None,
  }


def get_by_id(doc_id: str) -> Optional[SpecialAvailList]:
  try:
    oid = ObjectId(str(doc_id))
  except Exception:
    return None
  return SpecialAvailList.objects(id=oid).first()


def get_by_name(wholesaler_name: str) -> Optional[SpecialAvailList]:
  name = normalize_wholesaler_name(wholesaler_name)
  if not name:
    return None
  return SpecialAvailList.objects(wholesaler_name=name).first()


def create_entry(
  *,
  wholesaler_name: str,
  sender_emails: List[str],
  podio_item_ids: Optional[List[int]] = None,
  active: bool = True,
) -> SpecialAvailList:
  name = normalize_wholesaler_name(wholesaler_name)
  emails = normalize_emails(sender_emails)
  ids = normalize_podio_ids(podio_item_ids or [])

  if not name:
    raise ValueError("wholesaler_name is required")
  if not emails:
    raise ValueError("sender_emails must contain at least one email")

  if SpecialAvailList.objects(wholesaler_name=name).only("id").first():
    raise ValueError(f"wholesaler already exists: {name}")

  doc = SpecialAvailList(
    wholesaler_name=name,
    sender_emails=emails,
    podio_item_ids=ids,
    active=active,
  )
  doc.save()
  invalidate_cache()
  return doc


def update_entry(
  doc: SpecialAvailList,
  *,
  wholesaler_name: Optional[str] = None,
  sender_emails: Optional[List[str]] = None,
  podio_item_ids: Optional[List[int]] = None,
  active: Optional[bool] = None,
) -> SpecialAvailList:
  name = doc.wholesaler_name
  emails = list(doc.sender_emails or [])
  ids = list(doc.podio_item_ids or [])

  if wholesaler_name is not None:
    name = normalize_wholesaler_name(wholesaler_name)
    if not name:
      raise ValueError("wholesaler_name cannot be empty")
    doc.wholesaler_name = name

  if sender_emails is not None:
    emails = normalize_emails(sender_emails)
    if not emails:
      raise ValueError("sender_emails must contain at least one email")
    doc.sender_emails = emails

  if podio_item_ids is not None:
    ids = normalize_podio_ids(podio_item_ids)
    doc.podio_item_ids = ids

  if active is not None:
    doc.active = active

  existing = SpecialAvailList.objects(
    wholesaler_name=name,
    id__ne=doc.id,
  ).only("id").first()
  if existing:
    raise ValueError(f"wholesaler already exists: {name}")

  doc.touch()
  doc.save()
  invalidate_cache()
  return doc


def delete_entry(doc: SpecialAvailList) -> None:
  doc.delete()
  invalidate_cache()


def import_from_json(json_path: Optional[str] = None) -> Dict[str, int]:
  path = json_path or DEFAULT_SEED_PATH
  with open(path, "r", encoding="utf-8") as f:
    raw = json.load(f)

  if not isinstance(raw, dict):
    raise ValueError(
      "JSON root must be an object mapping wholesaler_name -> {sender_emails, podio_item_ids}"
    )

  created = 0
  updated = 0
  skipped = 0

  for wholesaler_name, cfg in raw.items():
    name = normalize_wholesaler_name(str(wholesaler_name or ""))
    if not name or not isinstance(cfg, dict):
      skipped += 1
      continue

    emails = normalize_emails(cfg.get("sender_emails", []))
    ids = normalize_podio_ids(cfg.get("podio_item_ids", []))
    if not emails:
      skipped += 1
      continue

    existing = SpecialAvailList.objects(wholesaler_name=name).first()
    if existing:
      existing.sender_emails = emails
      existing.podio_item_ids = ids
      existing.active = True
      existing.touch()
      existing.save()
      updated += 1
    else:
      SpecialAvailList(
        wholesaler_name=name,
        sender_emails=emails,
        podio_item_ids=ids,
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
