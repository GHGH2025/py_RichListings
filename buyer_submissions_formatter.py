from html import escape
from typing import Dict, Any, List, Optional


def _clean_list(vals: Any) -> List[str]:
    if not vals:
        return []
    if isinstance(vals, list):
        return [str(v).strip() for v in vals if str(v).strip()]
    s = str(vals).strip()
    return [s] if s else []


def _clean_types(vals: Any) -> List[str]:
    # frontend NEW: list[str]
    return _clean_list(vals)


def _normalize_price_ranges(state: Dict[str, Any]) -> List[str]:
    arr = _clean_list(state.get("priceRanges"))
    if arr:
        return arr
    legacy = str(state.get("priceRange") or "").strip()
    return [legacy] if legacy else []


def build_property_html(state: Dict[str, Any]) -> str:
    """
    Returns HTML suitable for Podio text fields.
    Supports NEW structure + legacy.
    """
    if not state or not state.get("enabled"):
        return ""

    types = _clean_types(state.get("type"))
    if not types:
        # legacy might be a string
        legacy_type = str(state.get("type") or "").strip()
        if legacy_type:
            types = [legacy_type]

    if not types:
        return ""

    price_ranges = _normalize_price_ranges(state)
    beds = _clean_list(state.get("beds"))
    baths = _clean_list(state.get("baths"))

    loc = state.get("location") or {}
    scope = str(loc.get("scope") or "").strip()
    counties = _clean_list(loc.get("counties"))  # may be empty now

    prefs: Dict[str, str] = state.get("preferences") or {}
    other_type = str(state.get("otherType") or "").strip()

    parts: List[str] = []
    parts.append(f"<p><b>Selected type(s):</b> {escape(', '.join(types))}</p>")

    if price_ranges:
        parts.append(f"<p><b>Price ranges:</b> {escape(', '.join(price_ranges))}</p>")
    if beds:
        parts.append(f"<p><b>Beds:</b> {escape(', '.join(beds))}</p>")
    if baths:
        parts.append(f"<p><b>Baths:</b> {escape(', '.join(baths))}</p>")

    if scope:
        parts.append(f"<p><b>Location scope:</b> {escape(scope)}</p>")
    if counties:
        parts.append(f"<p><b>Counties:</b> {escape(', '.join(counties))}</p>")

    if other_type:
        parts.append(f"<p><b>Other type:</b> {escape(other_type)}</p>")

    cleaned = [(k, v) for k, v in (prefs or {}).items() if (v or "").strip()]
    if cleaned:
        parts.append("<p><b>Special preferences:</b></p>")
        parts.append("<ul>")
        for k, v in sorted(cleaned, key=lambda x: x[0].lower()):
            parts.append(f"<li>{escape(k)}: <b>{escape(v)}</b></li>")
        parts.append("</ul>")

    return "\n".join(parts)


def build_all_property_html(properties: Dict[str, Any]) -> Dict[str, str]:
    return {
        "multiFamily": build_property_html(properties.get("multiFamily") or {}),
        "condo": build_property_html(properties.get("condo") or {}),
        "land": build_property_html(properties.get("land") or {}),
        "commercial": build_property_html(properties.get("commercial") or {}),
        "singleFamily": build_property_html(properties.get("singleFamily") or {}),
        "townhouse": build_property_html(properties.get("townhouse") or {}),
    }


def build_counties_html(state: Dict[str, Any], global_location: Optional[Dict[str, Any]] = None) -> str:
    """
    Podio 'Counties for X' HTML.
    NEW frontend: counties/cities are global, so we include global counties when provided.
    """
    if not state or not state.get("enabled"):
        return ""

    loc = state.get("location") or {}
    prop_scope = str(loc.get("scope") or "").strip()

    global_location = global_location or {}
    global_scope = str(global_location.get("scope") or "").strip()
    global_counties = _clean_list(global_location.get("counties"))

    parts: List[str] = []
    if prop_scope:
        parts.append(f"<p><b>Property scope:</b> {escape(prop_scope)}</p>")
    if global_scope:
        parts.append(f"<p><b>Global scope:</b> {escape(global_scope)}</p>")
    if global_counties:
        parts.append(f"<p><b>Counties:</b> {escape(', '.join(global_counties))}</p>")

    return "\n".join(parts).strip()


def build_all_counties_html(properties: Dict[str, Any], global_location: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    return {
        "multiFamily": build_counties_html(properties.get("multiFamily") or {}, global_location),
        "condo": build_counties_html(properties.get("condo") or {}, global_location),
        "land": build_counties_html(properties.get("land") or {}, global_location),
        "commercial": build_counties_html(properties.get("commercial") or {}, global_location),
        "singleFamily": build_counties_html(properties.get("singleFamily") or {}, global_location),
        "townhouse": build_counties_html(properties.get("townhouse") or {}, global_location),
    }
