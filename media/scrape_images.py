import asyncio
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

MAX_IMAGE_LINKS = 50

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


def extract_image_links_from_html(html: str, base_url: str = "") -> list:
    """Extract image candidates from ordinary or JavaScript-rendered HTML.

    This deliberately does not whitelist image hosts. A seller can use a CDN,
    Google Photos, an MLS host, or a private marketing site. The downstream
    Dropbox uploader validates the response content type before uploading.
    """
    if not html:
        return []

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
