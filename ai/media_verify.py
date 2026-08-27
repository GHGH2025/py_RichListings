# ai_media_verify.py
import json, re
from typing import Dict, Any, Optional, List
from openai import OpenAI

from models import ParsedListing, FilteredListingEmail
from mongoengine.queryset.visitor import Q
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime as _dt

# Reuse your env + client
import os
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-luna")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY)


def _model_supports_temperature(model: Optional[str]) -> bool:
    """gpt-5* models often reject temperature; omit it for that family."""
    if not model:
        return True
    return not str(model).lower().startswith("gpt-5")
import io, mimetypes, os, uuid, requests, boto3, tempfile
from urllib.parse import urlparse

AWS_REGION   = os.getenv("AWS_REGION", "us-east-1")
S3_BUCKET    = os.getenv("LISTINGS_S3_BUCKET", "")       # required
S3_PREFIX    = (os.getenv("LISTINGS_S3_PREFIX", "images/") or "").lstrip("/")

UA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# ---------- S3 helpers (your tested pattern) ----------
def get_s3_client(region: str):
    return boto3.client("s3", region_name=region)

def upload_to_s3(local_path: str, bucket: str, key: str, region: str) -> str:
    """Upload local file to S3 and return public URL."""
    s3 = get_s3_client(region)

    # Try to detect proper Content-Type
    content_type, _ = mimetypes.guess_type(local_path)
    extra_args = {}
    if content_type:
        extra_args["ContentType"] = content_type

    print(f"Uploading {local_path} to s3://{bucket}/{key}")
    if extra_args:
        s3.upload_file(local_path, bucket, key, ExtraArgs=extra_args)
    else:
        s3.upload_file(local_path, bucket, key)

    # region-aware public URL
    return f"https://{bucket}.s3.{region}.amazonaws.com/{key}"

