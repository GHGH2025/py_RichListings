# post_selection.py
import math
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from mongo_engine_conn import init_db
from models import ParsedListing
from dropboxImageUpload import handle_Link


# Allowed regions we will post from
ALLOWED_REGIONS = {
    "south_florida_tri_county",
    "st_lucie",
    "fort_pierce",
    "rest_of_florida",
}

# Reason strings
REASON_BAD_REGION = "unsupported_region_for_posting"
REASON_OVER_CAP   = "rest_of_florida_cap_exceeded_20_percent_policy"

def _get_region(pl: ParsedListing) -> Optional[str]:
    # Prefer extracted blob, fallback to any top-level field if you later add one
    ci = pl.complete_info or {}
    region = ci.get("region_bucket")
    if isinstance(region, str):
        return region.strip()
    return None

def _now():
    return datetime.utcnow()

def _slugify_for_folder(name: str, fallback: str) -> str:
    """
    Create a Dropbox-safe folder slug from an address.
    - strip weird chars
    - remove spaces
    - cap length
    """
    s = (name or "").strip()
    if not s:
        s = fallback
    # keep letters, digits, space, dot, dash, underscore; turn the rest into underscore
    s = re.sub(r"[^A-Za-z0-9 ._-]", "_", s)
    s = s.replace(" ", "")        # compact spaces so path is tidy
    s = s.strip("._-") or fallback
    return s[:80]

def select_passed_listings_for_post(
    limit: Optional[int] = None,
    sort_by: str = "created_at",       # or "price", "updated_at", etc.
    mark_ready_status: Optional[str] = None,  # e.g., "image_processed" or None to leave "passed"
) -> Dict[str, any]:
    """
    Pull 'passed' listings, filter to allowed regions, enforce 20% cap for rest_of_florida,
    skip overflow with reason, and (optionally) advance the kept ones to next status.

    Returns summary dict with IDs of kept and skipped.
    """
    # init_db()

    # Fetch all PASSED that are candidates for posting
    q = ParsedListing.objects(status="passed")

    # Optional sort (oldest first is typical for fairness)
    if sort_by in {"created_at", "updated_at", "price"}:
        q = q.order_by(f"+{sort_by}")
    else:
        q = q.order_by("+created_at")

    if limit:
        q = q.limit(int(limit))

    candidates: List[ParsedListing] = list(q)

    kept_ids: List[str] = []
    skipped_ids: List[str] = []

    # Separate by region, dropping unsupported
    non_rest: List[ParsedListing] = []   # south_florida_tri_county + st_lucie + fort_pierce
    rest: List[ParsedListing] = []       # rest_of_florida
    bad_region: List[ParsedListing] = [] # any other region or missing

    for pl in candidates:
        region = _get_region(pl)
        if region not in ALLOWED_REGIONS:
            bad_region.append(pl)
        else:
            if region == "rest_of_florida":
                rest.append(pl)
            else:
                # south_florida_tri_county / st_lucie / fort_pierce
                non_rest.append(pl)

    now = _now()
    # 2) skip unsupported regions
    for pl in bad_region:
        updates = {
            "set__status": "skipped",
            "set__rules_ai_rule_id": "POST_POLICY_REGION",
            "set__rules_ai_version": "v1",
            "set__rules_ai_reason": REASON_BAD_REGION,
            "set__updated_at": now,
        }
        if not pl.skipped_or_posted_at:
            updates["set__skipped_or_posted_at"] = now
        pl.update(**updates)
        skipped_ids.append(str(pl.id))

    # 3) enforce 20% cap for rest_of_florida relative to NON-REST
    base_count = len(non_rest)
    rest_cap = math.floor(0.20 * base_count)  # allowed count from rest_of_florida
    rest_keep = rest[:rest_cap]
    rest_overflow = rest[rest_cap:]


    for pl in rest_overflow:
        updates = {
            "set__status": "skipped",
            "set__rules_ai_rule_id": "POST_POLICY_20PC",
            "set__rules_ai_version": "v1",
            "set__rules_ai_reason": f"{REASON_OVER_CAP}: allowed={rest_cap}, base_non_rest={base_count}",
            "set__updated_at": now,
        }
        if not pl.skipped_or_posted_at:
            updates["set__skipped_or_posted_at"] = now
        pl.update(**updates)
        skipped_ids.append(str(pl.id))

    # 4) kept = all non_rest + up-to-cap rest → set to ready_to_post
    kept = non_rest + rest_keep
    for pl in kept:

        db_updates = {
            "set__status": "ready_for_image_processing",
            "set__updated_at": now,
        }

        # If we have a source gallery link and we haven't uploaded yet, push to Dropbox
        try:
            src = (pl.other_images_source or "").strip()
            already = (pl.other_images_dropbox_link or "").strip()
            print("src",src)
            print("already",already)
            if src and not already:
                # pick address from top-level field, or from the complete_info blob, or fallback to id
                addr = (pl.address or (pl.complete_info or {}).get("address") or str(pl.id)).strip()
                folder_slug = _slugify_for_folder(addr, fallback=str(pl.id))
                # IMPORTANT: pass only the slug; handle_Link prepends "/PropertyListings/"
                print("folder_slug",folder_slug)
                shared_links = handle_Link([src], folder=folder_slug)  # returns list, usually one folder link
                if shared_links:
                    # store the first link; it’s a shared link to the folder
                    db_updates["set__other_images_dropbox_link"] = shared_links[0]
        except Exception as e:
            print(f"dropbox_upload_error: {e}")
            # Don’t block posting if Dropbox fails; you can optionally log or track this
            # db_updates["set__rules_ai_reason"] = f"dropbox_upload_error: {e}"
            pass

        pl.update(**db_updates)
        kept_ids.append(str(pl.id))
        # # If already ready_to_post, this is a no-op update; if 'passed', we promote it.
        # pl.update(
        #     set__status="ready_to_post",
        #     set__updated_at=now,
        #     # We do NOT set skipped_or_posted_at here; set it when actually posted.
        # )
        # kept_ids.append(str(pl.id))

    return {
        "total_candidates": len(candidates),
        "non_rest_count": len(non_rest),
        "rest_count": len(rest),
        "rest_cap": rest_cap,
        "kept_count": len(kept),
        "kept_ids": kept_ids,
        "skipped_count": len(skipped_ids),
        "skipped_ids": skipped_ids,
    }


# summary = select_passed_listings_for_post(limit=200, sort_by="created_at", mark_ready_status=None)
# print(summary)