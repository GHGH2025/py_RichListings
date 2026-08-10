"""
Isolated dry-run email → publish pipeline.

Seeds one FilteredListingEmail from caller-provided HTML, runs the same
qualification stages as live (scoped to that message id), then builds the
WhatsApp / WordPress / Podio payloads without sending anything outbound.
"""
from __future__ import annotations

import logging
import traceback
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from models import (
    Bodies,
    FilteredListingEmail,
    FromInfo,
    InternalDate,
    ParsedListing,
    WindowRange,
)
from pipeline.process_email import _sender_slice_for
from pipeline.listing_details import upsert_parsed_listings_from_html
from ai.media_verify import verify_and_fill_missing_media_for_not_processed
from pipeline.dedup import process_not_processed_with_duplicate_rule
from ai.rules_runner import apply_ai_english_rules
from pipeline.post_selection import select_passed_listings_for_post
from ai.image_curation import (
    process_listings_ready_for_image_processing,
    process_primary_image_verification,
)
from ai.whatsapp_posts import make_whatsapp_posts_from_ready_to_post
from integrations.wordpress.ai_mapper import ai_build_wp_payload_for_posted
from integrations.wordpress.ai_property_description import (
    ai_build_wp_property_description_for_posted,
)
from integrations.wordpress.sync_poster import _build_post_body
from config.runtime import get_whatsapp_send_mode, get_group_jids_for_account
from core.paths import data_path
from whatsapp.sender import TEAM_NUMBERS, _first_image_url

logger = logging.getLogger(__name__)

TEST_MSG_PREFIX = "test_"


def _now_ms() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


def _stage(name: str, fn, stages: List[Dict[str, Any]]) -> Any:
    entry: Dict[str, Any] = {"stage": name, "ok": True}
    try:
        result = fn()
        entry["result"] = result
        stages.append(entry)
        return result
    except Exception as e:
        entry["ok"] = False
        entry["error"] = str(e)
        entry["traceback"] = traceback.format_exc()[-2000:]
        stages.append(entry)
        logger.exception("test_pipeline stage failed: %s", name)
        return None


def _seed_filtered_email(
    *,
    html: str,
    account_label: str,
    from_email: str,
    from_name: str,
    subject: str,
    text: str,
) -> FilteredListingEmail:
    msg_id = f"{TEST_MSG_PREFIX}{uuid.uuid4().hex}"
    now = datetime.utcnow()
    ts_ms = _now_ms()
    epoch = int(ts_ms / 1000)

    fe = FilteredListingEmail(
        account_label=account_label,
        gmail_message_id=msg_id,
        gmail_thread_id=msg_id,
        subject=subject or "[test-pipeline] seeded email",
        window=WindowRange(after_epoch=epoch - 60, before_epoch=epoch + 60),
        from_info=FromInfo(
            raw=f"{from_name} <{from_email}>" if from_name else from_email,
            name=from_name or "",
            email=(from_email or "").strip().lower(),
        ),
        rfc822_date=now.strftime("%a, %d %b %Y %H:%M:%S +0000"),
        internal_date=InternalDate(ts_ms=ts_ms, iso=now.isoformat() + "Z"),
        bodies=Bodies(
            text=text or "",
            html_full=html,
            html_ai=html,
        ),
        status="processing",  # locked away from live process_pending
        dev=True,
        created_at=now,
        updated_at=now,
    )
    fe.save()
    return fe


def _parse_seeded_email(fe: FilteredListingEmail) -> Dict[str, Any]:
    html = (fe.bodies.html_ai or fe.bodies.html_full or "") if fe.bodies else ""
    if not html.strip():
        FilteredListingEmail.objects(id=fe.id).update_one(
            set__status="error",
            set__updated_at=datetime.utcnow(),
        )
        return {"ok": False, "error": "empty_html", "ids": []}

    slice_range = _sender_slice_for(fe.account_label, getattr(fe.from_info, "email", None))
    res = upsert_parsed_listings_from_html(
        email_html=html,
        account_label=fe.account_label,
        gmail_message_id=fe.gmail_message_id,
        source_email_doc=fe,
        list_slice=slice_range,
    )
    FilteredListingEmail.objects(id=fe.id).update_one(
        set__status="processed",
        set__updated_at=datetime.utcnow(),
    )
    return {"ok": True, **(res or {})}


def _listings_for(msg_id: str) -> List[ParsedListing]:
    return list(ParsedListing.objects(gmail_message_id=msg_id).order_by("+list_index"))


def _dry_run_whatsapp(pl: ParsedListing) -> Dict[str, Any]:
    mode = get_whatsapp_send_mode()
    text = (getattr(pl, "post_content", "") or "").strip()
    img = _first_image_url(getattr(pl, "images", []) or [])
    would_send = bool(text)

    if mode == "group":
        targets = get_group_jids_for_account(getattr(pl, "account_label", None))
        payload = {"jids": targets, "text": text}
    else:
        targets = list(TEAM_NUMBERS or [])
        payload = {"to": targets, "text": text}
    if img:
        payload["imageUrl"] = img

    # Mark as sent so nothing else tries to deliver it.
    if would_send:
        ParsedListing.objects(id=pl.id).update_one(set__whatsapp_status="sent")

    return {
        "would_send": would_send,
        "sent": would_send,  # dry-run success = payload ready
        "mode": mode,
        "targets": targets,
        "post_content": text,
        "image_url": img,
        "payload": payload,
        "reason": None if would_send else "empty_post_content",
    }


