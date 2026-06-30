"""
Shared buyer-form special preference labels, email extraction hints, and matching helpers.

Used by:
  - pipeline/listing_details.py  (extract from email at parse time)
  - buyers/matching_api.py       (buyer ↔ listing automation)
  - ai/whatsapp_posts.py         (strip from WhatsApp payload — not shown in posts)
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Set, Tuple

# Canonical labels — must match react_RichBuyerInfoFrontEnd/constants.ts PROPERTY_CONFIG prefs
SPECIAL_PREF_LABELS: List[str] = [
    "$1 Million Dollar Houses and Up",
    "40 Year Inspection Failed",
    "40/10 Year Inspection Certificate Failed",
    "40/10 Year Inspection Certificate Passed",
    "55 Plus Communities",
    "Bulk Property Packages",
    "Eviction Needed/ In Progress",
    "Frame Construction",
    "Garage",
    "Located on Beach Front Only",
    "Located on Golf Course Only",
    "Located on Ocean Access / Intracoastal Way Only",
    "Located on Water Front Only",
    "Mobile Homes",
    "Mold Remediation Needed",
    "Need to Buy Property Sight Unseen (Bad Tenants, Other Access Issues) - Videos or Pictures might be available case by case.",
    "NO HOA",
    "Pool",
    "Post Occupancy Required (with escrow holdback and/ or rent)",
    "Property has Code Violations / Liens / Fines",
    "Property has Foundation / Structural Issues",
    "Property has Rental Restrictions",
    "Property has Special Assessments",
    "Property is Fire Damaged",
    "Property Needs a Full Rehab",
    "Property with Ocean Access / Intracoastal",
    "Tear-downs / Land Value Only",
    "Unpermitted Additions",
    "Water/ Flood Damage",
]

def _norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s


def _slug(s: str) -> str:
    s = _norm(s)
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s


# Build maps after _slug is defined
LABEL_TO_KEY = {lbl: _slug(lbl) for lbl in SPECIAL_PREF_LABELS}
KEY_TO_LABEL = {v: k for k, v in LABEL_TO_KEY.items()}

# Shorter manual / admin aliases
MANUAL_ALIASES: Dict[str, str] = {
    _slug("property needs a full rehab"): LABEL_TO_KEY["Property Needs a Full Rehab"],
    _slug("property has code violations"): LABEL_TO_KEY["Property has Code Violations / Liens / Fines"],
    _slug("property is fire damaged"): LABEL_TO_KEY["Property is Fire Damaged"],
    _slug("no hoa"): LABEL_TO_KEY["NO HOA"],
    _slug("mobile homes"): LABEL_TO_KEY["Mobile Homes"],
    _slug("water/ flood damage"): LABEL_TO_KEY["Water/ Flood Damage"],
    _slug("mold remediation needed"): LABEL_TO_KEY["Mold Remediation Needed"],
    _slug("foundation / structural issues"): LABEL_TO_KEY["Property has Foundation / Structural Issues"],
    _slug("frame construction"): LABEL_TO_KEY["Frame Construction"],
    _slug("sight unseen"): LABEL_TO_KEY[
        "Need to Buy Property Sight Unseen (Bad Tenants, Other Access Issues) - Videos or Pictures might be available case by case."
    ],
    _slug("ocean access"): LABEL_TO_KEY["Property with Ocean Access / Intracoastal"],
    _slug("55 plus"): LABEL_TO_KEY["55 Plus Communities"],
    _slug("teardown"): LABEL_TO_KEY["Tear-downs / Land Value Only"],
    _slug("unpermitted additions"): LABEL_TO_KEY["Unpermitted Additions"],
}

# Regex patterns per canonical label (conservative — do not invent presence)
SPECIAL_PREF_PATTERNS: Dict[str, List[str]] = {
    LABEL_TO_KEY["Property is Fire Damaged"]: [
        r"\bfire\s*damage(d)?\b",
        r"\bfire[-\s]?damaged\b",
        r"\bsmoke\s*damage\b",
        r"\bburn(?:t|ed)?\s*(?:out|down|damage)?\b",
    ],
    LABEL_TO_KEY["Property Needs a Full Rehab"]: [
        r"\bfull\s+rehab\b",
        r"\bgut\s+rehab\b",
        r"\bneeds\s+(a\s+)?(full\s+)?rehab\b",
        r"\bmajor\s+rehab\b",
        r"\bcomplete\s+rehab\b",
        r"\bcosmetic\s+rehab\b",
        r"\bfixer\s*upper\b",
    ],
    LABEL_TO_KEY["Property has Code Violations / Liens / Fines"]: [
        r"\bcode\s+violation(s)?\b",
        r"\blien(s)?\b",
        r"\bfine(s)?\b",
        r"\bviolation notice\b",
        r"\bopen\s+permit(s)?\b",
    ],
    LABEL_TO_KEY["Property has Foundation / Structural Issues"]: [
        r"\bfoundation\s+(?:issue|problem|damage|crack)",
        r"\bstructural\s+(?:issue|problem|damage|repair)",
        r"\bfoundation\s+repair\b",
        r"\bsinking\s+foundation\b",
    ],
    LABEL_TO_KEY["Frame Construction"]: [
        r"\bframe\s+(?:construction|built|home|house)\b",
        r"\bwood\s+frame\b",
        r"\bwooden\s+frame\b",
        r"\bframe\s+dwelling\b",
    ],
    LABEL_TO_KEY["NO HOA"]: [
        r"\bno\s*hoa\b",
        r"\bwithout\s+hoa\b",
    ],
    LABEL_TO_KEY["Water/ Flood Damage"]: [
        r"\bwater\s+damage\b",
        r"\bflood(?:ing|ed)?\b",
        r"\bwater\s+intrusion\b",
        r"\bflood\s+zone\b",
    ],
    LABEL_TO_KEY["Mold Remediation Needed"]: [
        r"\bmold\b",
        r"\bmould\b",
        r"\bmold\s+remediation\b",
    ],
    LABEL_TO_KEY["Mobile Homes"]: [
        r"\bmobile\s+home(s)?\b",
        r"\bmanufactured\s+home(s)?\b",
    ],
    LABEL_TO_KEY["Pool"]: [
        r"\bpool\b",
        r"\bswimming\s+pool\b",
    ],
    LABEL_TO_KEY["Garage"]: [
        r"\bgarage\b",
        r"\b2\s*car\s+garage\b",
        r"\b1\s*car\s+garage\b",
        r"\bgarage\s+conversion\b",
    ],
    LABEL_TO_KEY["Tear-downs / Land Value Only"]: [
        r"\btear\s*down\b",
        r"\bteardown\b",
        r"\bland\s+value\s+only\b",
        r"\blot\s+value\b",
        r"\bknock\s*down\b",
    ],
    LABEL_TO_KEY["Unpermitted Additions"]: [
        r"\bunpermitted\b",
        r"\bnon[-\s]?permitted\b",
        r"\bwithout\s+permit(s)?\b",
        r"\billegal\s+addition\b",
    ],
    LABEL_TO_KEY["Eviction Needed/ In Progress"]: [
        r"\beviction\b",
        r"\bcash\s+for\s+keys\b",
        r"\btenant\s+won'?t\s+leave\b",
    ],
    LABEL_TO_KEY["55 Plus Communities"]: [
        r"\b55\+\b",
        r"\b55\s*plus\b",
        r"\bactive\s+adult\b",
        r"\bage\s+restricted\b",
        r"\bsenior\s+community\b",
    ],
    LABEL_TO_KEY["Bulk Property Packages"]: [
        r"\bportfolio\b",
        r"\bbulk\b",
        r"\bbundle\b",
        r"\bmultiple\s+properties\b",
        r"\bpackage\s+deal\b",
    ],
    LABEL_TO_KEY[
        "Need to Buy Property Sight Unseen (Bad Tenants, Other Access Issues) - Videos or Pictures might be available case by case."
    ]: [
        r"\bsight\s+unseen\b",
        r"\bno\s+access\b",
        r"\bbad\s+tenant(s)?\b",
        r"\btenant\s+occupied\b",
        r"\boccupied\s+by\s+tenant\b",
        r"\bcan'?t\s+show\b",
    ],
    LABEL_TO_KEY["$1 Million Dollar Houses and Up"]: [
        r"\b1\s*million\b",
        r"\b\$1,?000,?000\b",
        r"\bmillion\s+dollar\b",
    ],
    LABEL_TO_KEY["Property with Ocean Access / Intracoastal"]: [
        r"\bocean\s+access\b",
        r"\bintracoastal\b",
        r"\bICW\b",
        r"\bwaterfront\b",
        r"\bwater\s*front\b",
        r"\bcreek\s+lot\b",
        r"\bon\s+(?:the\s+)?(?:canal|water)\b",
    ],
    LABEL_TO_KEY["Located on Ocean Access / Intracoastal Way Only"]: [
        r"\bocean\s+access\b",
        r"\bintracoastal\b",
        r"\bICW\b",
    ],
    LABEL_TO_KEY["Located on Water Front Only"]: [
        r"\bwaterfront\b",
        r"\bwater\s*front\b",
        r"\bon\s+(?:the\s+)?water\b",
        r"\bcanal\s+front\b",
        r"\bcreek\s+lot\b",
    ],
    LABEL_TO_KEY["Located on Beach Front Only"]: [
        r"\bbeachfront\b",
        r"\bbeach\s*front\b",
        r"\bon\s+(?:the\s+)?beach\b",
    ],
    LABEL_TO_KEY["Located on Golf Course Only"]: [
        r"\bgolf\s+course\b",
        r"\bgolf\s+front\b",
        r"\bon\s+(?:the\s+)?golf\b",
    ],
    LABEL_TO_KEY["Property has Rental Restrictions"]: [
        r"\brental\s+restriction(s)?\b",
        r"\bno\s+rentals?\b",
        r"\bminimum\s+lease\b",
    ],
    LABEL_TO_KEY["Property has Special Assessments"]: [
        r"\bspecial\s+assessment(s)?\b",
        r"\bassessment\s+fee\b",
    ],
    LABEL_TO_KEY["40 Year Inspection Failed"]: [
        r"\b40\s*year\s+inspection\s+fail",
        r"\b40\s*yr\s+inspection\s+fail",
    ],
    LABEL_TO_KEY["40/10 Year Inspection Certificate Failed"]: [
        r"\b40\s*/\s*10\s*year\s+inspection\s+certificate\s+fail",
        r"\b40/10\s+.*fail",
    ],
    LABEL_TO_KEY["40/10 Year Inspection Certificate Passed"]: [
        r"\b40\s*/\s*10\s*year\s+inspection\s+certificate\s+pass",
        r"\b40/10\s+.*pass",
    ],
    LABEL_TO_KEY["Post Occupancy Required (with escrow holdback and/ or rent)"]: [
        r"\bpost\s+occupancy\b",
        r"\bseller\s+rent\s*back\b",
        r"\boccupancy\s+after\s+close\b",
        r"\bescrow\s+holdback\b",
    ],
}

# Short hints for the email-parse AI prompt
EXTRACTION_HINTS: Dict[str, str] = {
    "Property is Fire Damaged": "fire damage, smoke damage, burned",
    "Property with Ocean Access / Intracoastal": "ocean access, intracoastal, ICW, creek lot, canal",
    "55 Plus Communities": "55+, 55 plus, active adult, age restricted, senior community",
    "Need to Buy Property Sight Unseen (Bad Tenants, Other Access Issues) - Videos or Pictures might be available case by case.": "sight unseen, no access, bad tenants, tenant occupied",
    "Frame Construction": "frame construction, wood frame (NOT concrete block / CBS alone)",
    "Mold Remediation Needed": "mold, mould, mold remediation",
    "Property has Foundation / Structural Issues": "foundation issues, structural damage, sinking foundation",
    "Water/ Flood Damage": "water damage, flooding, flood zone",
    "Tear-downs / Land Value Only": "tear down, teardown, land value only, knock down",
    "Unpermitted Additions": "unpermitted, non-permitted, illegal addition",
    "Property Needs a Full Rehab": "full rehab, gut rehab, cosmetic rehab, fixer upper",
    "Property has Code Violations / Liens / Fines": "code violation, lien, fine, open permit",
    "NO HOA": "no HOA, without HOA",
    "Pool": "pool, swimming pool",
    "Garage": "garage, garage conversion",
    "Mobile Homes": "mobile home, manufactured home",
    "Eviction Needed/ In Progress": "eviction, cash for keys",
    "Bulk Property Packages": "portfolio, bulk, package deal, multiple properties",
    "$1 Million Dollar Houses and Up": "price >= $1,000,000 or text says 1 million",
    "Located on Water Front Only": "waterfront, on the water, canal front",
    "Located on Beach Front Only": "beachfront, on the beach",
    "Located on Golf Course Only": "golf course, golf front",
    "Property has Rental Restrictions": "rental restrictions, no rentals",
    "Property has Special Assessments": "special assessment",
    "Post Occupancy Required (with escrow holdback and/ or rent)": "post occupancy, seller rent back, escrow holdback",
}


def build_extraction_prompt_block() -> str:
    lines = [
        "- Populate `special_preferences_detected` with ONLY labels from the allowed list below.",
        "- Include a label ONLY when the email clearly states that feature/condition for THIS listing.",
        "- Do NOT guess. If unclear, omit the label.",
        "- These labels are for internal matching — do NOT copy them into marketing text fields.",
        "",
        "Allowed labels (use EXACT text):",
    ]
    for lbl in SPECIAL_PREF_LABELS:
        hint = EXTRACTION_HINTS.get(lbl, "")
        lines.append(f'  • "{lbl}"' + (f" — look for: {hint}" if hint else ""))
    return "\n".join(lines)


def _labels_to_keys(labels: List[str]) -> Set[str]:
    keys: Set[str] = set()
    for raw in labels or []:
        s = str(raw or "").strip()
        if not s:
            continue
        if s in LABEL_TO_KEY:
            keys.add(LABEL_TO_KEY[s])
            continue
        k = MANUAL_ALIASES.get(_slug(s), _slug(s))
        if k in KEY_TO_LABEL:
            keys.add(k)
    return keys


def collect_listing_text(listing: Dict[str, Any]) -> str:
    ci = listing.get("complete_info") or {}
    if not isinstance(ci, dict):
        ci = {}
    chunks = [
        ci.get("complete_info"),
        ci.get("raw_description_excerpt"),
        listing.get("post_content"),
        " ".join(ci.get("marketing_tags") or []),
    ]
    return "\n".join([str(c) for c in chunks if c]).strip()


def _apply_structured_signals(ci: Dict[str, Any], listing: Dict[str, Any], present: Set[str], evidence: Dict[str, str]) -> None:
    price = ci.get("list_price_usd") or listing.get("price")
    if isinstance(price, (int, float)) and price >= 1_000_000:
        k = LABEL_TO_KEY["$1 Million Dollar Houses and Up"]
        present.add(k)
        evidence.setdefault(k, f"price:{price}")

    if ci.get("has_hoa") is False:
        k = LABEL_TO_KEY["NO HOA"]
        present.add(k)
        evidence.setdefault(k, "has_hoa:false")

    if ci.get("is_mobile_home") is True:
        k = LABEL_TO_KEY["Mobile Homes"]
        present.add(k)
        evidence.setdefault(k, "is_mobile_home:true")

    if ci.get("is_teardown_or_redevelopment") is True:
        k = LABEL_TO_KEY["Tear-downs / Land Value Only"]
        present.add(k)
        evidence.setdefault(k, "is_teardown_or_redevelopment:true")

    build_material = (ci.get("build_material") or "").lower()
    if build_material in ("frame", "wood") or ci.get("is_frame_or_wood") is True:
        k = LABEL_TO_KEY["Frame Construction"]
        present.add(k)
        evidence.setdefault(k, f"build_material:{build_material or 'is_frame_or_wood'}")

    water_feature = (ci.get("water_feature") or "").lower().replace(" ", "_")
    if water_feature in ("oceanfront", "ocean_access", "intracoastal", "canal", "lakefront", "riverfront", "bayfront"):
        for lbl in [
            "Property with Ocean Access / Intracoastal",
            "Located on Ocean Access / Intracoastal Way Only",
            "Located on Water Front Only",
        ]:
            if water_feature in ("ocean_access", "intracoastal", "canal"):
                k = LABEL_TO_KEY[lbl]
                present.add(k)
                evidence.setdefault(k, f"water_feature:{water_feature}")
            elif lbl == "Located on Water Front Only":
                k = LABEL_TO_KEY[lbl]
                present.add(k)
                evidence.setdefault(k, f"water_feature:{water_feature}")

    if ci.get("is_on_water") is True:
        k = LABEL_TO_KEY["Located on Water Front Only"]
        present.add(k)
        evidence.setdefault(k, "is_on_water:true")


def _scan_text(text: str, present: Set[str], evidence: Dict[str, str]) -> None:
    if not text:
        return
    norm_text = _norm(text)
    for key, patterns in SPECIAL_PREF_PATTERNS.items():
        if key in present:
            continue
        for pat in patterns:
            m = re.search(pat, norm_text, flags=re.IGNORECASE)
            if m:
                present.add(key)
                if key not in evidence:
                    start = max(0, m.start() - 25)
                    end = min(len(norm_text), m.end() + 25)
                    evidence[key] = f"text:...{norm_text[start:end]}..."
                break


def detect_listing_special_prefs(listing: Dict[str, Any]) -> Tuple[Set[str], Dict[str, str]]:
    """
    Return canonical pref keys present on a listing.
    Priority: stored extraction → manual admin prefs → structured fields → text scan.
    """
    present: Set[str] = set()
    evidence: Dict[str, str] = {}

    # 0) Pre-extracted at parse time (top-level or legacy nested)
    stored = listing.get("extracted_special_preferences") or []
    if isinstance(stored, list) and stored:
        for k in _labels_to_keys(stored):
            present.add(k)
            evidence.setdefault(k, "extracted_at_parse")

    ci = listing.get("complete_info") or {}
    if isinstance(ci, dict):
        nested = ci.get("special_preferences_detected") or []
        for k in _labels_to_keys(nested if isinstance(nested, list) else []):
            if k not in present:
                present.add(k)
                evidence.setdefault(k, "extracted_in_complete_info")

    # 1) Manual prefs from admin/Podio
    for raw in listing.get("manual_special_preferences_norm") or []:
        k = MANUAL_ALIASES.get(_slug(str(raw)), _slug(str(raw)))
        if k in KEY_TO_LABEL or k in present:
            present.add(k)
            evidence.setdefault(k, f"manual:{raw}")

    # 2) Structured booleans / enums
    if isinstance(ci, dict):
        _apply_structured_signals(ci, listing, present, evidence)

    # 3) Regex text scan (fallback)
    text = collect_listing_text(listing)
    _scan_text(text, present, evidence)

    return present, evidence


def finalize_extracted_special_preferences(listing_dict: Dict[str, Any]) -> List[str]:
    """
    Merge AI-detected labels + regex/structured detection into canonical label list.
    Called at email parse time before saving to ParsedListing.extracted_special_preferences.
    """
    doc = {
        "complete_info": listing_dict,
        "price": listing_dict.get("list_price_usd"),
        "extracted_special_preferences": listing_dict.get("special_preferences_detected") or [],
    }
    present, _ = detect_listing_special_prefs(doc)
    labels = sorted({KEY_TO_LABEL[k] for k in present if k in KEY_TO_LABEL})
    return labels


def normalize_buyer_prefs_kv(preferences_kv: List[Dict[str, str]]) -> List[Dict[str, str]]:
    out = []
    for kv in preferences_kv or []:
        label = (kv.get("label") or "").strip()
        value = _norm(kv.get("value") or "")
        if not label or not value:
            continue
        key = LABEL_TO_KEY.get(label, _slug(label))
        out.append({"key": key, "label": label, "value": value})
    return out


def build_preference_checks(
    buyer_prefs_kv: List[Dict[str, str]],
    listing_present: Set[str],
    listing_evidence: Dict[str, str],
) -> List[Dict[str, Any]]:
    checks: List[Dict[str, Any]] = []
    for p in normalize_buyer_prefs_kv(buyer_prefs_kv):
        status = "PRESENT" if p["key"] in listing_present else "ABSENT"
        checks.append({
            "label": p["label"],
            "selection": p["value"],
            "status": status,
            "confidence_0_to_1": 1.0,
            "evidence": listing_evidence.get(p["key"], ""),
        })
    return checks


def apply_special_preference_rules(pref_checks: List[Dict[str, Any]]) -> Tuple[bool, float, List[str], List[str]]:
    """
    Buyer Yes / Maybe / Only / No rules against listing presence.

    - No + listing has feature → DISQUALIFY
    - Only → at least one Only must be PRESENT
    - Yes/Maybe → at least one Yes or Maybe must be PRESENT (when any Yes/Maybe set)
    - Only No selections and none matched → qualify
    """
    checks = pref_checks or []

    def sel(c: Dict[str, Any]) -> str:
        return str(c.get("selection") or "").strip().lower()

    def status(c: Dict[str, Any]) -> str:
        return str(c.get("status") or "").strip().upper()

    no_checks = [c for c in checks if sel(c) == "no"]
    yes_checks = [c for c in checks if sel(c) == "yes"]
    maybe_checks = [c for c in checks if sel(c) == "maybe"]
    only_checks = [c for c in checks if sel(c) == "only"]

    reasons: List[str] = []
    failed: List[str] = []

    no_present = [c for c in no_checks if status(c) == "PRESENT"]
    if no_present:
        for c in no_present:
            failed.append(f"Disqualified: 'No' matched (PRESENT): {c.get('label')}")
        return False, 1.0, reasons, failed

    if only_checks:
        present_only = [c for c in only_checks if status(c) == "PRESENT"]
        if present_only:
            matched = [str(c.get("label") or "").strip() for c in present_only if c.get("label")]
            reasons.append("Qualified: at least one 'Only' matched (PRESENT)" + (f" -> {', '.join(matched)}" if matched else ""))
            return True, 1.0, reasons, failed
        failed.append("Disqualified: 'Only' selections provided but NONE matched (PRESENT)")
        return False, 1.0, reasons, failed

    if yes_checks or maybe_checks:
        present_yes = [c for c in yes_checks if status(c) == "PRESENT"]
        present_maybe = [c for c in maybe_checks if status(c) == "PRESENT"]
        if present_yes or present_maybe:
            if present_yes:
                reasons.append(
                    "Qualified: at least one 'Yes' matched (PRESENT) -> "
                    + ", ".join(str(c.get("label")) for c in present_yes)
                )
            if present_maybe:
                reasons.append(
                    "Qualified: at least one 'Maybe' matched (PRESENT) -> "
                    + ", ".join(str(c.get("label")) for c in present_maybe)
                )
            return True, 1.0, reasons, failed
        if yes_checks and maybe_checks:
            failed.append("Disqualified: No 'Yes' or 'Maybe' selections matched (PRESENT)")
        elif yes_checks:
            failed.append("Disqualified: No 'Yes' selections matched (PRESENT)")
        else:
            failed.append("Disqualified: No 'Maybe' selections matched (PRESENT)")
        return False, 1.0, reasons, failed

    reasons.append("Qualified: only 'No' restrictions and none matched")
    return True, 1.0, reasons, failed


def evaluate_buyer_special_prefs(
    buyer_prefs_kv: List[Dict[str, str]],
    listing_present: Set[str],
    listing_evidence: Dict[str, str],
) -> Tuple[bool, List[str], List[str]]:
    prefs = normalize_buyer_prefs_kv(buyer_prefs_kv)
    if not prefs:
        return True, ["no special preferences set"], []

    checks = build_preference_checks(buyer_prefs_kv, listing_present, listing_evidence)
    ok, _conf, reasons, failed = apply_special_preference_rules(checks)
    return ok, reasons, failed
