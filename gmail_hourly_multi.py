import base64
import json
import os
import re
import sys
import fnmatch
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from email.utils import parseaddr

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from emailExtract import extract_email_body_simple
from forwardInline import forward_inline_html
from mongo_engine_conn import init_db
from models import FilteredListingEmail, ParsedListing, WindowRange, FromInfo, InternalDate, Bodies
# -------------------- CONFIGURE YOUR ACCOUNTS HERE --------------------
ACCOUNTS = [
    # Example for acct1 (keep commented if not used)
    {
        "label": "acct1",
        "base_dir": os.path.join("accounts", "acct1"),
        "allowed_senders": [
            # leave empty to allow everyone, or add patterns:
            "info-jefinancialholdings.com@shared1.ccsend.com ",               # name contains
            "david@theligongroup.com",
            "talya@safetynetinv.com",
            "info@assetsbyalec.com",
            "todd@southfloridawholesalehomes.ccsend.com",
            "tsims-southfloridacashhomebuyers.com@shared1.ccsend.com",
            "kevin-titlerate.com@shared1.ccsend.com",
            "jpaul@7kidsandflipping.com",
            "the3minvestments-gmail.com@shared1.ccsend.com",
            "tricountyflippers-gmail.com@shared1.ccsend.com",
            "jc-quickturnproperties.com@shared1.ccsend.com",
            "erek@marketing.vesta.app",
            "info@zcginvestments.com",
            "john@wholesalejax.com",
            "sales-islandlivingrealty.com@shared1.ccsend.com",
            "re@1motivatedseller.com",
            "carlos@ccjinvestmentsgroup.com",
            "lenny-riverwalkproperties.net@shared1.ccsend.com",
            "info@avainvestmentproperties.com",
            "info-abccapitalgroupusa.com@shared1.ccsend.com",
            "deals@allaboutrealestate.com",
            "@homeventureinvestments.com",
            "manny@homeventureinvestments.com",
            "gene@bankonit.com",
            "oviedomike@oviedomike.robly.com",
            "dispositionsynergy-gmail.com@shared1.ccsend.com",
            "dispositions@maxofferproperties.com",
            "ryan@floridahomeownersolutions.com",
            "sharonr-32westrealty.com@shared1.ccsend.com",
            "sunny9444-hotmail.com@shared1.ccsend.com",
            "sunny9444@hotmail.com",
            "southfloridadispo@joehomebuyer.com",
            "branden@prop-hunters.com",
            "erniethetitleguy-gmail.com@shared1.ccsend.com",
            "deals@aoinvestments.ccsend.com",
            "ffassioli@peakregroup.ccsend.com",
            "jc@stellarholdingsllc.ccsend.com",
            "offmarketdeals@encorehomeoffer.com",
            "npabon34@gmail.com.send.mailchimpapp.com",
            "thesimplehomebuyers@95925329.mailchimpapp.com",
            "michaelzalkind@safetynetinv.com",
            "mitch.conyers@poplarhomebuyers.com",
            "mike.barone@outlook.com",
            "lorena@safetynetrealty.com",
            "may@lpihomebuyers.com",
            "flrpmwholesalefl-gmail.com@shared1.ccsend.com",
            "deals@onemotivatedseller.com",
            "simon@stellarholdingsllc.ccsend.com",
            "info@5thavefinancialgroup.com",
            "info@f13deluxe.com"

    #         # "agent@broker.com",            # exact email
    #         # "*@myfavdomain.com",           # any user at this domain
        ],
        "skip_senders": [
            # optional blocklist patterns (checked after allow)
        ],
        "only_inbox": True,
        "fallback_lookback_min": 60,
        "credentials_filename": "credentials.json",
        "token_filename": "token.json",
        "state_filename": "state.json",
    },
    {
        "label": "acct2",
        "base_dir": os.path.join("accounts", "acct2"),
        "allowed_senders": [
            # examples:
            "richard@oasispropertyinvestments.ccsend.com",
            "oasispropertydispo@gmail.com",
            "alex@lexicorealty.ccsend.com",
            # "iwantacheaphousenow-gmail.com@shared1.ccsend.com",
            "craig@nowhomebuyers.com",
            "ctorres@spectrumpropertygroup.com",
            "ryan@floridahomeownersolutions.com",
            "ffassioli@peakregroup.ccsend.com",
            "sguerrero-housingig.com@shared1.ccsend.com",
            "erniethetitleguy-gmail.com@shared1.ccsend.com",
            "info-rushproperties.net@shared1.ccsend.com",
            "cody@graystoneig.com",
            "deals@aoinvestments.ccsend.com",
            "info+hoodsyhomes.com@ec1.msgsndr.org",
            "lenny-riverwalkproperties.net@shared1.ccsend.com",
            "judson@securehomeinvest.com",
            "simon@stellarholdingsllc.ccsend.com"


            # or: "*@oasispropertyinvestments.ccsend.com",
        ],
        "skip_senders": [
            # e.g., "noreply@*"
        ],
        "only_inbox": True,
        "fallback_lookback_min": 60,
        "credentials_filename": "credentials.json",
        "token_filename": "token.json",
        "state_filename": "state.json",
    },
    #     {
    #     "label": "acct3",
    #     "base_dir": os.path.join("accounts", "acct3"),
    #     "allowed_senders": [
    #         # examples:
    #         # "richard@oasispropertyinvestments.ccsend.com",
    #         "sparsh@concepttocode.in"
    #         # or: "*@oasispropertyinvestments.ccsend.com",
    #     ],
    #     "skip_senders": [
    #         # e.g., "noreply@*"
    #     ],
    #     "only_inbox": True,
    #     "fallback_lookback_min": 60,
    #     "credentials_filename": "credentials.json",
    #     "token_filename": "token.json",
    #     "state_filename": "state.json",
    # },
]
# Gmail read-only scope
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify"
]
# ---------------------------------------------------------------------


