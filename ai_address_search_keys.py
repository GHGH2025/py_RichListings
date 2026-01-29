# ai_address_search_keys.py
import os, json
from typing import List, Optional
from openai import OpenAI
from bson import ObjectId
from models import ParsedListing
import logging

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.1")
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

def _response_format():
    # Top-level must be an OBJECT per OpenAI
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "address_search_keys",
            "strict": True,
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "search_keys": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 25
                    }
                },
                "required": ["search_keys"]
            }
        }
    }

# _SYSTEM_PROMPT = """\
# You will output ONLY a JSON object with a single key "search_keys", whose value is an array of strings.
# Each string must be a STREET+CITY search key derived from the input US address.

# Rules:
# - Do NOT include state or ZIP unless they are literally part of a street or city name. Prefer "street, city" only.
# - Normalize & create variants:
#   • Directionals: North/N./N → N; Northeast → NE; South → S; Southeast → SE; West → W; etc.
#   • Street types: Street↔St, Avenue↔Ave, Road↔Rd, Drive↔Dr, Court↔Ct, Terrace↔Ter, Place↔Pl,
#     Boulevard↔Blvd, Lane↔Ln, Circle↔Cir, Parkway↔Pkwy, Highway↔Hwy.
#   • City shorthands/expansions where commonly used: Beach↔Bch, Gardens↔Gdns, Springs↔Spgs, Terrace↔Terr,
#     Heights↔Hgts, Saint↔St, Fort↔Ft, Lake↔Lk, Mount↔Mt.
# - Remove country text (e.g., "USA", "United States").
# - Include both compact and expanded directionals when applicable (e.g., "NE" and "Northeast").
# - Keep 3–15 high-signal unique combinations, ordered best-first (most standard/likely first).
# - Output must be exactly: {"search_keys": ["<street, city>", ...]} with no other keys, no comments.
# """

# _SYSTEM_PROMPT = """\
# You will output ONLY a JSON object with a single key "search_keys", whose value is an array of strings.
# Each string must be a STREET+CITY search key derived from the input US address.

# OUTPUT FORMAT
# - Output exactly: {"search_keys": ["<street, city>", ...]} with no other keys.

# WHAT TO RETURN
# - Each entry must be "street, city" ONLY (no state, ZIP, or country).
# - Generate the FULL cross-product of permitted variants (see rules), then de-duplicate and return all.
# - Order from most standard to less standard.

# STRICT VARIANT RULES

# A) Directionals (DO NOT INVENT NEW ONES)
#   • If input has a COMPOUND directional (NW, NE, SW, SE):
#       - Produce exactly these two forms:
#         1) the original (e.g., "NW")
#         2) its full word ("Northwest")
#       - Do NOT convert to single-letter (e.g., "N"), do NOT split into "North" or "West",
#         do NOT create "N Northwest", and do NOT change the diagonal.
#   • If input has a SINGLE-LETTER directional (N, S, E, W):
#       - Produce exactly these two forms:
#         1) the original (e.g., "N")
#         2) its full word (e.g., "North")
#       - Do NOT convert to compound, and do NOT remove it.
#   • If NO directional exists in input, do NOT add any.

# B) Street types (ONLY these pairs; preserve street number)
#   Street↔St, Avenue↔Ave, Road↔Rd, Drive↔Dr, Court↔Ct, Terrace↔Ter, Place↔Pl,
#   Boulevard↔Blvd, Lane↔Ln, Circle↔Cir, Parkway↔Pkwy, Highway↔Hwy.
#   • If the input has a recognizable type from the list above, produce both the full and abbreviated form.
#   • If the input has a type not in the list, keep it as-is (no change).

