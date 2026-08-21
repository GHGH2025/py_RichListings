from datetime import datetime

from mongoengine import (
    BooleanField,
    DateTimeField,
    DictField,
    Document,
    DynamicField,
    IntField,
    StringField,
)


class WpProxyRequestLog(Document):
    """Audit log for POST /public/wp/create proxy requests."""

    meta = {
        "collection": "wp_proxy_request_logs",
        "indexes": [
            {"fields": ["-created_at"], "name": "created_at_desc_idx"},
            {"fields": ["posttitle", "-created_at"], "name": "posttitle_time_idx"},
            {"fields": ["post_status", "-created_at"], "name": "post_status_time_idx"},
        ],
    }

    created_at = DateTimeField(default=datetime.utcnow)
    client_ip = StringField(default="")
    # Parsed body params as JSON (token redacted).
    request_body = DictField(default=dict)
    posttitle = StringField(default="")
    post_status = StringField(default="")
    wp_ok = BooleanField()
    wp_status_code = IntField()
    wp_response = DynamicField()
    error = StringField(default="")