def _dry_run_wordpress(pl: ParsedListing) -> Dict[str, Any]:
    wp_status = getattr(pl, "wp_status", None)
    desc = (getattr(pl, "wp_property_description", None) or "").strip()
    if wp_status not in ("des_generated", "keys_generated", "ready_to_process") and not desc:
        return {
            "would_send": False,
            "sent": False,
            "wp_status": wp_status,
            "reason": f"wp_status={wp_status}; no description",
            "payload": None,
        }

    # Build the same create body live would POST (strip token from response).
    try:
        body = _build_post_body(pl)
    except Exception as e:
        return {
            "would_send": False,
            "sent": False,
            "wp_status": wp_status,
            "reason": f"build_failed: {e}",
            "payload": None,
        }

    safe_body = {k: v for k, v in body.items() if k != "token"}
    would_send = bool(desc) and bool(safe_body.get("address") or safe_body.get("posttitle"))

    if would_send:
        # Park away from live sync_wp_for_descriptions queue.
        ParsedListing.objects(id=pl.id).update_one(set__wp_status="posted")

    return {
        "would_send": would_send,
        "sent": would_send,
        "wp_status": "posted" if would_send else wp_status,
        "payload": safe_body,
        "description": desc,
        "reason": None if would_send else "incomplete_wp_payload",
    }


def _dry_run_podio(pl: ParsedListing) -> Dict[str, Any]:
    """
    Read-only Podio probe: search property + wholeseller, never write.
    """
    flag = getattr(pl, "direct_wholeseller", None)
    complete = pl.complete_info or {}
    agent_email = (complete.get("agent_email") or "").strip().lower()
    update_flag = str(complete.get("updateFlagForPodio", "")).strip().lower() == "true"

    base = {
        "direct_wholeseller": flag,
        "agent_email": agent_email or None,
        "updateFlagForPodio": update_flag,
        "would_link": False,
        "linked": False,
        "property_item_id": None,
        "wholeseller_item_id": None,
        "reason": None,
    }

    if flag in (None, "", "bypassed"):
        base["reason"] = "not_in_direct_wholeseller_flow"
        return base
    if flag == "no_agent_email" or not agent_email:
        base["reason"] = "no_agent_email"
        return base
    if flag not in ("not_processed", "processed", "property_not_found", "wholeseller_not_found", "not_found"):
        base["reason"] = f"status={flag}"
        return base

    try:
        from integrations.podio.direct_wholesaler import (
            get_podio_access_token,
            search_properties_app_for_listing,
            find_wholeseller_item_by_email,
            _get_item,
            _get_wholeseller_reference_item_id,
            _get_wholeseller_email_from_item,
        )

        token = get_podio_access_token()
        property_item_id = search_properties_app_for_listing(token, pl)
        if not property_item_id:
            base["reason"] = "property_not_found"
            # Keep live podio batch away from this test listing.
            ParsedListing.objects(id=pl.id).update_one(
                set__direct_wholeseller="property_not_found",
                set__FoundInPodioViaSearch="not_found",
            )
            base["direct_wholeseller"] = "property_not_found"
            return base

        base["property_item_id"] = property_item_id
        property_item = _get_item(token, property_item_id)
        existing_wh_id = _get_wholeseller_reference_item_id(property_item) if property_item else None

        if existing_wh_id:
            wh_item = _get_item(token, existing_wh_id)
            current_email = _get_wholeseller_email_from_item(wh_item) if wh_item else None
            if current_email and current_email.lower() == agent_email:
                base["would_link"] = True
                base["linked"] = True
                base["wholeseller_item_id"] = existing_wh_id
                base["reason"] = "already_correctly_linked"
                ParsedListing.objects(id=pl.id).update_one(set__direct_wholeseller="processed")
                base["direct_wholeseller"] = "processed"
                return base

        target_wh_id = find_wholeseller_item_by_email(token, agent_email)
        if not target_wh_id:
            base["reason"] = "wholeseller_not_found"
            ParsedListing.objects(id=pl.id).update_one(
                set__direct_wholeseller="wholeseller_not_found"
            )
            base["direct_wholeseller"] = "wholeseller_not_found"
            return base

        base["wholeseller_item_id"] = target_wh_id
        base["would_link"] = True
        base["linked"] = True  # dry-run: would have linked (no Podio write)
        base["reason"] = (
            "would_set_wholeseller_reference"
            if update_flag
            else "would_skip_write_updateFlagForPodio_false"
        )
        # Park away from live batch without writing to Podio.
        ParsedListing.objects(id=pl.id).update_one(set__direct_wholeseller="processed")
        base["direct_wholeseller"] = "processed"
        return base
    except Exception as e:
        base["reason"] = f"podio_probe_failed: {e}"
        logger.exception("podio dry-run failed for %s", pl.id)
        return base


