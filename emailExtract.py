import base64, re
from typing import Dict, List, Tuple

def _decode_part_data(data: str) -> str:
    try:
        return base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", errors="replace")
    except Exception:
        return ""

def _collect_bodies_from_payload(payload: dict) -> Tuple[List[str], List[str]]:
    """
    Return (text_parts, html_parts) extracted from the full MIME tree.
    Chooses not to fetch attachments; only decodes inline part bodies.
    """
    text_parts: List[str] = []
    html_parts: List[str] = []

    def walk(part: dict):
        mime = (part.get("mimeType") or "").lower()
        body = part.get("body", {}) or {}

        if mime.startswith("multipart/"):
            for p in part.get("parts", []) or []:
                walk(p)
            return

        data = body.get("data")
        if not data:
            return

        decoded = _decode_part_data(data)

        if mime == "text/plain":
            text_parts.append(decoded)
        elif mime == "text/html":
            html_parts.append(decoded)
        else:
            # Some providers use vendor types, but still HTML/text content;
            # if you see this in the wild, you can broaden detection here.
            pass

    walk(payload)
    return (text_parts, html_parts)

def _choose_largest(parts: List[str]) -> str:
    return max(parts, key=lambda s: len(s), default="").strip()

# def _strip_for_ai(html: str) -> str:
#     """
#     Produce an AI-friendly HTML: remove scripts, styles, comments, extra whitespace.
#     Keeps <img> tags as-is (since your images are already in the HTML).
#     """
#     if not html:
#         return html
#     # remove script/style blocks
#     html = re.sub(r"(?is)<script[^>]*>.*?</script>", "", html)
#     html = re.sub(r"(?is)<style[^>]*>.*?</style>", "", html)
#     # remove comments
#     html = re.sub(r"(?is)<!--.*?-->", "", html)
#     # collapse whitespace between tags/text
#     html = re.sub(r"[ \t\f\r\v]+", " ", html)
#     html = re.sub(r"\n{2,}", "\n", html)
#     return html.strip()