# C) City variants (produce BOTH full and abbreviation when applicable)
#   Allowed pairs (full ↔ abbrev), case-insensitive, matching FULL city words only:
#     Beach↔Bch, Gardens↔Gdns, Springs↔Spgs, Saint↔St, Fort↔Ft, Mount↔Mt
#   • If any of these words appear as full words in the city (e.g., "Pompano Beach"), produce BOTH:
#       - the full form (e.g., "Pompano Beach")
#       - the abbreviated form (e.g., "Pompano Bch")
#   • Apply to every matching word; if multiple words qualify (e.g., "Saint Augustine Beach"), include all combinations:
#       - "St Augustine Beach", "Saint Augustine Beach", "St Augustine Bch", "Saint Augustine Bch"
#   • Do NOT substitute different cities.

# D) Leading token preservation (IMPORTANT)
#   • If the street begins with a leading token before the main street name (e.g., "Xxxx Royal Palm Blvd"),
#     you MUST include BOTH:
#       1) the exact leading token version: "Xxxx Royal Palm Blvd, …"
#       2) a collapsed-first-letter version: "X Royal Palm Blvd, …"
#   • Do NOT drop the leading token and do NOT create other forms beyond those two.

# E) Cleanup & integrity
#   • Remove state/ZIP/country if present. Trim whitespace.
#   • Title case or USPS-like case is fine, but preserve the exact content per the rules.
#   • Never change numbers, street names, or city names beyond allowed variants.

# CROSS-PRODUCT (MAKE SURE YOU INCLUDE ALL)
# - Construct the set as:
#   leading-token ∈ {Exact, First-letter-only (if a leading token exists; otherwise just the original)}
#   × directional ∈ {Original, Full-word (if present); otherwise none}
#   × street-type ∈ {Full, Abbrev (if recognized from the list); otherwise just original}
#   × city ∈ {Full, Abbrev (for each matched city word; include all combos per matched words)}
# - Return the full de-duplicated list.

# PROHIBITED
# - Never add/remove/swap directionals beyond the rules.
# - Never add/remove street types outside the allowed pairs.
# - Never fabricate numbers, names, or cities.
# - Never include state/ZIP/country.

# Return only: {"search_keys": [...]}.
# """

# _SYSTEM_PROMPT = """\
# Return ONLY: {"search_keys": ["<street, city>", ...]}  — no other keys.
# Each entry = "street, city" (no state/ZIP/country). Deduplicate. Order most→least standard.

# VARIANT RULES
# 1) Directionals
#   - If input has NW/NE/SW/SE → include exactly: original (e.g., "NW") and full ("Northwest").
#   - If input has N/S/E/W → include exactly: original (e.g., "N") and full ("North").
#   - If none present → do NOT add any. Never swap/change diagonal. Never make "N Northwest".

# 2) Street types (toggle full↔abbr only if in list)
#   Street↔St, Avenue↔Ave, Road↔Rd, Drive↔Dr, Court↔Ct, Terrace↔Ter, Place↔Pl,
#   Boulevard↔Blvd, Lane↔Ln, Circle↔Cir, Parkway↔Pkwy, Highway↔Hwy.
#   If type not in list → keep as-is.

# 3) City word variants (apply to full words only; produce both full and abbr)
#   Beach↔Bch, Gardens↔Gdns, Springs↔Spgs, Saint↔St, Fort↔Ft, Mount↔Mt.
#   If multiple match (e.g., "Saint Augustine Beach") → include all combos of matched words.
#   Do NOT substitute a different city.

# 4) Leading token (if present before main street name, e.g., "Xxxx Royal Palm Blvd")
#   Include BOTH:
#     • exact leading token version
#     • collapsed first-letter version ("X")
#   Do NOT drop or invent other forms.

# CROSS-PRODUCT
# - Combine: {leading-token form(s)} × {directional original/full if present} ×
#            {street-type full/abbr if recognized} × {city full/abbr combos if applicable}.
# - Then dedupe and output.

# NEVER
# - Add/remove/swap directionals beyond above.
# - Never Remove steet number
# - Add/remove street types beyond the list.
# - Change numbers or names.
# - Include state/ZIP/country.
# """

# _SYSTEM_PROMPT = """\
# Return ONLY: {"search_keys": ["<street, city>", ...]} — no other keys.
# Each entry = "street, city" (no state/ZIP/country). Deduplicate. Order most→least standard.

