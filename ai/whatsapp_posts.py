# ai_make_whatsapp_posts.py
import json
import os
from datetime import datetime
from typing import Dict, Any

from dotenv import load_dotenv
from openai import OpenAI
from db.mongo_engine_conn import init_db
from models import ParsedListing
from pipeline.address_utils import resolve_street_address
from pipeline.property_description import append_full_property_description
import time, random
import requests 
from whatsapp.sender import send_listing_to_whatsapp

load_dotenv()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
client = OpenAI()


POSTED_LISTING_WEBHOOK_URL = os.getenv("POSTED_LISTING_WEBHOOK_URL")
TEAM_NUMBERS = [n.strip() for n in os.getenv("TEAM_WHATSAPP_NUMBERS","").split(",") if n.strip()]


def _iso(dt):
    try:
        return dt.isoformat()
    except Exception:
        return None


def _serialize_listing_full(pl) -> dict:
    """Serialize everything useful currently in DB for the posted listing."""
    street = resolve_street_address(pl)
    return {
        "id": str(pl.id),
        "status": pl.status,
        "account_label": pl.account_label,
        "gmail_message_id": pl.gmail_message_id,
        "list_index": pl.list_index,
        "address": street or pl.address,
        "city": pl.city,
        "state": pl.state,
        "zip": pl.zip,
        "price": pl.price,
        "images": list(pl.images or []),
        "other_images_source": pl.other_images_source,
        "other_images_dropbox_link": getattr(pl, "other_images_dropbox_link", None),
        "post_content": pl.post_content,
        "complete_info": pl.complete_info or {},
        "rules_ai_rule_id": pl.rules_ai_rule_id,
        "rules_ai_version": pl.rules_ai_version,
        "rules_ai_reason": pl.rules_ai_reason,
        "created_at": _iso(getattr(pl, "created_at", None)),
        "updated_at": _iso(getattr(pl, "updated_at", None)),
        "skipped_or_posted_at": _iso(getattr(pl, "skipped_or_posted_at", None)),
    }


def _post_listing_to_webhook(pl_id) -> None:
    """
    Best-effort webhook post. Never raises; short timeout.
    Sends the full, current DB view of the listing after it is marked posted.
    """
    if not POSTED_LISTING_WEBHOOK_URL:
        return  # disabled by config

    try:
        # re-load from DB to ensure we send exactly what's persisted
        fresh = ParsedListing.objects(id=pl_id).first()
        if not fresh:
            return

        payload = {
            "event": "listing_posted",
            "listing": _serialize_listing_full(fresh)
        }

        headers = {"Content-Type": "application/json"}

        r = requests.post(
            POSTED_LISTING_WEBHOOK_URL,
            json=payload,
            headers=headers,
            timeout=5,
        )
        # Don't raise—log-ish only
        if r.status_code >= 400:
            print(f"[webhook] non-2xx: {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"[webhook] failed: {e}")

# System prompt keeps it dead simple and forces WhatsApp formatting
SYSTEM_PROMPT = """You create short wholesale property posts for WHATSAPP.

Follow the human-written RULES exactly, but interpret **BOLD** as WhatsApp bold using *asterisks* (e.g., *text*).
Use ONLY the values from the LISTING object provided. Do NOT invent data.
Never include any item the rules prohibit.
Your output MUST be valid JSON of the form: {"post_content": "<the final WhatsApp message>"} with no extra keys."""

USER_TMPL = """RULES (verbatim text file):
{rules_text}

LISTING (full object we saved; use fields from complete_info first, fallback to top-level):
{listing_json}

TASK:
- Apply the RULES to this LISTING.
- Produce a WhatsApp-friendly post:
  - Bold address and price using *asterisks* (WhatsApp style).
  - Short, sales-friendly lines/bullets.
- Do NOT include any disallowed items from the rules (strip them if present in the source).
- Use US dollar formatting for price (commas, no cents).
- Return ONLY JSON: {{"post_content": "..."}}
"""

def _listing_payload(pl: ParsedListing) -> Dict[str, Any]:
    """Build a plain dict with everything the model might need, simply."""
    street = resolve_street_address(pl)
    ci = dict(pl.complete_info or {})
    # Internal buyer-matching flags — never include in WhatsApp post text
    ci.pop("special_preferences_detected", None)

    d: Dict[str, Any] = {
        "account_label": pl.account_label,
        "gmail_message_id": pl.gmail_message_id,
        "list_index": pl.list_index,
        "status": pl.status,
        "address": street or pl.address,
        "city": pl.city,
        "state": pl.state,
        "zip": pl.zip,
        "price": pl.price,
        "images": list(pl.images or []),
        "other_images_source": pl.other_images_source,
        "other_images_dropbox_link": pl.other_images_dropbox_link,
        "rules_ai_rule_id": pl.rules_ai_rule_id,
        "rules_ai_version": pl.rules_ai_version,
        "rules_ai_reason": pl.rules_ai_reason,
        "complete_info": ci,
    }

    # try to include sender display name as a tone hint (not to be printed)
    try:
        if getattr(pl, "source_email", None) and getattr(pl.source_email, "from_info", None):
            d["sender_name_hint"] = (pl.source_email.from_info.name or "").strip()
    except Exception:
        pass

    return d

def _compose_post(rules_text: str, listing_obj: Dict[str, Any]) -> str:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": USER_TMPL.format(
            rules_text=rules_text,
            listing_json=json.dumps(listing_obj, ensure_ascii=False, indent=2),
        )}
    ]
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=messages,
        temperature=0.2,
        response_format={"type": "json_object"}
    )
    raw = resp.choices[0].message.content
    data = json.loads(raw)
    post = (data.get("post_content") or "").strip()
    if not post:
        raise ValueError("empty post_content")
    return post

