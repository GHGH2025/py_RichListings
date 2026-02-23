from __future__ import annotations

import os
import json
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Tuple, Optional

from mongoengine.queryset.visitor import Q

from mongo_engine_conn import init_db
from models import FilteredListingEmail, ParsedListing, SpecialAvail

from zoneinfo import ZoneInfo 
import requests
import re
from podio_direct_wholeseller import get_podio_access_token  # <-- make sure this import path is correct

from openai import OpenAI

MATCH_MODEL = os.getenv("OPENAI_MATCH_MODEL", "gpt-4.1-mini")
MATCH_WEBHOOK_URL = os.getenv("SPECIAL_AVAIL_MATCH_WEBHOOK_URL", "").strip()

client = OpenAI()

PODIO_BASE_URL = "https://api.podio.com"

# Properties app + field IDs
PODIO_PROPERTIES_APP_ID = int(os.getenv("PODIO_PROPERTIES_APP_ID", "18339388"))

PODIO_FIELD_STATUS_ID = int(os.getenv("PODIO_FIELD_STATUS_ID", "144394555"))
PODIO_FIELD_WHOLESALER_ID = int(os.getenv("PODIO_FIELD_WHOLESALER_ID", "144394554"))
PODIO_FIELD_PROPERTY_ADDRESS_ID = int(os.getenv("PODIO_FIELD_PROPERTY_ADDRESS_ID", "144394550"))

# Category option id for "Active" status — MUST be set in .env
# e.g. PODIO_STATUS_ACTIVE_OPTION_ID=1  (whatever your option id is)
PODIO_STATUS_ACTIVE_OPTION_ID = 1
if not PODIO_STATUS_ACTIVE_OPTION_ID:
    raise RuntimeError("PODIO_STATUS_ACTIVE_OPTION_ID is not set in environment")
PODIO_STATUS_ACTIVE_OPTION_ID = int(PODIO_STATUS_ACTIVE_OPTION_ID)


# -------------------------
# CONFIG: dynamic sender bucket
# -------------------------
# Recommended: store JSON in env so you can update without code changes.
# Example value:
# WHOLESALER_SENDER_BUCKET_JSON='[
#   {"email":"deals@aoinvestments.ccsend.com","podio_item_id":123},
#   {"email":"foo@bar.com","podio_item_id":456}
# ]'
WHOLESALER_SENDER_BUCKET_JSON = os.getenv("WHOLESALER_SENDER_BUCKET_JSON", "").strip()


# DEFAULT_WHOLESALER_BUCKET: Dict[str, str] = {
#     # "sender_email": "podio_item_id"
#    "info-jefinancialholdings.com@shared1.ccsend.com":857496733,
#     "another@sender.com": 778054254,
# }

DEFAULT_WHOLESALER_BUCKET: Dict[str, List[str]] = {
    "Johnathan": [
        "info-jefinancialholdings.com@shared1.ccsend.com",
    ],
    "David": [
        "david@theligongroup.com",
    ],
    "Willy":["sales-islandlivingrealty.com@shared1.ccsend.com"],
    "Craig":["craig@nowhomebuyers.com"],
    "John":["john@wholesalejax.com"],
    "JC":["jc-quickturnproperties.com@shared1.ccsend.com","jc@stellarholdingsllc.ccsend.com"],
    "Todd":["tsims-southfloridacashhomebuyers.com@shared1.ccsend.com","kevin-titlerate.com@shared1.ccsend.com"],
    "Richard":["richard@oasispropertyinvestments.ccsend.com","oasispropertydispo@gmail.com"],
    "Derek":["derek@marketing.vesta.app"],
    "Mike_Barone":["mike.barone@outlook.com"],
    "Judson":["judson@securehomeinvest.com"],
    "Jerry_Vega":["tricountyflippers-gmail.com@shared1.ccsend.com"],
    "Mark_RPM_Wholesale":["flrpmwholesalefl-gmail.com@shared1.ccsend.com"],
    "Jesus":["deals@onemotivatedseller.com"],
    "Simon":["simon@stellarholdingsllc.ccsend.com"],
    "Ernie":["erniethetitleguy-gmail.com@shared1.ccsend.com"],
    "Alex":["alex@lexicorealty.ccsend.com"]
}

DEFAULT_WHOLESALER_BUCKET_PODIO: Dict[str, List[int]] = {
    # "sender_email": [podio_item_id_1, podio_item_id_2, ...]
    "Johnathan": [857496733, 778054254],
    "David":[816254022],
    "Willy":[618782696],
    "Craig":[618782689],
    "John":[2119852479,2088424136],
    "JC":[2084646293,1802627987],
    "Todd":[618782750],
    "Richard":[2083808740],
    "Derek":[2775546975],
    "Mike_Barone":[1854753148],
    "Judson":[2937718049],
    "Jerry_Vega":[2098341539],
    "Mark_RPM_Wholesale":[3251963737],
    "Jesus":[3204466987],
    "Simon":[3254339453],
    "Ernie":[3166266096],
    "Alex":[2088423246]
    # "another@sender.com": [111111111, 222222222],
}

# def _load_wholesaler_bucket() -> Dict[str, str]:
#     raw = (os.getenv("WHOLESALER_BUCKET_JSON") or "").strip()
#     if not raw:
#         # fallback to hardcoded
#         return {k.lower().strip(): str(v).strip() for k, v in DEFAULT_WHOLESALER_BUCKET.items() if k and v}

#     try:
#         val = json.loads(raw)
#         # If list of objects
#         if isinstance(val, list):
#             out: Dict[str, str] = {}
#             for it in val:
#                 if not isinstance(it, dict):
#                     continue
#                 email = (it.get("email") or "").strip().lower()
#                 podio_item_id = str(it.get("podio_item_id") or "").strip()
#                 if email and podio_item_id:
#                     out[email] = podio_item_id
#             return out

#         # If dict mapping email->podio
#         if isinstance(val, dict):
#             out = {}
#             for k, v in val.items():
#                 email = str(k or "").strip().lower()
#                 podio_item_id = str(v or "").strip()
#                 if email and podio_item_id:
#                     out[email] = podio_item_id
#             return out

#     except Exception:
#         pass

#     # If env is invalid JSON, fallback to hardcoded
#     return {k.lower().strip(): str(v).strip() for k, v in DEFAULT_WHOLESALER_BUCKET.items() if k and v}



def _load_wholesaler_config() -> Dict[str, List[str]]:
    """
    Returns config in the shape:
      { wholesaler_name: [sender_email_1, sender_email_2, ...] }

    If WHOLESALER_BUCKET_JSON is set, supports:

    - Dict form:
      {
        "Johnathan": ["info@...", "alex@..."],
        "David": ["david@..."]
      }

    - List form:
      [
        { "name": "Johnathan", "emails": ["info@...", "alex@..."] },
        { "name": "David", "emails": ["david@..."] }
      ]

    Otherwise falls back to DEFAULT_WHOLESALER_BUCKET.
    """
    raw = (os.getenv("WHOLESALER_BUCKET_JSON") or "").strip()
    cfg: Dict[str, List[str]] = {}

    def _norm_email_list(val) -> List[str]:
        out: List[str] = []
        if isinstance(val, list):
            src = val
        else:
            src = [val]
        for v in src:
            if not v:
                continue
            s = str(v).strip().lower()
            if s:
                out.append(s)
        return out

    # Try env JSON first
    if raw:
        try:
            val = json.loads(raw)

            # Case 1: dict mapping name -> emails/list
            if isinstance(val, dict):
                for name, emails in val.items():
                    name_norm = (name or "").strip()
                    email_list = _norm_email_list(emails)
                    if name_norm and email_list:
                        cfg[name_norm] = email_list
                if cfg:
                    return cfg

            # Case 2: list of {name, emails}
            if isinstance(val, list):
                for it in val:
                    if not isinstance(it, dict):
                        continue
                    name = (it.get("name") or "").strip()
                    emails_raw = it.get("emails")
                    email_list = _norm_email_list(emails_raw)
                    if name and email_list:
                        cfg[name] = email_list
                if cfg:
                    return cfg

        except Exception:
            # fall through to defaults
            pass

    # Fallback: DEFAULT_WHOLESALER_BUCKET
    for name, emails in DEFAULT_WHOLESALER_BUCKET.items():
        name_norm = (name or "").strip()
        email_list = _norm_email_list(emails)
        if name_norm and email_list:
            cfg[name_norm] = email_list

    return cfg


