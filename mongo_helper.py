# mongo_helper.py
from __future__ import annotations
import os
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

from dotenv import load_dotenv
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.collection import Collection
from pymongo.errors import DuplicateKeyError
from bson import ObjectId

# ----- ENV -----
load_dotenv()
MONGO_URI = os.getenv("MONGO_URI", "mongodb://127.0.0.1:27017")
MONGO_DB = os.getenv("MONGO_DB", "aiDb")

# Single shared client (thread-safe)
_client: Optional[MongoClient] = None


# ====== Core connection ======
def _client_cached() -> MongoClient:
    global _client
    if _client is None:
        _client = MongoClient(MONGO_URI, tlsAllowInvalidCertificates=True)
    return _client

def get_db():
    return _client_cached()[MONGO_DB]

def col(name: str) -> Collection:
    return get_db()[name]


# ====== Utilities ======
def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def to_object_id(maybe_id: Union[str, ObjectId, None]) -> Optional[ObjectId]:
    if isinstance(maybe_id, ObjectId) or maybe_id is None:
        return maybe_id
    try:
        return ObjectId(str(maybe_id))
    except Exception:
        return None


# ====== Index management ======
IndexKey = List[Tuple[str, int]]
IndexSpec = Dict[str, Any]
# Example spec:
# {"name": "uniq_msg", "keys": [("account_label", 1), ("gmail_message_id", 1)], "unique": True}

def ensure_indexes(collection: str, index_specs: Iterable[IndexSpec]) -> None:
    c = col(collection)
    for spec in index_specs:
        keys: IndexKey = [(k, ASCENDING if v in (1, "asc", "ASC") else DESCENDING)
                          for (k, v) in spec.get("keys", [])]
        if not keys:
            continue
        kwargs = {k: v for k, v in spec.items() if k not in ("keys",)}
        c.create_index(keys, **kwargs)


# ====== Generic CRUD ======
def insert_one(collection: str, doc: Dict[str, Any]) -> str:
    now = utcnow_iso()
    doc = dict(doc)
    doc.setdefault("created_at", now)
    doc["updated_at"] = now
    res = col(collection).insert_one(doc)
    return str(res.inserted_id)

def insert_many(collection: str, docs: List[Dict[str, Any]]) -> List[str]:
    now = utcnow_iso()
    docs = [dict(d, created_at=d.get("created_at", now), updated_at=now) for d in docs]
    res = col(collection).insert_many(docs)
    return [str(_id) for _id in res.inserted_ids]

def upsert_by_keys(collection: str, doc: Dict[str, Any], keys: List[str]) -> Dict[str, Any]:
    """
    Idempotent upsert by the given key fields.
    Example: upsert_by_keys("filtered_listing_emails", doc, ["account_label", "gmail_message_id"])
    """
    if not keys:
        raise ValueError("keys must be a non-empty list")

    filt = {k: doc.get(k) for k in keys}
    if any(v is None for v in filt.values()):
        missing = [k for k, v in filt.items() if v is None]
        raise ValueError(f"Missing key(s) in doc for upsert: {missing}")

    set_on_insert = {"created_at": utcnow_iso()}
    to_set = dict(doc)
    to_set["updated_at"] = utcnow_iso()

    res = col(collection).update_one(filt, {"$set": to_set, "$setOnInsert": set_on_insert}, upsert=True)
    return {
        "matched": res.matched_count,
        "modified": res.modified_count,
        "upserted_id": str(res.upserted_id) if res.upserted_id else None,
    }

def update_one(collection: str, filt: Dict[str, Any], update: Dict[str, Any], upsert: bool = False) -> Dict[str, Any]:
    if "$set" in update:
        update["$set"]["updated_at"] = utcnow_iso()
    else:
        update = {"$set": dict(update)}
        update["$set"]["updated_at"] = utcnow_iso()
    res = col(collection).update_one(filt, update, upsert=upsert)
    return {"matched": res.matched_count, "modified": res.modified_count, "upserted_id": str(res.upserted_id) if res.upserted_id else None}

def find_one(collection: str, filt: Dict[str, Any], projection: Optional[Dict[str, int]] = None) -> Optional[Dict[str, Any]]:
    return col(collection).find_one(filt, projection)

def get_by_id(collection: str, _id: Union[str, ObjectId]) -> Optional[Dict[str, Any]]:
    oid = to_object_id(_id)
    return col(collection).find_one({"_id": oid}) if oid else None

def find_many(
    collection: str,
    filt: Dict[str, Any],
    projection: Optional[Dict[str, int]] = None,
    sort: Optional[List[Tuple[str, int]]] = None,
    limit: int = 100,
    skip: int = 0,
) -> List[Dict[str, Any]]:
    cursor = col(collection).find(filt, projection)
    if sort:
        cursor = cursor.sort(sort)
    if skip:
        cursor = cursor.skip(skip)
    if limit:
        cursor = cursor.limit(limit)
    return list(cursor)

def delete_one(collection: str, filt: Dict[str, Any]) -> int:
    res = col(collection).delete_one(filt)
    return res.deleted_count

def delete_many(collection: str, filt: Dict[str, Any]) -> int:
    res = col(collection).delete_many(filt)
    return res.deleted_count

def count(collection: str, filt: Dict[str, Any]) -> int:
    return col(collection).count_documents(filt)



