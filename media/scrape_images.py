import asyncio
import re
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

MAX_IMAGE_LINKS = 50
GOOGLE_PHOTOS_SIZE = "w2048"

_DRIVE_FOLDER_RE = re.compile(
    r"(?:^https?://)?(?:www\.)?drive\.google\.com/drive/folders/([a-zA-Z0-9_-]+)",
    re.I,
)
_DRIVE_FILE_RE = re.compile(
    r'data-id="([a-zA-Z0-9_-]{10,})".*?aria-label="([^"]+)"',
    re.I,
)
_DRIVE_IMG_NAME_RE = re.compile(
    r"\.(?:png|jpe?g|gif|webp|bmp|heic|tif{1,2})$",
    re.I,
)
_HTTP_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
_GALLERY_HOST_NEEDLES = (
    "drive.google",
    "dropbox.com",
    "dl.dropboxusercontent.com",
    "photos.google",
    "photos.app.goo.gl",
    "sharepoint",
    "imgur.com",
    "cloudinary",
)
# ponytail: try every listing URL; curation drops logos. Cap pages so a
# 40-link newsletter cannot spawn 40 Playwright jobs.
MAX_PAGE_SCRAPES = 8
_SKIP_PAGE_NEEDLES = (
    "unsubscribe",
    "mailto:",
    "view-in-browser",
    "viewinbrowser",
    "view%20in%20browser",
    "facebook.com",
    "twitter.com",
    "linkedin.com",
    "instagram.com",
    "google-analytics.com",
    "doubleclick.net",
)

# Album photo tokens in share-page HTML, including JS-escaped URLs.
_GPHOTOS_PW_RE = re.compile(
    r"https:\\?/\\?/lh3\.googleusercontent\.com\\?/pw\\?/([A-Za-z0-9_-]+)",
    re.I,
)

SKIP_HOST_NEEDLES = (
    "s.rs6.net",
    "static.ctctcdn.com",
    "imgssl.constantcontact.com",
    "facebook.com",
    "facebook.net",
    "twitter.com",
    "linkedin.com",
    "doubleclick.net",
    "google-analytics.com",
)

SKIP_URL_NEEDLES = (
    "pixel",
    "beacon",
    "tracking",
    "referrallogos",
    "facebook.svg",
    "x-logo",
)


def _safe_int(value):
    try:
        return int(str(value).strip())
    except Exception:
        return None


def _is_junk_image(url: str, img, check_dimensions: bool = True) -> bool:
    host = (urlsplit(url).netloc or "").lower()
    if any(needle in host for needle in SKIP_HOST_NEEDLES):
        return True
    low = url.lower()
    if any(needle in low for needle in SKIP_URL_NEEDLES):
        return True
    # A tiny `src` is often a tracking placeholder while the real image is
    # in data-src/srcset. Do not apply the placeholder dimensions to lazy
    # attributes; only apply them to the actual src candidate.
    if check_dimensions and img is not None:
        width = _safe_int(img.get("width"))
        height = _safe_int(img.get("height"))
        if (width is not None and width <= 40) or (height is not None and height <= 40):
            return True
    return False


def _add_candidate(out: list, seen: set, raw_url: str, base_url: str = "") -> None:
    """Normalize one HTML image candidate and add it once."""
    value = (raw_url or "").strip().strip("'\"")
    if not value or value.startswith(("data:", "blob:", "javascript:")):
        return
    full_url = urljoin(base_url, value) if base_url else value
    if not full_url.lower().startswith(("http://", "https://")):
        return
    if full_url in seen or len(out) >= MAX_IMAGE_LINKS:
        return
    seen.add(full_url)
    out.append(full_url)


def _srcset_urls(value: str) -> list:
    return [part.strip().split(" ", 1)[0] for part in (value or "").split(",") if part.strip()]


def http_urls(text: str) -> list:
    seen = set()
    urls = []
    for match in _HTTP_URL_RE.finditer(text or ""):
        url = match.group(0).rstrip(").,]>\"'*_")
        if not url or url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def _is_skip_page_url(url: str) -> bool:
    low = (url or "").lower()
    if not low.startswith(("http://", "https://")):
        return True
    if any(needle in low for needle in _SKIP_PAGE_NEEDLES):
        return True
    host = (urlsplit(url).netloc or "").lower()
    return any(needle in host for needle in SKIP_HOST_NEEDLES)