def _build_sender_to_wholesaler(cfg: Dict[str, List[str]]) -> Dict[str, str]:
    """
    Reverse index: sender_email(lower) -> wholesaler_name
    If an email appears under multiple wholesalers, last one wins.
    """
    mapping: Dict[str, str] = {}
    for wholesaler_name, emails in cfg.items():
        for e in emails:
            if not e:
                continue
            mapping[e.strip().lower()] = wholesaler_name
    return mapping



def _load_wholesaler_bucket() -> Dict[str, List[int]]:
    """
    Returns:
      { wholesaler_name_lower: [podio_item_id_1, podio_item_id_2, ...] }
    """
    raw = (os.getenv("WHOLESALER_BUCKET_JSON") or "").strip()
    out: Dict[str, List[int]] = {}

    def _normalize_ids(val) -> List[int]:
        if isinstance(val, list):
            ids: List[int] = []
            for v in val:
                try:
                    ids.append(int(v))
                except (TypeError, ValueError):
                    continue
            return ids
        # single scalar -> wrap as list
        try:
            return [int(val)]
        except (TypeError, ValueError):
            return []

    # If no env, fall back to DEFAULT_WHOLESALER_BUCKET_PODIO
    if not raw:
        for name, ids in DEFAULT_WHOLESALER_BUCKET_PODIO.items():
            name_norm = (name or "").strip().lower()
            id_list = _normalize_ids(ids)
            if name_norm and id_list:
                out[name_norm] = id_list
        return out

    # Try to parse env JSON
    try:
        val = json.loads(raw)

        # Case 1: list of objects
        if isinstance(val, list):
            for it in val:
                if not isinstance(it, dict):
                    continue
                name = (it.get("name") or "").strip().lower()
                if "podio_item_ids" in it:
                    id_list = _normalize_ids(it.get("podio_item_ids"))
                else:
                    id_list = _normalize_ids(it.get("podio_item_id"))
                if name and id_list:
                    out[name] = id_list
            return out

        # Case 2: dict mapping name -> ids (list or scalar)
        if isinstance(val, dict):
            for k, v in val.items():
                name = str(k or "").strip().lower()
                id_list = _normalize_ids(v)
                if name and id_list:
                    out[name] = id_list
            return out

    except Exception:
        # fall back silently to defaults if JSON invalid
        pass

    # Fallback: defaults again if env is invalid
    for name, ids in DEFAULT_WHOLESALER_BUCKET_PODIO.items():
        name_norm = (name or "").strip().lower()
        id_list = _normalize_ids(ids)
        if name_norm and id_list:
            out[name_norm] = id_list

    return out
# -----------------------
# DATE RANGE (YESTERDAY UTC)
# -----------------------
# def _yesterday_range_utc() -> Tuple[datetime, datetime]:
#     """
#     Returns [start, end) for yesterday in UTC, as naive datetimes:
#       start = yesterday 00:00:00 UTC
#       end   = today     00:00:00 UTC
#     """
#     now = datetime.utcnow()
#     today_00 = now.replace(hour=0, minute=0, second=0, microsecond=0)
#     start = today_00 - timedelta(days=4)
#     end = today_00
#     return start, end

def _yesterday_range_utc() -> Tuple[datetime, datetime]:
    """
    Returns [start, end) for **yesterday in America/New_York**, converted to UTC.

    Example:
      If now in EST is 2025-12-23 00:15,
      this returns the UTC equivalents of:
        start = 2025-12-22 00:00:00 EST
        end   = 2025-12-23 00:00:00 EST

    Both returned datetimes are naive UTC (tzinfo=None), ready for Mongo queries.
    """
    est = ZoneInfo("America/New_York")

    # Current time in EST/EDT
    now_est = datetime.now(est)

    # EST "today" and "yesterday" dates
    today_est = now_est.date()
    yesterday_est = today_est - timedelta(days=1)

    # Midnight boundaries in EST
    start_est = datetime(
        year=yesterday_est.year,
        month=yesterday_est.month,
        day=yesterday_est.day,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
        tzinfo=est,
    )
    end_est = datetime(
        year=today_est.year,
        month=today_est.month,
        day=today_est.day,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
        tzinfo=est,
    )

    # Convert to UTC
    start_utc = start_est.astimezone(timezone.utc)
    end_utc = end_est.astimezone(timezone.utc)

    # Return as naive UTC datetimes (what you were using before)
    return start_utc.replace(tzinfo=None), end_utc.replace(tzinfo=None)


def _sender_email_q(sender_emails: List[str]) -> Q:
    """
    Case-insensitive exact match builder for from_info.email.
    Avoids case-sensitivity issues.
    """
    q = Q()
    for e in sender_emails:
        e2 = (e or "").strip()
        if e2:
            q |= Q(from_info__email__iexact=e2)
    return q


def _norm_key(addr: str, city: str) -> Optional[Tuple[str, str]]:
    a = (addr or "").strip()
    c = (city or "").strip()
    if not a or not c:
        return None
    return (a.lower(), c.lower())


# -----------------------
# MAIN FUNCTION
# -----------------------
# def build_yesterday_unique_parsed_listings_for_wholesalers() -> Dict[str, Any]:
#     """
#     1) Load wholesaler bucket (sender_email -> podio_item_id)
#     2) Fetch yesterday's FilteredListingEmail where:
#          - created_at in yesterday UTC range
#          - from_info.email matches bucket senders (case-insensitive)
#     3) Fetch ParsedListing where source_email in those FilteredListingEmail IDs
#     4) Return unique by (address, city) (case-insensitive)
#     """
#     init_db()

#     bucket = _load_wholesaler_bucket()
#     sender_emails = list(bucket.keys())
#     print("sender_emails",sender_emails)

#     start_dt, end_dt = _yesterday_range_utc()

#     if not sender_emails:
#         return {
#             "ok": True,
#             "range_utc": {"start": start_dt.isoformat(), "end": end_dt.isoformat()},
#             "bucket_size": 0,
#             "emails_found": 0,
#             "parsed_found": 0,
#             "unique_count": 0,
#             "items": [],
#             "note": "wholesaler bucket is empty",
#         }

#     # 1) FilteredListingEmail from yesterday + sender emails
#     email_q = (
#         Q(created_at__gte=start_dt)
#         & Q(created_at__lt=end_dt)
#         & _sender_email_q(sender_emails)
#     )

#     emails = list(
#         FilteredListingEmail.objects(email_q).only("id", "from_info", "created_at")
#     )
#     print("emails",emails)
#     email_ids = [e.id for e in emails]

#     print("email_ids",email_ids)

