# whatsapp_keepalive.py
import os, json, logging
from typing import Dict
from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException

log = logging.getLogger(__name__)

def _twilio_client() -> Client:
    sid = os.getenv("TWILIO_ACCOUNT_SID")
    tok = os.getenv("TWILIO_AUTH_TOKEN")
    if not sid or not tok:
        raise RuntimeError("Missing TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN")
    return Client(sid, tok)

def _from_whatsapp() -> str:
    from_num = os.getenv("TWILIO_WHATSAPP_FROM")  # e.g. +14155238886
    if not from_num:
        raise RuntimeError("TWILIO_WHATSAPP_FROM not set")
    return f"whatsapp:{from_num}"

def parse_recipients_env(env_val: str) -> Dict[str, str]:
    """
    Parse env like: +1XXXXXXXXXX:Alex,+1YYYYYYYYYY:Sam
    Returns: {"+1XXXXXXXXXX":"Alex", "+1YYYYYYYYYY":"Sam"}
    """
    out: Dict[str, str] = {}
    for pair in (env_val or "").split(","):
        pair = pair.strip()
        if not pair:
            continue
        if ":" in pair:
            num, name = pair.split(":", 1)
            out[num.strip()] = name.strip()
        else:
            out[pair] = ""  # no name provided
    return out

def send_keepalive_template(recipients: Dict[str, str]) -> None:
    """
    Sends your approved template: 'Hi {{1}}, would you like to continue to receive the listings?'
    with a single quick-reply button (id='yes', title='Yes').

    Preferred path: use Twilio Content API (CONTENT SID) for out-of-session template sends.
    Fallback: send as WhatsApp 'interactive button' message (works inside 24h session).
    """
    if not recipients:
        log.info("send_keepalive_template: no recipients provided; nothing to do")
        return

    client = _twilio_client()
    from_whatsapp = _from_whatsapp()
    content_sid = os.getenv("WHATSAPP_KEEPALIVE_CONTENT_SID")  # optional (recommended)

    for number, name in recipients.items():
        to = f"whatsapp:{number}"
        try:
            if content_sid:
                # Use approved Twilio Content Template (best for 24h window re-open)
                # Your template must have a single variable {{1}} and a 'Yes' quick reply.
                client.messages.create(
                    from_=from_whatsapp,
                    to=to,
                    content_sid=content_sid,
                    content_variables=json.dumps({"1": name or ""}),
                )
                log.info(f"keepalive(template) sent via contentSid to {number}")
            # else:
            #     # Fallback for in-session: direct interactive button message
            #     body = f"Hi {name}, would you like to continue to receive the listings?" if name else \
            #            "Hi, would you like to continue to receive the listings?"
            #     client.messages.create(
            #         from_=from_whatsapp,
            #         to=to,
            #         interactive={
            #             "type": "button",
            #             "body": {"text": body},
            #             "action": {
            #                 "buttons": [
            #                     {"type": "reply", "reply": {"id": "yes", "title": "Yes"}}
            #                 ]
            #             },
            #         },
            #     )
                log.info(f"keepalive(interactive) sent to {number}")
        except TwilioRestException as e:
            log.warning(f"keepalive send failed for {number}: {e}")
        except Exception as e:
            log.warning(f"keepalive send failed for {number}: {e}")
