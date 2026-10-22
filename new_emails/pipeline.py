"""A durable per-message orchestrator, independent of server_runner's cron stages."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from mongoengine.queryset.visitor import Q
from .models import NewEmailJob
from .workflow import extract_properties


def persist_properties(job, rows):
    from models import Bodies, FilteredListingEmail, FromInfo, ParsedListing, WindowRange
    now = datetime.utcnow()
    mid = job.pipeline_message_id
    source = FilteredListingEmail.objects(account_label=job.account_label, gmail_message_id=mid).modify(
        upsert=True, new=True, set__window=WindowRange(after_epoch=0, before_epoch=1),
        set__subject=job.subject, set__from_info=FromInfo(email=job.sender_email),
        set__bodies=Bodies(html_full=job.html, html_ai=job.html),
        set__status="processed", set__input_source="new_email",
        set__pipeline_stage="processing", set__forward_status="skipped", set__updated_at=now)
    ids = []
    for index, row in enumerate(rows, 1):
        row = dict(row)
        row["beds"] = row.get("bedrooms")
        full, half = row.get("bathrooms_full"), row.get("bathrooms_half")
        row["baths"] = full + 0.5 * (half or 0) if full is not None else None
        row["sqft"] = row.get("living_area_sqft")
        listing = ParsedListing.objects(account_label=job.account_label, gmail_message_id=mid,
                                        list_index=index).modify(
            upsert=True, new=True, set_on_insert__source_email=source,
            set_on_insert__address=row.get("address"), set_on_insert__city=row.get("city"),
            set_on_insert__state=row.get("state"), set_on_insert__zip=row.get("zip"),
            set_on_insert__price=row.get("list_price_usd"), set_on_insert__complete_info=row,
            set_on_insert__images=row.get("images") or [],
            set_on_insert__other_images_source=row.get("other_images_source"),
            set_on_insert__input_source="new_email", set_on_insert__status="not_processed",
            set_on_insert__direct_wholeseller="not_found", set_on_insert__created_at=now,
            set_on_insert__updated_at=now)
        ids.append(str(listing.id))
    return ids


def live_stages():
    # Lazy imports keep preview/CRUD independent of all publishing integrations.
    from core.paths import data_path
    from ai.media_verify import verify_and_fill_missing_media_for_not_processed
    from .dedup import process_not_processed_with_duplicate_rule
    from .rules_runner import apply_ai_english_rules
    from pipeline.price_drop_activate import process_price_drop_activations
    from pipeline.post_selection import select_passed_listings_for_post
    from ai.image_curation import process_listings_ready_for_image_processing, process_primary_image_verification
    from ai.whatsapp_posts import make_whatsapp_posts_from_ready_to_post
    from integrations.wordpress.ai_mapper import ai_build_wp_payload_for_posted
    from integrations.wordpress.ai_property_description import ai_build_wp_property_description_for_posted
    from whatsapp.sender import process_whatsapp_queue
    from .publishing import publish_podio, sync_wordpress
    from .gallery import publish_galleries
    return [
        ("galleries", publish_galleries, {}),
        ("media", verify_and_fill_missing_media_for_not_processed, {}),
        ("dedup", process_not_processed_with_duplicate_rule, {}),
        ("rules", apply_ai_english_rules, {"rules_path": str(data_path("new_email_listing_rules.yaml"))}),
        ("price_drop", process_price_drop_activations, {}),
        ("selection", select_passed_listings_for_post, {}),
        ("images", process_listings_ready_for_image_processing, {}),
        ("primary_image", process_primary_image_verification, {}),
        ("podio_ad_copy", make_whatsapp_posts_from_ready_to_post,
         {"rules_path": str(data_path("ad_post_rules.txt")), "skip_webhook": True}),
        ("podio", publish_podio, {}),
        ("wordpress_keys", ai_build_wp_payload_for_posted, {}),
        ("wordpress_description", ai_build_wp_property_description_for_posted, {}),
        ("wordpress_sync", sync_wordpress, {}),
        ("whatsapp", process_whatsapp_queue, {}),
    ]


def assert_finished(job):
    from models import ParsedListing
    terminal = {"skipped", "skipped_quota", "bypassed", "image_curation_failed", "primary_image_failed", "held_no_gallery", "price_drop_review"}
    unfinished = []
    for listing in ParsedListing.objects(gmail_message_id=job.pipeline_message_id):
        if listing.status in terminal:
            continue
        if (listing.status != "posted" or listing.whatsapp_status != "sent"
                or listing.wp_status not in ("posted", "already_found", "needs_address_review")
                or listing.new_email_podio_sent_at is None):
            unfinished.append(str(listing.id))
    if unfinished:
        raise RuntimeError("Listings require retry: " + ", ".join(unfinished))


def process_one(*, stages_factory=live_stages, extract=extract_properties):
    now = datetime.utcnow()
    # Crash recovery uses the lease, rather than source-email created_at.
    NewEmailJob.objects(status="processing", lease_until__lt=now).update(
        set__status="retry", set__next_attempt_at=now)
    job = NewEmailJob.objects(Q(status__in=["queued", "retry"]) & Q(next_attempt_at__lte=now)).order_by(
        "created_at").modify(new=True, set__status="processing", inc__attempts=1,
                             set__lease_until=now + timedelta(hours=2), set__updated_at=now)
    if not job:
        return None
    try:
        if "extract" not in job.stage_results:
            config = SimpleNamespace(**job.config_snapshot)
            rows = extract(job.html, config)
            ids = persist_properties(job, rows)
            job.update(set__listing_ids=ids, set__stage_results__extract={
                "count": len(ids), "navigation_errors": getattr(rows, "navigation_errors", [])})
            job.reload()
        for name, fn, kwargs in stages_factory():
            # Run status-driven stages again on retries. Already-advanced records
            # are ignored by each adapter; failed publication queues get another attempt.
            job.update(set__stage=name, set__lease_until=datetime.utcnow() + timedelta(hours=2))
            result = fn(gmail_message_id=job.pipeline_message_id, limit=max(1, len(job.listing_ids)), **kwargs)
            job.update(**{f"set__stage_results__{name}": {"result": result},
                          "set__updated_at": datetime.utcnow()})
            if name == "price_drop" and isinstance(result, dict) and result.get("failed", 0):
                raise RuntimeError("Price-drop activation failed; retry required")
        assert_finished(job)
        job.update(set__status="completed", set__stage="done", set__error="", set__lease_until=None)
    except Exception as exc:
        job.update(set__status="failed" if job.attempts >= 5 else "retry",
                   set__error=str(exc)[:2000], set__lease_until=None,
                   set__next_attempt_at=datetime.utcnow() + timedelta(seconds=min(3600, 30 * 2 ** job.attempts)),
                   set__updated_at=datetime.utcnow())
    job.reload()
    return {"id": str(job.id), "status": job.status, "stage": job.stage, "error": job.error}