#     if not email_ids:
#         return {
#             "ok": True,
#             "range_utc": {"start": start_dt.isoformat(), "end": end_dt.isoformat()},
#             "bucket_size": len(sender_emails),
#             "emails_found": 0,
#             "parsed_found": 0,
#             "unique_count": 0,
#             "items": [],
#         }

#     # Map email_id -> sender_email (lower) for later
#     email_id_to_sender: Dict[str, str] = {}
#     for e in emails:
#         print("e",e)
#         sender = ""
#         try:
#             sender = e.from_info.email 
#             print("sender",sender)
#         except Exception as ex:
#             print("ex",ex)
#             sender = ""
#         sender = (sender or "").strip().lower()
#         if sender:
#             email_id_to_sender[str(e.id)] = sender

#     print("email_id_to_sender",email_id_to_sender)

#     # 2) ParsedListing by source_email reference
#     pls = list(
#         ParsedListing.objects(source_email__in=email_ids)
#         .only("id", "address", "city", "source_email")
#     )

#     # 3) Unique by (address, city)
#     unique_map: Dict[Tuple[str, str], Dict[str, Any]] = {}

#     for pl in pls:
#         addr = (getattr(pl, "address", "") or "").strip()
#         city = (getattr(pl, "city", "") or "").strip()
#         key = _norm_key(addr, city)
#         if not key:
#             continue

#         src = getattr(pl, "source_email", None)
#         # print("src>>",src)
#         # src_id = str(getattr(src, "_id", src) or "")
#         # print("src_id",src_id)
#         # sender = email_id_to_sender.get(src_id, "")
#         sender = src
#         # bucket may have sender normalized; if sender isn't found, keep empty
#         # podio_item_id = bucket.get(sender, "") if sender else ""

#         if key not in unique_map:
#             unique_map[key] = {
#                 "address": addr,
#                 "city": city,
#                 "source":sender,
#                 # "sender_email": sender or None,
#                 # "podio_item_id": podio_item_id or None,
#                 # "source_email_id": src_id or None,   # first seen
#                 "parsed_listing_ids": [str(pl.id)],  # keep all duplicates here
#             }
#         else:
#             unique_map[key]["parsed_listing_ids"].append(str(pl.id))

#     items = list(unique_map.values())

#     return {
#         "ok": True,
#         "range_utc": {"start": start_dt.isoformat(), "end": end_dt.isoformat()},
#         "bucket_size": len(sender_emails),
#         # "emails_found": len(emails),
#         # "parsed_found": len(pls),
#         "unique_count": len(items),
#         "items": items,
#     }

def _load_wholesaler_senders() -> List[str]:
    """
    Returns a list of sender emails (lowercased) for all wholesalers,
    ignoring whether they have Podio item IDs or not.
    
    It reads WHOLESALER_BUCKET_JSON if present, otherwise falls back to
    DEFAULT_WHOLESALER_BUCKET keys.
    """
    raw = (os.getenv("WHOLESALER_BUCKET_JSON") or "").strip()
    senders: set[str] = set()

    if raw:
        try:
            val = json.loads(raw)

            # Case 1: list of objects: [{ "email": "...", "podio_item_ids": [...] }, ...]
            if isinstance(val, list):
                for it in val:
                    if not isinstance(it, dict):
                        continue
                    email = (it.get("email") or "").strip().lower()
                    if email:
                        senders.add(email)

            # Case 2: dict mapping email -> ids
            elif isinstance(val, dict):
                for k in val.keys():
                    email = str(k or "").strip().lower()
                    if email:
                        senders.add(email)

        except Exception:
            # If JSON invalid, fall back to defaults below
            pass

    # # Fallback or merge with defaults
    if not senders:
        for email in DEFAULT_WHOLESALER_BUCKET.keys():
            em = (email or "").strip().lower()
            if em:
                senders.add(em)

    # return sorted list just for determinism
    return sorted(senders)

# def build_yesterday_unique_parsed_listings_for_wholesalers() -> Dict[str, Any]:
#     """
#     Per-wholesaler breakdown:

#     1) Load wholesaler bucket (sender_email -> [podio_item_ids...]) just to get sender emails.
#     2) Fetch yesterday's FilteredListingEmail where:
#          - created_at in yesterday UTC range
#          - from_info.email matches bucket senders (case-insensitive).
#     3) Map FilteredListingEmail.id -> sender_email (lowercased).
#     4) Fetch ParsedListing where source_email in those FilteredListingEmail IDs.
#     5) For each wholesaler, build a unique map by (address, city).
#     6) Return per-wholesaler dict:
#        { sender_email: { "unique_count": N, "unique_items": [ ... ] }, ... }.
#     """
#     init_db()

#     # bucket = _load_wholesaler_bucket()  # { sender_email_lower: [podio_ids...] }
#     # print("bucket",bucket)
    
#     sender_emails = _load_wholesaler_senders()
#     print("sender_emails", sender_emails)

#     start_dt, end_dt = _yesterday_range_utc()

#     if not sender_emails:
#         return {
#             "ok": True,
#             "range_utc": {"start": start_dt.isoformat(), "end": end_dt.isoformat()},
#             "bucket_size": 0,
#             "wholesalers": {},
#             "note": "wholesaler bucket is empty",
#         }

#     # 1) FilteredListingEmail from yesterday + sender emails
#     email_q = (
#         Q(created_at__gte=start_dt)
#         & Q(created_at__lt=end_dt)
#         & _sender_email_q(sender_emails)
#     )

#     emails = list(
#         FilteredListingEmail.objects(email_q).only("id", "from_info", "created_at")
#     )
#     print("emails", emails)
#     email_ids = [e.id for e in emails]
#     print("email_ids", email_ids)

#     if not email_ids:
#         # Still return per-wholesaler structure but all empty
#         wholesalers_out: Dict[str, Any] = {
#             s: {"unique_count": 0, "unique_items": []} for s in sender_emails
#         }
#         return {
#             "ok": True,
#             "range_utc": {"start": start_dt.isoformat(), "end": end_dt.isoformat()},
#             "bucket_size": len(sender_emails),
#             "wholesalers": wholesalers_out,
#         }

#     # Map FilteredListingEmail.id -> sender_email (lower) for later
#     email_id_to_sender: Dict[str, str] = {}
#     for e in emails:
#         sender = ""
#         try:
#             sender = e.from_info.email  # adjust if your field structure differs
#             print("sender", sender)
#         except Exception as ex:
#             print("from_info.email error:", ex)
#             sender = ""
#         sender = (sender or "").strip().lower()
#         if sender:
#             email_id_to_sender[str(e.id)] = sender

#     print("email_id_to_sender", email_id_to_sender)

#     # 2) ParsedListing by source_email reference
#     pls = list(
#         ParsedListing.objects(source_email__in=email_ids)
#         .only("id", "address", "city", "source_email")
#     )

#     # 3) Per-wholesaler unique maps: { sender_email: { (addr,city): {...} } }
#     per_wholesaler: Dict[str, Dict[Tuple[str, str], Dict[str, Any]]] = {}

#     for pl in pls:
#         addr = (getattr(pl, "address", "") or "").strip()
#         city = (getattr(pl, "city", "") or "").strip()
#         key = _norm_key(addr, city)
#         if not key:
#             continue

#         src = getattr(pl, "source_email", None)
#         # src might be a Reference or raw ObjectId; normalize to string id
#         src_id = str(getattr(src, "id", src) or "")
#         sender_email = email_id_to_sender.get(src_id)
#         if not sender_email:
#             # listing came from a sender not in our bucket (or mapping failed)
#             continue

#         sender_map = per_wholesaler.setdefault(sender_email, {})

