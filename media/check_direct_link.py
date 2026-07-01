import os
from urllib.parse import urlsplit
import requests
import mimetypes
import time

IMG_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.tiff', '.tif'}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/120.0.0.0 Safari/537.36")

def is_direct_image_url(url: str) -> tuple[bool, str | None]:
    """
    Returns (is_image, content_type_if_known)
    True if URL looks like a direct image, either by extension in path or HEAD Content-Type.
    """
    path = urlsplit(url).path  # strips ?query#fragment
    ext = os.path.splitext(path)[1].lower()
    if ext in IMG_EXTS:
        return True, None   
    # Fallback: HEAD check Content-Type
    try:
        r = requests.head(url, allow_redirects=True, timeout=10, headers={"User-Agent": UA})
        ct = (r.headers.get("Content-Type") or "").lower()
        if ct.startswith("image/"):
            return True, ct
    except requests.RequestException:
        pass
    return False, None

def safe_filename_from_url(url: str, content_type: str | None = None) -> str:
    """Derive a filename from URL path; if missing an extension, infer from Content-Type."""
    path = urlsplit(url).path
    base = os.path.basename(path) or "downloaded_file"
    name, ext = os.path.splitext(base)
    if ext.lower() not in IMG_EXTS:
        # Try to infer extension from content-type (e.g., image/jpeg -> .jpg)
        if content_type:
            guessed = mimetypes.guess_extension(content_type.split(";")[0].strip())
            if guessed:
                ext = guessed
        if not ext:
            ext = ".jpg"  # last-resort default
        base = f"{name or 'image_' + str(int(time.time()*1000))}{ext}"
    return base
       