def page_urls_from_text(text: str) -> list:
    """Every http(s) URL in the listing text, minus unsubscribe/social/tracking.

    Known photo hosts (Drive, Dropbox, Photos) are listed first. Image curation
    later drops logos and flyers; this step does not judge photo-intent.
    """
    prefer = []
    other = []
    for url in http_urls(text):
        if _is_skip_page_url(url):
            continue
        if any(needle in url.lower() for needle in _GALLERY_HOST_NEEDLES):
            prefer.append(url)
        else:
            other.append(url)
    return prefer + other


def gallery_url_from_text(text: str) -> str:
    """First usable page URL. Drive/Dropbox win if present; else any http(s) link."""
    urls = page_urls_from_text(text)
    return urls[0] if urls else ""


def drive_folder_id(url: str) -> str:
    """Folder id only — strip ?usp=sharing and other query junk."""
    match = _DRIVE_FOLDER_RE.search(url or "")
    return match.group(1) if match else ""


def drive_folder_page_url(url: str) -> str:
    """Canonical folder page. Keep resourcekey; drop sharing noise like usp=."""
    folder_id = drive_folder_id(url)
    if not folder_id:
        return ""
    qs = parse_qs(urlsplit(url).query, keep_blank_values=True)
    keep = {}
    if "resourcekey" in qs:
        keep["resourcekey"] = qs["resourcekey"]
    query = urlencode(keep, doseq=True)
    base = f"https://drive.google.com/drive/folders/{folder_id}"
    return f"{base}?{query}" if query else base


def drive_folder_image_urls_from_html(html: str, cap: int = MAX_IMAGE_LINKS) -> list:
    """uc?export=download URLs for files in a public Drive folder listing page."""
    if not html:
        return []
    out = []
    seen = set()
    for file_id, name in _DRIVE_FILE_RE.findall(html):
        if file_id in seen:
            continue
        label = (name or "").strip()
        if "Shared folder" in label or label == "Shared":
            continue
        if not (
            _DRIVE_IMG_NAME_RE.search(label)
            or "image" in label.lower()
            or "photo" in label.lower()
            or "img" in label.lower()
        ):
            continue
        seen.add(file_id)
        out.append(f"https://drive.google.com/uc?export=download&id={file_id}")
        if len(out) >= cap:
            break
    return out


def drive_folder_image_urls(url: str, cap: int = MAX_IMAGE_LINKS, html: str | None = None) -> list:
    folder_id = drive_folder_id(url)
    if not folder_id:
        return []
    if html is None:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        }
        try:
            response = requests.get(
                drive_folder_page_url(url) or f"https://drive.google.com/drive/folders/{folder_id}",
                headers=headers,
                timeout=15,
                allow_redirects=True,
            )
            response.raise_for_status()
            html = response.text
        except Exception as exc:
            print(f"Drive folder scrape failed for {url}: {exc}")
            return []
    return drive_folder_image_urls_from_html(html, cap=cap)


def gallery_image_urls(url: str, cap: int = 12, html: str | None = None) -> list:
    """Direct image URLs from a gallery/folder link. Drive folders are not HTML <img> pages."""
    if not (url or "").strip().lower().startswith(("http://", "https://")):
        return []
    if drive_folder_id(url):
        return drive_folder_image_urls(url, cap=cap, html=html)
    return extract_image_links(url, html=html)[:cap]


def gallery_image_urls_from_text(text: str, cap: int = 12) -> list:
    """Scrape images from every listing URL. Curation filters non-property photos."""
    out = []
    seen = set()
    for url in page_urls_from_text(text)[:MAX_PAGE_SCRAPES]:
        for img in gallery_image_urls(url, cap=cap):
            if img in seen:
                continue
            seen.add(img)
            out.append(img)
            if len(out) >= cap:
                return out
    return out


