# image_curation.py
import json
from datetime import datetime
from typing import Dict, List, Any, Optional

from dotenv import load_dotenv
from openai import OpenAI
import os
from db.mongo_engine_conn import init_db
from models import ParsedListing
from media.check_direct_link import blocked_image_filename_reason

load_dotenv()
OPENAI_MODEL_VISION = os.getenv("OPENAI_VISION_MODEL", "gpt-5.4-mini")
client = OpenAI()

MIDDLEWARE_STATUS_PRIMARY = "ready_for_primary_image_check"
PRIMARY_FAIL_STATUS = "primary_image_failed"
PRIMARY_PASS_STATUS = "ready_to_post"


def _model_supports_temperature(model: Optional[str]) -> bool:
    """gpt-5 mini variants reject temperature; omit it for those models."""
    if not model:
        return True
    return model not in {"gpt-5-mini", "gpt-5.4-mini"}

# CURATOR_SYSTEM_PROMPT = """You are an expert real-estate photo curator.
# Given a set of image URLs for one property listing, return ONLY JSON describing:
# - Which images are genuine property photos (house/condo/townhome interior/exterior), in BEST viewing order.
# - Which images should be skipped (not real property photos or low value), with a brief reason.

# What to SKIP:
# - Company logos, QR codes, headshots/people/selfies, agent cards, signatures.
# - Screenshots of text, watermarked ads/flyers, memes, heavy text tiles, price/terms graphics.
# - Maps, floor plans, appraisal docs, spreadsheets, closing statements.
# - Duplicates or near-duplicates (keep the clearest one).

# Ordering for KEPT images (best-first):
# 1) Clear exterior FRONT/ELEVATION (daytime, unobstructed)
# 2) High-value interiors: kitchen, living/dining, primary bed, baths
# 3) Other useful rooms/spaces
# 4) Backyard/patio/garage/driveway
# 5) Street/lot/context if helpful

# Rules:
# - Prefer higher clarity, less obstruction, good lighting.
# - If multiple similar shots, keep only the best one.
# - If no property images, keep none.
# - Return JSON ONLY in the schema requested—no extra text.
# """

# CURATOR_SYSTEM_PROMPT =  """
# You are an expert real-estate photo curator.

# You receive a set of image URLs for a single property listing. The property can be:
# - A built structure (house / condo / townhome / commercial, etc.), or
# - Land-only / plots / farms / lots, including aerial/drone photos of the parcel.

# Your job: return ONLY JSON describing:
# - Which images are genuine photos of this property's land or built structures (interior/exterior, ground-level, or aerial/drone), in BEST viewing order.
# - Which images should be skipped (not real property photos, not showing this property, or low value), with a brief reason.

# Treat as valid "property photos":
# - Exterior or interior photos of buildings on the property.
# - Ground-level photos of the land / parcel / lot (fields, plots, vacant land).
# - Aerial / drone / satellite-style images that clearly show the actual property/parcel (not just a generic area map).
# - Driveways, garages, carports, and **dedicated parking areas/parking bays/covered parking clearly associated with this property or its building**, even if the main building is not fully visible.


# What to SKIP (must NOT appear in "kept_ordered"):
# - Company logos, QR codes, headshots/people/selfies, agent cards, signatures.
# - Screenshots of text, watermarked ads/flyers, memes, heavy text tiles, price/terms graphics.
# - **Any image that is primarily a marketing tile, flyer, banner, or logo, EVEN IF
#   there is a property photo in the background. If the logo/text/branding covers a
#   large part of the image or is the main focus, SKIP it and mark the reason as
#   "logo/text tile".**   

# Ordering for KEPT images (best-first):
# 1) Clear main view of the property:
#    - For built properties: front exterior / main elevation (daytime, unobstructed).
#    - For land-only: the best wide view of the parcel (ground-level or aerial/drone).
# 2) Other strong exterior views (front/side/back, additional angles, good aerial overviews of the parcel).
# 3) High-value interiors (if buildings exist): kitchen, living/dining, primary bedroom, bathrooms.
# 4) Other useful rooms/spaces or close-up land features (entrance, access road, notable features on the land).
# 5) Backyard/patio/garage/driveway, additional land context views.
# 6) Street/area/context shots if helpful and clearly related to the property.

# Rules:
# - Prefer higher clarity, less obstruction, and good lighting.
# - If multiple similar shots exist, keep only the best one.
# - If no valid property images exist, keep none.
# - Only keep images that visually show the property land, structures, **or its dedicated parking/driveway/garage areas**; skip anything that is just text, paperwork, maps, or meta/reference material.
# - Return JSON ONLY in the schema requested—no extra text.
# """