def _listing_report(pl: ParsedListing) -> Dict[str, Any]:
    return {
        "id": str(pl.id),
        "list_index": pl.list_index,
        "address": pl.address,
        "city": pl.city,
        "state": pl.state,
        "zip": pl.zip,
        "price": pl.price,
        "status": pl.status,
        "rules_ai_rule_id": getattr(pl, "rules_ai_rule_id", None),
        "rules_ai_reason": getattr(pl, "rules_ai_reason", None),
        "whatsapp_status": getattr(pl, "whatsapp_status", None),
        "wp_status": getattr(pl, "wp_status", None),
        "direct_wholeseller": getattr(pl, "direct_wholeseller", None),
        "post_content": getattr(pl, "post_content", None),
        "images": list(getattr(pl, "images", None) or [])[:5],
    }


def run_test_email_pipeline(
    *,
    html: str,
    account_label: str = "acct1",
    from_email: str = "test-sender@example.com",
    from_name: str = "Test Sender",
    subject: str = "[test-pipeline] seeded email",
    text: str = "",
    cleanup: bool = False,
) -> Dict[str, Any]:
    """
    Full dry-run: ingest HTML → qualify → build publish payloads (no outbound sends).
    """
    if not (html or "").strip():
        return {"ok": False, "error": "html is required"}

    stages: List[Dict[str, Any]] = []
    fe = _seed_filtered_email(
        html=html,
        account_label=account_label,
        from_email=from_email,
        from_name=from_name,
        subject=subject,
        text=text,
    )
    msg_id = fe.gmail_message_id
    email_id = str(fe.id)

    parse_res = _stage("parse", lambda: _parse_seeded_email(fe), stages)
    if not parse_res or not parse_res.get("ok"):
        return {
            "ok": False,
            "dry_run": True,
            "email_id": email_id,
            "gmail_message_id": msg_id,
            "stages": stages,
            "error": (parse_res or {}).get("error") or "parse_failed",
        }

    msg_kw = {"gmail_message_id": msg_id}

    _stage(
        "media_verify",
        lambda: verify_and_fill_missing_media_for_not_processed(
            limit=50, max_workers=4, **msg_kw
        ),
        stages,
    )

    _stage(
        "dedup_30d",
        lambda: process_not_processed_with_duplicate_rule(limit=50, **msg_kw),
        stages,
    )
    _stage(
        "ai_rules",
        lambda: apply_ai_english_rules(
            str(data_path("ai_listing_rules.yaml")), limit=50, **msg_kw
        ),
        stages,
    )
    _stage(
        "post_selection",
        lambda: select_passed_listings_for_post(
            limit=50,
            sort_by="created_at",
            mark_ready_status=None,
            skip_webhooks=True,
            **msg_kw,
        ),
        stages,
    )
    _stage(
        "image_curation",
        lambda: process_listings_ready_for_image_processing(limit=50, **msg_kw),
        stages,
    )
    _stage(
        "primary_image",
        lambda: process_primary_image_verification(limit=50, **msg_kw),
        stages,
    )
    _stage(
        "whatsapp_ad_copy",
        lambda: make_whatsapp_posts_from_ready_to_post(
            str(data_path("ad_post_rules.txt")),
            limit=50,
            skip_webhook=True,
            **msg_kw,
        ),
        stages,
    )
    _stage(
        "wordpress_keys",
        lambda: ai_build_wp_payload_for_posted(limit=50, batch_size=10, **msg_kw),
        stages,
    )
    _stage(
        "wordpress_description",
        lambda: ai_build_wp_property_description_for_posted(
            limit=50, batch_size=10, per_item_sleep_s=0.0, **msg_kw
        ),
        stages,
    )

    listings = _listings_for(msg_id)
    listing_results: List[Dict[str, Any]] = []
    for pl in listings:
        # Refresh after WP AI stages
        pl.reload()
        wa = _dry_run_whatsapp(pl)
        pl.reload()
        wp = _dry_run_wordpress(pl)
        pl.reload()
        podio = _dry_run_podio(pl)
        pl.reload()
        listing_results.append(
            {
                "listing": _listing_report(pl),
                "whatsapp": wa,
                "wordpress": wp,
                "podio": podio,
            }
        )

    summary = {
        "listings_total": len(listing_results),
        "whatsapp_would_send": sum(1 for r in listing_results if r["whatsapp"].get("would_send")),
        "wordpress_would_send": sum(1 for r in listing_results if r["wordpress"].get("would_send")),
        "podio_would_link": sum(1 for r in listing_results if r["podio"].get("would_link")),
        "final_statuses": [r["listing"]["status"] for r in listing_results],
    }

    if cleanup:
        ParsedListing.objects(gmail_message_id=msg_id).delete()
        FilteredListingEmail.objects(id=fe.id).delete()

    return {
        "ok": True,
        "dry_run": True,
        "email_id": email_id,
        "gmail_message_id": msg_id,
        "account_label": account_label,
        "from_email": from_email,
        "subject": subject,
        "cleanup": cleanup,
        "stages": stages,
        "summary": summary,
        "listings": listing_results,
    }
