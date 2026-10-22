"""Upload explicit sender-collected photos, with per-photo retry checkpoints."""
from datetime import datetime


def publish_galleries(*, gmail_message_id, limit=100, upload=None):
    from models import ParsedListing
    from media.slugify import slugify_for_folder
    from .extraction import validate_public_url
    if upload is None:
        from media.dropbox_upload import handle_Link
        upload = handle_Link
    uploaded = 0
    listings = ParsedListing.objects(gmail_message_id=gmail_message_id,
                                     input_source="new_email",
                                     status__in=["not_processed", "verified", "processed", "passed"]).limit(limit)
    for listing in listings:
        facts = listing.complete_info or {}
        if not facts.get("new_email_gallery_required") or listing.other_images_dropbox_link:
            continue
        photos = list(dict.fromkeys(facts.get("new_email_gallery_images") or []))
        if not photos:
            raise ValueError(f"Required gallery has no images: {listing.id}")
        completed = list(facts.get("new_email_gallery_uploaded") or [])
        folder = slugify_for_folder(listing.address or "", fallback=str(listing.id))
        # Include record identity even when street addresses match across deals.
        folder = f"{folder}-{listing.id}"
        shared_link = facts.get("new_email_gallery_link")
        for photo in photos:
            if photo in completed:
                continue
            validate_public_url(photo)
            links = upload([photo], folder=folder, curate_media=False, listing_id=str(listing.id))
            if not links:
                raise RuntimeError(f"Gallery photo upload failed: {listing.id}")
            shared_link = links[0]
            completed.append(photo)
            listing.update(set__complete_info__new_email_gallery_uploaded=completed,
                           set__complete_info__new_email_gallery_link=shared_link,
                           set__updated_at=datetime.utcnow())
        if not shared_link:
            raise RuntimeError(f"Gallery shared link missing: {listing.id}")
        listing.update(set__other_images_dropbox_link=shared_link, set__updated_at=datetime.utcnow())
        uploaded += 1
    return {"uploaded": uploaded}