CURATOR_CLASSIFIER_PROMPT = """
You are classifying ONE image for a real estate listing.

The property can be:
- A built structure (house / condo / townhome / commercial / mixed-use, etc.), or
- Land-only / plots / farms / lots, including aerial/drone photos of the parcel.

Your job: decide if THIS SINGLE IMAGE is a valid PROPERTY PHOTO for this listing,
or if it must be SKIPPED.

Treat as valid "property photos" (KEEP = true) when the image clearly shows:
- Exterior or interior photos of buildings on the property.
- Ground-level photos of the land / parcel / lot (fields, plots, vacant land).
- Aerial / drone / satellite-style images that clearly show the actual property/parcel
  (not just a generic area map or diagram).
- Driveways, garages, carports, and dedicated parking areas/parking bays/covered parking
  clearly associated with this property or its building, even if the main building is
  not fully visible.

What to SKIP (KEEP = false; image must NOT be treated as a property photo):
- Brand / company logos of any kind — including standalone realtor/wholesaler logos,
  wordmarks, emblems, icons, or "Real Estate Group" / brokerage branding on a solid
  or plain background (e.g. a company name + leaf/icon mark with no property visible).
  These MUST be skipped; they must never be kept for posting or Dropbox galleries.
- QR codes, headshots/people/selfies, agent cards, business cards, signatures.
- Screenshots of text, ads/flyers, memes, heavy text tiles, price/terms graphics.
- Any image that is primarily a marketing tile, flyer, banner, or logo, EVEN IF there is
  a property photo in the background. If the logo/text/branding covers a large part of
  the image or is the main focus, SKIP it and use a reason like "brand logo", "logo",
  "text tile", or "marketing banner".
- A tiny discreet watermark in a corner of an otherwise clear property photo may KEEP.
  If branding is the main subject, or the image is mostly logo/text with little or no
  real property visible, SKIP it.
- Website/app listing screenshots (portal pages, app UIs, search results, etc.).
- Generic maps (e.g., Google Maps with a pin), location diagrams, zoning diagrams,
  parcel drawings that are not actual photos.
- Floor plans, appraisal docs, spreadsheets, closing statements, or other documents.
- Any image that does NOT directly show this property's land or structures
  (e.g., unrelated stock photos, random interiors, other properties).

IMPORTANT:
- Only mark KEEP = true if the image is a genuine property photo as defined above.
- Brand logos and company branding images are NEVER valid property photos — always SKIP.
- If there is any doubt and the image looks like branding, a flyer, a text tile,
  a document, a map, or a UI screenshot, SKIP it.

Return ONLY JSON with this exact structure:
{
  "url": "<same URL as given>",
  "keep": true or false,
  "reason": "short reason like 'property exterior', 'kitchen', 'vacant land', 'brand logo', 'logo', 'text tile', 'document', etc."
}
"""

def _response_format() -> Dict[str, Any]:
    return {"type": "json_object"}

def _build_user_prompt(images: List[str]) -> str:
    return (
        "IMAGES (array of URLs):\n"
        + json.dumps(images, ensure_ascii=False, indent=2)
        + "\n\nTASK: Classify each image and rank the real property photos. "
          "Return JSON like:\n"
          "{\n"
          '  "kept_ordered": ["url1", "url2", ...],\n'
          '  "skipped": [{"url": "urlX", "reason": "logo"}, ...],\n'
          '  "primary": "url1"  // first best image or null\n'
          "}\n"
          "Do not include any keys other than the three above."
    )

def classify_primary_image(url: str, model: Optional[str] = None) -> Dict[str, Any]:
    """
    Stricter re-check for the PRIMARY image (images[0]) of a listing.

    Uses the same CURATOR_CLASSIFIER_PROMPT rules:
    - keep=True only if it's clearly a property photo (exterior/interior/land/aerial)
    - keep=False for logos, flyers, documents, maps, UI screenshots, etc.
    """
    content = [
        {"type": "text", "text": CURATOR_CLASSIFIER_PROMPT},
        {"type": "text", "text": f"PRIMARY_CHECK_URL: {url}"},
        {"type": "image_url", "image_url": {"url": url}},
    ]

    # Build kwargs so we can conditionally include temperature
    kwargs = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "response_format": {"type": "json_object"},
    }
    if _model_supports_temperature(model):
        kwargs["temperature"] = 0

    resp = client.chat.completions.create(**kwargs)

    raw = resp.choices[0].message.content
    data = json.loads(raw)

    # normalize output
    if "url" not in data or not isinstance(data["url"], str):
        data["url"] = url

    keep_val = data.get("keep")
    if not isinstance(keep_val, bool):
        # force fail if the model gives something weird
        return {
            "url": data["url"],
            "keep": False,
            "reason": f"invalid_keep_value: {keep_val!r}",
        }

    if not isinstance(data.get("reason"), str):
        data["reason"] = ""

    return data