# ---------- image fetch + upload wiring ----------
def _sniff_image(content: bytes) -> Optional[str]:
    """Return a file extension if bytes look like a real image. Dropbox often lies about Content-Type."""
    if not content or len(content) < 12:
        return None
    if content[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if content[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return ".webp"
    return None

def _is_our_s3_url(url: str) -> bool:
    host = (urlparse(url).netloc or "").lower()
    if not host:
        return False
    if S3_BUCKET and host.startswith(f"{S3_BUCKET.lower()}.s3"):
        return True
    return "amazonaws.com" in host and "s3" in host

def _build_s3_key(ext: str) -> str:
    # S3 key = <prefix>/<uuid><ext>
    name = uuid.uuid4().hex + (ext or "")
    return f"{S3_PREFIX}/{name}" if S3_PREFIX else name

def _fetch_image_bytes(url: str) -> Optional[bytes]:
    # Keep the full URL, including query params (e.g. Dropbox rlkey). Those are access keys.
    attempts = (
        {"timeout": 15},
        {"timeout": 25, "headers": UA_HEADERS},
    )
    for kwargs in attempts:
        try:
            r = requests.get(url, allow_redirects=True, **kwargs)
            if r.status_code == 200 and _sniff_image(r.content):
                return r.content
        except requests.RequestException:
            continue
    return None

def _upload_bytes_to_s3(content: bytes, ext: str) -> str:
    s3key = _build_s3_key(ext)
    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tf:
        tf.write(content)
        tmp_path = tf.name
    try:
        return upload_to_s3(tmp_path, S3_BUCKET, s3key, AWS_REGION)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

def _fetch_forbidden_then_upload(url: str) -> str:
    """
    Download the image and remirror to S3 so WordPress gets a clean public .jpg URL.
    Keep the original only if it is already our S3 object or the fetch fails.
    """
    if _is_our_s3_url(url):
        return url
    if not S3_BUCKET:
        raise RuntimeError("LISTINGS_S3_BUCKET is not set")

    content = _fetch_image_bytes(url)
    if not content:
        return url
    ext = _sniff_image(content)
    if not ext:
        return url
    try:
        return _upload_bytes_to_s3(content, ext)
    except Exception:
        return url

def _fix_forbidden_images(urls: list[str]) -> list[str]:
    """Remirror each non-S3 image URL to a public S3 URL. Keep the original on failure."""
    out = []
    for u in urls or []:
        if not isinstance(u, str) or not u.strip():
            continue
        try:
            new_u = _fetch_forbidden_then_upload(u.strip())
            out.append(new_u)
        except Exception:
            out.append(u.strip())
    return out

mirror_images_to_s3 = _fix_forbidden_images

def _image_mirror_updates(original, mirrored) -> dict:
    """Persist remirrored S3 URLs. Also replace images when the list changed."""
    updates = {}
    if mirrored != list(original or []):
        updates["set__images"] = mirrored
    if mirrored:
        updates["set__images_s3"] = mirrored
    return updates


# ---------- utils ----------
def _now():
    return _dt.utcnow()

_IMG_EXT_RE = re.compile(r"\.(?:png|jpe?g|gif|webp|bmp|tiff?)($|\?)", re.I)
_HTTP_RE    = re.compile(r"https?://[^\s)>\]}\"']+", re.I)

def _looks_like_image_url(u: str) -> bool:
    return bool(_IMG_EXT_RE.search(u))

def _first_url_by_keywords(text: str) -> Optional[str]:
    """
    Fallback for 'more pictures' link when AI can't find it:
    looks for common photo-hosting anchors.
    """
    candidates = _HTTP_RE.findall(text or "")
    if not candidates:
        return None
    prefer = []
    okay = []
    for u in candidates:
        low = u.lower()
        if any(k in low for k in [
            "drive.google", "dropbox.com", "photos.google", "sharepoint",
            "imgur.com", "cloudinary", "file", "gallery", "images", "photos"
        ]):
            prefer.append(u)
        elif any(k in low for k in ["view", "pictures", "photos", "album", "gallery"]):
            okay.append(u)
    return (prefer[0] if prefer else (okay[0] if okay else None))

# def _clean_images(arr: Optional[List[str]]) -> List[str]:
#     if not arr:
#         return []
#     out, seen = [], set()
#     for u in arr:
#         if not isinstance(u, str):
#             continue
#         u2 = u.strip()
#         if not u2:
#             continue
#         # strip tracking like ?rdr=true for images too (optional)
#         if u2.endswith("?rdr=true"):
#             u2 = u2[:-10]
#         if u2 not in seen and (_looks_like_image_url(u2) or u2.startswith("http")):
#             seen.add(u2)
#             out.append(u2)
#         if len(out) >= 12:  # cap to 12 like your extractor
#             break
#     return out

def _clean_images(arr):
    out = []
    for u in arr or []:
        if isinstance(u, str):
            u2 = u.strip()
            if u2.lower().startswith(("http://", "https://")):
                out.append(u2)
    return out[:12]  # cap to 12

# ---------- OpenAI schema + prompts ----------
def _response_format() -> Dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "media_verify_payload",
            "strict": True,
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "matched": {"type": "boolean"},
                    "images":  {"type": "array", "items": {"type": "string"}},
                    "other_images_source": {"type": ["string", "null"]},
                    "notes": {"type": "array", "items": {"type": "string"}}
                },
                "required": ["matched", "images", "other_images_source", "notes"]
            }
        }
    }

_SYSTEM_PROMPT = """\
You are verifying media for ONE property listing inside an email body of markdown text.
Return ONLY the JSON keys defined by the schema.

What to do:
- Scan the listing and use the provided ADDRESS (street + city [+ state/zip may appear]) to locate the exact section for that listing.
- From that section:
  • Collect direct image URLs(http/https) that depict the property, might present under image tag, extract exact urls.
  • If there is a single "more pictures", "click here for more pictures" / "view photos" / "gallery" / shared drive link, return it as other_images_source VERBATIM (exact URL as it appears in the content). If multiple, pick the best main gallery.
- Ignore unsubscribe, logos, social icons, QR-code tracking, signatures, or generic banners.
- If nothing is found, return matched=false with empty images and other_images_source=null.
Rules:
- Keep order of most relevant first.
- Do not invent URLs.
- Prefer https over http if both exist; return what appears in the content if only one exists.
"""

_USER_TMPL = """\
ADDRESS:
{address}

EMAIL_MARKDOWN_HTML:
{html_ai}
"""

