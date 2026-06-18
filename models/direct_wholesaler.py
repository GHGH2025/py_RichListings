from datetime import datetime

from mongoengine import BooleanField, DateTimeField, Document, StringField


class DirectWholesaler(Document):
    meta = {
        "collection": "direct_wholesalers",
        "indexes": [
            {"fields": ["sender_email"], "unique": True, "name": "uniq_sender_email"},
            {"fields": ["email"], "name": "contact_email_idx"},
        ],
    }

    sender_email = StringField(required=True)
    email = StringField(required=True)
    name = StringField(required=True)
    phone = StringField(default="")
    updateFlagForPodio = BooleanField(default=True)
    created_at = DateTimeField(default=datetime.utcnow)
    updated_at = DateTimeField(default=datetime.utcnow)

    def touch(self):
        self.updated_at = datetime.utcnow()
