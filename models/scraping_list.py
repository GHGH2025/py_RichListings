from datetime import datetime

from mongoengine import BooleanField, DateTimeField, Document, StringField


class ScrapingList(Document):
  """Gmail sender allow/skip patterns per scraping account."""

  meta = {
    "collection": "scraping_list",
    "indexes": [
      {
        "fields": ["account_label", "sender_pattern", "list_type"],
        "unique": True,
        "name": "uniq_account_pattern_type",
      },
      {"fields": ["account_label", "list_type", "active"], "name": "account_type_active_idx"},
    ],
  }

  account_label = StringField(required=True)
  sender_pattern = StringField(required=True)
  list_type = StringField(choices=("allow", "skip"), default="allow")
  active = BooleanField(default=True)
  created_at = DateTimeField(default=datetime.utcnow)
  updated_at = DateTimeField(default=datetime.utcnow)

  def touch(self):
    self.updated_at = datetime.utcnow()
