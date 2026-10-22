"""Explicit boundary between legacy background queues and the new worker."""
PREFIX = "new_email_"


def scope_queue(queryset, message_id=None):
    if message_id:
        return queryset.filter(gmail_message_id=message_id)
    return queryset.filter(gmail_message_id__not__startswith=PREFIX)


def registered_senders():
    from .models import NewEmailsList
    return [entry.sender_email for entry in NewEmailsList.objects(active=True).only("sender_email")]