@dataclass
class AccountConfig:
    label: str
    base_dir: str
    allowed_senders: List[str]
    skip_senders: List[str]
    only_inbox: bool
    fallback_lookback_min: int
    credentials_path: str
    token_path: str
    state_path: str


def _ensure_paths(acct: dict) -> AccountConfig:
    base_dir = acct["base_dir"]
    os.makedirs(base_dir, exist_ok=True)

    cred_path = acct["credentials_filename"]
    if not os.path.isabs(cred_path):
        cred_path = os.path.join(base_dir, cred_path)

    token_path = acct["token_filename"]
    if not os.path.isabs(token_path):
        token_path = os.path.join(base_dir, token_path)

    state_path = acct["state_filename"]
    if not os.path.isabs(state_path):
        state_path = os.path.join(base_dir, state_path)

    return AccountConfig(
        label=acct["label"],
        base_dir=base_dir,
        allowed_senders=[s.strip() for s in acct.get("allowed_senders", []) if s.strip()],
        skip_senders=[s.strip() for s in acct.get("skip_senders", []) if s.strip()],
        only_inbox=acct["only_inbox"],
        fallback_lookback_min=acct.get("fallback_lookback_min", 60),
        credentials_path=cred_path,
        token_path=token_path,
        state_path=state_path,
    )


def _load_state(path: str) -> Dict:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_state(path: str, state: Dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f)


def _gmail_service(credentials_path: str, token_path: str):
    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(credentials_path):
                raise FileNotFoundError(
                    f"credentials.json not found for this account: {credentials_path}"
                )
            flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
            # For headless servers, you can use flow.run_console()
            creds = flow.run_local_server(port=0)
        with open(token_path, "w", encoding="utf-8") as token:
            token.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def _build_time_window_query(after_epoch: int, before_epoch: int) -> str:
    # Gmail expects epoch seconds here (not ms)
    return f"after:{after_epoch} before:{before_epoch}"


def build_service_by_account() -> Dict[str, any]:
    services = {}
    for raw in ACCOUNTS:
        acct = _ensure_paths(raw)
        services[acct.label] = _gmail_service(acct.credentials_path, acct.token_path)
    return services

