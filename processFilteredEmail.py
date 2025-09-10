from mongo_engine_conn import init_db
from models import FilteredListingEmail, ParsedListing
from listingDetails import upsert_parsed_listings_from_html

SENDER_LISTING_SLICES = {
    "acct1": [
        {"pattern": "jc-quickturnproperties.com@shared1.ccsend.com", "range": "1-1"},  # first only
        # {"pattern": "*.ccsend.com", "range": "1-2"},  # example: take first two items
    ],
    "acct2": [
        # examples per account
    ],
}


def _parse_range_spec(spec: str) -> tuple[int, int] | None:
    if not spec:
        return None
    s = spec.strip().replace(" ", "")
    if "-" in s:
        a, b = s.split("-", 1)
    elif "," in s:
        a, b = s.split(",", 1)
    else:
        a, b = s, s
    try:
        start = int(a)
        end = int(b)
        if start < 1 or end < 1:
            return None
        if end < start:
            start, end = end, start
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

def process_pending(limit=1):
    init_db()
    FilteredListingEmail.ensure_indexes()
    ParsedListing.ensure_indexes()

    # fetch a batch of emails to process
    pending = FilteredListingEmail.objects(status="not_processed").limit(limit)

    print("pending",pending)

    for fe in pending:
        print("fe",fe)
        html = (fe.bodies.html_ai or fe.bodies.html_full or "")
        if not html.strip():
            fe.update(set__status="error")
            continue

         # resolve slice by sender (1-based inclusive)
        slice_range = _sender_slice_for(fe.account_label, getattr(fe.from_info, "email", None))

        res = upsert_parsed_listings_from_html(
            email_html=html,
            account_label=fe.account_label,
            gmail_message_id=fe.gmail_message_id,
            source_email_doc=fe,
            list_slice=slice_range,
        )
        print("saved:", res)

        # mark the source email as processed (or leave as-is if you want a second pass)
        fe.update(set__status="processed")


process_pending()