def classify_single_image(url: str) -> Dict[str, Any]:
    content = [
        {"type": "text", "text": CURATOR_CLASSIFIER_PROMPT},
        {"type": "text", "text": f"URL: {url}"},
        {"type": "image_url", "image_url": {"url": url}},
    ]

    kwargs = {
        "model": OPENAI_MODEL_VISION,
        "messages": [{"role": "user", "content": content}],
        "response_format": {"type": "json_object"},
    }
    if _model_supports_temperature(OPENAI_MODEL_VISION):
        kwargs["temperature"] = 0

    resp = client.chat.completions.create(**kwargs)

    raw = resp.choices[0].message.content
    data = json.loads(raw)

    # --- basic schema validation ---
    # Ensure url is present and correct-ish
    if "url" not in data or not isinstance(data["url"], str):
        data["url"] = url

    # Ensure keep is a proper bool; if not, force skip
    keep_val = data.get("keep")
    if not isinstance(keep_val, bool):
        return {
            "url": data["url"],
            "keep": False,
            "reason": f"invalid_keep_value: {keep_val!r}",
        }

    # Ensure reason is a string
    if not isinstance(data.get("reason"), str):
        data["reason"] = ""

    return data


def _filter_by_filename(kept_urls: List[str], skipped: List[Dict[str, str]]):
    """Second-pass filter: drop URLs whose filename contains logo/headshot tokens."""
    still_kept: List[str] = []
    for u in kept_urls:
        reason = blocked_image_filename_reason(u)
        if reason:
            skipped.append({"url": u, "reason": reason})
        else:
            still_kept.append(u)
    return still_kept


def _filter_property_images(image_urls: List[str]):
    kept_urls: List[str] = []
    skipped: List[Dict[str, str]] = []

    for u in image_urls:
        try:
            result = classify_single_image(u)
        except Exception as e:
            # Any API / HTTP / invalid_image_url / JSON error ⇒ skip this image
            skipped.append({
                "url": u,
                "reason": f"vision_error: {e}"
            })
            continue

        if result.get("keep") is True:  # strictly True, not just truthy
            kept_urls.append(result.get("url", u))
        else:
            skipped.append({
                "url": result.get("url", u),
                "reason": (result.get("reason") or "").strip()
            })

    return kept_urls, skipped

CURATOR_ORDERING_PROMPT = """
You will see ONLY valid property photos (no logos, no documents, no text tiles).
Each image belongs to the same property listing.

Order these images from BEST cover photo to least important, using this priority:

1) Clear main view of the property:
   - For built properties: front exterior / main elevation (daytime, unobstructed).
   - For land-only: the best wide view of the parcel (ground-level or aerial/drone).
2) Other strong exterior views (front/side/back, additional angles, good aerials).
3) High-value interiors: kitchen, living/dining, primary bedroom, bathrooms.
4) Other useful rooms/spaces or close-up land features.
5) Backyard/patio/garage/driveway, additional land context views.
6) Street/area/context shots if helpful and clearly related.

Prefer:
- Higher clarity, less obstruction, good lighting.
- If multiple similar shots exist, put the best one earlier.

Return ONLY JSON:
{
  "kept_ordered": ["url1", "url2", ...],
  "primary": "url1"  // best image or null if none
}
Use ONLY URLs that I provide, do not invent new ones.
"""



def order_property_images(kept_urls: List[str]) -> Dict[str, Any]:
    if not kept_urls:
        return {"kept_ordered": [], "primary": None}

    content = [{"type": "text", "text": CURATOR_ORDERING_PROMPT}]

    for u in kept_urls:
        content.append({"type": "text", "text": f"URL: {u}"})
        content.append({"type": "image_url", "image_url": {"url": u}})

    kwargs = {
        "model": OPENAI_MODEL_VISION,
        "messages": [{"role": "user", "content": content}],
        "response_format": {"type": "json_object"},
    }
    if _model_supports_temperature(OPENAI_MODEL_VISION):
        kwargs["temperature"] = 0.2

    resp = client.chat.completions.create(**kwargs)

    return json.loads(resp.choices[0].message.content)