#         if key not in sender_map:
#             sender_map[key] = {
#                 "address": addr,
#                 "city": city,
#                 "parsed_listing_ids": [str(pl.id)],
#             }
#         else:
#             sender_map[key]["parsed_listing_ids"].append(str(pl.id))

#     # 4) Build final per-wholesaler output, ensuring all bucket senders are present
#     wholesalers_out: Dict[str, Any] = {}
#     for sender in sender_emails:
#         sender_map = per_wholesaler.get(sender, {})
#         unique_items = list(sender_map.values())
#         wholesalers_out[sender] = {
#             "unique_count": len(unique_items),
#             "unique_items": unique_items,
#         }

#     return {
#         "ok": True,
#         "range_utc": {"start": start_dt.isoformat(), "end": end_dt.isoformat()},
#         "bucket_size": len(sender_emails),
#         "wholesalers": wholesalers_out,
#     }

def build_yesterday_unique_parsed_listings_for_wholesalers() -> Dict[str, Any]:
    """
    Per-wholesaler breakdown, grouped by wholesaler NAME:

    1) Load wholesaler config:
         { "Johnathan": [email1, email2], "David": [email3], ... }
    2) Build reverse index sender_email -> wholesaler_name.
    3) Fetch yesterday's FilteredListingEmail where:
         - created_at in yesterday UTC range
         - from_info.email in all sender emails (case-insensitive).
    4) Map FilteredListingEmail.id -> wholesaler_name.
    5) Fetch ParsedListing where source_email in those FilteredListingEmail IDs.
    6) For each wholesaler, build a unique map by (address, city).
    7) Return per-wholesaler dict:

       {
         "Johnathan": {
             "unique_count": N,
             "unique_items": [
                 {
                     "address": "...",
                     "city": "...",
                     "parsed_listing_ids": ["...", "..."]
                 },
                 ...
             ]
         },
         "David": { ... }
       }
    """
    init_db()

    cfg = _load_wholesaler_config()  # { wholesaler_name: [email1, email2...] }
    sender_to_wh = _build_sender_to_wholesaler(cfg)  # { email_lower: wholesaler_name }
    sender_emails = list(sender_to_wh.keys())

    print("wholesaler_config", cfg)
    print("sender_to_wholesaler", sender_to_wh)
    print("all_senders", sender_emails)

    start_dt, end_dt = _yesterday_range_utc()

    if not sender_emails:
        # No senders at all
        wholesalers_out = {
            name: {"unique_count": 0, "unique_items": []}
            for name in cfg.keys()
        }
        return {
            "ok": True,
            "range_utc": {"start": start_dt.isoformat(), "end": end_dt.isoformat()},
            "bucket_size": len(cfg),
            "wholesalers": wholesalers_out,
            "note": "no sender emails configured",
        }

    # 1) FilteredListingEmail from yesterday + all sender emails
    email_q = (
        Q(created_at__gte=start_dt)
        & Q(created_at__lt=end_dt)
        & _sender_email_q(sender_emails)
    )
    print("email_q",email_q)

    emails = list(
        FilteredListingEmail.objects(email_q).only("id", "from_info", "created_at")
    )
    email_ids = [e.id for e in emails]

    print("email_ids",email_ids)

    if not email_ids:
        # Return structure but empty listings per wholesaler
        wholesalers_out = {
            name: {"unique_count": 0, "unique_items": []}
            for name in cfg.keys()
        }
        return {
            "ok": True,
            "range_utc": {"start": start_dt.isoformat(), "end": end_dt.isoformat()},
            "bucket_size": len(cfg),
            "wholesalers": wholesalers_out,
        }

    # Map FilteredListingEmail.id -> wholesaler_name
    email_id_to_wholesaler: Dict[str, str] = {}
    for e in emails:
        raw_sender = ""
        try:
            raw_sender = e.from_info.email  # adjust if your field structure differs
        except Exception as ex:
            print("from_info.email error:", ex)
            raw_sender = ""

        sender_email = (raw_sender or "").strip().lower()
        if not sender_email:
            continue

        wh_name = sender_to_wh.get(sender_email)
        if wh_name:
            email_id_to_wholesaler[str(e.id)] = wh_name

    print("email_id_to_wholesaler", email_id_to_wholesaler)

    # 2) ParsedListing by source_email reference
    pls = list(
        ParsedListing.objects(source_email__in=email_ids)
        .only("id", "address", "city", "state", "zip", "source_email")
    )



    # 3) Per-wholesaler unique maps:
    #    { wholesaler_name: { (addr,city): {address, city, parsed_listing_ids[]} } }
    per_wholesaler: Dict[str, Dict[Tuple[str, str], Dict[str, Any]]] = {}

    for pl in pls:
        addr = (getattr(pl, "address", "") or "").strip()
        city = (getattr(pl, "city", "") or "").strip()
        state = (getattr(pl, "state", "") or "").strip()
        zip = (getattr(pl, "zip", "") or "").strip()
        key = _norm_key(addr, city)
        if not key:
            continue

        src = getattr(pl, "source_email", None)
        src_id = str(getattr(src, "id", src) or "")
        wh_name = email_id_to_wholesaler.get(src_id)
        if not wh_name:
            # listing came from a sender not mapped to any wholesaler
            continue

        wh_map = per_wholesaler.setdefault(wh_name, {})

        if key not in wh_map:
            wh_map[key] = {
                "address": addr,
                "city": city,
                "state": state,
                "zip": zip,
                "parsed_listing_ids": [str(pl.id)],
            }
        else:
            wh_map[key]["parsed_listing_ids"].append(str(pl.id))

    # 4) Build final per-wholesaler output, ensuring every wholesaler name is present
    wholesalers_out: Dict[str, Any] = {}
    for wh_name in cfg.keys():
        wh_map = per_wholesaler.get(wh_name, {})
        unique_items = list(wh_map.values())
        wholesalers_out[wh_name] = {
            "unique_count": len(unique_items),
            "unique_items": unique_items,
        }

    return {
        "ok": True,
        "range_utc": {"start": start_dt.isoformat(), "end": end_dt.isoformat()},
        "bucket_size": len(cfg),
        "wholesalers": wholesalers_out,
    }


def snapshot_yesterday_special_avail() -> Dict[str, Any]:
    """
    1) Calls build_yesterday_unique_parsed_listings_for_wholesalers()
    2) For each wholesaler:
         - Upsert a SpecialAvail row with:
             wholesaler_name
             range_start, range_end
             items = unique_items[]
             status = "new"
    3) Returns a small summary dict.
    """
    init_db()

    # Get the same date range used by the builder
    range_start, range_end = _yesterday_range_utc()

    # Build per-wholesaler uniques
    res = build_yesterday_unique_parsed_listings_for_wholesalers()
    wholesalers = res.get("wholesalers", {})

    created = 0
    updated = 0

    for wh_name, wh_data in wholesalers.items():
        unique_items = wh_data.get("unique_items") or []

        # ✅ Skip wholesalers with no unique items
        if not unique_items:
            continue

        # Normalize wholesaler name (we store as-is, not lowercased)
        name_str = str(wh_name).strip()
        if not name_str:
            continue

        # Upsert: if the same wholesaler + date range snapshot already exists,
        # update items/status instead of creating duplicates.
        q = {
            "wholesaler_name": name_str,
            "range_start": range_start,
            "range_end": range_end,
        }

        existing = SpecialAvail.objects(**q).first()
        if existing:
            existing.items = unique_items
            existing.status = "new"
            existing.save()
            updated += 1
        else:
            SpecialAvail(
                wholesaler_name=name_str,
                range_start=range_start,
                range_end=range_end,
                items=unique_items,
                status="new",
            ).save()
            created += 1

    return {
        "ok": True,
        "range_utc": {
            "start": range_start.isoformat(),
            "end": range_end.isoformat(),
        },
        "wholesalers_found": len(wholesalers),
        "created": created,
        "updated": updated,
    }



