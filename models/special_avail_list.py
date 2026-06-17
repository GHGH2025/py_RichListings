from datetime import datetime

from mongoengine import BooleanField, DateTimeField, Document, IntField, ListField, StringField


class SpecialAvailList(Document):
  """Wholesaler sender emails and Podio item IDs for the special-avail pipeline."""

  meta = {
    "collection": "special_avail_list",
    "indexes": [
      {"fields": ["wholesaler_name"], "unique": True, "name": "uniq_wholesaler_name"},
      {"fields": ["active"], "name": "active_idx"},
    ],
  }

  wholesaler_name = StringField(required=True)
  sender_emails = ListField(StringField(), default=list)
  podio_item_ids = ListField(IntField(), default=list)
  active = BooleanField(default=True)
  created_at = DateTimeField(default=datetime.utcnow)
  updated_at = DateTimeField(default=datetime.utcnow)

  def touch(self):
    self.updated_at = datetime.utcnow()
