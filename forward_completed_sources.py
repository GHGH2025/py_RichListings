# forward_completed_sources.py
from datetime import datetime
from typing import Dict, List, Optional
from mongoengine.queryset.visitor import Q

from models import FilteredListingEmail, ParsedListing
from forwardInline import forward_inline_html  # wherever you put your function

# If your model is STRICT, add these fields:
# class FilteredListingEmail(Document):
#     ...
#     forward_status       = StringField(choices=["forwarded","skipped"], null=True)  # unset initially
#     forwarded_at         = DateTimeField()
#     forward_to           = StringField()
#     forward_preface_text = StringField()
#     forward_error        = StringField()

ALLOWED_FINALS = {"posted", "skipped"}

def _fmt_addr(pl: ParsedListing) -> str:
    # Build a single-line address; fall back to complete_info if needed
    addr = (pl.address or "").strip()
    city = (pl.city or "").strip()
    state = (pl.state or "").strip()
    zipc = (pl.zip or "").strip()
    pieces = [p for p in [addr, ", ".join([x for x in [city, state] if x]), zipc] if p]
    out = " ".join(pieces).strip()
    if not out:
        try:
            ci = pl.complete_info or {}
            cand = " ".join([
                (ci.get("address") or "").strip(),
                (ci.get("city") or "").strip(),
                (ci.get("state") or "").strip(),
                (ci.get("zip") or "").strip(),
            ]).strip()
            if cand:
                out = cand
        except Exception:
            pass
    return out or "(no address)"

def _get_or_create_label(service, label_name: str) -> Optional[str]:
    """
    Return the labelId for `label_name`. Create it if it doesn't exist.
    """
    try:
        resp = service.users().labels().list(userId="me").execute()
        for lab in resp.get("labels", []):
            if lab.get("name") == label_name:
                return lab.get("id")

        # Not found → create it
        created = service.users().labels().create(
            userId="me",
            body={
                "name": label_name,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show"
            }
        ).execute()
        return created.get("id")
    except Exception as e:
        print(f"Warning: could not get/create label '{label_name}': {e}")
        return None

def _label_message(service, gmail_message_id: str, label_ids: List[str]) -> None:
    """
    Add one or more labels to a message.
    """
    if not gmail_message_id or not label_ids:
        return
    service.users().messages().modify(
        userId="me",
        id=gmail_message_id,
        body={"addLabelIds": label_ids, "removeLabelIds": []},
    ).execute()


def _star_and_label_original(service, gmail_message_id: str, ai_label_id: Optional[str], keep_in_inbox: bool = True) -> None:
    """
    Adds STARRED, optional INBOX, and AI_Agent to the original Gmail message.
    """
    if not gmail_message_id:
        return
    add_labels = ["STARRED"]
    if keep_in_inbox:
        add_labels.append("INBOX")
    if ai_label_id:
        add_labels.append(ai_label_id)
    service.users().messages().modify(
        userId="me",
        id=gmail_message_id,
        body={"addLabelIds": add_labels, "removeLabelIds": []},
    ).execute()


def _star_original_message(service, gmail_message_id: str, keep_in_inbox: bool = True) -> None:
    """
    Adds STARRED (and optionally INBOX) to the original Gmail message.
    """
    if not gmail_message_id:
        return
    add_labels = ["STARRED"]
    if keep_in_inbox:
        add_labels.append("INBOX")
    service.users().messages().modify(
        userId="me",
        id=gmail_message_id,
        body={"addLabelIds": add_labels, "removeLabelIds": []},
    ).execute()


