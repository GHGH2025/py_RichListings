from __future__ import annotations

from html import escape

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from models import ParsedListing

router = APIRouter(tags=["needs-photo"])

# The reader is Ekta on the client side - plain English, no jargon.
# Keys are the raw reasons the image curator writes into skipped_images.
REASON_MAP = {
    "marketing text tile": "A marketing graphic with text on it, not a photo of the house",
    "headshot": "A photo of a person (agent or owner), not the property",
    "logo": "A company or agency logo",
    "banner": "A banner or advert header",
    "marketing banner": "A banner or advert header",
    "floor plan": "A floor plan drawing, not a photo",
    "map": "A map or location graphic",
    "generic map": "A map or location graphic",
    "text only": "An image containing only text",
    "text tile": "An image containing only text",
    "watermark": "The image is covered by a watermark",
    "placeholder image": "A placeholder image, no real photo",
    "agent card": "An agent's contact card, not the property",
    "not a genuine property photo": "Not a genuine photo of the property",
    "wrong_property": "This photo appears to be of a different property",
}


def _human_reason(raw: str) -> str:
    key = (raw or "").strip().lower()
    if key in REASON_MAP:
        return REASON_MAP[key]
    # a vision call that errored is not a judgement about the picture
    if key.startswith("vision_error"):
        return "We could not check this image (temporary technical error)"
    return f"Rejected automatically: {raw}" if raw else "Rejected automatically"


def _fmt_price(price) -> str:
    if price is None:
        return "—"
    try:
        return f"${price:,.0f}"
    except (TypeError, ValueError):
        return "—"


def _fmt_date(dt) -> str:
    if dt is None:
        return "—"
    return dt.strftime("%d %b %Y, %H:%M UTC")


@router.get("/needs-photo", response_class=HTMLResponse)
def needs_photo_page():
    rows = list(ParsedListing.objects(status="needs_photo").order_by("-updated_at"))

    cards = ""
    for r in rows:
        address_full = ", ".join(
            filter(None, [r.address, r.city, r.state, r.zip])
        )

        skipped = r.skipped_images or []
        if isinstance(skipped, dict):
            skipped = [skipped]

        images_html = ""
        for img in skipped:
            if not isinstance(img, dict):
                continue
            url = (img.get("url") or "").strip()
            reason = _human_reason(img.get("reason", ""))
            if url:
                # addresses and urls come from third-party emails - always escape
                images_html += f"""
            <div class="img-block">
                <img src="{escape(url, quote=True)}" alt="rejected image" onerror="this.style.display='none'">
                <p class="reason">&#9888; {escape(reason)}</p>
            </div>"""

        if not images_html:
            images_html = '<p class="no-img">No rejected image was kept</p>'

        cards += f"""
        <div class="card">
            <div class="address">{escape(address_full)}</div>
            <div class="price">{_fmt_price(r.price)}</div>
            <div class="date">Flagged: {_fmt_date(r.updated_at)}</div>
            {images_html}
        </div>"""

    if not cards:
        cards = '<p class="empty">Nothing is waiting for a photo right now.</p>'

    count = len(rows)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Needs a photo ({count})</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; background: #f0f2f5; color: #222; padding: 16px; }}
  h1 {{ font-size: 1.15rem; font-weight: 700; margin-bottom: 6px; color: #333; }}
  .sub {{ font-size: 0.85rem; color: #777; margin-bottom: 16px; line-height: 1.45; }}
  .card {{ background: #fff; border-radius: 12px; padding: 16px; margin-bottom: 16px;
           box-shadow: 0 1px 4px rgba(0,0,0,.1); }}
  .address {{ font-size: 1rem; font-weight: 600; margin-bottom: 4px; }}
  .price {{ font-size: 1.1rem; color: #1a7f37; font-weight: 700; margin-bottom: 4px; }}
  .date {{ font-size: 0.78rem; color: #999; margin-bottom: 12px; }}
  .img-block {{ margin-top: 8px; }}
  .img-block img {{ width: 100%; border-radius: 8px; max-height: 260px; object-fit: cover; display: block; }}
  .reason {{ margin-top: 6px; font-size: 0.82rem; color: #b03030; background: #fdf0ef;
             padding: 6px 10px; border-radius: 6px; line-height: 1.4; }}
  .no-img {{ font-size: 0.82rem; color: #aaa; font-style: italic; }}
  .empty {{ text-align: center; color: #aaa; margin-top: 60px; font-size: 0.95rem; }}
</style>
</head>
<body>
<h1>Listings waiting for a photo ({count})</h1>
<p class="sub">These listings were held back because the only image we found was not a photo of the property.
The picture below is what we rejected, and why. They are not published until a photo is added.</p>
{cards}
</body>
</html>"""

    return HTMLResponse(content=html)