def make_whatsapp_posts_from_ready_to_post(rules_path: str, limit: int = 100) -> Dict[str, int]:
    """
    - Read human rules text from file.
    - Pull ready_to_post listings (limit N).
    - For each: create WhatsApp post via AI, save to post_content, mark posted.
    - On error: store reason in rules_ai_reason, leave status unchanged.
    """
    # init_db()

    with open(rules_path, "r", encoding="utf-8") as f:
        rules_text = f.read().strip()

    total = done = failed = 0
    for pl in ParsedListing.objects(status="ready_to_post").limit(limit):
        total += 1
        try:
            listing_obj = _listing_payload(pl)
            post_text = _compose_post(rules_text, listing_obj)
            post_text = append_full_property_description(post_text, pl.complete_info)

            pl.update(
                set__post_content=post_text,
                set__status="posted",
                set__wp_status="ready_to_process",
                set__skipped_or_posted_at=datetime.utcnow(),
                set__updated_at=datetime.utcnow(),
                set__rules_ai_reason=None,
                set__whatsapp_status="pending"
            )

            try:
                from observability.pipeline_metrics import record_listing_stage
                record_listing_stage(
                    str(pl.id),
                    "posted",
                    listing_status="posted",
                    wp_status="ready_to_process",
                    whatsapp_status="pending",
                )
                record_listing_stage(str(pl.id), "podio_webhook")
            except Exception:
                pass

            # NEW: best-effort webhook (does not affect flow)
            _post_listing_to_webhook(pl.id)

            # try:
            #     if TEAM_NUMBERS:
            #         send_listing_to_whatsapp(pl.id, TEAM_NUMBERS)
            # except Exception as we:
            #     print(f"[warn] WhatsApp send failed for {pl.id}: {we}")

            # try:
            #     if TEAM_NUMBERS:
            #         for num in TEAM_NUMBERS:
            #             send_listing_to_whatsapp(pl.id, [num])  # send per recipient
            #             time.sleep(random.uniform(10, 15))        # 2–5s pause
            # except Exception as we:
            #     print(f"[warn] WhatsApp send failed for {pl.id}: {we}")

            done += 1
        except Exception as e:
            pl.update(
                set__rules_ai_reason=f"post_generation_failed: {e}",
                set__updated_at=datetime.utcnow(),
            )
            failed += 1

    return {"total": total, "posted": done, "failed": failed}


# stats = make_whatsapp_posts_from_ready_to_post("ad_post_rules.txt", limit=5)
# print(stats)