def forward_completed_source_emails(
    service_by_account: Dict[str, any],   # {"acct1": gmail_service, ...}
    to_addr: str,
    limit: int = 100,
) -> Dict[str, int]:
    """
    Scan FilteredListingEmail with no forward_status set.
    For each email:
      - If all related ParsedListing are final (posted|skipped):
          - If >=1 posted: forward & mark forwarded
          - Else: mark skipped (no forward)
      - Else (some still processing): leave as-is
    Returns simple stats.
    """
    scanned = forwarded = skipped = pending = 0

    # "Not forwarded yet" — either forward_status missing or None/""
    fe_q = FilteredListingEmail.objects(
        (
            Q(forward_status=None) |
            Q(forward_status__exists=False) |
            Q(forward_status="")
        ) & Q(status="processed")
    ).order_by("+created_at").limit(limit)

    for fe in fe_q:
        scanned += 1

        # Gather all parsed listings for this email
        listings: List[ParsedListing] = list(ParsedListing.objects(source_email=fe))
        if not listings:
            # Nothing to do; mark 'skipped' so we don't keep re-checking forever
            fe.update(
                set__forward_status="skipped",
                set__forward_error="no_parsed_listings_found",
                set__updated_at=datetime.utcnow(),
            )
            skipped += 1
            continue

        statuses = { (pl.status or "").strip().lower() for pl in listings }
        if not statuses.issubset(ALLOWED_FINALS):
            # Some still in-flight (passed/ready_to_post/processing/etc) → wait
            pending += 1
            continue

        # Now all final → see if any were posted
        posted = [pl for pl in listings if (pl.status or "").lower() == "posted"]
        if not posted:
            fe.update(
                set__forward_status="skipped",
                set__forward_error="no_posted_listings",
                set__updated_at=datetime.utcnow(),
            )
            skipped += 1
            continue

        # Build preface text from posted addresses
        lines = [" >> "]
        for pl in posted:
            addr_line = _fmt_addr(pl)
            lines.append(f"- {addr_line}")
        preface_text = "\n".join(lines)

        # Choose the account's Gmail service
        service = service_by_account.get(fe.account_label)
        if not service:
            fe.update(
                set__forward_status="skipped",
                set__forward_error=f"no_gmail_service_for_account:{fe.account_label}",
                set__updated_at=datetime.utcnow(),
            )
            skipped += 1
            continue

        # Ensure AI_Agent label exists (or create)
        ai_label_id = _get_or_create_label(service, "AI_Agent")

        # Original subject + HTML body (prefer full HTML)
        subj = getattr(fe, "subject", "") or ""
        html = ""
        try:
            bodies = getattr(fe, "bodies", None)
            if bodies:
                html = (getattr(bodies, "html_full", None) or
                        getattr(bodies, "html_ai", None) or
                        "")
        except Exception:
            pass
        if not html:
            html = "<p>(no HTML found)</p>"

        # Try to forward
        try:
            sent_id = forward_inline_html(
                service=service,
                to_addr=to_addr,
                original_subject=subj,
                original_html=html,
                preface_text=preface_text
            )
            
            # ⭐ Star the original message and keep it in Inbox
            try:
                # _star_original_message(service, getattr(fe, "gmail_message_id", None), keep_in_inbox=True)
                _star_and_label_original(
                    service,
                    getattr(fe, "gmail_message_id", None),
                    ai_label_id,
                    keep_in_inbox=True
                )
            except Exception as star_err:
                # Non-fatal: log it but don't fail the forward flow
                print(f"Warning: could not star original message {getattr(fe, 'gmail_message_id', None)}: {star_err}")

            # Label the SENT/forwarded message with AI_Agent (if we got its id)
            try:
                if ai_label_id and sent_id:
                    _label_message(service, sent_id, [ai_label_id])
            except Exception as lab_err:
                print(f"Warning: could not label sent message {sent_id}: {lab_err}")

            fe.update(
                set__forward_status="forwarded",
                set__forwarded_at=datetime.utcnow(),
                set__forward_to=to_addr,
                set__forward_preface_text=preface_text,
                set__updated_at=datetime.utcnow(),
            )
            forwarded += 1
        except Exception as e:
            # Don’t block the pipeline; mark this source email as skipped with an error
            fe.update(
                set__forward_status="skipped",
                set__forward_error=f"forward_failed: {e}",
                set__updated_at=datetime.utcnow(),
            )
            skipped += 1

    return {
        "scanned": scanned,
        "forwarded": forwarded,
        "skipped": skipped,
        "pending": pending,  # awaiting final listing statuses
    }
