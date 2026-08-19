from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

VALID_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".tif")

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


def _is_constant_contact_url(url: str) -> bool:
    host = (urlsplit(url or "").netloc or "").lower()
    return (
        host == "conta.cc"
        or host.endswith(".conta.cc")
        or "constantcontact.com" in host
        or host.endswith(".rs6.net")
        or host == "rs6.net"
    )


def _safe_int(value):
    try:
        return int(str(value).strip())
    except Exception:
        return None


def _is_junk_image(url: str, img) -> bool:
    host = (urlsplit(url).netloc or "").lower()
    if any(needle in host for needle in SKIP_HOST_NEEDLES):
        return True
    low = url.lower()
    if any(needle in low for needle in SKIP_URL_NEEDLES):
        return True
    width = _safe_int(img.get("width"))
    height = _safe_int(img.get("height"))
    if (width is not None and width <= 40) or (height is not None and height <= 40):
        return True
    return False


def extract_image_links_from_html(html: str, base_url: str = "") -> list:
    """JG Equity / Constant Contact pages only. Existing scrapes do not use this."""
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    image_links = []
    seen = set()

    for img in soup.find_all("img"):
        src = (img.get("src") or "").strip()
        if not src or src.startswith("data:"):
            continue

        full_url = urljoin(base_url, src) if base_url else src
        path = urlsplit(full_url).path.lower()
        if not path.endswith(VALID_EXTS):
            continue
        if _is_junk_image(full_url, img):
            continue
        if full_url in seen:
            continue
        seen.add(full_url)
        image_links.append(full_url)

    return image_links


def extract_image_links(url: str) -> list:
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        }

        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()

        final_url = response.url or url
        if _is_constant_contact_url(url) or _is_constant_contact_url(final_url):
            return extract_image_links_from_html(response.text, final_url)

        soup = BeautifulSoup(response.text, "html.parser")

        valid_exts = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".tif")
        image_links = []

        for img in soup.find_all("img"):
            src = img.get("src")
            if not src:
                continue

            full_url = requests.compat.urljoin(url, src)

            # only add if matches allowed extensions (skip .svg etc.)
            if full_url.lower().endswith(valid_exts):
                image_links.append(full_url)

        return image_links

    except Exception as e:
        print(f"Error: {e}")
        return []
