from datetime import datetime

from mongoengine import (
    BooleanField,
    DateTimeField,
    DictField,
    Document,
    IntField,
    ListField,
    StringField,
)


class SpecialAvailInactiveTracker(Document):
    """
    Per-property consecutive-miss tracker for Active Podio listings
    that stop appearing in the wholesaler's daily avail email.
    """

    meta = {
        "collection": "special_avail_inactive_trackers",
        "indexes": [
            {"fields": ["podio_item_id"], "unique": True, "name": "uniq_podio_item_id"},
            {"fields": ["wholesaler_name", "status"], "name": "wholesaler_status_idx"},
            {"fields": ["status", "-updated_at"], "name": "status_updated_idx"},
            {"fields": ["-miss_streak"], "name": "miss_streak_desc"},
        ],
    }

    podio_item_id = IntField(required=True)
    address = StringField(required=True)
    address_normalized = StringField()
    wholesaler_name = StringField(required=True)

    miss_streak = IntField(default=0)
    last_checked_date = StringField()  # YYYY-MM-DD (America/New_York)
    last_seen_in_email = StringField()  # YYYY-MM-DD when last found

    status = StringField(
        choices=("watching", "fired", "resolved"),
        default="watching",
    )
    webhook_fired_at = DateTimeField()
    webhook_ok = BooleanField()
    last_result = StringField()  # found | missed | skipped_no_email

    created_at = DateTimeField(default=datetime.utcnow)
    updated_at = DateTimeField(default=datetime.utcnow)

    def touch(self):
        self.updated_at = datetime.utcnow()


class SpecialAvailInactiveJobRun(Document):
    """Persisted result of the daily special-avail inactive / non-active cron."""

    meta = {
        "collection": "special_avail_inactive_job_runs",
        "indexes": [
            {"fields": ["-run_at"], "name": "run_at_desc"},
            {"fields": ["target_date", "-run_at"], "name": "target_date_run_desc"},
        ],
    }

    run_at = DateTimeField(required=True, default=datetime.utcnow)
    target_date = StringField(required=True)  # EST calendar day checked
    ok = BooleanField(default=False)
    skipped = BooleanField(default=False)
    reason = StringField()

    lookback_days = IntField(default=7)
    miss_threshold = IntField(default=3)

    wholesalers_checked = IntField(default=0)
    properties_checked = IntField(default=0)
    found_count = IntField(default=0)
    missed_count = IntField(default=0)
    skipped_no_email = IntField(default=0)
    webhooks_fired = IntField(default=0)
    webhook_failures = IntField(default=0)

    fired_addresses = ListField(StringField(), default=list)
    wholesaler_summaries = ListField(DictField(), default=list)
    errors = ListField(StringField(), default=list)

    created_at = DateTimeField(default=datetime.utcnow)
