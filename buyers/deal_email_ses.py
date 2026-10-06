"""buyers/deal_email_ses.py - #40 (06.10.2026, Blagojche option A): send buyer deal emails through Amazon SES
instead of Rich's Google mailbox (server 02 /rich_ai_deal_Email -> Gmail SMTP, ~2,000/day per mailbox).

Switch: DEAL_EMAIL_VIA=ses (anything else = the old route, untouched). Credentials: SES_DEAL_ACCESS_KEY_ID /
SES_DEAL_SECRET_ACCESS_KEY / SES_DEAL_REGION (send-only use). From: DEAL_EMAIL_FROM (default the verified
app.wholesaledealfinder.ai address until wholesaledealfinder.ai itself finishes DKIM verification), display name
DEAL_EMAIL_FROM_NAME, replies to DEAL_EMAIL_REPLY_TO (Rich's mailbox, as before). List-Unsubscribe = mailto to
the same mailbox (there is no self-service opt-out for matched buyers yet).

Returns the SAME dict shape as send_email_to_buyer so the caller, the cap and the send log need no change."""
import os
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid
from typing import Optional

DEAL_EMAIL_VIA = os.getenv("DEAL_EMAIL_VIA", "gmail").strip().lower()
DEAL_EMAIL_FROM = os.getenv("DEAL_EMAIL_FROM", "rich@app.wholesaledealfinder.ai").strip()
DEAL_EMAIL_FROM_NAME = os.getenv("DEAL_EMAIL_FROM_NAME", "Rich").strip()
DEAL_EMAIL_REPLY_TO = os.getenv("DEAL_EMAIL_REPLY_TO", "rich@wholesaledealfinder.ai").strip()
DEAL_EMAIL_UNSUB_MAILTO = os.getenv("DEAL_EMAIL_UNSUB_MAILTO", DEAL_EMAIL_REPLY_TO).strip()
DEAL_EMAIL_SES_CONFIG_SET = os.getenv("DEAL_EMAIL_SES_CONFIG_SET", "").strip()

_client = None


def ses_enabled() -> bool:
    return DEAL_EMAIL_VIA == "ses"


def _ses():
    global _client
    if _client is None:
        import boto3
        _client = boto3.client(
            "ses",
            region_name=os.getenv("SES_DEAL_REGION", "us-east-1"),
            aws_access_key_id=os.environ["SES_DEAL_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["SES_DEAL_SECRET_ACCESS_KEY"],
        )
    return _client


def _html_to_text(html: str) -> str:
    import re
    t = re.sub(r"<br\s*/?>|</p>|</div>|</li>", "\n", html or "", flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)
    t = re.sub(r"[ \t]+\n", "\n", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def build_message(to_email: str, subject: str, html_body: str) -> MIMEMultipart:
    msg = MIMEMultipart("alternative")
    msg["From"] = formataddr((DEAL_EMAIL_FROM_NAME, DEAL_EMAIL_FROM))
    msg["To"] = to_email
    msg["Subject"] = subject
    msg["Reply-To"] = DEAL_EMAIL_REPLY_TO
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain=DEAL_EMAIL_FROM.split("@")[-1])
    msg["List-Unsubscribe"] = f"<mailto:{DEAL_EMAIL_UNSUB_MAILTO}?subject=unsubscribe>"
    msg.attach(MIMEText(_html_to_text(html_body), "plain", "utf-8"))
    msg.attach(MIMEText(html_body or "", "html", "utf-8"))
    return msg


def send_via_ses(to_email: str, subject: str, html_body: str) -> dict:
    """Same result shape as matched_process.send_email_to_buyer."""
    to_email = (to_email or "").strip()
    subject = (subject or "").strip()
    base = {"ok": False, "status_code": None, "response_text": None, "response_json": None,
            "error": None, "invalid_email": False}
    if not to_email:
        return dict(base, error="missing_to_email")
    if not subject:
        return dict(base, error="missing_subject")
    try:
        msg = build_message(to_email, subject, html_body or "")
        kwargs = {"Source": DEAL_EMAIL_FROM, "Destinations": [to_email],
                  "RawMessage": {"Data": msg.as_bytes()},
                  "Tags": [{"Name": "campaign", "Value": "deal"}]}
        if DEAL_EMAIL_SES_CONFIG_SET:
            kwargs["ConfigurationSetName"] = DEAL_EMAIL_SES_CONFIG_SET
        resp = _ses().send_raw_email(**kwargs)
        mid = resp.get("MessageId")
        code = int((resp.get("ResponseMetadata") or {}).get("HTTPStatusCode") or 200)
        return dict(base, ok=True, status_code=code, response_text=f"ses MessageId={mid}",
                    response_json={"MessageId": mid, "provider": "ses", "from": DEAL_EMAIL_FROM})
    except Exception as e:  # botocore ClientError and anything else: never raise into the send loop
        err = str(e)
        code = None
        try:
            code = int(e.response["ResponseMetadata"]["HTTPStatusCode"])  # type: ignore[attr-defined]
        except Exception:
            pass
        invalid = "InvalidParameterValue" in err and "address" in err.lower()
        logging.warning("send_via_ses failed for %s: %s", to_email, err[:300])
        return dict(base, status_code=code, response_text=err[:500], error=err[:200], invalid_email=invalid)
