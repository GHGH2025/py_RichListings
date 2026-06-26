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
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY)
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
def _guess_ext(content_type: str, url: str) -> str:
    if content_type:
        ext = mimetypes.guess_extension(content_type.split(";")[0].strip())
        if ext:
            return ext
    path = urlparse(url).path.lower()
    for e in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        if path.endswith(e):
            return e
    return ".jpg"

def _build_s3_key(ext: str) -> str:
    # S3 key = <prefix>/<uuid><ext>
    name = uuid.uuid4().hex + (ext or "")
    return f"{S3_PREFIX}/{name}" if S3_PREFIX else name

def _fetch_forbidden_then_upload(url: str) -> str:
    """
    Try GET (no headers). If 200 -> keep original.
    If not 200, retry with UA; if success, upload to S3 and return S3 URL.
    If still not accessible, return original URL.
    """
    # quick sanity
    if not S3_BUCKET:
        raise RuntimeError("LISTINGS_S3_BUCKET is not set")

    try:
        r0 = requests.get(url, timeout=15, allow_redirects=True)
        if r0.status_code == 200:
            return url
        # fall through to UA attempt
    except requests.RequestException:
        pass

    try:
        r1 = requests.get(url, headers=UA_HEADERS, timeout=25, allow_redirects=True)
        if r1.status_code == 200 and r1.content:
            ctype = r1.headers.get("Content-Type", "image/jpeg")
            ext   = _guess_ext(ctype, url)
            s3key = _build_s3_key(ext)

            # Write bytes to a temp file then upload (matches your tested approach)
            with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tf:
                tf.write(r1.content)
                tmp_path = tf.name

            try:
                public_url = upload_to_s3(tmp_path, S3_BUCKET, s3key, AWS_REGION)
                return public_url
            finally:
                # best-effort cleanup
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
    except requests.RequestException:
        pass

    # Could not fetch → keep original (better than dropping)
    return url

def _fix_forbidden_images(urls: list[str]) -> list[str]:
    """
    For each URL, if plain request fails but UA fetch works,
    upload to S3 and replace with the S3 URL. Otherwise keep original.
    """
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

def ai_verify_media_for_listing(address: str, html_ai: str, model: Optional[str] = None) -> Dict[str, Any]:
    msg = _USER_TMPL.format(address=address.strip(), html_ai=(html_ai or "").strip())
    chat = client.chat.completions.create(
        model=(model or OPENAI_MODEL),
        messages=[{"role": "system", "content": _SYSTEM_PROMPT},
                  {"role": "user", "content": msg}],
        temperature=0.1,
        response_format=_response_format()
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
    model: Optional[str] = None
) -> Dict[str, Any]:
    """
    Processes ALL 'not_processed' listings.
    - If both images & other_images_source exist: skip AI, mark verified.
    - If either is missing: run AI; update ONLY the missing fields (no overwrites, no regex fallback).
    - Regardless of outcome: mark verified at the end.
    """
    qs = ParsedListing.objects(status="not_processed") \
        .only("id", "address", "city", "state", "zip", "images", "other_images_source", "source_email") \
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

                # 🔽 fix any 403 images in place
                fixed = _fix_forbidden_images(pl.images)
                if fixed != pl.images:
                    ParsedListing.objects(id=pl.id).update_one(
                        set__images=fixed,
                        set__updated_at=_now(),
                    )

                ParsedListing.objects(id=pl.id).update_one(
                    set__status="verified",
                    set__wp_check="pending",
                    set__updated_at=_now(),
                )
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
                ai = ai_verify_media_for_listing(anchor, str(html_ai), model=model)
                # Only harvest for fields that are missing locally
                if not has_imgs:
                    ai_images = _clean_images(ai.get("images", []))
                if not has_other:
                    ai_other = ai.get("other_images_source")

            updates = {}
            if (not has_imgs) and ai_images:
                # updates["set__images"] = ai_images
                            # 🔽 pre-fix 403s before saving
                safe_imgs = _fix_forbidden_images(ai_images)
                updates["set__images"] = safe_imgs
            if (not has_other) and ai_other:
                updates["set__other_images_source"] = ai_other

             # If we already had images originally, still fix them now
            if has_imgs and not updates.get("set__images"):
                fixed_existing = _fix_forbidden_images(pl.images)
                print("fixed_existing",fixed_existing)
                if fixed_existing != pl.images:
                    updates["set__images"] = fixed_existing

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