def extract_google_photos_links(html: str) -> list:
    """Unique full-size URLs from a public Google Photos album page.

    Share HTML already embeds lh3.googleusercontent.com/pw/ tokens. The visible
    <img> tags are 54x72 thumbs; rewrite each token to a usable size.
    """
    if not html:
        return []
    out = []
    seen = set()
    for token in _GPHOTOS_PW_RE.findall(html):
        if token in seen or len(out) >= MAX_IMAGE_LINKS:
            continue
        seen.add(token)
        out.append(f"https://lh3.googleusercontent.com/pw/{token}={GOOGLE_PHOTOS_SIZE}")
    return out


def extract_image_links_from_html(html: str, base_url: str = "") -> list:
    """Extract image candidates from ordinary or JavaScript-rendered HTML.

    This deliberately does not whitelist image hosts. A seller can use a CDN,
    Google Photos, an MLS host, or a private marketing site. The downstream
    Dropbox uploader validates the response content type before uploading.
    """
    if not html:
        return []

    gphotos = extract_google_photos_links(html)
    if gphotos:
        return gphotos

    soup = BeautifulSoup(html, "html.parser")
    image_links = []
    seen = set()

    for img in soup.find_all("img"):
        # Modern pages frequently keep the real URL in a lazy-loading
        # attribute or srcset instead of src. Evaluate each attribute
        # independently so a tracking src cannot hide data-src.
        for attr in ("src", "data-src", "data-lazy-src", "data-original"):
            value = img.get(attr)
            if value and not _is_junk_image(
                value,
                img,
                check_dimensions=(attr == "src"),
            ):
                _add_candidate(image_links, seen, value, base_url)
        for src in _srcset_urls(img.get("srcset") or img.get("data-srcset") or ""):
            if not _is_junk_image(src, img, check_dimensions=False):
                _add_candidate(image_links, seen, src, base_url)

    # Album pages often expose a preview image through Open Graph/Twitter
    # metadata even when the actual gallery is rendered later.
    for meta in soup.find_all("meta"):
        key = (meta.get("property") or meta.get("name") or "").lower()
        if key in {"og:image", "og:image:url", "twitter:image", "twitter:image:src"}:
            value = meta.get("content")
            if value and not _is_junk_image(value, meta, check_dimensions=False):
                _add_candidate(image_links, seen, value, base_url)

    return image_links


def _render_page_html(url: str) -> tuple[str, str]:
    """Render a page when its images are injected by JavaScript.

    Playwright is optional at import time so the normal pipeline still starts
    if browser binaries are not installed. Sync Playwright cannot run inside an
    existing asyncio loop (FastAPI webhook); skip in that case.
    """
    try:
        asyncio.get_running_loop()
        return "", "playwright_sync_in_asyncio_loop"
    except RuntimeError:
        pass

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return "", "playwright_not_installed"

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    )
                )
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                # Give client-side album code time to insert images, then scroll
                # a few times for lazy-loaded galleries.
                page.wait_for_timeout(1500)
                for _ in range(3):
                    page.mouse.wheel(0, 1800)
                    page.wait_for_timeout(500)
                rendered = page.content()
                return rendered, page.url or url
            finally:
                browser.close()
    except Exception as exc:
        return "", f"browser_render_failed:{type(exc).__name__}:{exc}"


def extract_image_links(url: str, html: str | None = None) -> list:
    if not (url or "").strip().lower().startswith(("http://", "https://")):
        return []

    static_links = []
    final_url = url
    if html is not None:
        static_links = extract_image_links_from_html(html, url)
    else:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        }
        try:
            response = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
            response.raise_for_status()
            final_url = response.url or url
            static_links = extract_image_links_from_html(response.text, final_url)
        except Exception as exc:
            print(f"Static media scrape failed for {url}: {exc}")

    if static_links:
        return static_links

    # JS galleries only. A static hit is enough; do not spawn Chromium per listing.
    rendered_html, rendered_url_or_error = _render_page_html(final_url or url)
    if rendered_html:
        return extract_image_links_from_html(
            rendered_html,
            rendered_url_or_error or final_url or url,
        )
    if rendered_url_or_error:
        print(f"Rendered media scrape skipped for {url}: {rendered_url_or_error}")
    return static_links