def _gmail_search(service, query: str, only_inbox: bool) -> List[str]:
    user_id = "me"
    msg_ids = []
    page_token = None

    while True:
        params = {"userId": user_id, "q": query, "maxResults": 500}
        if page_token:
            params["pageToken"] = page_token
        if only_inbox:
            params["labelIds"] = ["INBOX"]

        resp = service.users().messages().list(**params).execute()
        messages = resp.get("messages", [])
        msg_ids.extend([m["id"] for m in messages])

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return msg_ids


def _get_message(service, msg_id: str) -> dict:
    return service.users().messages().get(userId="me", id=msg_id, format="full").execute()


def _header(headers: List[dict], name: str) -> Optional[str]:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value")
    return None


def _decode_body(payload: dict) -> Tuple[str, str]:
    plain_parts, html_parts = [], []

    def walk(part):
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        data = body.get("data")

        if mime.startswith("multipart/"):
            for p in part.get("parts", []) or []:
                walk(p)
        else:
            if data:
                decoded = base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", errors="replace")
                if mime == "text/plain":
                    plain_parts.append(decoded)
                elif mime == "text/html":
                    html_parts.append(decoded)
                else:
                    if not plain_parts and not html_parts:
                        plain_parts.append(decoded)

    walk(payload)
    return ("\n".join(plain_parts).strip(), "\n".join(html_parts).strip())


def _now_epoch() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _choose_window(state_path: str, fallback_lookback_min: int) -> Tuple[int, int]:
    now_ep = _now_epoch()
    state = _load_state(state_path)
    last_ep = state.get("last_run_epoch")

    if last_ep is None:
        after_ep = int((datetime.now(timezone.utc) - timedelta(minutes=fallback_lookback_min)).timestamp())
    else:
        after_ep = int(last_ep)

    before_ep = now_ep
    # return 1755775802, 1755779402
    return after_ep, before_ep


def _update_state(state_path: str, before_ep: int) -> None:
    _save_state(state_path, {"last_run_epoch": before_ep})


# ---------- Sender filtering helpers (post-fetch) ----------

def _normalize_from(from_header: str) -> Tuple[str, str, str]:
    """
    Returns (full_from_lower, name_lower, email_lower_domain_lower)
    """
    from_lower = (from_header or "").strip().lower()
    name, email_addr = parseaddr(from_header or "")
    name_lower = (name or "").strip().lower()
    email_lower = (email_addr or "").strip().lower()
    domain_lower = email_lower.split("@", 1)[1] if "@" in email_lower else ""
    return from_lower, name_lower, email_lower, domain_lower


def _pattern_hit(value: str, pattern: str) -> bool:
    """
    Match with wildcards. If pattern contains * or ?, use fnmatch.
    Otherwise, check exact equality OR substring (for name/full From).
    """
    p = pattern.strip().lower()
    if not p:
        return False
    if any(ch in p for ch in "*?"):
        return fnmatch.fnmatch(value, p)
    # no wildcards → try exact, then substring
    return value == p or (p in value)


def _sender_allowed(from_header: str, allow: List[str], deny: List[str]) -> bool:
    full, name, email, domain = _normalize_from(from_header)

    # If allow-list is provided, require a hit in ANY of these fields
    if allow:
        allow_ok = any(
            _pattern_hit(email, pat) or
            _pattern_hit(domain, pat.lstrip("@")) or  # allow "@domain.com" style
            _pattern_hit(full, pat) or
            _pattern_hit(name, pat)
            for pat in allow
        )
        if not allow_ok:
            return False

    # If deny-list contains a hit in ANY field, reject
    if deny and any(
        _pattern_hit(email, pat) or
        _pattern_hit(domain, pat.lstrip("@")) or
        _pattern_hit(full, pat) or
        _pattern_hit(name, pat)
        for pat in deny
    ):
        return False

    return True
# -----------------------------------------------------------


