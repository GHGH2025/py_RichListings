from mongo_engine_conn import init_db
from models import FilteredListingEmail, ParsedListing
from listingDetails import upsert_parsed_listings_from_html


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

        res = upsert_parsed_listings_from_html(
            email_html=html,
            account_label=fe.account_label,
            gmail_message_id=fe.gmail_message_id,
            source_email_doc=fe,
        )
        print("saved:", res)

        # mark the source email as processed (or leave as-is if you want a second pass)
        fe.update(set__status="processed")


process_pending()
