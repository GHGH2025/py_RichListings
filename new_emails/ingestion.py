"""Independent Gmail cursors: every active sender applies to both inboxes."""
import os
from datetime import datetime, timezone
from email.utils import parseaddr
from core.paths import accounts_path
from .models import NewEmailsList, NewEmailJob, NewEmailCursor


def snapshot(config):
    return {"sender_email": config.sender_email, "handler_key": config.handler_key,
            "prompt": config.prompt, "prompt_version": config.prompt_version,
            "button_labels": list(config.button_labels),
            "detail_button_labels": list(config.detail_button_labels),
            "max_depth": config.max_depth, "max_pages": config.max_pages}


def scan_account(label, *, service=None, now_epoch=None):
    from ingestion.gmail import _gmail_service, _gmail_search, _get_message, _header, _decode_body
    if label not in ("acct1", "acct2"):
        raise ValueError("Expected acct1 or acct2")
    configs = {c.sender_email: c for c in NewEmailsList.objects(active=True)}
    if not configs:
        return {"account": label, "queued": 0}
    service = service or _gmail_service(str(accounts_path(label, "credentials.json")),
                                       str(accounts_path(label, "token.json")))
    before = now_epoch if now_epoch is not None else int(datetime.now(timezone.utc).timestamp())
    cursor = NewEmailCursor.objects(account_label=label).first()
    after = cursor.last_epoch - 120 if cursor else before - int(os.getenv("NEW_EMAILS_LOOKBACK_SECONDS", "86400"))
    count = 0
    # Individual sender queries avoid Gmail query-length limits as the list grows.
    for email, config in configs.items():
        for mid in _gmail_search(service, f'{{from:({email}) "{email}"}} after:{after} before:{before}', only_inbox=True):
            if NewEmailJob.objects(account_label=label, message_id=mid).first():
                continue
            message = _get_message(service, mid)
            payload = message.get("payload") or {}
            headers = payload.get("headers") or []
            text, html = _decode_body(payload)
            from .sender_match import matches_sender
            if not matches_sender(_header(headers, "From") or "", text, html, email):
                continue
            sender = email
            if not html:
                raise ValueError(f"Message {label}/{mid} has no HTML body")
            from mongoengine.errors import NotUniqueError
            try:
                NewEmailJob(account_label=label, message_id=mid,
                            pipeline_message_id=f"new_email_{label}_{mid}", sender_email=sender,
                            config_snapshot=snapshot(config), html=html,
                            subject=_header(headers, "Subject") or "").save()
                count += 1
            except NotUniqueError:
                pass
    # Move only after every fetched message has a durable job.
    NewEmailCursor.objects(account_label=label).update_one(upsert=True, max__last_epoch=before)
    return {"account": label, "queued": count}