def process_account(acct: AccountConfig) -> None:
    print(f"\n========== Processing {acct.label} ==========")
    service = _gmail_service(acct.credentials_path, acct.token_path)

    after_ep, before_ep = _choose_window(acct.state_path, acct.fallback_lookback_min)
    time_query = _build_time_window_query(after_ep, before_ep)
    q = time_query.strip()
    print(f"[{acct.label}] Gmail query: {q}")

    if not q:
        print(f"[{acct.label}] No query built — skipping.")
        return

    msg_ids = _gmail_search(service, q, acct.only_inbox)

    messages: List[dict] = []
    for mid in msg_ids:
        m = _get_message(service, mid)
        messages.append(m)

    # Oldest → newest by Gmail internalDate (ms)
    messages.sort(key=lambda m: int(m.get("internalDate", "0")))
    print(f"[{acct.label}] Window {after_ep} → {before_ep} | fetched: {len(messages)}")

    kept = 0
    for m in messages:
        payload = m.get("payload", {}) or {}
        headers = payload.get("headers", []) or []

        from_h = _header(headers, "From") or ""
        if not _sender_allowed(from_h, acct.allowed_senders, acct.skip_senders):
            # Skip this message based on post-fetch sender rules
            continue

        kept += 1
        subj = _header(headers, "Subject") or "(no subject)"
        date_h = _header(headers, "Date") or ""

        full_norm, name_norm, email_norm, domain_norm = _normalize_from(from_h)

        # Gmail internalDate is in **milliseconds**
        internal_ts_ms = int(m.get("internalDate", "0") or 0)
        internal_dt = datetime.fromtimestamp(internal_ts_ms / 1000, tz=timezone.utc).isoformat()

        # plain, html = _decode_body(payload)

        content = extract_email_body_simple(m)
        plain = content["text"]
        html_full = content["html_full"]
        html_ai = content["html_ai"]

        q = FilteredListingEmail.objects(
            account_label=acct.label,
            gmail_message_id=m.get("id"),
        )

        q.update_one(
            upsert=True,
            set__gmail_thread_id=m.get("threadId"),
            set__subject=subj,
            set__window=WindowRange(after_epoch=after_ep, before_epoch=before_ep),
            set__from_info=FromInfo(
                raw=from_h,
                name=(full_norm or "").strip(),
                email=(email_norm or "").strip().lower(),
            ),
            set__rfc822_date=date_h,
            set__internal_date=InternalDate(ts_ms=internal_ts_ms, iso=internal_dt),
            set__bodies=Bodies(text=plain, html_full=html_full, html_ai=html_ai),
            set_on_insert__status="not_processed",
            set__updated_at=datetime.utcnow(),
            set_on_insert__created_at=datetime.utcnow(),
        )

        # Get the saved _id (for referencing later)
        saved = q.only("id").first()
        saved_id = str(saved.id) if saved else None
        print(f"[{acct.label}] saved filtered email ⇒ _id={saved_id}")

        # step to forward the complete email

        # forward_inline_html(
        #     service,
        #     to_addr="sparsh@concepttocode.in",
        #     original_subject=subj,
        #     original_html=html_full,
        #     preface_text="Use the below email to process for test",
        #         )

        print("-" * 80)
        print(f"[{acct.label}] From: {from_h}")
        print(f"[{acct.label}] Subject: {subj}")
        print(f"[{acct.label}] internalDate: {internal_dt} | RFC822 Date: {date_h}")
        print(f"[{acct.label}] Message ID: {m.get('id')}")
        # preview = plain or re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()
        # print(f"[{acct.label}] Preview:\n{preview[:400]}")
        # Preview for logs
        preview = plain or re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html_full)).strip()
        # print(f"[{acct.label}] Preview:\n{preview[:400]}")
        print("html_ai",html_ai)
        # TODO: listing parser / AI call here
        # listing = extract_listing_info(plain or html)
        # save_listing_to_db(acct.label, listing, gmail_id=m["id"])

    print(f"[{acct.label}] Processed (after sender filter): {kept}")

    # checkpoint after successful processing
    _update_state(acct.state_path, before_ep)


# def main():
#     init_db()  # connect using env; handles TLS flags if set
#     # Optionally ensure indexes now:
#     FilteredListingEmail.ensure_indexes()
#     ParsedListing.ensure_indexes()
#     any_error = False
#     for raw in ACCOUNTS:
#         acct = _ensure_paths(raw)
#         try:
#             process_account(acct)
#         except Exception as e:
#             any_error = True
#             print(f"[ERROR][{acct.label}] {e}", file=sys.stderr)
#     if any_error:
#         sys.exit(1)


# if __name__ == "__main__":
#     main()
