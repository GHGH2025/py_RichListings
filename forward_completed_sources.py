# # forward_completed_sources.py

# import os
# import json
# from datetime import datetime
# from typing import Dict, List, Optional
# from mongoengine.queryset.visitor import Q

# from models import FilteredListingEmail, ParsedListing
# from forwardInline import forward_inline_html  # wherever you put your function

# # If your model is STRICT, add these fields:
# # class FilteredListingEmail(Document):
# #     ...
# #     forward_status       = StringField(choices=["forwarded","skipped"], null=True)  # unset initially
# #     forwarded_at         = DateTimeField()
# #     forward_to           = StringField()
# #     forward_preface_text = StringField()
# #     forward_error        = StringField()

# ALLOWED_FINALS = {"posted", "skipped"}

# # ------------ Direct Wholeseller config ------------
# DIRECT_WHOLESELLER_PATH = os.path.join(
#     os.path.dirname(__file__),
#     "direct_wholeseller.json"
# )

# # email (lowercased) -> config dict
# DIRECT_WHOLESELLER_MAP: Dict[str, dict] = {}

# try:
#     with open(DIRECT_WHOLESELLER_PATH, "r", encoding="utf-8") as f:
#         raw = json.load(f) or {}
#         if isinstance(raw, dict):
#             # normalize keys to lowercase emails
#             DIRECT_WHOLESELLER_MAP = {
#                 str(k).strip().lower(): (v or {})
#                 for k, v in raw.items()
#             }
#         else:
#             print("Warning: direct_wholeseller.json root is not an object; ignoring.")
# except FileNotFoundError:
#     print("Info: direct_wholeseller.json not found; Ai Direct Wholesaler Finder labeling disabled.")
# except Exception as e:
#     print(f"Warning: could not load direct_wholeseller.json: {e}")


# def _get_sender_email(fe: FilteredListingEmail) -> Optional[str]:
#     """
#     Safely extract the sender email from fe.from_info.email (if present).
#     Returns lowercase email or None.
#     """
#     try:
#         from_info = getattr(fe, "from_info", None)
#         if not from_info:
#             return None
#         email = getattr(from_info, "email", None)
#         if not email:
#             return None
#         email = str(email).strip()
#         return email.lower() or None
#     except Exception:
#         return None


# def _is_direct_wholeseller_sender(from_email: Optional[str]) -> bool:
#     """
#     Check if this sender is configured as Direct Wholeseller with updateFlagForPodio == 'true'.
#     """
#     if not from_email:
#         return False
#     cfg = DIRECT_WHOLESELLER_MAP.get(from_email.strip().lower())
#     if not cfg or not isinstance(cfg, dict):
#         return False
#     flag = str(cfg.get("updateFlagForPodio", "")).strip().lower()
#     # JSON spec: 'true' is a string
#     return flag == "true"


# def _fmt_addr(pl: ParsedListing) -> str:
#     # Build a single-line address; fall back to complete_info if needed
#     addr = (pl.address or "").strip()
#     city = (pl.city or "").strip()
#     state = (pl.state or "").strip()
#     zipc = (pl.zip or "").strip()
#     pieces = [p for p in [addr, ", ".join([x for x in [city, state] if x]), zipc] if p]
#     out = " ".join(pieces).strip()
#     if not out:
#         try:
#             ci = pl.complete_info or {}
#             cand = " ".join([
#                 (ci.get("address") or "").strip(),
#                 (ci.get("city") or "").strip(),
#                 (ci.get("state") or "").strip(),
#                 (ci.get("zip") or "").strip(),
#             ]).strip()
#             if cand:
#                 out = cand
#         except Exception:
#             pass
#     return out or "(no address)"

# def _get_or_create_label(service, label_name: str) -> Optional[str]:
#     """
#     Return the labelId for `label_name`. Create it if it doesn't exist.
#     """
#     try:
#         resp = service.users().labels().list(userId="me").execute()
#         for lab in resp.get("labels", []):
#             if lab.get("name") == label_name:
#                 return lab.get("id")

#         # Not found → create it
#         created = service.users().labels().create(
#             userId="me",
#             body={
#                 "name": label_name,
#                 "labelListVisibility": "labelShow",
#                 "messageListVisibility": "show"
#             }
#         ).execute()
#         return created.get("id")
#     except Exception as e:
#         print(f"Warning: could not get/create label '{label_name}': {e}")
#         return None

