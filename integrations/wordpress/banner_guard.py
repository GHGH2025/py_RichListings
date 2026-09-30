"""
banner_guard.py - detect marketing banners / logos that are NOT property photos.

Why: some source emails carry only a campaign cover banner or a company logo.
sync_poster used to send that as featured_image, so a listing looked complete
while showing another company logo instead of the house.

Three signals, cheapest first. Any one of them marks the image as a banner.
Fail-open: if a signal cannot be evaluated, it does not block.
"""
from __future__ import annotations
import re, struct, logging
from typing import Optional, Tuple
import requests

HDR_BYTES   = 32768
FETCH_TIMEOUT = 8

# a house photo is roughly landscape or portrait; a banner is a long strip
MAX_ASPECT  = 2.5      # width/height above this -> banner
MIN_HEIGHT  = 300      # shorter than this -> banner/logo
MIN_WIDTH   = 300

EMAIL_CDN = re.compile(
    r"(constantcontact|mcusercontent|mcauto-images|campaign-image|mlcdn|stratus|"
    r"sendgrid|mailchimp|hubspot|activecampaign)", re.I)
BANNERISH = re.compile(r"(cover|banner|header|logo|footer|masthead|signature|badge)", re.I)
DIMS_IN_NAME = re.compile(r"(\d{2,4})\s*[xX]\s*(\d{2,4})")


def _dims_from_bytes(b: bytes) -> Optional[Tuple[int, int]]:
    """Read width/height from the file header. No external image library."""
    try:
        if b[:8] == b"\x89PNG\r\n\x1a\n" and b[12:16] == b"IHDR":
            w, h = struct.unpack(">II", b[16:24]); return int(w), int(h)
        if b[:3] == b"GIF":
            w, h = struct.unpack("<HH", b[6:10]); return int(w), int(h)
        if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
            if b[12:16] == b"VP8X":
                w = int.from_bytes(b[24:27], "little") + 1
                h = int.from_bytes(b[27:30], "little") + 1
                return w, h
            if b[12:16] == b"VP8 ":
                return int.from_bytes(b[26:28], "little") & 0x3FFF, int.from_bytes(b[28:30], "little") & 0x3FFF
            if b[12:16] == b"VP8L":
                n = int.from_bytes(b[21:25], "little")
                return (n & 0x3FFF) + 1, ((n >> 14) & 0x3FFF) + 1
        if b[:2] == b"\xff\xd8":                       # JPEG: walk the segments
            i = 2; n = len(b)
            while i + 9 < n:
                if b[i] != 0xFF: i += 1; continue
                m = b[i+1]
                if m in (0xC0,0xC1,0xC2,0xC3,0xC5,0xC6,0xC7,0xC9,0xCA,0xCB,0xCD,0xCE,0xCF):
                    h, w = struct.unpack(">HH", b[i+5:i+9]); return int(w), int(h)
                if m in (0xD8,0xD9) or 0xD0 <= m <= 0xD7: i += 2; continue
                seg = struct.unpack(">H", b[i+2:i+4])[0]; i += 2 + seg
    except Exception:
        pass
    return None


def image_dimensions(url: str) -> Optional[Tuple[int, int]]:
    try:
        r = requests.get(url, timeout=FETCH_TIMEOUT, stream=True,
                         headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            r.close(); return None
        buf = b""
        for chunk in r.iter_content(4096):
            buf += chunk
            if len(buf) >= HDR_BYTES: break
        r.close()
        return _dims_from_bytes(buf)
    except Exception:
        return None


def _url_used_elsewhere(url: str, current_id) -> int:
    """How many OTHER distinct addresses already use this exact image."""
    try:
        from models import ParsedListing
        addrs = set()
        for d in ParsedListing.objects(images=url).only("address", "id"):
            if current_id is not None and d.id == current_id:
                continue
            a = (getattr(d, "address", "") or "").strip().lower()
            if a: addrs.add(a)
        return len(addrs)
    except Exception:
        return 0


def looks_like_banner(url: str, pl=None, check_reuse: bool = True) -> Tuple[bool, str]:
    """Return (is_banner, reason). Fail-open: unknown -> (False, "")."""
    if not url:
        return False, ""

    name = url.split("/")[-1].split("?")[0]

    # signal 1 - dimensions in the filename (cheapest, no network)
    m = DIMS_IN_NAME.search(name)
    if m:
        w, h = int(m.group(1)), int(m.group(2))
        if h and (w / h > MAX_ASPECT or h < MIN_HEIGHT):
            return True, f"filename_dims {w}x{h}"

    # signal 2 - email CDN host AND banner-ish word in the filename
    host = url.split("/")[2] if url.startswith("http") and len(url.split("/")) > 2 else ""
    if EMAIL_CDN.search(host) and BANNERISH.search(name):
        return True, f"email_cdn_bannerword {host}"

    # signal 3 - real dimensions from the file header
    dims = image_dimensions(url)
    if dims:
        w, h = dims
        if h and w and (w / h > MAX_ASPECT or h < MIN_HEIGHT or w < MIN_WIDTH):
            return True, f"dims {w}x{h}"

    # signal 4 - the same image already sits on another address
    if check_reuse:
        n = _url_used_elsewhere(url, getattr(pl, "id", None) if pl is not None else None)
        if n >= 1:
            return True, f"reused_on_{n}_other_addresses"

    return False, ""
