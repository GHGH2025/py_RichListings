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

def _strip_for_ai(html: str) -> str:
    """
    Produce an AI-friendly HTML: remove scripts, styles, comments, extra whitespace.
    Keeps <img> tags as-is (since your images are already in the HTML).
    """
    if not html:
        return html
    # remove script/style blocks
    html = re.sub(r"(?is)<script[^>]*>.*?</script>", "", html)
    html = re.sub(r"(?is)<style[^>]*>.*?</style>", "", html)
    # remove comments
    html = re.sub(r"(?is)<!--.*?-->", "", html)
    # collapse whitespace between tags/text
    html = re.sub(r"[ \t\f\r\v]+", " ", html)
    html = re.sub(r"\n{2,}", "\n", html)
    return html.strip()

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
