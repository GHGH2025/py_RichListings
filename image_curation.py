# image_curation.py
import json
from datetime import datetime
from typing import Dict, List, Any

from dotenv import load_dotenv
from openai import OpenAI
import os
from mongo_engine_conn import init_db
from models import ParsedListing

load_dotenv()
OPENAI_MODEL_VISION = os.getenv("OPENAI_VISION_MODEL", "gpt-4.1-mini")
client = OpenAI()

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

CURATOR_SYSTEM_PROMPT =  """
You are an expert real-estate photo curator.

You receive a set of image URLs for a single property listing. The property can be:
- A built structure (house / condo / townhome / commercial, etc.), or
- Land-only / plots / farms / lots, including aerial/drone photos of the parcel.

Your job: return ONLY JSON describing:
- Which images are genuine photos of this property's land or built structures (interior/exterior, ground-level, or aerial/drone), in BEST viewing order.
- Which images should be skipped (not real property photos, not showing this property, or low value), with a brief reason.

Treat as valid "property photos":
- Exterior or interior photos of buildings on the property.
- Ground-level photos of the land / parcel / lot (fields, plots, vacant land).
- Aerial / drone / satellite-style images that clearly show the actual property/parcel (not just a generic area map).

What to SKIP:
- Company logos, QR codes, headshots/people/selfies, agent cards, signatures.
- Screenshots of text, watermarked ads/flyers, memes, heavy text tiles, price/terms graphics.
- Website/app listing screenshots that only reference the listing (portal pages, app UIs, search results, etc.).
- Generic maps (e.g., Google Maps with a pin), location diagrams, zoning diagrams, parcel drawings that are not actual photos.
- Floor plans, appraisal docs, spreadsheets, closing statements, or other documents.
- Any image that does NOT directly show this property’s land or structures (e.g., unrelated stock photos, random interiors, other properties).
- Duplicates or near-duplicates (keep the clearest one).

Ordering for KEPT images (best-first):
1) Clear main view of the property:
   - For built properties: front exterior / main elevation (daytime, unobstructed).
   - For land-only: the best wide view of the parcel (ground-level or aerial/drone).
2) Other strong exterior views (front/side/back, additional angles, good aerial overviews of the parcel).
3) High-value interiors (if buildings exist): kitchen, living/dining, primary bedroom, bathrooms.
4) Other useful rooms/spaces or close-up land features (entrance, access road, notable features on the land).
5) Backyard/patio/garage/driveway, additional land context views.
6) Street/area/context shots if helpful and clearly related to the property.

Rules:
- Prefer higher clarity, less obstruction, and good lighting.
- If multiple similar shots exist, keep only the best one.
- If no valid property images exist, keep none.
- Only keep images that visually show the property land or structures; skip anything that is just text, paperwork, maps, or meta/reference material.
- Return JSON ONLY in the schema requested—no extra text.
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

def _invoke_vision_model(image_urls: List[str]) -> Dict[str, Any]:
    # Build a single multimodal message with all images; the model can reason across them.
    print("image_urls",image_urls)
    content: List[Dict[str, Any]] = [{"type": "text", "text": _build_user_prompt(image_urls)}]
    for u in image_urls:
        content.append({"type": "image_url", "image_url": {"url": u}})

    resp = client.chat.completions.create(
        model=OPENAI_MODEL_VISION,
        messages=[
            {"role": "system", "content": CURATOR_SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
        temperature=0.2,
        response_format=_response_format(),
    )
    try:
        print("resp.choices[0].message.content",resp.choices[0].message.content)
        return json.loads(resp.choices[0].message.content)
    except Exception:
        print("Exception",Exception)
        # Fallback: keep original order, skip nothing
        return {"kept_ordered": image_urls, "skipped": [], "primary": image_urls[0] if image_urls else None}

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
                set__status="ready_to_post",
                set__updated_at=now,
            )
            done += 1

        except Exception as e:
            print("Exception",e)
            pl.update(
                set__rules_ai_reason=f"image_curation_failed: {e}",
                set__status="ready_to_post",
                set__updated_at=now,
            )
            failed += 1

    return {"total": total, "curated": done, "no_images": no_images, "failed": failed}
