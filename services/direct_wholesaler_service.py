# services/direct_wholesaler_service.py
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, Optional

from bson import ObjectId

from models.direct_wholesaler import DirectWholesaler
from core.paths import data_path

DEFAULT_JSON_PATH = str(data_path("direct_wholeseller.json"))

_CACHE_TTL_SECONDS = 60
_cache_map: Optional[Dict[str, dict]] = None
_cache_at: float = 0.0


def normalize_email(value: str) -> str:
    return (value or "").strip().lower()


def parse_update_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return True
    return str(value).strip().lower() in ("true", "1", "yes", "y")


def invalidate_cache() -> None:
    global _cache_map, _cache_at
    _cache_map = None
    _cache_at = 0.0


def _doc_to_entry(doc: DirectWholesaler) -> dict:
    return {
        "name": doc.name,
        "email": doc.email,
        "phone": doc.phone or "",
        "updateFlagForPodio": bool(doc.updateFlagForPodio),
    }


def get_wholesaler_map(*, force_refresh: bool = False) -> Dict[str, dict]:
    global _cache_map, _cache_at

    now = time.time()
    if (
        not force_refresh
        and _cache_map is not None
        and (now - _cache_at) < _CACHE_TTL_SECONDS
    ):
        return _cache_map

    out: Dict[str, dict] = {}
    for doc in DirectWholesaler.objects.only(
        "sender_email", "name", "email", "phone", "updateFlagForPodio"
    ):
        key = normalize_email(doc.sender_email)
        if not key:
            continue
        out[key] = _doc_to_entry(doc)

    _cache_map = out
    _cache_at = now
    return out


def doc_to_response(doc: DirectWholesaler) -> dict:
    return {
        "id": str(doc.id),
        "sender_email": doc.sender_email,
        "email": doc.email,
        "name": doc.name,
        "phone": doc.phone or "",
        "updateFlagForPodio": bool(doc.updateFlagForPodio),
        "created_at": doc.created_at.isoformat() if doc.created_at else None,
        "updated_at": doc.updated_at.isoformat() if doc.updated_at else None,
    }


def get_by_id(doc_id: str) -> Optional[DirectWholesaler]:
    try:
        oid = ObjectId(str(doc_id))
    except Exception:
        return None
    return DirectWholesaler.objects(id=oid).first()


def get_by_sender_email(sender_email: str) -> Optional[DirectWholesaler]:
    key = normalize_email(sender_email)
    if not key:
        return None
    return DirectWholesaler.objects(sender_email=key).first()


def create_wholesaler(
    *,
    sender_email: str,
    email: str,
    name: str,
    phone: str = "",
    update_flag_for_podio: bool = True,
) -> DirectWholesaler:
    sender = normalize_email(sender_email)
    contact = normalize_email(email)
    if not sender or not contact or not (name or "").strip():
        raise ValueError("sender_email, email, and name are required")

    if DirectWholesaler.objects(sender_email=sender).only("id").first():
        raise ValueError(f"sender_email already exists: {sender}")

    doc = DirectWholesaler(
        sender_email=sender,
        email=contact,
        name=(name or "").strip(),
        phone=(phone or "").strip(),
        updateFlagForPodio=update_flag_for_podio,
    )
    doc.save()
    invalidate_cache()
    return doc


def update_wholesaler(
    doc: DirectWholesaler,
    *,
    sender_email: Optional[str] = None,
    email: Optional[str] = None,
    name: Optional[str] = None,
    phone: Optional[str] = None,
    update_flag_for_podio: Optional[bool] = None,
) -> DirectWholesaler:
    if sender_email is not None:
        sender = normalize_email(sender_email)
        if not sender:
            raise ValueError("sender_email cannot be empty")
        existing = DirectWholesaler.objects(sender_email=sender, id__ne=doc.id).only("id").first()
        if existing:
            raise ValueError(f"sender_email already exists: {sender}")
        doc.sender_email = sender

    if email is not None:
        contact = normalize_email(email)
        if not contact:
            raise ValueError("email cannot be empty")
        doc.email = contact

    if name is not None:
        cleaned = (name or "").strip()
        if not cleaned:
            raise ValueError("name cannot be empty")
        doc.name = cleaned

    if phone is not None:
        doc.phone = (phone or "").strip()

    if update_flag_for_podio is not None:
        doc.updateFlagForPodio = update_flag_for_podio

    doc.touch()
    doc.save()
    invalidate_cache()
    return doc


def delete_wholesaler(doc: DirectWholesaler) -> None:
    doc.delete()
    invalidate_cache()


def import_from_json(json_path: Optional[str] = None) -> Dict[str, int]:
    path = json_path or DEFAULT_JSON_PATH
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    if not isinstance(raw, dict):
        raise ValueError("JSON root must be an object")

    created = 0
    updated = 0
    skipped = 0

    for sender_key, cfg in raw.items():
        if not isinstance(sender_key, str):
            skipped += 1
            continue
        sender = normalize_email(sender_key)
        if not sender:
            skipped += 1
            continue

        if not isinstance(cfg, dict):
            cfg = {}

        contact = normalize_email(cfg.get("email") or sender)
        name = (cfg.get("name") or "").strip()
        phone = str(cfg.get("phone") or "").strip()
        update_flag = parse_update_flag(cfg.get("updateFlagForPodio"))

        if not name:
            skipped += 1
            continue

        existing = DirectWholesaler.objects(sender_email=sender).first()
        if existing:
            existing.email = contact
            existing.name = name
            existing.phone = phone
            existing.updateFlagForPodio = update_flag
            existing.touch()
            existing.save()
            updated += 1
        else:
            DirectWholesaler(
                sender_email=sender,
                email=contact,
                name=name,
                phone=phone,
                updateFlagForPodio=update_flag,
            ).save()
            created += 1

    invalidate_cache()
    return {
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "total": created + updated,
    }
