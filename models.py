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
        "strict": False,
        "indexes": [
            # one row per listing within an email
            {"fields": ["account_label", "gmail_message_id", "list_index"], "unique": True, "name": "uniq_email_list_index"},
            {"fields": ["status"], "name": "listing_status_idx"},
            {"fields": ["city", "state", "zip"], "name": "city_state_zip_idx"},
            {"fields": ["address", "price"], "name": "addr_price_idx"},
            {"fields": ["buyer_matching_status", "-updated_at"], "name": "buyer_match_status_idx"},
            {"fields": ["buyer_matching_status", "-buyer_matching_last_attempt_at"], "name": "buyer_match_attempt_idx"},
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
        choices=("property_not_found","not_found", "not_processed", "processed", "no_agent_email","wholeseller_not_found","bypassed")
    )

    FoundInPodioViaSearch= StringField(
        choices=("not_found", "found")
    )

    status            = StringField(
        choices=("not_processed", "verified", "ready_to_post", "processed", "passed", "posted", "skipped","skipped_quota","ready_for_image_processing","image_curation_failed","ready_for_primary_image_check","primary_image_failed","bypassed"),
        default="not_processed"
    )

    post_content = StringField()

    wp_property_description = StringField()

    wp_parsed_data = DictField()

    wp_status = StringField(
        choices=("ready_to_process", "keys_generated", "description_generated","posted")
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

    matched_buyer_ids = ListField(StringField())   

    # ✅ NEW: Rematch flag + only-new buyer ids for that rematch run
    rematch = BooleanField(default=False)
    re_matched_buyer_ids = ListField(StringField(), default=list) 
    
    # -----------------------------
    # Buyer Matching Queue (Phase 2 hardening)
    # -----------------------------
    buyer_matching_status = StringField(
        choices=("none", "pending", "processing", "matched", "errored_listing","skipped"),
        default="none"
    )
    buyer_matching_podio_item_id = IntField()  # podio properties item id passed by globiflow
    buyer_matching_attempts = IntField(default=0)
    buyer_matching_consecutive_errors = IntField(default=0)
    buyer_matching_last_error_sig = StringField()
    buyer_matching_last_error = StringField()
    buyer_matching_last_attempt_at = DateTimeField(null=True)
    buyer_send_status = StringField(
        choices=("pending", "des_generated", "sent", "failed"),
        default=None
    )

    buyer_sms_description = StringField()
    buyer_email_description = StringField()
    
    # Manual special prefs (set from Podio property field)
    manual_special_preferences_raw = StringField(null=True)
    manual_special_preferences_norm = ListField(StringField(), default=list)
    manual_special_preferences_updated_at = DateTimeField(null=True)
    # ✅ add these (required by your endpoint update_fields)
    manual_special_preferences_saved_at = DateTimeField(null=True)     # always updates when admin saves
    manual_special_preferences_rematch_at = DateTimeField(null=True)   # only updates when should_rematch=True
        


    primary_image_check = DictField(null=True)



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

class SpecialAvail(Document):
    meta = {
        "collection": "special_avail",  # name of the Mongo collection
        "indexes": [
            {"fields": ["wholesaler_name", "range_start", "range_end"], "unique": True}
        ],
    }

    wholesaler_name = StringField(required=True)  # e.g. "Johnathan"
    range_start     = DateTimeField(required=True)  # start of UTC range (yesterday 00:00)
    range_end       = DateTimeField(required=True)  # end of UTC range (today 00:00)

    # This will store the "unique_items" array from your function:
    # [
    #   {
    #     "address": "...",
    #     "city": "...",
    #     "state": "...",
    #     "zip": "...",
    #     "parsed_listing_ids": [...]
    #   }, ...
    # ]
    items           = ListField(DictField(), default=list)

    active_listings = ListField(DictField(), default=list)

    # workflow status for this snapshot
    status          = StringField(default="new")  # "new", "processed", etc.

    created_at      = DateTimeField(default=datetime.utcnow)
    updated_at      = DateTimeField(default=datetime.utcnow)

    def save(self, *args, **kwargs):
        self.updated_at = datetime.utcnow()
        return super().save(*args, **kwargs)





# models.py (append at the bottom)

from mongoengine import (
    Document, StringField, IntField, DateTimeField, DictField, ListField, BooleanField
)
from datetime import datetime

class RCMediaLinkLog(Document):
    meta = {
        "collection": "rc_media_link_logs",
        "indexes": [
            {"fields": ["conversation_id", "-created_at"], "name": "conv_time_idx"},
            {"fields": ["status"], "name": "status_idx"},
            {"fields": ["wp_post_id"], "name": "wp_post_idx"},
        ]
    }

    # What conversation / when
    conversation_id         = StringField(required=True)
    last_message_time_iso   = StringField()      # keep as string to avoid tz parsing gotchas
    message_count_considered= IntField()         # e.g., 10

    # What we extracted/used
    selected_url            = StringField()
    selected_street_number  = StringField()
    selected_address        = StringField()

    # What we targeted in WP
    wp_post_id              = IntField(null=True)
    wp_address              = StringField()
    wp_old_picture_button   = StringField()
    wp_new_picture_button   = StringField()

    # Outcome
    status                  = StringField(choices=(
        "updated",                      # WP update succeeded
        "prepared_update",              # built update body (you currently return the body)
        "already_has_picture_button_url",
        "not_found_in_wp",
        "no_action",
        "dropbox_error",
        "wp_update_failed",
        "skipped_non_media_url",   # ← add this
    ), required=True)
    reason                  = StringField()      # human-readable reason or short note
    error                   = StringField()      # exception text if any

    # Debug blobs
    ai_extract              = DictField()
    search_debug            = DictField()
    recent_dialog           = ListField(DictField())  # [{speaker, text}], sanitized

    created_at              = DateTimeField(default=datetime.utcnow)

# -------------------------------
# Web Form Buyer Submissions
# -------------------------------

class BuyerContact(EmbeddedDocument):
    name = StringField()
    company = StringField()  # legacy (may be blank now)
    email = StringField()

    # legacy fields (kept so old logic never breaks)
    text_number = StringField()
    phone_call = StringField()

    # ✅ legacy single value (keep it)
    preference = StringField()  # "whatsapp" | "call" | "sms" | "email" | "text" etc

    # NEW fields
    call_whatsapp = StringField()

    # ✅ NEW: store multi-select from frontend
    preferences = ListField(StringField(), default=list)


class BuyerLocation(EmbeddedDocument):
    # legacy (kept for indexes + old logic)
    county = StringField()
    city = StringField()

    # ✅ NEW global location (from updated frontend)
    scope = StringField()  # "south_florida" | "all_florida"
    counties = ListField(StringField(), default=list)
    cities = ListField(StringField(), default=list)


class BuyerPropertyLocation(EmbeddedDocument):
    scope = StringField()  # e.g. "south_florida" | "all_florida"
    counties = ListField(StringField(), default=list)  # legacy support (may be empty now)
    cities = ListField(StringField(), default=list)  # ✅ NEW


class BuyerPropertyPrefs(EmbeddedDocument):
    enabled = BooleanField(default=False)

    # ✅ legacy single type (keep populated for old logic)
    type = StringField()

    # ✅ NEW: multi-select types from updated frontend
    types = ListField(StringField(), default=list)

    # LEGACY (kept)
    price_range = StringField()

    # NEW
    price_ranges = ListField(StringField(), default=list)
    beds = ListField(StringField(), default=list)
    baths = ListField(StringField(), default=list)
    location = EmbeddedDocumentField(BuyerPropertyLocation)
    other_type = StringField()

    # {"Preference Label": "No/Yes/Maybe/Only"}
    preferences = DictField()
    preferences_kv = ListField(DictField(), default=list)


class WebFormBuyerSubmission(Document):
    meta = {
        "collection": "web_form_buyer_submissions",
        "indexes": [
            {"fields": ["-created_at"], "name": "created_desc"},
            {"fields": ["contact.email"], "name": "contact_email_idx"},
            {"fields": ["location.county", "location.city"], "name": "county_city_idx"},  # ✅ still valid
            {"fields": ["podio_status"], "name": "podio_status_idx"},
        ],
    }

    contact = EmbeddedDocumentField(BuyerContact, required=True)
    location = EmbeddedDocumentField(BuyerLocation, required=True)

    # property blocks (fixed keys used by frontend)
    multi_family = EmbeddedDocumentField(BuyerPropertyPrefs)
    condo = EmbeddedDocumentField(BuyerPropertyPrefs)
    land = EmbeddedDocumentField(BuyerPropertyPrefs)
    commercial = EmbeddedDocumentField(BuyerPropertyPrefs)
    single_family = EmbeddedDocumentField(BuyerPropertyPrefs)
    townhouse = EmbeddedDocumentField(BuyerPropertyPrefs)

    # store raw request for safety/future (phase 2)
    raw_payload = DictField()
    raw_payload_json = StringField()

    podio_property_html = DictField()
    podio_counties_html = DictField()

    podio_item_id = IntField()
    podio_status = StringField(choices=("not_sent", "sent", "failed"), default="not_sent")
    podio_error = StringField()

    created_at = DateTimeField(default=datetime.utcnow)
    updated_at = DateTimeField(default=datetime.utcnow)

    def touch(self):
        self.updated_at = datetime.utcnow()
