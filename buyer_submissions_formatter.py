# buyer_submissions_formatter.py
from html import escape
from typing import Dict, Any, List, Tuple


def _clean_list(vals: Any) -> List[str]:
    if not vals:
        return []
    if isinstance(vals, list):
        return [str(v).strip() for v in vals if str(v).strip()]
    # if string accidentally passed
    s = str(vals).strip()
    return [s] if s else []


def _normalize_price_ranges(state: Dict[str, Any]) -> List[str]:
    # NEW (array)
    arr = _clean_list(state.get("priceRanges"))
    if arr:
        return arr
    # LEGACY (string)
    legacy = str(state.get("priceRange") or "").strip()
    return [legacy] if legacy else []


def build_property_html(state: Dict[str, Any]) -> str:
    """
    Returns HTML suitable for Podio text fields (format html).
    Works with NEW structure + keeps legacy support.
    """
    if not state or not state.get("enabled") or not (state.get("type") or "").strip():
        return ""

    type_val = (state.get("type") or "").strip()

    # NEW: priceRanges array; LEGACY: priceRange string
    price_ranges: List[str] = list(state.get("priceRanges") or [])
    if not price_ranges and (state.get("priceRange") or "").strip():
        price_ranges = [(state.get("priceRange") or "").strip()]
    price_ranges = [x.strip() for x in price_ranges if (x or "").strip()]

    beds = [x.strip() for x in (state.get("beds") or []) if (x or "").strip()]
    baths = [x.strip() for x in (state.get("baths") or []) if (x or "").strip()]

    loc = state.get("location") or {}
    scope = (loc.get("scope") or "").strip()
    counties = [c.strip() for c in (loc.get("counties") or []) if (c or "").strip()]

    prefs: Dict[str, str] = state.get("preferences") or {}
    other_type = (state.get("otherType") or "").strip()

    parts = []
    parts.append(f"<p><b>Selected type:</b> {escape(type_val)}</p>")

    if price_ranges:
        parts.append(f"<p><b>Price ranges:</b> {escape(', '.join(price_ranges))}</p>")

    if beds:
        parts.append(f"<p><b>Beds:</b> {escape(', '.join(beds))}</p>")

    if baths:
        parts.append(f"<p><b>Baths:</b> {escape(', '.join(baths))}</p>")

    # show location if present
    if scope:
        parts.append(f"<p><b>Location scope:</b> {escape(scope)}</p>")
    if counties:
        parts.append(f"<p><b>Counties:</b> {escape(', '.join(counties))}</p>")

    if other_type:
        parts.append(f"<p><b>Other type:</b> {escape(other_type)}</p>")

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


def build_counties_html(state: Dict[str, Any]) -> str:
    """
    Dedicated HTML for the new Podio "Counties for X" fields.
    Returns "" if property is disabled.
    """
    if not state or not state.get("enabled"):
        return ""

    loc = state.get("location") or {}
    scope = str(loc.get("scope") or "").strip()
    counties = _clean_list(loc.get("counties"))

    parts: List[str] = []
    if scope:
        parts.append(f"<p><b>Scope:</b> {escape(scope)}</p>")
    if counties:
        parts.append(f"<p><b>Counties:</b> {escape(', '.join(counties))}</p>")

    return "\n".join(parts).strip()


def build_all_counties_html(properties: Dict[str, Any]) -> Dict[str, str]:
    return {
        "multiFamily": build_counties_html(properties.get("multiFamily") or {}),
        "condo": build_counties_html(properties.get("condo") or {}),
        "land": build_counties_html(properties.get("land") or {}),
        "commercial": build_counties_html(properties.get("commercial") or {}),
        "singleFamily": build_counties_html(properties.get("singleFamily") or {}),
        "townhouse": build_counties_html(properties.get("townhouse") or {}),
    }


def build_counties_html(state: Dict[str, Any]) -> str:
    """
    Dedicated HTML for the new Podio 'Counties for X' fields.
    """
    if not state or not state.get("enabled") or not (state.get("type") or "").strip():
        return ""

    loc = state.get("location") or {}
    scope = (loc.get("scope") or "").strip()
    counties = [c.strip() for c in (loc.get("counties") or []) if (c or "").strip()]

    parts = []
    if scope:
        parts.append(f"<p><b>Scope:</b> {escape(scope)}</p>")
    if counties:
        parts.append(f"<p><b>Counties:</b> {escape(', '.join(counties))}</p>")
    else:
        # still useful for all_florida
        if scope:
            parts.append("<p><b>Counties:</b> (none selected)</p>")

    return "\n".join(parts)

def build_all_counties_html(properties: Dict[str, Any]) -> Dict[str, str]:
    return {
        "multiFamily": build_counties_html(properties.get("multiFamily") or {}),
        "condo": build_counties_html(properties.get("condo") or {}),
        "land": build_counties_html(properties.get("land") or {}),
        "commercial": build_counties_html(properties.get("commercial") or {}),
        "singleFamily": build_counties_html(properties.get("singleFamily") or {}),
        "townhouse": build_counties_html(properties.get("townhouse") or {}),
    }