# def _label_message(service, gmail_message_id: str, label_ids: List[str]) -> None:
#     """
#     Add one or more labels to a message.
#     """
#     if not gmail_message_id or not label_ids:
#         return
#     service.users().messages().modify(
#         userId="me",
#         id=gmail_message_id,
#         body={"addLabelIds": label_ids, "removeLabelIds": []},
#     ).execute()


# def _star_and_label_original(
#     service,
#     gmail_message_id: str,
#     ai_label_id: Optional[str],
#     keep_in_inbox: bool = True,
#     extra_label_ids: Optional[List[str]] = None,  # NEW
# ) -> None:
#     """
#     Adds STARRED, optional INBOX, AI_Agent (or any main label),
#     and optional extra labels to the original Gmail message.
#     """
#     if not gmail_message_id:
#         return
#     add_labels = ["STARRED"]
#     if keep_in_inbox:
#         add_labels.append("INBOX")
#     if ai_label_id:
#         add_labels.append(ai_label_id)
#     if extra_label_ids:
#         for lid in extra_label_ids:
#             if lid and lid not in add_labels:
#                 add_labels.append(lid)

#     service.users().messages().modify(
#         userId="me",
#         id=gmail_message_id,
#         body={"addLabelIds": add_labels, "removeLabelIds": []},
#     ).execute()


# def _star_original_message(service, gmail_message_id: str, keep_in_inbox: bool = True) -> None:
#     """
#     Adds STARRED (and optionally INBOX) to the original Gmail message.
#     """
#     if not gmail_message_id:
#         return
#     add_labels = ["STARRED"]
#     if keep_in_inbox:
#         add_labels.append("INBOX")
#     service.users().messages().modify(
#         userId="me",
#         id=gmail_message_id,
#         body={"addLabelIds": add_labels, "removeLabelIds": []},
#     ).execute()


# def forward_completed_source_emails(
#     service_by_account: Dict[str, any],   # {"acct1": gmail_service, ...}
#     to_addr: str,
#     limit: int = 100,
# ) -> Dict[str, int]:
#     """
#     Scan FilteredListingEmail with no forward_status set.
#     For each email:
#       - If all related ParsedListing are final (posted|skipped):
#           - If >=1 posted: forward & mark forwarded
#           - Else: mark skipped (no forward)
#       - Else (some still processing): leave as-is
#     Returns simple stats.
#     """
#     scanned = forwarded = skipped = pending = 0

#     # "Not forwarded yet" — either forward_status missing or None/""
#     fe_q = FilteredListingEmail.objects(
#         (
#             Q(forward_status=None) |
#             Q(forward_status__exists=False) |
#             Q(forward_status="")
#         ) & Q(status="processed")
#     ).order_by("+created_at").limit(limit)

#     for fe in fe_q:
#         scanned += 1
#         # Choose the account's Gmail service
#         service = service_by_account.get(fe.account_label)

#         # Determine if this email's sender is a Direct Wholeseller (from JSON config)
#         sender_email = _get_sender_email(fe)
#         is_direct_wholeseller = _is_direct_wholeseller_sender(sender_email)

#         direct_wholeseller_label_id: Optional[str] = None
#         if is_direct_wholeseller and service:
#             direct_wholeseller_label_id = _get_or_create_label(service, "AI Direct Wholesaler Finder")

#         # Gather all parsed listings for this email
#         listings: List[ParsedListing] = list(ParsedListing.objects(source_email=fe))
#         if not listings:
#             # Nothing to do; mark 'skipped' so we don't keep re-checking forever
#             fe.update(
#                 set__forward_status="skipped",
#                 set__forward_error="no_parsed_listings_found",
#                 set__updated_at=datetime.utcnow(),
#             )
#             skipped += 1
#             continue

#         statuses = { (pl.status or "").strip().lower() for pl in listings }
#         if not statuses.issubset(ALLOWED_FINALS):
#             # Some still in-flight (passed/ready_to_post/processing/etc) → wait
#             pending += 1
#             continue

#         # Now all final → see if any were posted
#         posted = [pl for pl in listings if (pl.status or "").lower() == "posted"]
#         if not posted:
#             fe.update(
#                 set__forward_status="skipped",
#                 set__forward_error="no_posted_listings",
#                 set__updated_at=datetime.utcnow(),
#             )


#             # flag true

       
#             try:
#                 no_deals_label_id = _get_or_create_label(service, "Ai No Deals Found")

#                 extra_labels: List[str] = []
#                 if direct_wholeseller_label_id:
#                     extra_labels.append(direct_wholeseller_label_id)