def process_primary_image_verification(
    limit: int = 100,
    model: Optional[str] = None,
) -> Dict[str, int]:
    """
    Middleware step between image curation and posting.

    Flow:
      - Pull listings with status == 'ready_for_primary_image_check'.
      - For each listing:
          * Take images[0] as the intended primary image.
          * Run a stricter vision check via classify_primary_image().
          * If keep=True => status -> 'ready_to_post'.
          * If keep=False or any error => status -> 'primary_image_failed'.
      - Store the full check result in 'primary_image_check' for debugging/audit.

    Returns stats about how many passed/failed.
    """
    now = datetime.utcnow()

    qs = ParsedListing.objects(status=MIDDLEWARE_STATUS_PRIMARY) \
        .only("id", "images", "primary_image_check") \
        .limit(limit)

    total = checked = passed = failed = no_image = 0
    errors: List[str] = []

    primary_model = model
    secondary_model = "gpt-5-mini"

    for pl in qs:
        total += 1

        images = list(pl.images or [])
        images = [u.strip() for u in images if isinstance(u, str) and u.strip()]

        if not images:
            # No primary image to verify – mark as failed & note reason
            pl.update(
                set__primary_image_check={
                    "url": None,
                    "keep": False,
                    "reason": "no_images_available_for_primary_check",
                },
                set__status=PRIMARY_PASS_STATUS,
                set__updated_at=now,
            )
            no_image += 1
            failed += 1
            continue

        primary_url = images[0]
        checked += 1


        try:
            # 1st pass: main model (gpt-5.6-luna)
            result_1 = classify_primary_image(primary_url, model=primary_model)
            keep_1 = bool(result_1.get("keep", False))
            reason_1 = (result_1.get("reason") or "").strip()

            # 2nd pass: mini model (gpt-5-mini)
            result_2 = classify_primary_image(primary_url, model=secondary_model)
            keep_2 = bool(result_2.get("keep", False))
            reason_2 = (result_2.get("reason") or "").strip()

            both_keep = keep_1 and keep_2

            if both_keep:
                # Accept: move to ready_to_post
                pl.update(
                    set__primary_image_check={
                        "url": primary_url,
                        "keep": True,
                        "reason": "both_models_keep_true",
                        "model_primary": {
                            "name": primary_model,
                            "keep": keep_1,
                            "reason": reason_1
                        },
                        "model_secondary": {
                            "name": secondary_model,
                            "keep": keep_2,
                            "reason": reason_2
                        },
                    },
                    set__status=PRIMARY_PASS_STATUS,
                    set__updated_at=now,
                )
                try:
                    from observability.pipeline_metrics import record_listing_stage
                    record_listing_stage(str(pl.id), "primary_image", listing_status=PRIMARY_PASS_STATUS)
                except Exception:
                    pass
                passed += 1
            else:
                # Reject: at least one model said keep=False (or both)
                pl.update(
                    set__primary_image_check={
                        "url": primary_url,
                        "keep": False,
                        "reason": "one_or_both_models_rejected",
                        "model_primary": {
                            "name": primary_model,
                            "keep": keep_1,
                            "reason": reason_1
                        },
                        "model_secondary": {
                            "name": secondary_model,
                            "keep": keep_2,
                            "reason": reason_2
                        },
                    },
                    set__status=PRIMARY_FAIL_STATUS,
                    set__updated_at=now,
                )
                try:
                    from observability.pipeline_metrics import record_listing_stage
                    record_listing_stage(str(pl.id), "primary_image_failed", listing_status=PRIMARY_FAIL_STATUS)
                except Exception:
                    pass
                failed += 1

        except Exception as e:
            msg = f"{pl.id}: {type(e).__name__}: {e}"
            errors.append(msg)
            # Treat as failure but keep URL + reason for debugging
            pl.update(
                set__primary_image_check={
                    "url": primary_url,
                    "keep": False,
                    "reason": f"primary_check_exception: {e}",
                },
                set__status=PRIMARY_FAIL_STATUS,
                set__updated_at=now,
            )
            failed += 1

    return {
        "total": total,
        "checked": checked,
        "passed": passed,
        "failed": failed,
        "no_image": no_image,
        "errors": errors[:20],
    }


