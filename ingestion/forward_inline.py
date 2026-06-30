import base64, re
from email.header import decode_header, make_header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

def _decode_mime_header(val: str) -> str:
    if not val:
        return ""
    try:
        return str(make_header(decode_header(val)))
    except Exception:
        return val

def _send_raw_message(service, raw_bytes: bytes):
    raw = base64.urlsafe_b64encode(raw_bytes).decode("utf-8")
    return service.users().messages().send(userId="me", body={"raw": raw}).execute()

def forward_inline_html(service, to_addr: str, original_subject: str, original_html: str,
                        preface_text: str = "Use the below email to process for test"):
    """
    Sends a new email: your note (plain + HTML) + original HTML content below it.
    """
    msg = MIMEMultipart("alternative")
    msg["To"] = to_addr
    subj = _decode_mime_header(original_subject or "")
    msg["Subject"] = f"Fwd: {subj}" if subj else "Fwd:"

    # plain-text fallback from the HTML
    plain_original = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", original_html or "")).strip()
    plain_body = f"{preface_text}\n\n--- Original Email (text) ---\n{plain_original}"
    msg.attach(MIMEText(plain_body, "plain", "utf-8"))

    html_body = f"""<div>
        <p>{preface_text}</p>
        <hr>
        {original_html or "<p>(no HTML found)</p>"}
    </div>"""
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    resp = _send_raw_message(service, msg.as_bytes())
    # resp is a Gmail Message resource (usually includes "id")
    return (resp or {}).get("id", "")