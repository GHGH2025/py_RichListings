from datetime import datetime

from mongoengine import DateTimeField, DictField, Document, ListField, StringField


class WhatsappTrackedMessage(Document):
    """Inbound WhatsApp group messages tracked for listing ingestion."""

    meta = {
        "collection": "whatsapp_tracked_messages",
        # Mongoose writes __v; ignore unknown fields so ingest can load docs.
        "strict": False,
        "indexes": [
            {
                "fields": ["group_jid", "message_id"],
                "unique": True,
                "name": "uniq_group_message",
            },
            {"fields": ["status"], "name": "status_idx"},
            {"fields": ["timestamp"], "name": "timestamp_idx"},
        ],
    }

    group_jid = StringField(required=True)
    group_name = StringField(default="")
    sender_phone = StringField(required=True)
    sender_email = StringField(default="")
    sender_jid = StringField(default="")
    message_id = StringField(required=True)
    type = StringField(default="text")
    text = StringField(default="")
    media_urls = ListField(StringField(), default=list)
    timestamp = DateTimeField(default=datetime.utcnow)
    raw = DictField(default=None)
    status = StringField(
        choices=("pending", "processing", "processed", "error"),
        default="pending",
    )
    errorMessage = StringField(default="")
