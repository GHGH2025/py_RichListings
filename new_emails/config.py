"""Generic seeding; sender-specific defaults come from their own modules."""
from .registry import registry
# Preserve existing imports without duplicating sender configuration.
from .senders.ethan import SENDER_EMAIL as ETHAN_EMAIL, AI_PROMPT as ETHAN_PROMPT
from .senders.michelle import SENDER_EMAIL as MICHELLE_EMAIL, AI_PROMPT as MICHELLE_PROMPT
from .senders.john import SENDER_EMAIL as JOHN_EMAIL, AI_PROMPT as JOHN_PROMPT
from .senders.ivan import SENDER_EMAIL as IVAN_EMAIL, AI_PROMPT as IVAN_PROMPT


def seed_defaults(defaults, handler_registry=registry):
    from .models import NewEmailsList
    for values in defaults:
        values = dict(values)
        sender = values.pop("sender_email")
        NewEmailsList.objects(sender_email=sender).update_one(
            upsert=True, set_on_insert__active=True,
            **{f"set_on_insert__{key}": value for key, value in values.items()})
        doc = NewEmailsList.objects(sender_email=sender).first()
        handler = handler_registry.get(values["handler_key"])
        history = getattr(handler, "prompt_history", {})
        if (doc.handler_key == values["handler_key"] and doc.prompt_version < values["prompt_version"]
                and history.get(doc.prompt_version) == doc.prompt):
            # Only an exact, unedited earlier code default can be upgraded.
            # User-edited prompts and already queued snapshots remain intact.
            NewEmailsList.objects(id=doc.id, prompt=doc.prompt, prompt_version=doc.prompt_version).update_one(
                set__prompt=values["prompt"], set__prompt_version=values["prompt_version"])


def seed_ethan():
    seed_defaults([registry.get("ethan_v1").default_config()])


def seed_templates(handler_registry=registry):
    seed_defaults(handler_registry.defaults(), handler_registry=handler_registry)