def _podio_headers(access_token: str) -> Dict[str, str]:
    return {
        "Authorization": f"OAuth2 {access_token}",
        "Content-Type": "application/json",
    }


def _parse_podio_created_on(item: Dict[str, Any]) -> Optional[datetime]:
    """
    Best-effort parse of Podio created_on/created_at to an aware UTC datetime.
    Podio usually returns "YYYY-MM-DD HH:MM:SS" in UTC.
    """
    raw = item.get("created_on") or item.get("created_at")
    if not raw:
        return None

    try:
        # Handle ISO-like string with Z
        if isinstance(raw, str) and raw.endswith("Z"):
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        # Handle "YYYY-MM-DD HH:MM:SS"
        if isinstance(raw, str):
            dt = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
            return dt.replace(tzinfo=timezone.utc)
        # If it’s already a datetime, normalize to UTC if naive
        if isinstance(raw, datetime):
            if raw.tzinfo is None:
                return raw.replace(tzinfo=timezone.utc)
            return raw.astimezone(timezone.utc)
    except Exception as e:
        print(f"[Manny] Failed to parse created_on: {raw} ({e})")

    return None

def _fetch_active_properties_for_wholesaler(
    access_token: str,
    wholesaler_podio_item_id: int,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """
    Fetch all ACTIVE property items for a single wholesaler from the properties app.

    Filter:
      - status field (category) == Active option
      - wholesaler app field references wholesaler_podio_item_id

    Returns:
      [
        { "address": "<string>", "podio_item_id": <int> },
        ...
      ]
    """
    url = f"{PODIO_BASE_URL}/item/app/{PODIO_PROPERTIES_APP_ID}/filter"
    headers = _podio_headers(access_token)

    results: List[Dict[str, Any]] = []
    offset = 0

    while True:
        body = {
            "filters": {
                PODIO_FIELD_STATUS_ID: [PODIO_STATUS_ACTIVE_OPTION_ID],
                PODIO_FIELD_WHOLESALER_ID: [wholesaler_podio_item_id],
            },
            "limit": limit,
            "offset": offset,
        }

        print("body", body)

        resp = requests.post(url, headers=headers, json=body, timeout=25)
        resp.raise_for_status()
        data = resp.json()

        batch = data.get("items", [])
        if not batch:
            break

        for item in batch:
            addr = _extract_property_address(item)
            if not addr:
                continue

            # Podio filter returns items with "item_id" (or sometimes "id")
            prop_id = item.get("item_id") or item.get("id")
            created_on = _parse_podio_created_on(item)
            if not prop_id:
                # if for some reason id is missing, still keep address
                results.append({"address": addr, "podio_item_id": None, "created_on": created_on})
            else:
                results.append({"address": addr, "podio_item_id": prop_id, "created_on": created_on})

        total = data.get("total", 0)
        offset += len(batch)
        if offset >= total:
            break

    return results

def _extract_property_address(item: Dict[str, Any]) -> Optional[str]:
    """
    Extract the 'property-address' field (field_id = PODIO_FIELD_PROPERTY_ADDRESS_ID)
    from a Podio item.

    Handles common Podio shapes:
      - Text field: {"value": "123 Main St, Miami, FL 33101"}
      - Location field: {"value": {"street": "...", "city": "...", ...}}

    Returns a single string or None if not found.
    """
    for f in item.get("fields", []):
        if f.get("field_id") == PODIO_FIELD_PROPERTY_ADDRESS_ID:
            values = f.get("values") or []
            if not values:
                return None

            v = values[0].get("value")  # first value only
            if isinstance(v, str):
                return v.strip()

            if isinstance(v, dict):
                # Common pattern for location-type fields
                parts = []
                for k in ["street", "city", "state", "postal_code", "country"]:
                    val = v.get(k)
                    if val:
                        parts.append(str(val).strip())
                if parts:
                    return ", ".join(parts)

                # Fallback: stringify dict if nothing else
                return json.dumps(v, ensure_ascii=False)

    return None

def process_one_special_avail_with_active_listings() -> Dict[str, Any]:
    """
    Worker-style step:

    1) Find ONE SpecialAvail with status="new" (oldest first).
    2) Look up its wholesaler_name in DEFAULT_WHOLESALER_BUCKET_PODIO
       (via _load_wholesaler_bucket).
    3) For all mapped Podio wholesaler item IDs:
         - fetch ACTIVE properties from the Properties app
         - each property -> { "address": str, "podio_item_id": int | None }
    4) Store combined list into special_avail.active_listings.
    5) Set status="ready_to_search" and save.

    Returns a summary dict about what was processed.
    """
    init_db()

    # 1) Get the next pending SpecialAvail
    sa = SpecialAvail.objects(status="new").order_by("+created_at").first()
    if not sa:
        return {
            "ok": True,
            "processed": False,
            "reason": "no SpecialAvail with status=new",
        }

    wholesaler_name_raw = sa.wholesaler_name or ""
    wholesaler_key = wholesaler_name_raw.strip().lower()
    if not wholesaler_key:
        sa.status = "no_wholesaler_name"
        sa.save()
        return {
            "ok": False,
            "processed": False,
            "special_avail_id": str(sa.id),
            "reason": "SpecialAvail has empty wholesaler_name",
        }

    # 2) Load Podio bucket and find this wholesaler's Podio IDs
    bucket = _load_wholesaler_bucket()  # { wholesaler_name_lower: [podio_id, ...] }
    podio_ids = bucket.get(wholesaler_key, [])

    if not podio_ids:
        # No mapping for this wholesaler in the Podio bucket
        sa.status = "no_podio_mapping"
        sa.save()
        return {
            "ok": False,
            "processed": False,
            "special_avail_id": str(sa.id),
            "wholesaler_name": wholesaler_name_raw,
            "reason": "no Podio item IDs configured for this wholesaler",
        }

    # 3) Fetch active properties from Podio for ALL configured wholesaler item IDs
    access_token = get_podio_access_token()
    all_active: List[Dict[str, Any]] = []
    errors: List[str] = []

    for pid in podio_ids:
        try:
            props = _fetch_active_properties_for_wholesaler(access_token, int(pid))
            all_active.extend(props)
        except requests.RequestException as e:
            msg = f"request_error_for_id_{pid}:{e}"
            errors.append(msg)
            print(msg)

    # (Optional) Deduplicate by (address, podio_item_id) if you want:
    # seen = set()
    # deduped = []
    # for it in all_active:
    #     key = (it.get("address"), it.get("podio_item_id"))
    #     if key in seen:
    #         continue
    #     seen.add(key)
    #     deduped.append(it)
    # all_active = deduped

    # 4) Save into SpecialAvail and update status
    sa.active_listings = all_active
    sa.status = "ready_to_search"
    sa.save()

    return {
        "ok": True,
        "processed": True,
        "special_avail_id": str(sa.id),
        "wholesaler_name": wholesaler_name_raw,
        "podio_ids": podio_ids,
        "active_count": len(all_active),
        "errors": errors,
    }

def fetch_active_properties_for_wholesaler_bucket() -> Dict[str, Any]:
    """
    For each wholesaler in the bucket:
      - Use its list of wholesaler app podio_item_ids
      - Pull all ACTIVE properties from the Properties app
        where:
          - status == Active
          - wholesaler app field references ANY of those ids

    Returns:
    {
      "ok": true,
      "bucket_size": N,
      "wholesalers": {
        "johnathan": {
          "wholesaler_podio_item_ids": [857496733, 778054254],
          "count": 232,
          "items": [
            {
              "address": "618 Monroe Ave, Lehigh Acres, FL 33972, USA",
              "podio_item_id": 46743743
            },
            ...
          ],
          "errors": ["...optional..."]
        },
        "david": { ... }
      }
    }
    """
    init_db()

    bucket = _load_wholesaler_bucket()  # { wholesaler_name_lower: [podio_id, ...] }
    if not bucket:
        return {
            "ok": True,
            "bucket_size": 0,
            "wholesalers": {},
            "note": "wholesaler bucket is empty",
        }

    print("bucket", bucket)

    access_token = get_podio_access_token()

    wholesalers_out: Dict[str, Any] = {}
    for wholesaler_name, id_list in bucket.items():
        all_items: List[Dict[str, Any]] = []
        errors: List[str] = []

        for pid in id_list:
            try:
                props = _fetch_active_properties_for_wholesaler(access_token, int(pid))
                all_items.extend(props)
            except requests.RequestException as e:
                msg = f"request_error_for_id_{pid}:{e}"
                errors.append(msg)
                print(msg)

        wholesalers_out[wholesaler_name] = {
            "wholesaler_podio_item_ids": id_list,
            "count": len(all_items),
            "items": all_items,
        }
        if errors:
            wholesalers_out[wholesaler_name]["errors"] = errors

    return {
        "ok": True,
        "bucket_size": len(bucket),
        "wholesalers": wholesalers_out,
    }




def _format_full_address(addr: str, city: str, state: str, zip_code: str) -> Optional[str]:
    """
    Build '123 Main St, Miami, FL 33101, USA' style string.
    Returns None if address+city are missing.
    """
    addr = (addr or "").strip()
    city = (city or "").strip()
    state = (state or "").strip()
    zip_code = (zip_code or "").strip()

    if not addr and not city:
        return None

    parts = []

    if addr:
        parts.append(addr)
    if city:
        parts.append(city)

    # Combine state + zip without comma
    state_zip = " ".join(p for p in [state, zip_code] if p)
    if state_zip:
        parts.append(state_zip)

    parts.append("USA")

    return ", ".join(parts)


def ai_match_active_to_unique(
    active_address: str,
    candidate_addresses: List[str],
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Use an LLM to check if `active_address` matches ANY of the `candidate_addresses`.

    Returns JSON like:
      {
        "found": true/false,
        "matched_index": int or null,    # 0-based index into candidate_addresses
        "matched_address": "string or null",
        "reason": "short explanation"
      }
    """
    model = model or MATCH_MODEL
    active_address = (active_address or "").strip()
    candidates = [c for c in candidate_addresses if isinstance(c, str) and c.strip()]

    if not active_address or not candidates:
        return {
            "found": False,
            "matched_index": None,
            "matched_address": None,
            "reason": "missing_active_or_candidate_addresses",
        }

    system_msg = {
        "role": "system",
        "content": (
            "You are an assistant that matches real-estate property addresses.\n"
            "You will be given one ACTIVE property address and a list of candidate addresses.\n"
            "Addresses may differ in formatting (abbreviations like St vs Street, missing 'USA', etc.).\n"
            "Your job is to decide if any candidate refers to the SAME physical property.\n\n"
            "Return JSON only:\n"
            "{\n"
            '  \"found\": true or false,\n'
            '  \"matched_index\": integer index of the best match in the candidates array (0-based), or null,\n'
            '  \"matched_address\": string of the matched candidate address or null,\n'
            '  \"reason\": short explanation\n'
            "}\n"
            "If you are not confident that any candidate is the same property, set found=false."
        ),
    }

    user_msg = {
        "role": "user",
        "content": (
            "ACTIVE_ADDRESS:\n"
            f"{active_address}\n\n"
            "CANDIDATE_ADDRESSES (0-based index):\n"
            + json.dumps(
                {i: addr for i, addr in enumerate(candidates)},
                ensure_ascii=False,
                indent=2,
            )
        ),
    }

    # Handle 'gpt-5-mini' temperature quirk similar to your earlier pattern
    kwargs: Dict[str, Any] = {
        "model": model,
        "messages": [system_msg, user_msg],
        "response_format": {"type": "json_object"},
    }
    if model != "gpt-5-mini":
        kwargs["temperature"] = 0

    resp = client.chat.completions.create(**kwargs)
    raw = resp.choices[0].message.content

    try:
        data = json.loads(raw)
    except Exception:
        return {
            "found": False,
            "matched_index": None,
            "matched_address": None,
            "reason": f"json_parse_error:{raw[:200]}",
        }

    # basic normalization
    found = bool(data.get("found", False))
    idx = data.get("matched_index")
    matched_addr = data.get("matched_address")

    if found:
        if not isinstance(idx, int) or not (0 <= idx < len(candidates)):
            # If model said found=true but index is invalid, treat as not found
            return {
                "found": False,
                "matched_index": None,
                "matched_address": None,
                "reason": "invalid_matched_index_from_model",
            }
        matched_addr = candidates[idx]

    return {
        "found": found,
        "matched_index": idx if found else None,
        "matched_address": matched_addr if found else None,
        "reason": (data.get("reason") or "").strip(),
    }

def _normalize_address_for_match(addr: str) -> str:
    """
    Cheap normalization for string-based address matching.

    - Lowercase
    - Strip leading/trailing spaces
    - Collapse multiple spaces
    - Remove commas and periods
    - Remove trailing ', usa' if present

    This is intentionally simple to keep it fast & deterministic.
    """
    if not addr:
        return ""
    a = addr.strip().lower()

    # remove trailing ", usa" or " usa"
    a = re.sub(r",?\s*usa$", "", a)

    # remove commas and periods
    a = a.replace(",", " ").replace(".", " ")

    # collapse multiple spaces
    a = re.sub(r"\s+", " ", a)

    return a

def process_one_special_avail_matching() -> Dict[str, Any]:
    """
    Workflow:

    1) Find ONE SpecialAvail with status="ready_to_search" (oldest).
       - Immediately set status="processing" and save.
    2) Build a list of unique full-address strings from its `items`:
         "<address>, <city>, <state> <zip>, USA"
    3) For each `active_listings` entry:
         - Use AI to search the active address within the unique address list.
         - Determine found / not_found.
         - POST result to webhook with:
             wholesaler_name, range_start, range_end,
             active_address, podio_item_id, found, matched_address.
    4) After processing all active listings, set status="finished" and save.

    Returns summary dict.
    """
    init_db()

    # 1) Pull one ready_to_search item
    sa = SpecialAvail.objects(status="ready_to_search").order_by("+created_at").first()
    if not sa:
        return {
            "ok": True,
            "processed": False,
            "reason": "no SpecialAvail with status=ready_to_search",
        }

    # mark as processing to avoid concurrent pick-ups
    sa.status = "processing"
    sa.save()

    wh_name = sa.wholesaler_name
    range_start = sa.range_start
    range_end = sa.range_end

    # 2) Build unique full-address list from items
    items = sa.items or []
    unique_full_addresses: List[str] = []
    for it in items:
        addr = (it.get("address") or "").strip()
        city = (it.get("city") or "").strip()
        state = (it.get("state") or "").strip()
        zip_code = (it.get("zip") or "").strip()
        full = _format_full_address(addr, city, state, zip_code)
        if full:
            unique_full_addresses.append(full)

    active_listings = sa.active_listings or []

    normalized_map: Dict[str, Dict[str, Any]] = {}
    for idx, full in enumerate(unique_full_addresses):
        norm = _normalize_address_for_match(full)
        if not norm:
            continue
        # keep first occurrence (we just need one canonical match)
        if norm not in normalized_map:
            normalized_map[norm] = {"index": idx, "address": full}

    active_listings = sa.active_listings or []

    if not MATCH_WEBHOOK_URL:
        print("[SpecialAvail] WARNING: SPECIAL_AVAIL_MATCH_WEBHOOK_URL is not set. "
              "Will not POST to any webhook.")

    match_results: List[Dict[str, Any]] = []
    print("unique_full_addresses",unique_full_addresses)

    # 3) Loop through active listings and AI-match each one
    for active in active_listings:
        active_addr = (active.get("address") or "").strip()
        podio_item_id = active.get("podio_item_id")

        if not active_addr or not unique_full_addresses:
            ai_result = {
                "found": False,
                "matched_index": None,
                "matched_address": None,
                "reason": "missing_active_address_or_no_candidates",
            }
        else:
            # ai_result = ai_match_active_to_unique(
            #     active_address=active_addr,
            #     candidate_addresses=unique_full_addresses,
            # )
            norm_active = _normalize_address_for_match(active_addr)
            direct = normalized_map.get(norm_active)

            if direct:
                # direct match found; skip AI
                ai_result = {
                    "found": True,
                    "matched_index": direct["index"],
                    "matched_address": direct["address"],
                    "reason": "direct_string_match",
                }
            else:
                # fallback to AI-based semantic/fuzzy match
                ai_result = ai_match_active_to_unique(
                    active_address=active_addr,
                    candidate_addresses=unique_full_addresses,
                )

        found = bool(ai_result.get("found", False))
        matched_address = ai_result.get("matched_address")

        payload = {
            "wholesaler_name": wh_name,
            "range_start": range_start.isoformat() if range_start else None,
            "range_end": range_end.isoformat() if range_end else None,
            "active_address": active_addr,
            "podio_item_id": podio_item_id,
            "found": found,
            "matched_address": matched_address,
            "ai_reason": ai_result.get("reason"),
        }

        print("active_address",active_addr,"  >  ",matched_address,"  >>  ",found,ai_result.get("reason"))

        webhook_ok = False
        if MATCH_WEBHOOK_URL:
            try:
                resp = requests.post(
                    MATCH_WEBHOOK_URL,
                    json=payload,
                    timeout=15,
                )
                webhook_ok = resp.status_code in (200, 201, 202)
                if not webhook_ok:
                    print(
                        f"[SpecialAvail] Webhook non-2xx: {resp.status_code}, body={resp.text[:300]}"
                    )
            except requests.RequestException as e:
                print(f"[SpecialAvail] Webhook request failed: {e}")

        payload["webhook_ok"] = webhook_ok
        match_results.append(payload)

    # 4) Mark as finished
    sa.status = "finished"
    sa.save()

    return {
        "ok": True,
        "processed": True,
        "special_avail_id": str(sa.id),
        "wholesaler_name": wh_name,
        "unique_count": len(unique_full_addresses),
        "active_count": len(active_listings),
        "matches": match_results,
    }



MANNY_WEBHOOK_URL = os.getenv("MANNY_MATCH_WEBHOOK_URL", "").strip()  # or pass as arg
# You will pass Manny's wholesaler Podio item IDs as a parameter.
MANNY_MATCH_MODEL = "gpt-5-mini"

def _to_est_date(dt: datetime) -> date:
    """
    Convert a datetime (any tz or naive) to a date in America/New_York.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    est = dt.astimezone(ZoneInfo("America/New_York"))
    return est.date()


