from datetime import datetime
from mongoengine import (Document, StringField, BooleanField, IntField,
                         DateTimeField, ListField, DictField)


class NewEmailsList(Document):
    meta = {"collection": "new_emails_list", "indexes": [
        {"fields": ["sender_email"], "unique": True}, "active"]}
    sender_email = StringField(required=True)
    handler_key = StringField(required=True)
    prompt = StringField(required=True)
    prompt_version = IntField(default=1, min_value=1)
    button_labels = ListField(StringField(), required=True)
    detail_button_labels = ListField(StringField(), default=lambda: ["Get More Info", "View More Details", "Click to View More Details"])
    max_depth = IntField(default=3, min_value=1, max_value=10)
    max_pages = IntField(default=100, min_value=1, max_value=500)
    active = BooleanField(default=True)
    created_at = DateTimeField(default=datetime.utcnow)
    updated_at = DateTimeField(default=datetime.utcnow)


class NewEmailJob(Document):
    meta = {"collection": "new_email_jobs", "indexes": [
        {"fields": ["account_label", "message_id"], "unique": True},
        ["status", "next_attempt_at"]]}
    account_label = StringField(required=True, choices=("acct1", "acct2"))
    message_id = StringField(required=True)
    pipeline_message_id = StringField(required=True)
    sender_email = StringField(required=True)
    config_snapshot = DictField(required=True)
    subject = StringField()
    html = StringField()
    status = StringField(default="queued", choices=("queued", "processing", "retry", "completed", "failed"))
    stage = StringField(default="extract")
    attempts = IntField(default=0)
    listing_ids = ListField(StringField())
    stage_results = DictField()
    error = StringField()
    lease_until = DateTimeField()
    next_attempt_at = DateTimeField(default=datetime.utcnow)
    created_at = DateTimeField(default=datetime.utcnow)
    updated_at = DateTimeField(default=datetime.utcnow)


class NewEmailCursor(Document):
    meta = {"collection": "new_email_cursors"}
    account_label = StringField(primary_key=True)
    last_epoch = IntField(required=True)
