from datetime import datetime

from mongoengine import DateTimeField, DictField, Document, FloatField, StringField


class RawListing(Document):
    """Latest Florida Off Market scrape snapshot (one doc per listing_id)."""

    meta = {
        "collection": "raw",
        "strict": False,
        "indexes": [
            {"fields": ["listing_id"], "unique": True, "name": "uniq_listing_id"},
            {"fields": ["source", "listing_id"], "unique": True, "name": "uniq_source_listing_id"},
            {"fields": ["updated_at"], "name": "raw_updated_at_idx"},
        ],
    }

    listing_id = StringField(required=True)
    source = StringField(default="florida_off_market")
    source_website = StringField()
    source_listing_id = StringField()
    address = StringField()
    address_norm = StringField()
    city = StringField()
    state = StringField()
    zip = StringField()
    created_at = DateTimeField(default=datetime.utcnow)
    updated_at = DateTimeField(default=datetime.utcnow)

    def touch(self):
        self.updated_at = datetime.utcnow()


class FilteredListing(Document):
    """Scraped deals queued for the listing pipeline after the 30-day address check."""

    meta = {
        "collection": "filtered",
        "indexes": [
            {"fields": ["address_norm", "status"], "name": "addr_norm_status_idx"},
            {"fields": ["status", "parsed_listing_id"], "name": "status_parsed_idx"},
            {"fields": ["listing_id"], "name": "filtered_listing_id_idx"},
            {"fields": ["posted_at"], "name": "filtered_posted_at_idx"},
        ],
    }

    listing_id = StringField()
    raw_id = StringField()
    source = StringField(default="florida_off_market")
    source_website = StringField()
    address = StringField()
    address_norm = StringField()
    city = StringField()
    state = StringField()
    zip = StringField()
    county = StringField()
    title = StringField()
    url = StringField()
    price = StringField()
    price_usd = FloatField()
    listing = DictField()
    status = StringField(choices=("pending", "posted", "rejected"), default="pending")
    parsed_listing_id = StringField()
    posted_at = DateTimeField()
    reject_reason = StringField()
    created_at = DateTimeField(default=datetime.utcnow)
    updated_at = DateTimeField(default=datetime.utcnow)

    def touch(self):
        self.updated_at = datetime.utcnow()