def _business_days_between(start: date, end: date) -> int:
    """
    Count business days between start and end (exclusive of start, inclusive of end).
    If end <= start, returns 0.
    """
    if end <= start:
        return 0

    delta_days = (end - start).days
    full_weeks, extra_days = divmod(delta_days, 7)

    business_days = full_weeks * 5

    for i in range(extra_days):
        day = start + timedelta(days=full_weeks * 7 + i + 1)  # start+1 .. start+delta
        if day.weekday() < 5:  # 0–4 = Mon–Fri
            business_days += 1

    return business_days


def _older_than_business_days(created_on: datetime, n_days: int = 3) -> bool:
    """
    True if created_on is more than `n_days` business days ago in EST.
    """
    created_date = _to_est_date(created_on)
    today_est = datetime.now(ZoneInfo("America/New_York")).date()
    bdays = _business_days_between(created_date, today_est)
    return bdays > n_days  # strictly 'more than 3 business days ago'


def _fetch_google_sheet_text(sheet_url: str, max_chars: int = 12000) -> str:
    """
    Fetches the *rendered* public Google Sheet HTML/text and returns a
    plain-text version (all tabs combined). Truncates to max_chars to
    keep prompts reasonable.
    """
    try:
        resp = requests.get(sheet_url, timeout=20)
        resp.raise_for_status()
        text = resp.text or ""
    except requests.RequestException as e:
        print(f"[Manny] Failed to fetch Google Sheet: {e}")
        return ""

    # Very simple HTML tag strip; you can replace with BeautifulSoup if desired.
    # We just want a big text blob with rows separated by newlines.
    import re
    # remove script/style
    text = re.sub(r"<script.*?</script>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    # replace <br> and <td>/<th> with newlines
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</tr\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(td|th)\s*>", " | ", text, flags=re.IGNORECASE)
    # strip remaining tags
    text = re.sub(r"<[^>]+>", " ", text)
    # normalize whitespace
    text = re.sub(r"\s+", " ", text).strip()

    if len(text) > max_chars:
        text = text[:max_chars] + " …(truncated)"

    return text

def _fetch_google_sheets_text(sheet_urls: List[str], max_chars: int = 24000) -> str:
    """
    Fetch and combine text from multiple public Google Sheet URLs.
    Each sheet is truncated individually, then the combined result
    is truncated to max_chars overall.
    """
    combined_parts: List[str] = []

    for url in sheet_urls:
        url = (url or "").strip()
        if not url:
            continue
        txt = _fetch_google_sheet_text(url, max_chars=max_chars // max(1, len(sheet_urls)))
        if txt:
            combined_parts.append(f"[SHEET_START] {url}\n{txt}\n[SHEET_END]\n")

    combined = "\n".join(combined_parts).strip()
    if len(combined) > max_chars:
        combined = combined[:max_chars] + " …(combined truncated)"

    return combined

def ai_match_address_in_sheet(
    active_address: str,
    sheet_text: str,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Use an LLM to check if `active_address` appears to be present in the sheet_text,
    allowing for formatting differences, abbreviations, etc.

    Returns:
      {
        "found": bool,
        "reason": str,
      }
    """
    active_address = (active_address or "").strip()
    sheet_text = (sheet_text or "").strip()
    model = model or MANNY_MATCH_MODEL

    if not active_address or not sheet_text:
        return {
            "found": False,
            "reason": "missing_active_address_or_sheet_text",
        }

    system_msg = {
        "role": "system",
        "content": (
            "You are an assistant that matches real-estate property addresses.\n"
            "You will be given ONE Podio active property address and the plain-text content of a Google Sheet\n"
            "that contains multiple rows of properties.\n\n"
            "Your task: decide whether ANY row in the sheet corresponds to the SAME physical property\n"
            "as the Podio active address, even if formatting differs (abbreviations, missing 'USA', etc.).\n"
            "If you are not reasonably confident, return found=false.\n\n"
            "Return ONLY valid JSON:\n"
            "{\n"
            '  \"found\": true or false,\n'
            '  \"reason\": \"short explanation\"\n'
            "}\n"
        ),
    }

    user_msg = {
        "role": "user",
        "content": (
            "PODIO_ACTIVE_ADDRESS:\n"
            f"{active_address}\n\n"
            "SHEET_TEXT (multiple rows, may be truncated):\n"
            f"{sheet_text}\n"
        ),
    }

    kwargs: Dict[str, Any] = {
        "model": model,
        "messages": [system_msg, user_msg],
        "response_format": {"type": "json_object"},
    }
    if model != "gpt-5-mini":
        kwargs["temperature"] = 0

    resp = client.chat.completions.create(**kwargs)
    raw = resp.choices[0].message.content

    try:
        data = json.loads(raw)
    except Exception:
        return {
            "found": False,
            "reason": f"json_parse_error:{raw[:200]}",
        }

    found = bool(data.get("found", False))
    reason = (data.get("reason") or "").strip()

    return {
        "found": found,
        "reason": reason,
    }

def process_manny_special_avails(
    manny_podio_item_ids: List[int],
    sheet_urls: List[str],
    webhook_url: Optional[str] = None,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """
    1) Fetch ACTIVE properties from Podio for Manny (one or more wholesaler app item IDs).
    2) Fetch Google Sheet public URL (all tabs text combined).
    3) For each Podio active listing:
         - First, cheap case-insensitive substring check in sheet_text.
         - If not found, call OpenAI (ai_match_address_in_sheet) to fuzzy match.
         - POST result to webhook with:
             {podio_item_id, address, found, reason}
    4) Return summary.

    webhook_url: if None, uses MANNY_WEBHOOK_URL env var.
    """
    webhook_url = (webhook_url or MANNY_WEBHOOK_URL or "").strip()
    print("webhook_url",webhook_url)
    if not manny_podio_item_ids:
        return {"ok": False, "error": "no_manny_podio_item_ids_provided"}

    # 1) Fetch active properties from Podio
    access_token = get_podio_access_token()
    all_active: List[Dict[str, Any]] = []
    errors: List[str] = []

    for pid in manny_podio_item_ids:
        try:
            props = _fetch_active_properties_for_wholesaler(access_token, int(pid))
            all_active.extend(props)
        except requests.RequestException as e:
            msg = f"[Manny] request_error_for_id_{pid}: {e}"
            print(msg)
            errors.append(msg)

    if not all_active:
        return {
            "ok": True,
            "active_count": 0,
            "webhook_url": webhook_url or None,
            "errors": errors,
        }

    # 2) Fetch combined sheet text (from multiple URLs)
    sheet_text = _fetch_google_sheets_text(sheet_urls)
    if not sheet_text:
        return {
            "ok": False,
            "error": "failed_to_fetch_sheet_text",
            "active_count": len(all_active),
            "errors": errors,
        }

    # # 2) Fetch sheet text once
    # sheet_text = _fetch_google_sheet_text(sheet_url)
    # if not sheet_text:
    #     return {
    #         "ok": False,
    #         "error": "failed_to_fetch_sheet_text",
    #         "active_count": len(all_active),
    #         "errors": errors,
    #     }

    sheet_text_lower = sheet_text.lower()


    # 3) Loop over active listings and match
    results: List[Dict[str, Any]] = []
    ai_calls = 0
    webhook_failures = 0

    # for item in all_active:
    #     addr = (item.get("address") or "").strip()
    #     podio_item_id = item.get("podio_item_id")
        

    #     if not addr or not podio_item_id:
    #         # skip if missing basics
    #         results.append({
    #             "podio_item_id": podio_item_id,
    #             "address": addr,
    #             "found": False,
    #             "reason": "missing_address_or_podio_item_id",
    #             "webhook_ok": False,
    #         })
    #         continue

    #     # First: cheap substring check (case-insensitive)
    #     addr_norm = addr.lower()
    #     if addr_norm and addr_norm in sheet_text_lower:
    #         found = True
    #         reason = "simple_substring_match"
    #     else:
    #         # Fuzzy AI check
    #         ai_calls += 1
    #         ai_result = ai_match_address_in_sheet(
    #             active_address=addr,
    #             sheet_text=sheet_text,
    #             model=model,
    #         )
    #         found = bool(ai_result.get("found", True))
    #         reason = ai_result.get("reason") or "ai_no_reason"

    #     payload = {
    #         "podio_item_id": podio_item_id,
    #         "address": addr,
    #         "found": found,
    #         "reason": reason,
    #     }

    #     print("podio_item_id",podio_item_id," - ",addr," - ",found)


    #     webhook_ok = False
    #     if webhook_url:
    #         try:
    #             resp = requests.post(webhook_url, json=payload, timeout=15)
    #             webhook_ok = resp.status_code in (200, 201, 202)
    #             if not webhook_ok:
    #                 webhook_failures += 1
    #                 print(
    #                     f"[Manny] Webhook non-2xx ({resp.status_code}) "
    #                     f"for podio_item_id={podio_item_id}, body={resp.text[:300]}"
    #                 )
    #         except requests.RequestException as e:
    #             webhook_failures += 1
    #             print(f"[Manny] Webhook request failed for {podio_item_id}: {e}")

    #     payload["webhook_ok"] = webhook_ok
    #     results.append(payload)


    skipped_recent = 0

    for item in all_active:
        addr = (item.get("address") or "").strip()
        podio_item_id = item.get("podio_item_id")
        created_on = item.get("created_on")  # datetime or None

        if not addr or not podio_item_id:
            results.append({
                "podio_item_id": podio_item_id,
                "address": addr,
                "found": False,
                "reason": "missing_address_or_podio_item_id",
                "webhook_ok": False,
            })
            continue

        # ✅ NEW: only process if older than 3 business days
        if not isinstance(created_on, datetime) or not _older_than_business_days(created_on, 3):
            skipped_recent += 1
            print("New",addr,created_on)
            # Option 1: completely skip without logging
            # continue

            # Option 2 (recommended): include in results but mark as skipped
            results.append({
                "podio_item_id": podio_item_id,
                "address": addr,
                "found": False,
                "reason": "skipped_too_recent_or_missing_created_on",
                "webhook_ok": False,
            })
            continue


        addr_norm = addr.lower()
        if addr_norm and addr_norm in sheet_text_lower:
            found = True
            reason = "simple_substring_match"
        else:
            ai_calls += 1
            ai_result = ai_match_address_in_sheet(
                active_address=addr,
                sheet_text=sheet_text,
                model=model,
            )
            found = bool(ai_result.get("found", True))
            reason = ai_result.get("reason") or "ai_no_reason"

        payload = {
            "podio_item_id": podio_item_id,
            "address": addr,
            "found": found,
            "reason": reason,
        }


        webhook_ok = False
        if webhook_url:
            try:
                resp = requests.post(webhook_url, json=payload, timeout=15)
                webhook_ok = resp.status_code in (200, 201, 202)
                if not webhook_ok:
                    webhook_failures += 1
                    print(
                        f"[Manny] Webhook non-2xx ({resp.status_code}) "
                        f"for podio_item_id={podio_item_id}, body={resp.text[:300]}"
                    )
            except requests.RequestException as e:
                webhook_failures += 1
                print(f"[Manny] Webhook request failed for {podio_item_id}: {e}")

        payload["webhook_ok"] = webhook_ok
        results.append(payload)

    return {
        "ok": True,
        "active_count": len(all_active),
        "ai_calls": ai_calls,
        "webhook_url": webhook_url or None,
        "webhook_failures": webhook_failures,
        "errors": errors,
        "results": results,
    }