# RULES

# 0) House-number masks (digits + X/x, e.g., "137XX")
# - Keep the exact original (normalize X to uppercase).
# - Also add ONE collapsed-X form where any run of Xs becomes a single "X" (e.g., "137XX" → "137X").
# - Never drop digits or collapse to a single digit (e.g., never "1").

# 1) Directionals (do NOT invent)
# - If NW/NE/SW/SE → include exactly: original (e.g., "NW") AND full word ("Northwest").
# - If N/S/E/W → include exactly: original (e.g., "N") AND full word ("North").
# - If none present → do not add any. Never change the diagonal or create "N Northwest".

# 2) Street types (toggle only these pairs)
# Street↔St, Avenue↔Ave, Road↔Rd, Drive↔Dr, Court↔Ct, Terrace↔Ter, Place↔Pl,
# Boulevard↔Blvd, Lane↔Ln, Circle↔Cir, Parkway↔Pkwy, Highway↔Hwy.
# - If a listed type appears, output both full and abbrev; otherwise keep as-is.

# 3) City word variants (match whole words only; output both)
# Beach↔Bch, Gardens↔Gdns, Springs↔Spgs, Saint↔St, Fort↔Ft, Mount↔Mt.
# - If multiple match (e.g., “Saint Augustine Beach”), include all combinations.
# - Do not substitute a different city.

# 4) Leading non-numeric token (e.g., “Xxxx Royal Palm Blvd”)
# - Output BOTH: the exact token form, and a first-letter-only collapsed form (“X …”).
# - Do not drop it or invent additional forms.
# - (This rule is for word tokens, not for numeric+X masks—see Rule 0.)

# 5) Integrity
# - Remove state/ZIP/country. Trim whitespace.
# - Never change numbers or names beyond allowed variants.
# - Do not add/remove/swap directionals beyond Rule 1.

# CROSS-PRODUCT
# {house-number mask variants, if any} × {leading-token variants, if any} ×
# {directional original/full, if any} × {street-type full/abbrev, if applicable} ×
# {city full/abbrev combos, if applicable}.
# Deduplicate and return exactly: {"search_keys": [...]}.
# """

_SYSTEM_PROMPT = """\
Return ONLY: {"search_keys": ["<street, city>", ...]} — no other keys.
Each entry = "street, city" (no state/ZIP/country). Deduplicate. Order most→least standard.

RULES

0) House-number masks (digits + X/x, e.g., "137XX")
- Keep the exact original (normalize X→uppercase).
- Also add ONE collapsed-X form where any run of Xs becomes a single "X" (e.g., "137XX"→"137X").
- Never drop digits or collapse to a single digit (e.g., never "1").

1) Directionals (do NOT invent)
- If NW/NE/SW/SE → include exactly: original (e.g., "NW") AND full word ("Northwest").
- If N/S/E/W → include exactly: original (e.g., "N") AND full word ("North").
- If none present → do not add any. Never change diagonal or create "N Northwest".

2) Street types (toggle only these pairs)
Street↔St, Avenue↔Ave, Road↔Rd, Drive↔Dr, Court↔Ct, Terrace↔Ter, Place↔Pl,
Boulevard↔Blvd, Lane↔Ln, Circle↔Cir, Parkway↔Pkwy, Highway↔Hwy.
- If a listed type appears, output both full and abbrev; otherwise keep as-is.

2A) Ordinal street-number toggle (NOT the house number)
- Identify a numeric token immediately before the street-type token (e.g., "NE 12 Dr", "SW 11th Ave").
- Produce BOTH forms for that token:
  • cardinal ↔ ordinal (12 ↔ 12th, 11 ↔ 11th, 1 ↔ 1st, 2 ↔ 2nd, 3 ↔ 3rd; 11/12/13 always "th").
- Apply this ONLY to the street-number-in-name (the token before Street/Ave/Dr/etc.), NEVER to the house number or masks from Rule 0.
- If the input already has an ordinal, also include the cardinal form; if it has a cardinal, also include the ordinal form.

3) City word variants (match whole words only; output both)
Beach↔Bch, Gardens↔Gdns, Springs↔Spgs, Saint↔St, Fort↔Ft, Mount↔Mt.
- If multiple match (e.g., “Saint Augustine Beach”), include all combinations.
- Do not substitute a different city.

4) Leading non-numeric token (e.g., “Xxxx Royal Palm Blvd”)
- Output BOTH: the exact token form, and a first-letter-only collapsed form (“X”).
- Do not drop it or invent additional forms.
- (This rule is for word tokens, not for numeric+X masks—see Rule 0.)

5) Integrity
- Remove state/ZIP/country. Trim whitespace.
- Never change numbers or names beyond allowed variants.
- DO NOT add/remove/swap directionals beyond Rule 1.
- If Input have the house/street number MUST be present in every output variant.

CROSS-PRODUCT
{house-number mask variants, if any} × {leading-token variants, if any} ×
{directional original/full, if any} × {street-type full/abbrev, if applicable} ×
{ordinal/cardinal for the street-number-in-name, if applicable} ×
{city full/abbrev combos, if applicable}.
Deduplicate and return exactly: {"search_keys": [...]}.
"""