def _strip_for_ai(html: str, *, keep_gallery_links: bool = True, max_chars: int = 120_000) -> str:
    """
    Return a tiny, model-friendly HTML but KEEP links:
      - Keep <a href="...">text</a> (href only), unwrap anchors with no href.
      - Remove scripts/styles/head/meta/link/svg/iframe/object/embed/noscript and comments
      - Flatten tables -> block containers, normalize to minimal tag set
      - Drop ALL attributes from all tags except: <img src>, <a href>
      - Collapse whitespace and cap total characters
    """
    if not html:
        return ""

    try:
        from bs4 import BeautifulSoup, Comment

        def _safe_int(x):
            try:
                return int(str(x).strip())
            except Exception:
                return None

        soup = BeautifulSoup(html, "lxml")

        # 1) strip junk blocks
        for tag in soup(["script","style","head","meta","link","svg","form","iframe","object","embed","noscript"]):
            tag.decompose()
        for c in soup.find_all(string=lambda t: isinstance(t, Comment)):
            c.extract()

        # 2) flatten tables to block containers
        for t in soup.find_all(["table","thead","tbody","tfoot","tr","th","td"]):
            t.name = "div"

        # 3) drop/normalize images (keep only src; remove tracking/logos/tiny)
        def _looks_tracking_or_logo(src: str) -> bool:
            s = src.lower()
            return any(k in s for k in [
                "pixel","track","trk","open","beacon","utm_", "qr","qrcode","logo","icon",
                "facebook","instagram","twitter","youtube","linkedin","header","footer","banner"
            ])

        for img in soup.find_all("img"):
            src = (img.get("src") or "").strip()
            w = _safe_int(img.get("width"))
            h = _safe_int(img.get("height"))
            if (w and w <= 40) or (h and h <= 40) or not src or _looks_tracking_or_logo(src):
                img.decompose()
            else:
                img.attrs = {"src": src}

        # 4) anchors: KEEP them; keep only href; unwrap if no href
        for a in soup.find_all("a"):
            href = (a.get("href") or "").strip()
            if href:
                a.attrs = {"href": href}
            else:
                a.unwrap()

        # 5) normalize tag set
        allowed = {"p","br","strong","b","em","i","img","a"}  # <-- include 'a'
        for tag in soup.find_all(True):
            if tag.name in ("div","span"):
                tag.name = "p"
            elif tag.name not in allowed:
                tag.unwrap()

        # 6) drop attributes from remaining tags EXCEPT <img src> and <a href>
        for tag in soup.find_all(True):
            if tag.name == "img":
                tag.attrs = {"src": tag.get("src")} if tag.get("src") else {}
            elif tag.name == "a":
                tag.attrs = {"href": tag.get("href")} if tag.get("href") else {}
                if not tag.attrs:
                    tag.unwrap()
            else:
                tag.attrs = {}

        # 7) remove empty <p>
        for p in soup.find_all("p"):
            if not (p.get_text(strip=True) or p.find("img") or p.find("a")):
                p.decompose()

        # 8) compact + cap
        body = soup.body or soup
        html_min = re.sub(r"^<body[^>]*>|</body>$", "", str(body), flags=re.I)
        html_min = re.sub(r"[ \t\f\r\v]+", " ", html_min)
        html_min = re.sub(r"\n{2,}", "\n", html_min)
        html_min = re.sub(r">\s+<", "><", html_min)

        if len(html_min) > max_chars:
            html_min = html_min[:max_chars] + "\n<!-- TRUNCATED -->"

        return html_min.strip()

    except Exception:
        # Regex fallback that PRESERVES anchors:
        h = html
        # remove junk blocks
        h = re.sub(r"(?is)<(script|style|head|meta|link|svg|iframe|object|embed|noscript)[^>]*>.*?</\1>", "", h)
        h = re.sub(r"(?is)<!--.*?-->", "", h)

        # Normalize anchors to <a href="...">inner</a>
        def _anchor_keep(m):
            url = m.group(2)
            inner = re.sub(r"<[^>]+>", " ", m.group(3))
            inner = re.sub(r"\s+", " ", inner).strip()
            return f'<a href="{url}">{inner}</a>'

        # keep anchors with href
        h = re.sub(r'(?is)<a\b[^>]*href=(["\'])(.*?)\1[^>]*>(.*?)</a>', _anchor_keep, h)
        # unwrap anchors without href
        h = re.sub(r'(?is)<a\b(?![^>]*href=)[^>]*>(.*?)</a>', r'\1', h)

        # drop noisy attributes but NOT href
        h = re.sub(r'\s(?:class|style|id|width|height|onclick|on\w+|data-[\w-]+)="[^"]*"', "", h)
        h = re.sub(r"\s(?:class|style|id|width|height|onclick|on\w+|data-[\w-]+)='[^']*'", "", h)

        # remove obvious tracking/logo images
        h = re.sub(r'(?is)<img[^>]*?(pixel|track|qr|logo|icon|facebook|instagram|twitter|youtube|linkedin)[^>]*>', "", h)

        # flatten tables
        h = re.sub(r"(?i)</?(table|thead|tbody|tfoot|tr|th|td)\b[^>]*>", "<div>", h)

        # compact + cap
        h = re.sub(r"[ \t\f\r\v]+", " ", h)
        h = re.sub(r"\n{2,}", "\n", h)
        h = re.sub(r">\s+<", "><", h)
        if len(h) > max_chars:
            h = h[:max_chars] + "\n<!-- TRUNCATED -->"
        return h.strip()

        
def extract_email_body_simple(msg: dict) -> Dict[str, str]:
    """
    Returns:
      {
        "text": full best-effort plain text,
        "html_full": the longest text/html part (complete email body),
        "html_ai": cleaned HTML for LLM parsing
      }
    """
    payload = msg.get("payload", {}) or {}
    text_parts, html_parts = _collect_bodies_from_payload(payload)

    best_text = _choose_largest(text_parts)
    best_html = _choose_largest(html_parts)

    # Fallback: if no HTML, wrap text so downstream can treat consistently
    if not best_html and best_text:
        best_html = f"<pre>{best_text}</pre>"

    return {
        "text": best_text,
        "html_full": best_html,
        "html_ai": _strip_for_ai(best_html),
    }