def _invoke_vision_model(image_urls: List[str]) -> Dict[str, Any]:
    # 1) Vision classification (property photo vs logo/headshot/flyer/etc.)
    kept_raw, skipped = _filter_property_images(image_urls)

    # 2) Filename filter (e.g. "...Logo-Design-2", "...Headshot-scaled")
    kept_raw = _filter_by_filename(kept_raw, skipped)

    if not kept_raw:
        return {
            "kept_ordered": [],
            "skipped": skipped,
            "primary": None,
        }

    # 3) Ordering among valid property photos only
    ordered = order_property_images(kept_raw)
    kept_ordered = ordered.get("kept_ordered") or kept_raw
    primary = ordered.get("primary") or (kept_ordered[0] if kept_ordered else None)

    return {
        "kept_ordered": kept_ordered,
        "skipped": skipped,
        "primary": primary,
    }



# def _invoke_vision_model(image_urls: List[str]) -> Dict[str, Any]:
#     # Build a single multimodal message with all images; the model can reason across them.
#     print("image_urls",image_urls)
#     content: List[Dict[str, Any]] = [{"type": "text", "text": _build_user_prompt(image_urls)}]
#     for u in image_urls:
#         content.append({"type": "image_url", "image_url": {"url": u}})

#     resp = client.chat.completions.create(
#         model=OPENAI_MODEL_VISION,
#         messages=[
#             {"role": "system", "content": CURATOR_SYSTEM_PROMPT},
#             {"role": "user", "content": content},
#         ],
#         temperature=0.2,
#         response_format=_response_format(),
#     )
#     try:
#         print("resp.choices[0].message.content",resp.choices[0].message.content)
#         return json.loads(resp.choices[0].message.content)
#     except Exception:
#         print("Exception",Exception)
#         # Fallback: keep original order, skip nothing
#         return {"kept_ordered": image_urls, "skipped": [], "primary": image_urls[0] if image_urls else None}

def _dedupe_preserve_order(urls: List[str]) -> List[str]:
    seen = set()
    out = []
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out

def process_listings_ready_for_image_processing(limit: int = 100) -> Dict[str, int]:
    """
    Pull listings with status == 'ready_for_image_processing' and curate images.
    - If images empty => mark ready_to_post.
    - Else call vision model to filter/reorder.
    - Save 'images' (kept ordered), 'skipped_images' (list of dicts with url, reason).
    - Mark status 'ready_to_post'.
    """
    # init_db()
    total = done = no_images = failed = 0
    now = datetime.utcnow()

    q = ParsedListing.objects(status="ready_for_image_processing").order_by("+created_at").limit(limit)
    for pl in q:
        total += 1
        try:
            images: List[str] = list(pl.images or [])
            images = [u.strip() for u in images if isinstance(u, str) and u.strip()]

            if not images:
                # Nothing to curate — move forward
                pl.update(
                    set__status="ready_to_post",
                    set__updated_at=now,
                )
                try:
                    from observability.pipeline_metrics import record_listing_stage
                    record_listing_stage(str(pl.id), "ready_to_post", listing_status="ready_to_post")
                except Exception:
                    pass
                no_images += 1
                continue

            result = _invoke_vision_model(images)

            kept = _dedupe_preserve_order(result.get("kept_ordered") or [])
            skipped_items = result.get("skipped") or []
            print("kept",kept)
            print("skipped_items",skipped_items)
            # Normalize skipped to list of {"url":..., "reason":...}
            norm_skipped = []
            for it in skipped_items:
                if isinstance(it, dict) and it.get("url"):
                    norm_skipped.append({"url": it.get("url"), "reason": (it.get("reason") or "").strip()})
                elif isinstance(it, str):
                    norm_skipped.append({"url": it, "reason": ""})

            # Safety: if model kept nothing (over-filtered), fall back to original first image
            # if not kept and images:
            #     kept = [images[0]]

            print("norm_skipped",norm_skipped)

            pl.update(
                set__images=kept,
                set__skipped_images=norm_skipped,   # <-- new array to hold filtered out ones
                set__status=MIDDLEWARE_STATUS_PRIMARY,
                set__updated_at=now,
            )
            try:
                from observability.pipeline_metrics import record_listing_stage
                record_listing_stage(str(pl.id), "image_curation", listing_status=MIDDLEWARE_STATUS_PRIMARY)
            except Exception:
                pass
            done += 1

        except Exception as e:
            print("Exception",e)
            pl.update(
                set__rules_ai_reason=f"image_curation_failed: {e}",
                set__status="image_curation_failed",
                set__updated_at=now,
            )
            try:
                from observability.pipeline_metrics import record_listing_stage
                record_listing_stage(str(pl.id), "image_curation_failed", listing_status="image_curation_failed", skip_reason=str(e))
            except Exception:
                pass
            failed += 1

    return {"total": total, "curated": done, "no_images": no_images, "failed": failed}
