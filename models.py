# models.py
from datetime import datetime
from mongoengine import (
    Document, EmbeddedDocument,
    StringField, IntField, DateTimeField,FloatField,ListField,DictField,
    EmbeddedDocumentField,ReferenceField,BooleanField,DynamicField,
)

class WindowRange(EmbeddedDocument):
    after_epoch = IntField(required=True)
    before_epoch = IntField(required=True)

class FromInfo(EmbeddedDocument):
    raw   = StringField()
    name  = StringField()
    email = StringField()

class InternalDate(EmbeddedDocument):
    ts_ms = IntField()
    iso   = StringField()

class Bodies(EmbeddedDocument):
    text      = StringField()
    html_full = StringField()
    html_ai   = StringField()

class FilteredListingEmail(Document):
    meta = {
        "collection": "filtered_listing_emails",
        "indexes": [
            {"fields": ["account_label", "gmail_message_id"], "unique": True, "name": "uniq_account_msg"},
            {"fields": ["from_info.email"], "name": "from_email"},
            {"fields": ["window.after_epoch", "window.before_epoch"], "name": "window_range"},
            {"fields": ["status"], "name": "status_idx"},  # optional but handy
        ]
    }

    # keys
    account_label    = StringField(required=True)
    gmail_message_id = StringField(required=True)

    # refs / convenience
    gmail_thread_id  = StringField()
    subject          = StringField()

    # window used to fetch
    window           = EmbeddedDocumentField(WindowRange, required=True)

    # headers / dates
    from_info        = EmbeddedDocumentField(FromInfo)
    rfc822_date      = StringField()
    internal_date    = EmbeddedDocumentField(InternalDate)

    # bodies
    bodies           = EmbeddedDocumentField(Bodies)

        # processing status
    status           = StringField(
        choices=("not_processed", "processing", "processed", "error"),
        default="not_processed"
    )

    forward_status       = StringField(choices=["forwarded","skipped"], null=True)  # unset initially
    forwarded_at         = DateTimeField()
    forward_to           = StringField()
    forward_preface_text = StringField()
    forward_error        = StringField()

    # audit
    created_at       = DateTimeField(default=datetime.utcnow)
    updated_at       = DateTimeField(default=datetime.utcnow)

    def touch(self):
        self.updated_at = datetime.utcnow()



# ---------- NEW: per-listing collection ----------
class ParsedListing(Document):
    meta = {
        "collection": "parsed_listings",
        # "strict": False,
        "indexes": [
            # one row per listing within an email
            {"fields": ["account_label", "gmail_message_id", "list_index"], "unique": True, "name": "uniq_email_list_index"},
            {"fields": ["status"], "name": "listing_status_idx"},
            {"fields": ["city", "state", "zip"], "name": "city_state_zip_idx"},
            {"fields": ["address", "price"], "name": "addr_price_idx"},
        ]
    }

    # linkage
    account_label     = StringField(required=True)
    gmail_message_id  = StringField(required=True)
    list_index        = IntField(required=True)
    source_email      = ReferenceField(FilteredListingEmail, required=True)

    # requested fields
    address      = StringField()
    city              = StringField()
    state             = StringField()
    zip               = StringField()
    price             = FloatField()  # store USD price (list_price_usd)

    images            = ListField(StringField())   # array of URLs
    skipped_images    = DictField() 
    other_images_source = StringField()            # single URL
    other_images_dropbox_link  = StringField()

    complete_info     = DictField()  # full JSON blob returned for this listing

    addr_city_fmt_done = DynamicField()


    # NEW: direct_wholeseller flag
    direct_wholeseller = StringField(
        choices=("property_not_found","not_found", "not_processed", "processed", "no_agent_email","wholeseller_not_found")
    )

    FoundInPodioViaSearch= StringField(
        choices=("not_found", "found")
    )

    status            = StringField(
        choices=("not_processed", "verified", "ready_to_post", "processed", "passed", "posted", "skipped","ready_for_image_processing"),
        default="not_processed"
    )

    post_content = StringField()

    wp_property_description = StringField()

    wp_parsed_data = DictField()

    wp_status = StringField(
        choices=("ready_to_process", "keys_generated", "description_generated")
    )

    wp_check = StringField(
        choices=("pending", "processed")
    )

    wp_check_post_id = IntField() 

    wp_check_reduced = StringField()


    post_id = IntField() 

    address_search_keys = ListField(StringField())

    geo_code_response = DictField()

    whatsapp_status = StringField(
        choices=("pending", "failed","sent")
    )


    rules_ai_rule_id            = StringField()   # e.g., "R3"
    rules_ai_version            = StringField()   # store YAML version as string (flexible)
    rules_ai_reason             = StringField()   # short reason when Skipped
    skipped_or_posted_at        = DateTimeField(null=True)

     # NEW: flags for your new logic
    over_35_percent = StringField(
        choices=("found", "not_found")
    )
    do_not_post_city = StringField(
        choices=("found", "not_found")
    )

    created_at        = DateTimeField(default=datetime.utcnow)
    updated_at        = DateTimeField(default=datetime.utcnow)

    def touch(self):
        self.updated_at = datetime.utcnow()


class DailyBaseCount(Document):
    meta = {
        "collection": "daily base count",  # as requested
        "indexes": [
            {
                "fields": ["current_date"],
                "unique": True,
                "name": "uniq_daily_current_date"
            },
        ],
    }

    # Date bucket for the counts (we treat this as per-day, UTC)
    current_date = DateTimeField(required=True)

    # Rolling "base" count (non_rest listings) for that day
    daily_base_count = IntField(required=True, default=0)

