# buyer_submissions_formatter.py
from html import escape
from typing import Dict, Any

def build_property_html(state: Dict[str, Any]) -> str:
    """
    Returns HTML suitable for Podio text fields (format html).
    If property is disabled or missing type -> returns "" (so we leave Podio field blank).
    """
    if not state or not state.get("enabled") or not (state.get("type") or "").strip():
        return ""

    type_val = (state.get("type") or "").strip()
    price_val = (state.get("priceRange") or "").strip()
    prefs: Dict[str, str] = state.get("preferences") or {}

    parts = []
    parts.append(f"<p><b>Selected type:</b> {escape(type_val)}</p>")

    if price_val:
        parts.append(f"<p><b>Price range:</b> {escape(price_val)}</p>")

    # Only include prefs that have a value
    cleaned = [(k, v) for k, v in prefs.items() if (v or "").strip()]
    if cleaned:
        parts.append("<p><b>Special preferences:</b></p>")
        parts.append("<ul>")
        for k, v in sorted(cleaned, key=lambda x: x[0].lower()):
            parts.append(f"<li>{escape(k)}: <b>{escape(v)}</b></li>")
        parts.append("</ul>")

    return "\n".join(parts)


def build_all_property_html(properties: Dict[str, Any]) -> Dict[str, str]:
    """
    keys match frontend: multiFamily, condo, land, commercial, singleFamily, townhouse
    """
    return {
        "multiFamily": build_property_html(properties.get("multiFamily") or {}),
        "condo": build_property_html(properties.get("condo") or {}),
        "land": build_property_html(properties.get("land") or {}),
        "commercial": build_property_html(properties.get("commercial") or {}),
        "singleFamily": build_property_html(properties.get("singleFamily") or {}),
        "townhouse": build_property_html(properties.get("townhouse") or {}),
    }