def ai_verify_media_for_listing(
    address: str,
    html_ai: str,
    model: Optional[str] = None,
    listing_id: Optional[str] = None,
) -> Dict[str, Any]:
    from observability.openai_usage import tracked_chat_create

    msg = _USER_TMPL.format(address=address.strip(), html_ai=(html_ai or "").strip())
    use_model = model or OPENAI_MODEL
    create_kwargs: Dict[str, Any] = {
        "model": use_model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": msg},
        ],
        "response_format": _response_format(),
    }
    if _model_supports_temperature(use_model):
        create_kwargs["temperature"] = 0.1
    chat = tracked_chat_create(
        client,
        stage="verified",
        call_name="ai_verify_media",
        listing_id=listing_id,
        **create_kwargs,
    )
    data = json.loads(chat.choices[0].message.content)
    # light sanitation
    data["images"] = _clean_images(data.get("images", []))
    o = data.get("other_images_source")
    if isinstance(o, str) and o.strip():
        data["other_images_source"] = o.strip()
    else:
        data["other_images_source"] = None
    return data

# ---------- Orchestrator ----------
def _address_line_for_match(pl: ParsedListing) -> str:
    """Compose a simple 'address, city, state zip' to help the model anchor the correct block."""
    addr = (pl.address or "").strip()
    city = (pl.city or "").strip()
    state = (pl.state or "").strip()
    zip_ = (pl.zip or "").strip()
    parts = [p for p in [addr, city, state, zip_] if p]
    return ", ".join(parts)

# def verify_and_fill_missing_media_for_not_processed(
#     limit: int = 200,
#     max_workers: int = 6,
#     model: Optional[str] = None
# ) -> Dict[str, Any]:
#     """
#     Find listings with status=not_processed where images or other_images_source are empty.
#     Re-scan the original html_ai body to recover media. On success, update and set status='verified'.
#     """
#     # candidates: images empty OR other_images_source empty
#     qs = ParsedListing.objects(
#         Q(status="not_processed") & (
#             Q(images__exists=False) | Q(images=[]) | Q(other_images_source=None) | Q(other_images_source="")
#         )
#     ).only("id", "address", "city", "state", "zip", "images", "other_images_source", "source_email")

#     total = qs.count()
#     scanned = updated = no_body = 0
#     errs: List[str] = []

#     def _work(pl_id):
#         nonlocal updated, no_body, scanned
#         try:
#             pl = ParsedListing.objects(id=pl_id).first()
#             if not pl:
#                 return
#             scanned += 1

#             # get original html_ai
#             se: FilteredListingEmail = getattr(pl, "source_email", None)
#             html_ai = se.bodies.html_ai or se.bodies.html_full or ""
#             # if se and getattr(se, "bodies", None):
#             #     html_ai = (se.bodies or {}).get("html_ai")

#             # if not html_ai or not str(html_ai).strip():
#             #     no_body += 1
#             #     return

#             anchor = _address_line_for_match(pl)
#             ai = ai_verify_media_for_listing(anchor, str(html_ai), model=model)


#             # Fallbacks if AI missed obvious things
#             images = _clean_images(ai.get("images", []))
#             other = ai.get("other_images_source")
#             if not other:
#                 # try regex fallback
#                 maybe = _first_url_by_keywords(str(html_ai))
#                 if maybe:
#                     other = maybe

#             # Decide if we update and mark verified
#             changed = False
#             updates = {}

#             if images and (not pl.images or pl.images == []):
#                 updates["set__images"] = images
#                 changed = True
#             if other and (not pl.other_images_source):
#                 updates["set__other_images_source"] = other
#                 changed = True

#             if changed:
#                 updates["set__status"] = "verified"
#                 updates["set__updated_at"] = _now()
#                 ParsedListing.objects(id=pl.id).update_one(**updates)
#                 updated += 1
#         except Exception as e:
#             errs.append(f"{pl_id}: {type(e).__name__}: {e}")

#     # parallel
#     with ThreadPoolExecutor(max_workers=max_workers) as ex:
#         futs = [ex.submit(_work, str(pl.id)) for pl in qs.limit(limit)]
#         for _ in as_completed(futs):
#             pass

#     return {
#         "total_candidates": total,
#         "scanned": scanned,
#         "updated": updated,
#         "missing_html_ai": no_body,
#         "errors": errs[:10],  # cap
#     }



