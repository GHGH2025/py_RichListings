"""Manage incremental email templates and inspect their independent job queue."""
from datetime import datetime
from types import SimpleNamespace
import re
from bson import ObjectId
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from mongoengine.errors import NotUniqueError
from new_emails.registry import registry
from new_emails.models import NewEmailsList, NewEmailJob
from new_emails.ingestion import snapshot

router = APIRouter(prefix="/api/new-emails-list", tags=["new-emails-list"])


class EntryPayload(BaseModel):
    sender_email: str
    handler_key: str
    prompt: str = Field(min_length=1, max_length=20000)
    button_labels: list[str] = Field(min_length=1, max_length=20)
    detail_button_labels: list[str] = Field(default_factory=lambda: ["Get More Info", "View More Details", "Click to View More Details"])
    max_depth: int = Field(3, ge=1, le=10)
    max_pages: int = Field(100, ge=1, le=500)
    active: bool = True


class PreviewPayload(BaseModel):
    html: str = Field(min_length=1, max_length=2_000_000)


def entry_response(doc):
    return {"id": str(doc.id), **snapshot(doc), "active": doc.active,
            "created_at": doc.created_at, "updated_at": doc.updated_at,
            "accounts": ["acct1", "acct2"]}


def find(model, doc_id):
    if not ObjectId.is_valid(doc_id):
        raise HTTPException(404, "Record not found")
    doc = model.objects(id=ObjectId(doc_id)).first()
    if doc is None:
        raise HTTPException(404, "Record not found")
    return doc


def validated(payload):
    values = payload.model_dump()
    email = values["sender_email"].strip().lower()
    if not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,}", email):
        raise HTTPException(422, "A concrete sender email is required")
    try:
        registry.get(values["handler_key"])
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    values["sender_email"] = email
    values["prompt"] = values["prompt"].strip()
    values["button_labels"] = list(dict.fromkeys(label.strip() for label in values["button_labels"]))
    if not values["prompt"] or any(not label for label in values["button_labels"]):
        raise HTTPException(422, "Prompt and button labels must not be blank")
    try:
        registry.get(values["handler_key"]).validate_config(values)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return values


@router.get("")
def list_entries():
    return [entry_response(doc) for doc in NewEmailsList.objects.order_by("sender_email")]


@router.get("/handlers")
def list_handlers():
    return {"handlers": registry.keys()}


@router.post("/seed-ethan")
def seed():
    from new_emails.config import seed_ethan, ETHAN_EMAIL
    seed_ethan()
    return entry_response(NewEmailsList.objects(sender_email=ETHAN_EMAIL).get())


@router.post("/seed-templates")
def seed_all():
    from new_emails.config import seed_templates
    seed_templates()
    return list_entries()


@router.post("", status_code=201)
def create_entry(payload: EntryPayload):
    try:
        return entry_response(NewEmailsList(**validated(payload)).save())
    except NotUniqueError:
        raise HTTPException(409, "Sender already exists")


@router.get("/jobs")
def jobs(status: str | None = None, limit: int = Query(50, ge=1, le=200)):
    query = NewEmailJob.objects
    if status:
        query = query.filter(status=status)
    return [{"id": str(j.id), "account_label": j.account_label, "message_id": j.message_id,
             "sender_email": j.sender_email, "prompt_version": j.config_snapshot.get("prompt_version"),
             "status": j.status, "stage": j.stage, "attempts": j.attempts,
             "error": j.error, "listing_ids": j.listing_ids, "stage_results": j.stage_results}
            for j in query.order_by("-created_at").limit(limit)]


@router.post("/jobs/{job_id}/retry")
def retry_job(job_id: str):
    job = find(NewEmailJob, job_id)
    updated = NewEmailJob.objects(id=job.id, status__in=["failed", "retry"]).update_one(
        set__status="retry", set__attempts=0, set__next_attempt_at=datetime.utcnow())
    if not updated:
        raise HTTPException(409, "Only failed or waiting retry jobs can be retried")
    return {"id": job_id, "status": "retry"}


@router.get("/{entry_id}")
def get_entry(entry_id: str):
    return entry_response(find(NewEmailsList, entry_id))


@router.put("/{entry_id}")
def replace_entry(entry_id: str, payload: EntryPayload):
    doc = find(NewEmailsList, entry_id)
    values = validated(payload)
    try:
        # Atomic version increment avoids lost versions from concurrent edits.
        doc = NewEmailsList.objects(id=doc.id).modify(new=True, inc__prompt_version=1,
                    set__updated_at=datetime.utcnow(), **{f"set__{key}": value for key, value in values.items()})
    except NotUniqueError:
        raise HTTPException(409, "Sender already exists")
    return entry_response(doc)


@router.delete("/{entry_id}")
def delete_entry(entry_id: str):
    find(NewEmailsList, entry_id).delete()
    return {"deleted_id": entry_id}


@router.post("/{entry_id}/preview")
def preview(entry_id: str, payload: PreviewPayload):
    from new_emails.pipeline import extract_properties
    config = find(NewEmailsList, entry_id)
    try:
        listings = extract_properties(payload.html, SimpleNamespace(**snapshot(config)))
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return {"dry_run": True, "prompt_version": config.prompt_version, "listings": listings,
            "navigation_errors": listings.navigation_errors}