#                 _star_and_label_original(
#                     service,
#                     getattr(fe, "gmail_message_id", None),
#                     no_deals_label_id,
#                     keep_in_inbox=True,  # keeps existing INBOX; just appends new ones
#                     extra_label_ids=extra_labels or None,
#                 )

#             except Exception as lab_err:
#                 print(f"Warning: could not label original {getattr(fe, 'gmail_message_id', None)}: {lab_err}")
#             skipped += 1
#             continue

#         # Build preface text from posted addresses
#         lines = [" >> "]
#         for pl in posted:
#             addr_line = _fmt_addr(pl)
#             lines.append(f"- {addr_line}")
#         preface_text = "\n".join(lines)

#         if not service:
#             fe.update(
#                 set__forward_status="skipped",
#                 set__forward_error=f"no_gmail_service_for_account:{fe.account_label}",
#                 set__updated_at=datetime.utcnow(),
#             )


#             skipped += 1
#             continue

#         # Ensure AI_Agent label exists (or create)
#         ai_label_id = _get_or_create_label(service, "AI_Agent")

#         # here add label here for direct wholeseller


#         # Original subject + HTML body (prefer full HTML)
#         subj = getattr(fe, "subject", "") or ""
#         html = ""
#         try:
#             bodies = getattr(fe, "bodies", None)
#             if bodies:
#                 html = (getattr(bodies, "html_full", None) or
#                         getattr(bodies, "html_ai", None) or
#                         "")
#         except Exception:
#             pass
#         if not html:
#             html = "<p>(no HTML found)</p>"

#         # Try to forward
#         try:
#             sent_id = forward_inline_html(
#                 service=service,
#                 to_addr=to_addr,
#                 original_subject=subj,
#                 original_html=html,
#                 preface_text=preface_text
#             )
            

#             try:
#                 extra_labels: List[str] = []
#                 if direct_wholeseller_label_id:
#                     extra_labels.append(direct_wholeseller_label_id)

#                 _star_and_label_original(
#                     service,
#                     getattr(fe, "gmail_message_id", None),
#                     ai_label_id,
#                     keep_in_inbox=True,
#                     extra_label_ids=extra_labels or None,
#                 )
#             except Exception as star_err:
#                 # Non-fatal: log it but don't fail the forward flow
#                 print(f"Warning: could not star original message {getattr(fe, 'gmail_message_id', None)}: {star_err}")



#             # Label the SENT/forwarded message with AI_Agent and optionally Direct Wholeseller
#             try:
#                 if sent_id:
#                     labels_for_sent: List[str] = []
#                     if ai_label_id:
#                         labels_for_sent.append(ai_label_id)
#                     if direct_wholeseller_label_id:
#                         labels_for_sent.append(direct_wholeseller_label_id)

#                     if labels_for_sent:
#                         _label_message(service, sent_id, labels_for_sent)
#             except Exception as lab_err:
#                 print(f"Warning: could not label sent message {sent_id}: {lab_err}")

#             fe.update(
#                 set__forward_status="forwarded",
#                 set__forwarded_at=datetime.utcnow(),
#                 set__forward_to=to_addr,
#                 set__forward_preface_text=preface_text,
#                 set__updated_at=datetime.utcnow(),
#             )
#             forwarded += 1
#         except Exception as e:
#             # Don’t block the pipeline; mark this source email as skipped with an error
#             fe.update(
#                 set__forward_status="skipped",
#                 set__forward_error=f"forward_failed: {e}",
#                 set__updated_at=datetime.utcnow(),
#             )
#             skipped += 1

#     return {
#         "scanned": scanned,
#         "forwarded": forwarded,
#         "skipped": skipped,
#         "pending": pending,  # awaiting final listing statuses
#     }




import os
import json
from datetime import datetime
from typing import Dict, List, Optional
from mongoengine.queryset.visitor import Q

from models import FilteredListingEmail, ParsedListing
from forwardInline import forward_inline_html  # wherever you put your function

ALLOWED_FINALS = {"posted", "skipped", "image_curation_failed", "primary_image_failed"}

# ------------ Direct Wholeseller config ------------
DIRECT_WHOLESELLER_PATH = os.path.join(
    os.path.dirname(__file__),
    "direct_wholeseller.json"
)

# email (lowercased) -> config dict
DIRECT_WHOLESELLER_MAP: Dict[str, dict] = {}

try:
    with open(DIRECT_WHOLESELLER_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f) or {}
        if isinstance(raw, dict):
            # normalize keys to lowercase emails
            DIRECT_WHOLESELLER_MAP = {
                str(k).strip().lower(): (v or {})
                for k, v in raw.items()
            }
        else:
            print("Warning: direct_wholeseller.json root is not an object; ignoring.")