def verify_and_fill_missing_media_for_not_processed(
    limit: int = 200,
    max_workers: int = 6,
    model: Optional[str] = None,
    gmail_message_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Processes ALL 'not_processed' listings.
    - If both images & other_images_source exist: skip AI, mark verified.
    - If either is missing: run AI; update ONLY the missing fields (no overwrites, no regex fallback).
    - Regardless of outcome: mark verified at the end.
    """
    qs = ParsedListing.objects(status="not_processed")
    if gmail_message_id:
        qs = qs.filter(gmail_message_id=gmail_message_id)
    else:
        qs = qs.filter(gmail_message_id__not__startswith="test_")
    qs = qs.only("id", "address", "city", "state", "zip", "images", "other_images_source", "source_email") \
        .limit(limit)

    total = qs.count()
    scanned = 0
    updated = 0
    verified_direct = 0
    verified_ai_path = 0
    errs: List[str] = []

    def _safe_html_ai_from_source_email(se) -> str:
        if not se:
            return ""
        bodies = getattr(se, "bodies", None)
        if not bodies:
            return ""
        # prefer html_ai, fallback to html_full
        return (getattr(bodies, "html_ai", None) or getattr(bodies, "html_full", None) or "") or ""

    def _work(pl_id: str):
        nonlocal scanned, updated, verified_direct, verified_ai_path
        try:
            pl = ParsedListing.objects(id=pl_id).first()
            if not pl:
                return

            scanned += 1
            has_imgs  = bool(pl.images) and len(pl.images) > 0
            has_other = bool(getattr(pl, "other_images_source", None))

            # If both present → no AI, just verify
            if has_imgs and has_other:

                # 🔽 remirror to S3 (and persist images_s3)
                fixed = _fix_forbidden_images(pl.images)
                verify_updates = {
                    "set__status": "verified",
                    "set__wp_check": "pending",
                    "set__updated_at": _now(),
                }
                verify_updates.update(_image_mirror_updates(pl.images, fixed))
                ParsedListing.objects(id=pl.id).update_one(**verify_updates)
                try:
                    from observability.pipeline_metrics import record_listing_stage
                    record_listing_stage(str(pl.id), "verified", listing_status="verified")
                except Exception:
                    pass
                verified_direct += 1
                return

            # Otherwise, attempt to fill ONLY missing fields
            html_ai = _safe_html_ai_from_source_email(getattr(pl, "source_email", None))
            ai_images, ai_other = [], None

            if html_ai.strip():
                anchor = _address_line_for_match(pl)
                ai = ai_verify_media_for_listing(anchor, str(html_ai), model=model, listing_id=pl_id)
                # Only harvest for fields that are missing locally
                if not has_imgs:
                    ai_images = _clean_images(ai.get("images", []))
                if not has_other:
                    ai_other = ai.get("other_images_source")

            updates = {}
            if (not has_imgs) and ai_images:
                safe_imgs = _fix_forbidden_images(ai_images)
                updates.update(_image_mirror_updates([], safe_imgs))
            if (not has_other) and ai_other:
                updates["set__other_images_source"] = ai_other

            # If we already had images originally, still remirror them now
            if has_imgs and not updates.get("set__images") and not updates.get("set__images_s3"):
                fixed_existing = _fix_forbidden_images(pl.images)
                print("fixed_existing",fixed_existing)
                updates.update(_image_mirror_updates(pl.images, fixed_existing))

            # Mark verified (and apply any updates)
            updates["set__status"] = "verified"
            updates["set__wp_check"] = "pending"
            updates["set__updated_at"] = _now()

            ParsedListing.objects(id=pl.id).update_one(**updates)

            try:
                from observability.pipeline_metrics import record_listing_stage
                record_listing_stage(str(pl.id), "verified", listing_status="verified")
            except Exception:
                pass

            if ("set__images" in updates) or ("set__other_images_source" in updates):
                updated += 1
            verified_ai_path += 1

        except Exception as e:
            errs.append(f"{pl_id}: {type(e).__name__}: {e}")

    # parallel execution
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = [ex.submit(_work, str(pl.id)) for pl in qs]
        for _ in as_completed(futs):
            pass

    return {
        "total_not_processed": total,
        "scanned": scanned,
        "verified_direct": verified_direct,   # had both fields; no AI
        "verified_ai_path": verified_ai_path, # needed AI (even if nothing new found)
        "updated_missing_fields": updated,    # actually filled a missing field
        "errors": errs[:20],
    }