_USER_TEMPLATE = "ADDRESS:\n{addr}\n"



def ai_address_search_keys(address_str: str, model: Optional[str] = None) -> List[str]:
    chat = client.chat.completions.create(
        model=(model or OPENAI_MODEL),
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user",    "content": _USER_TEMPLATE.format(addr=address_str)}
        ],
        # temperature=0.0,
        response_format=_response_format()
    )

    data = json.loads(chat.choices[0].message.content)  # {"search_keys": [...]}
    arr = data.get("search_keys", []) if isinstance(data, dict) else []

    # Dedupe & tidy
    seen = set()
    out: List[str] = []
    for s in arr:
        if not isinstance(s, str):
            continue
        t = s.strip()
        if not t:
            continue
        k = t.lower()
        if k in seen:
            continue
        # enforce "street, city" only (defensive trim if the model slips in state/zip)
        # If a comma exists, take only first two comma-separated chunks.
        parts = [p.strip() for p in t.split(",")]
        if len(parts) >= 2:
            t = f"{parts[0]}, {parts[1]}"
        seen.add(k)
        out.append(t)

    # Final clamp
    return out[:20]

# Convenience alias
def address_keys(address_str: str) -> List[str]:
    return ai_address_search_keys(address_str)


def _call_ai_address_keys(addr: str, city: str) -> List[str]:
    """
    Call your AI generator to expand address variants.
    Always returns a de-duplicated list (may be empty).
    """
    try:
        # If you have a different import name, adjust this call:
        resp = address_keys(f"{addr}, {city}")
        if isinstance(resp, dict):
            keys = resp.get("search_keys", []) or []
        elif isinstance(resp, list):
            keys = resp
        else:
            keys = []
    except Exception as e:
        logging.warning("address_keys() failed for %r, %r: %s", addr, city, e)
        keys = []

    # Always include the canonical "<addr>, <city>" too
    if addr and city:
        keys.append(f"{addr}, {city}")

    # De-dup while preserving order
    out, seen = [], set()
    for k in (x.strip() for x in keys if isinstance(x, str)):
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def update_parsed_listing_address_keys(listing_id: str, addr: str, city: str) -> bool:
    """
    Resolve and save address_search_keys for the ParsedListing(_id=listing_id).
    Returns True on success, False otherwise. No exceptions bubble up.
    """
    if not (listing_id and addr and city):
        return False

    try:
        oid = ObjectId(listing_id)
    except Exception:
        logging.warning("Invalid listing_id for address keys: %r", listing_id)
        return False

    try:
        keys = _call_ai_address_keys(addr, city)
        if not keys:
            return False

        ParsedListing.objects(id=oid).update_one(
            set__address_search_keys=keys
        )
        return True
    except Exception as e:
        logging.warning("Failed to update address_search_keys for %s: %s", listing_id, e)
        return False