except FileNotFoundError:
    print("Info: direct_wholeseller.json not found; Ai Direct Wholesaler Finder labeling disabled.")
except Exception as e:
    print(f"Warning: could not load direct_wholeseller.json: {e}")


def _get_sender_email(fe: FilteredListingEmail) -> Optional[str]:
    """
    Safely extract the sender email from fe.from_info.email (if present).
    Returns lowercase email or None.
    """
    try:
        from_info = getattr(fe, "from_info", None)
        if not from_info:
            return None
        email = getattr(from_info, "email", None)
        if not email:
            return None
        email = str(email).strip()
        return email.lower() or None
    except Exception:
        return None


def _is_direct_wholeseller_sender(from_email: Optional[str]) -> bool:
    """
    Check if this sender is configured as Direct Wholeseller with updateFlagForPodio == 'true'.
    """
    if not from_email:
        return False
    cfg = DIRECT_WHOLESELLER_MAP.get(from_email.strip().lower())
    if not cfg or not isinstance(cfg, dict):
        return False
    flag = str(cfg.get("updateFlagForPodio", "")).strip().lower()
    # JSON spec: 'true' is a string
    return flag == "true"


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


def _star_and_label_original(
    service,
    gmail_message_id: str,
    ai_label_id: Optional[str],
    keep_in_inbox: bool = True,
    extra_label_ids: Optional[List[str]] = None,
) -> None:
    """
    Adds STARRED, optional INBOX, AI_Agent (or any main label),
    and optional extra labels to the original Gmail message.
    """
    if not gmail_message_id:
        return
    add_labels = ["STARRED"]
    if keep_in_inbox:
        add_labels.append("INBOX")
    if ai_label_id:
        add_labels.append(ai_label_id)
    if extra_label_ids:
        for lid in extra_label_ids:
            if lid and lid not in add_labels:
                add_labels.append(lid)

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
          - If >=1 posted or any skipped with special flags: forward & mark forwarded
          - Else: mark skipped (no forward)
      - Else (some still processing): leave as-is
    Returns simple stats.
    """
    scanned = forwarded = skipped = pending = 0

    # "Not forwarded yet" — either forward_status missing or None/"" + status processed
    fe_q = FilteredListingEmail.objects(
        (
            Q(forward_status=None) |
            Q(forward_status__exists=False) |
            Q(forward_status="")
        ) & Q(status="processed")
    ).order_by("+created_at").limit(limit)
    print("fe_q========",fe_q)
    for fe in fe_q:

        scanned += 1
        # Choose the account's Gmail service
        service = service_by_account.get(fe.account_label)

        # Determine if this email's sender is a Direct Wholeseller (from JSON config)
        sender_email = _get_sender_email(fe)
        is_direct_wholeseller = _is_direct_wholeseller_sender(sender_email)

        direct_wholeseller_label_id: Optional[str] = None
        if is_direct_wholeseller and service:
            direct_wholeseller_label_id = _get_or_create_label(service, "AI Direct Wholesaler Finder")

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

        # All listing statuses must be final (posted or skipped)
        statuses = {(pl.status or "").strip().lower() for pl in listings}
        if not statuses.issubset(ALLOWED_FINALS):
            # Some still in-flight (passed/ready_to_post/processing/etc) → wait
            pending += 1
            continue

        # Partition listings by status and flags
        posted = [pl for pl in listings if (pl.status or "").strip().lower() == "posted"]

        skipped_over_35 = [
            pl for pl in listings
            if (pl.status or "").strip().lower() == "skipped"
            and (getattr(pl, "over_35_percent", None) or "").strip().lower() == "found"
        ]

        skipped_do_not_post_city = [
            pl for pl in listings
            if (pl.status or "").strip().lower() == "skipped"
            and (getattr(pl, "do_not_post_city", None) or "").strip().lower() == "found"
        ]

        has_over_35 = bool(skipped_over_35)
        has_do_not_post_city = bool(skipped_do_not_post_city)

        # If there are no posted AND no special skipped listings → preserve old "no_posted_listings" behaviour
        if not posted and not has_over_35 and not has_do_not_post_city:
            fe.update(
                set__forward_status="skipped",
                set__forward_error="no_posted_listings",
                set__updated_at=datetime.utcnow(),
            )

            try:
                no_deals_label_id = _get_or_create_label(service, "Ai No Deals Found")

                extra_labels: List[str] = []
                if direct_wholeseller_label_id:
                    extra_labels.append(direct_wholeseller_label_id)

                _star_and_label_original(
                    service,
                    getattr(fe, "gmail_message_id", None),
                    no_deals_label_id,
                    keep_in_inbox=True,  # keeps existing INBOX; just appends new ones
                    extra_label_ids=extra_labels or None,
                )

            except Exception as lab_err:
                print(f"Warning: could not label original {getattr(fe, 'gmail_message_id', None)}: {lab_err}")
            skipped += 1
            continue

        # # Build preface text:
        # # - posted block (as before)
        # # - skipped due to Over 35% block
        # # - skipped due to Do Not Post City block
        # lines: List[str] = []

        # if posted:
        #     lines.append(" >> ")
        #     for pl in posted:
        #         addr_line = _fmt_addr(pl)
        #         lines.append(f"- {addr_line}")

        # if skipped_over_35:
        #     lines.append(">>(Skipped due to Non-Rest Quota Limit)")
        #     for pl in skipped_over_35:
        #         addr_line = _fmt_addr(pl)
        #         lines.append(f"- {addr_line}")

        # if skipped_do_not_post_city:
        #     lines.append(">>(Skipped due to Do Not Post City rule)")
        #     for pl in skipped_do_not_post_city:
        #         addr_line = _fmt_addr(pl)
        #         lines.append(f"- {addr_line}")

        # preface_text = "\n".join(lines) if lines else ""


        # Build preface text as HTML-friendly sections:
        # - posted block
        # - skipped due to Over 35% block
        # - skipped due to Do Not Post City block
        sections: List[str] = []

        if posted:
            block_lines: List[str] = []
            # Header for posted listings
            block_lines.append(">>")
            for pl in posted:
                addr_line = _fmt_addr(pl)
                block_lines.append(f"- {addr_line}")
            # Join this block with <br> so it renders as separate lines
            sections.append("<br>".join(block_lines))

        if skipped_over_35:
            block_lines = []
            # Header for Non-Rest quota skipped listings
            block_lines.append(">>(Skipped due to Non-Rest Quota Limit)")
            for pl in skipped_over_35:
                addr_line = _fmt_addr(pl)
                block_lines.append(f"- {addr_line}")
            sections.append("<br>".join(block_lines))

        if skipped_do_not_post_city:
            block_lines = []
            # Header for Do Not Post City skipped listings
            block_lines.append(">>(Skipped due to Do Not Post City rule)")
            for pl in skipped_do_not_post_city:
                addr_line = _fmt_addr(pl)
                block_lines.append(f"- {addr_line}")
            sections.append("<br>".join(block_lines))

        # Blank line (double <br>) between sections
        preface_text = "<br><br>".join(sections) if sections else ""



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

        # New labels for policy-based skips
        over_35_label_id: Optional[str] = None
        do_not_post_city_label_id: Optional[str] = None

        if has_over_35:
            over_35_label_id = _get_or_create_label(service, "Over 35%")
        if has_do_not_post_city:
            do_not_post_city_label_id = _get_or_create_label(service, "Do Not Post City")

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

            # Star + label original message (AI_Agent + optional direct wholeseller + new policy labels)
            try:
                extra_labels: List[str] = []
                if direct_wholeseller_label_id:
                    extra_labels.append(direct_wholeseller_label_id)
                if over_35_label_id:
                    extra_labels.append(over_35_label_id)
                if do_not_post_city_label_id:
                    extra_labels.append(do_not_post_city_label_id)

                _star_and_label_original(
                    service,
                    getattr(fe, "gmail_message_id", None),
                    ai_label_id,
                    keep_in_inbox=True,
                    extra_label_ids=extra_labels or None,
                )
            except Exception as star_err:
                # Non-fatal: log it but don't fail the forward flow
                print(f"Warning: could not star original message {getattr(fe, 'gmail_message_id', None)}: {star_err}")

            # Label the SENT/forwarded message with AI_Agent, Direct Wholeseller, and any policy labels
            try:
                if sent_id:
                    labels_for_sent: List[str] = []
                    if ai_label_id:
                        labels_for_sent.append(ai_label_id)
                    if direct_wholeseller_label_id:
                        labels_for_sent.append(direct_wholeseller_label_id)
                    if over_35_label_id:
                        labels_for_sent.append(over_35_label_id)
                    if do_not_post_city_label_id:
                        labels_for_sent.append(do_not_post_city_label_id)

                    if labels_for_sent:
                        _label_message(service, sent_id, labels_for_sent)
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
