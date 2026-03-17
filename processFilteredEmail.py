from datetime import datetime, timezone, timedelta
# from mongo_engine_conn import init_db
from models import FilteredListingEmail, ParsedListing
from listingDetails import upsert_parsed_listings_from_html

from email.utils import parsedate_to_datetime
try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except ImportError:
    from backports.zoneinfo import ZoneInfo  # if you use backports

TZ_NY = ZoneInfo("America/New_York")
START_MIN = 7 * 60    # 07:00
END_MIN   = 21 * 60   # 21:00

def _email_local_dt(fe) -> datetime:
    """
    Return the email's datetime in America/New_York.
    Prefers internal_date.ts_ms; falls back to RFC822 Date header.
    """
    # Prefer Gmail internalDate (ms since epoch, UTC)
    try:
        ts_ms = int(getattr(fe.internal_date, "ts_ms", 0) or 0)
        if ts_ms:
            return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).astimezone(TZ_NY)
    except Exception as e:
        pass

    # Fallback: RFC822 Date header
    try:
        rfc822 = (fe.rfc822_date or "").strip()
        if rfc822:
            dt = parsedate_to_datetime(rfc822)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(TZ_NY)
    except Exception:
        pass

    # Last resort: now (ET)
    return datetime.now(tz=TZ_NY)

def _within_et_window(dt_local: datetime) -> bool:
    """
    True if local ET time is between 07:00 and 21:00 inclusive.
    """
    mins = dt_local.hour * 60 + dt_local.minute
    return START_MIN <= mins <= END_MIN

SENDER_LISTING_SLICES = {
    "acct1": [
        {"pattern": "jc-quickturnproperties.com@shared1.ccsend.com", "range": "1-1"},
        {"pattern": "jc@stellarholdingsllc.ccsend.com", "range": "1-1"},
        {"pattern":"m.mcgrane@stellarholdingsllc.ccsend.com", "range":"1-1"},
        {"pattern": "tsims-southfloridacashhomebuyers.com@shared1.ccsend.com", "range": "0-0"},
        {"pattern": "kevin-titlerate.com@shared1.ccsend.com", "range": "0-0"}
    ],
    "acct2": [],
}

def _parse_range_spec(spec: str) -> tuple[int, int] | None:
    if not spec: return None
    s = spec.strip().replace(" ", "")
    if "-" in s: a, b = s.split("-", 1)
    elif "," in s: a, b = s.split(",", 1)
    else: a, b = s, s
    try:
        start, end = int(a), int(b)
        # if start < 1 or end < 1: return None
        if end < start: start, end = end, start
        return (start, end)
    except Exception:
        return None

def _sender_slice_for(acct_label: str, sender_email: str) -> tuple[int, int] | None:
    import fnmatch
    rules = SENDER_LISTING_SLICES.get(acct_label, [])
    eml = (sender_email or "").strip().lower()
    for rule in rules:
        pat = (rule.get("pattern") or "").strip().lower()
        rng = _parse_range_spec(rule.get("range") or "")
        if pat and rng and fnmatch.fnmatch(eml, pat):
            return rng
    return None

def process_pending(limit=2):
    # init_db()
    # FilteredListingEmail.ensure_indexes()
    # ParsedListing.ensure_indexes()

    pending = FilteredListingEmail.objects(status="not_processed").limit(limit)

    for fe in pending:
        print("fe.id",fe.id)
        # ---- new window check (before taking the lock) ----
        # local_dt = _email_local_dt(fe)
        # if not _within_et_window(local_dt):
        #     # mark skipped and continue
        #     FilteredListingEmail.objects(id=fe.id, status="not_processed").update_one(
        #         set__status="skipped",
        #         set__updated_at=datetime.utcnow(),
        #     )
        #     continue
        # ---------------------------------------------------
        # Atomically mark this record as 'processing' only if it’s still 'not_processed'
        updated = FilteredListingEmail.objects(id=fe.id, status="not_processed").update_one(
            set__status="processing",
            set__updated_at=datetime.utcnow(),
        )
        if updated == 0:
            # Someone else took it, skip
            continue

        try:
            html = (fe.bodies.html_ai or fe.bodies.html_full or "") if fe.bodies else ""
            if not html.strip():
                FilteredListingEmail.objects(id=fe.id).update_one(
                    set__status="error",
                    set__updated_at=datetime.utcnow(),
                )
                continue

            slice_range = _sender_slice_for(fe.account_label, getattr(fe.from_info, "email", None))

            res = upsert_parsed_listings_from_html(
                email_html=html,
                account_label=fe.account_label,
                gmail_message_id=fe.gmail_message_id,
                source_email_doc=fe,
                list_slice=slice_range,  # make sure your function supports this optional arg
            )
            print("saved:", res)

            FilteredListingEmail.objects(id=fe.id).update_one(
                set__status="processed",
                set__updated_at=datetime.utcnow(),
            )
        except Exception as e:
            print(f"[filtered_email] error processing {fe.id}: {e}")
            FilteredListingEmail.objects(id=fe.id).update_one(
                set__status="error",
                set__updated_at=datetime.utcnow(),
            )


def reset_stale_processing_emails(hours: int = 6) -> dict:
    """
    Find FilteredListingEmail docs that are stuck in 'processing' for more than `hours`
    (based on created_at) and reset them back to 'not_processed'.

    Returns a small summary dict.
    """

    now = datetime.utcnow()
    cutoff = now - timedelta(hours=hours)

    # Query all items stuck in processing and older than cutoff
    q = FilteredListingEmail.objects(
        status="processing",
        created_at__lt=cutoff,
    )

    stuck_count = q.count()
    if stuck_count == 0:
        return {
            "ok": True,
            "stuck_count": 0,
            "updated": 0,
            "cutoff_utc": cutoff.isoformat(),
        }

    # Bulk update back to not_processed
    updated = q.update(
        set__status="not_processed",
        set__updated_at=now,
    )

    return {
        "ok": True,
        "stuck_count": stuck_count,
        "updated": updated,
        "cutoff_utc": cutoff.isoformat(),
    }

# process_pending()
