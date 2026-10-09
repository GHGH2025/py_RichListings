import os
import json
import re
from datetime import datetime
from typing import Dict, Any, List, Optional
from openai import OpenAI
from dotenv import load_dotenv
from models import ParsedListing, FilteredListingEmail
from ai.address_keys import update_parsed_listing_address_keys
from integrations.google_formatter import geocode_response, street_city_zip_from_geocode
from services.direct_wholesaler_service import get_wholesaler_map, normalize_email
from pipeline.address_utils import (
    is_bed_bath_descriptor_address,
    resolve_street_address_from_fields,
)
from buyers.special_preferences import (
    build_extraction_prompt_block,
    finalize_extracted_special_preferences,
)
from ingestion.whatsapp import first_http_url, is_jg_equity_group
from media.scrape_images import gallery_url_from_text

from concurrent.futures import ThreadPoolExecutor
import logging


# -------------------------
# CONFIG
# -------------------------
# Load environment variables
load_dotenv()

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-6-luna")  # supports structured outputs
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
# client = OpenAI(api_key=OPENAI_API_KEY)
client = OpenAI(
    api_key=OPENAI_API_KEY,
    timeout=800.0,        # 30s hard timeout for network+read
    max_retries=0        # keep low; you can set 0 or 1
)


def _model_supports_temperature(model: Optional[str]) -> bool:
    """gpt-5* models often reject temperature; omit it for that family."""
    if not model:
        return True
    return not str(model).lower().startswith(("gpt-5", "gpt-6"))


ADDRESS_KEYS_POOL = ThreadPoolExecutor(max_workers=6)  # tune as you like

def _update_keys_async(listing_id: str, addr: str, city: str) -> None:
    try:
        from ai.address_keys import update_parsed_listing_address_keys
        ok = update_parsed_listing_address_keys(listing_id, addr, city)
        if not ok:
            logging.warning("address_search_keys update returned False for %s", listing_id)
    except Exception as e:
        logging.exception("address_search_keys async failed for %s: %s", listing_id, e)




def _listing_schema() -> Dict[str, Any]:
    # Define once so we can compute `required` = all keys
    props: Dict[str, Any] = {
        "complete_info": {
            "type": ["string", "null"],
            "description": "Verbatim text for this listing exactly as written in the email. Strip HTML tags but keep original wording, numbers, symbols, and line breaks. Do not paraphrase. If very long, truncate to ~2000 chars."
        },
        # 1) Identification
        "source_title": {"type": ["string", "null"]},
        "listing_url": {"type": ["string", "null"]},
        "mls_id": {"type": ["string", "null"]},
        "agent_name": {"type": ["string", "null"]},
        "agent_phone": {"type": ["string", "null"]},
        "agent_email": {"type": ["string", "null"]},

        # 2) Location
        "address": {"type": ["string", "null"]},
        "city": {"type": ["string", "null"]},
        "county": {"type": ["string", "null"]},
        "state": {"type": ["string", "null"]},
        "zip": {"type": ["string", "null"]},

        # 3) Price & Fees
        "list_price_usd": {"type": ["number", "null"]},
        "hoa_fee_monthly_usd": {"type": ["number", "null"]},
        "hoa_assessment_monthly_usd": {"type": ["number", "null"]},
        "hoa_total_monthly_usd": {"type": ["number", "null"]},
        "taxes_annual_usd": {"type": ["number", "null"]},
        # B.3 (Blagojche 01.10): a seller-STATED After Repair Value + the ad's comparable sales
        # (OTHER sold properties). Captured so the WP description can show them; NEVER used as the
        # deal's own address/price (see the COMPS rule in the prompt).
        "arv_usd": {"type": ["number", "null"]},
        "comparable_sales": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "address": {"type": ["string", "null"]},
                    "sold_price_usd": {"type": ["number", "null"]},
                    "sold_date": {"type": ["string", "null"]},
                    "note": {"type": ["string", "null"]},
                },
                "required": ["address", "sold_price_usd", "sold_date", "note"],
            },
        },

        # 4) Property Type & Basics
        "property_type": {
            "type": ["string", "null"],
            "enum": ["single_family", "condo", "townhouse", "multi_family", "land", "mobile_home", "manufactured", "other", None]
        },
        "bedrooms": {"type": ["number", "null"]},
        "bathrooms_full": {"type": ["number", "null"]},
        "bathrooms_half": {"type": ["number", "null"]},
        "living_area_sqft": {"type": ["number", "null"]},
        "occupancy": {"type": ["string", "null"]},
        "year_built": {"type": ["number", "null"]},
        "is_condo": {"type": ["boolean", "null"]},

        # 5) Lot / Land
        "lot_size_sqft": {"type": ["number", "null"]},
        "lot_size_acres": {"type": ["number", "null"]},
        "is_land_only": {"type": ["boolean", "null"]},

        # 6) Waterfront / Water Access
        "water_feature": {
            "type": ["string", "null"],
            "enum": ["oceanfront", "ocean_access", "intracoastal", "bayfront", "canal", "lakefront", "riverfront", "water_view_only", "none", "unknown", None]
        },
        "is_on_water": {"type": ["boolean", "null"]},
        "water_notes": {"type": ["string", "null"]},

        # 7) Structure / Build
        "build_material": {
            "type": ["string", "null"],
            "enum": ["frame", "wood", "concrete_block", "brick", "stucco", "mixed", "unknown", None]
        },
        "is_frame_or_wood": {"type": ["boolean", "null"]},

        # 8) Keywords & Exceptional Flags
        "is_teardown_or_redevelopment": {"type": ["boolean", "null"]},
        "marketing_tags": {"type": "array", "items": {"type": "string"}},
        "raw_description_excerpt": {"type": ["string", "null"]},

        # 9) Region Classification
        "region_bucket": {
            "type": ["string", "null"],
            "enum": ["south_florida_tri_county", "st_lucie", "fort_pierce", "rest_of_florida", "outside_florida", "unknown", None]
        },
        "tri_county_name": {
            "type": ["string", "null"],
            "enum": ["miami_dade", "broward", "palm_beach", None]
        },

        # 10) Mobile Home
        "is_mobile_home": {"type": ["boolean", "null"]},

        # 11) Derived convenience flags
        "bath_combo_label": {"type": ["string", "null"]},
        "has_hoa": {"type": ["boolean", "null"]},
        "under_900_sqft": {"type": ["boolean", "null"]},
        "land_under_5000_sqft": {"type": ["boolean", "null"]},
        "water_exception_applicable": {"type": ["boolean", "null"]},

        # Images
        "images": {"type": "array", "items": {"type": "string"}},
        "other_images_source": {"type": ["string", "null"]},

        # Internal buyer-matching flags (exact labels from buyer form — not for marketing posts)
        "special_preferences_detected": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Canonical buyer-form special preference labels detected in this listing's email text. "
                "Use ONLY exact labels from the allowed list. Omit if not clearly stated."
            ),
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": props,
        "required": list(props.keys()),  # STRICT: all keys must appear (can be null)
    }

def _response_format() -> Dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "email_property_extraction",
            "strict": True,
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "listings": {
                        "type": "array",
                        "items": _listing_schema()
                    },
                    "notes": {
                        "type": "array",
                        "items": {"type": "string"}
                    }
                },
                "required": ["listings", "notes"]
            }
        }
    }



IMAGE_RULES_DEFAULT = """
- For each listing, populate image fields:
  • "images": collect direct image URLs (http/https) that *visually depict the property* within that listing's section, might present under img tag.
    - If URLs are relative, include them as-is.
    - Cap to the first 12 unique URLs per listing.
  • "other_images_source": return a page/gallery URL from THIS listing so we can scrape photos later.
     - Any http(s) URL on this listing is valid: Google Drive, Dropbox, Google Photos, MLS,
         a seller website, a short link, or a generic property/listing page.
     - Prefer a Drive / Dropbox / Photos folder when one is present. Otherwise use the listing's
         main http(s) link. Do not require the words photos/gallery/pics in the URL.
     - Preserve the selected URL verbatim, including query parameters.
    - Skip only unsubscribe, mailto, "view in browser", social icons, and tracking links.
    - Image curation later drops logos and non-property photos. Do not reject a URL just because
         it might also have a logo.
""".strip()

IMAGE_RULES_NEAREST = """
- For each listing, populate image fields:
  • "images": collect direct image URLs (http/https) that visually depict the property for that listing.
    - Look for images both just BEFORE and just AFTER the listing’s address/price/ARV lines.
    - An image that appears immediately BEFORE the address (with no other property address in between)
      belongs to that listing, not to the next one.
    - In general, within the same section (between county/header text and the next property address/header),
      attach each image to the NEAREST property address in that section.
    - Ignore obvious non-property images (logos, social icons, tiny spacer GIFs, generic dividers/banners).
    - Cap to the first 12 unique URLs per listing.
  • "other_images_source": return a page/gallery URL from THIS listing so we can scrape photos later.
     - Any http(s) URL on this listing is valid: Google Drive, Dropbox, Google Photos, MLS,
         a seller website, a short link, or a generic property/listing page.
     - Prefer a Drive / Dropbox / Photos folder when one is present. Otherwise use the listing's
         main http(s) link. Do not require the words photos/gallery/pics in the URL.
     - Preserve the selected URL verbatim, including query parameters.
    - Skip only unsubscribe, mailto, "view in browser", social icons, and tracking links.
    - Image curation later drops logos and non-property photos. Do not reject a URL just because
         it might also have a logo.
""".strip()


def build_system_prompt(use_nearest_image_rules: bool = False) -> str:
    image_block = IMAGE_RULES_NEAREST if use_nearest_image_rules else IMAGE_RULES_DEFAULT
    special_prefs_block = build_extraction_prompt_block()
    return f"""\
You extract structured data from EMAIL Markdown Content containing MULTIPLE property listings, Process and return ALL listings with real street addresses.
Make sure if listing have images include in images.
VERY IMPORTANT: Use the field names EXACTLY as defined in the JSON schema.

OUTPUT CONTRACT (must follow exactly):
- Use the field names EXACTLY as in the JSON schema. No aliases, no renames, no extra fields.
- Every field in the schema MUST be present in every listing. If unknown, put null (or "unknown" for enums).
- Do not invent fields like "address_line", "property_address", "price", "price_usd", etc. The only valid keys are in the schema. Valid keys for locations are "address", "city", "state", "county", "zip" and for property price its should be "list_price_usd" only.
- For "list_price_usd", NEVER use ARV / "after repair value" / "estimated value" numbers. Only use the actual asking / purchase / contract price the property is being offered at.

Rules:
- For `address`, include the full street line as written in the email, INCLUDING the house/building number when present (e.g. "1234 India Street", "137XX Royal Palm Blvd", "2*** SW Natura Ave", "2**0 NW 91st St", "2XX0 NW 91st St"). Preserve masked/partial numbers EXACTLY as written - keep every real digit and every mask character in place. NEVER replace a masked position with a guessed digit, and NEVER collapse a masked number to a single digit or to "0" (e.g. "2**0" must stay "2**0", never "0" and never "2220"). If the house number is entirely masked/unknown, keep the masked token rather than inventing one. Do not strip the house number.
- `address` is the SUBJECT / deal property ONLY. NEVER use an address from a "Comps" / "Comparable" / "Comparable Sales" section, or any line containing "SOLD" / "sold for" - those are comparables, not the deal. If the deal's own street line has no house number, keep it WITHOUT a number (do not borrow a number from a comp). Put comparable/sold addresses in `comparable_sales`, never in `address`.
- SKIP non-street "address" lines that are only bed/bath/size summaries with a city. These are NOT addresses.
  Examples to SKIP (do not emit a listing, or set address=null and exclude):
    • "3 Beds / 2 Baths, Miami, FL 33143"
    • "4 Beds / 2.5 Bath, Pinecrest, FL 33156"
    • "4 Bed/3 Bath- 2732 sqft, Lehigh Acres, FL 33936"
    • "3 bed | 2 bath | 1322 sf, Daytona Beach, FL"
  Examples to KEEP (real street addresses):
    • "7926 213th St E, Bradenton, FL 34202"
    • "513 Kel Ave, Titusville, FL 32796"
  If a block has only beds/baths/sqft + city/state/ZIP and no house-number street line, do not invent an address — omit that block from `listings`.
- DO NOT GUESS. Only return values explicitly present in the HTML (or safe numeric conversions/derivations described below).
- If a field is missing/unclear, return null or "unknown" (for enums).
- Normalize numbers: strip $ and commas. Convert acres->sqft (1 acre = 43560 sqft) when only acres given.
- `bedrooms` may be 0 for a studio / efficiency / 0-bed condo. 0 is a real value, not missing.
- `occupancy`: copy the source wording when present (e.g. Vacant, Occupied, Tenant occupied).
- Keep Bed/Bath, living area, occupancy, HOA amount+period, STR allowed, rehab, and assessment
  lines inside `complete_info` verbatim — do not drop them.
- Compute `hoa_total_monthly_usd` = fee + assessments (if both present).
- Compute convenience booleans (is_condo, is_land_only, under_900_sqft, land_under_5000_sqft, has_hoa, water_exception_applicable).
- Water exception applies only for water_feature in {{"oceanfront", "ocean_access", "intracoastal"}}.
- Classify `region_bucket`:
  • south_florida_tri_county if county is Miami-Dade, Broward, or Palm Beach (set tri_county_name accordingly)
  • st_lucie if county=St. Lucie
  • fort_pierce if city=Fort Pierce (also in St. Lucie County)
  • rest_of_florida if state=FL but not any above
  • outside_florida if state != FL
  • unknown if cannot determine
- County inference policy (Florida only):
  • If county is missing but state="FL" and either ZIP or city is present, infer the county using general US geographic knowledge (no external lookups).
  • Prefer ZIP→county; if ZIP is absent, use city→county.
  • If the city spans multiple counties, pick the most common/central county for that city (e.g., Miami→Miami-Dade; Fort Lauderdale→Broward; West Palm Beach→Palm Beach; Fort Pierce→St. Lucie).
  • If you cannot infer with high confidence, leave county=null.
  • After inferring county, update region_bucket/tri_county_name accordingly using the rules above.
- Map "CBS" or "concrete block structure" → build_material = concrete_block.
- Accept listings anywhere in the HTML; there may be separators or repeated blocks.
- For each listing, also include "complete_info":
  • Copy/paste the VERBATIM text content for that listing only (strip HTML tags, keep line breaks and punctuation).
  • Do NOT paraphrase or normalize wording; preserve numbers, currency symbols, and units as written.
  • If extremely long, keep the first ~1800–2000 characters and append an ellipsis (…) at the end.
  • Do not mix content from different listings.

{special_prefs_block}

  Agent / Wholesaler/ Sender Contact Details handling (important), Make sure to include:
- First, scan the email for a single GLOBAL contact block (often in the header or footer) that contains any of: name, phone, email of the sender/agent/wholesaler.
- Extract at most one global triple: agent_name, agent_phone, agent_email. If multiple candidates exist, pick the one that appears to be the primary sender/contact for the blast (e.g., signature or “Contact us” section).
- For each listing:
  • If that listing already has its own agent_name/agent_phone/agent_email, KEEP those (do not overwrite).
  • If any of those three fields are missing/null for the listing, fill the missing ones from the GLOBAL contact (if available).
- Formatting:
  • agent_email: lower-case; must look like a valid email address; otherwise leave null.
  • agent_phone: keep as a readable string (digits with punctuation ok). If multiple phones exist, prefer the one labeled sales/primary; otherwise the first plausible US phone.
  • agent_name: keep as written (person or team name).
  
  Property-type classification (very important; use these exact enum values):
- "multi_family" if the text clearly indicates MULTIPLE UNITS/DOORS: e.g., "multi-family", "multifamily", "duplex", "triplex", "fourplex/quadplex", "multiple units", "2 units", "3 doors", "4plex", or lists several units.
- "single_family" if it mentions "single family", "SFR", "home", or "house" referring to the subject property.
- "land" if it says "land", "vacant land/lot", "tear down", "teardown", "knockdown", or "development opportunity". (When property_type="land", also set is_land_only=true if no structure is being sold.)
- "condo" if it says "condo". (Also set is_condo=true.)
- "townhouse" if it says "townhouse", "townhome", "TH".
- "mobile_home" or "manufactured" if it explicitly says "mobile home", "manufactured", "MH". Prefer "mobile_home" if unsure between the two.
- If none of the above are explicitly indicated, return property_type=null (do NOT guess).
- These keywords may appear in subject, title blocks, body text, bullets, image captions, or buttons.

COMPS / COMPARABLES (critical - Rich 29.09):
- NEVER take a listing address from under a "Comps", "Comparables", "Comparable Sales", "Sold Comps" or "Recent Sales" heading, or from any line that says "sold for". Those are comparable/sold OTHER properties, not the deal.
- The deal address is almost always at the TOP of the ad; use it. (A few senders, e.g. Diplomat/Alex, put it at the bottom - still use the DEAL address, never a comp.)
- DO capture the comps THEMSELVES into `comparable_sales`: for each comp/sold line under such a heading, record its address (if given), sold_price_usd, and sold_date when present, and any leftover text in `note`. These are OTHER sold properties - never use them as the deal address or list_price_usd.
- Capture a seller-STATED After Repair Value into `arv_usd` (e.g. "ARV: $600,000", "After Repair Value $600k"). Only a value explicitly written in the ad; never estimate, compute, or guess one.

{image_block}
Output MUST strictly match the provided JSON schema.
""".strip()

_USER_INSTRUCTIONS_TEMPLATE = """\
EMAIL_HTML:
{email_html}

TASK:
Extract ALL listings present. Return an object with:
- "listings": array of listing objects conforming to the JSON schema.
- "notes": optional array of short warnings (e.g., "county not found", "ambiguous bed/bath", etc.).
"""


# -------------------------
# MAIN HELPER
# -------------------------
def _guard_masked_house_number(addr, source_text):
    """Never let the extractor fill a masked house number (** / xx) with a fabricated one.
    Carlos 52402: source "22**/22** NW 56th Ave" -> the model invented "2222". If the raw extracted
    leading number is a clean integer that is NOT present verbatim in the source, and the source
    shows a masked number token, revert the leading number to that masked token so the address stays
    masked (geocode then fails and the dup-gate flags it 'masked' = post+flag, not a fake address)."""
    import re
    a = (addr or "").strip()
    st = source_text or ""
    # B.1 (Blagojche 01.10): ignore URLs in the "verbatim" test - a link such as
    # dropbox...&dl=0 contains a standalone "0" that would wrongly make a collapsed house
    # number look real. Strip http(s) URLs before testing.
    st = re.sub(r"https?://\S+", " ", st)
    m = re.match(r"^(\d+)\b(.*)$", a)          # only a CLEAN leading integer can be fabricated
    if not m or not st:
        return addr
    num, rest = m.group(1), m.group(2)
    # B.1 (Blagojche 01.10): a digit that sits INSIDE a masked token is not a real verbatim
    # number. "2**0" -> the extractor emitted "0", and "0" does appear in the source, but only as
    # the trailing digit of the mask "2**0". Exclude adjacency to mask chars (* x X _ #) from the
    # "verbatim" test so such a collapsed digit is reverted to the masked token below.
    if re.search(r"(?<![\d*xX_#•])" + re.escape(num) + r"(?![\d*xX_#•])", st):
        return addr                              # the number is verbatim in the source -> real
    msrc = re.search(r"(\d*[*xX]{2,}[\d*xX/\-\u2013]*)", st)   # a masked number in the source
    if not msrc:
        return addr
    return (msrc.group(1) + rest).strip()        # e.g. "22**/22**" + " NW 56th Ave"


def _comp_street_key(a):
    """Street-level key for comparing addresses (ignore city/state/zip)."""
    a = (a or "").split(",")[0].strip().lower()
    return re.sub(r"\s+", " ", a)


_COMPS_HEADING_RE_LD = re.compile(
    r"(?im)^\s*(comps?|comparable sales|comparables?|recently\s+sold|recent\s+sold|sold\s+comps|sold)\b.*$"
)


def _guard_comp_as_deal_address(addr, lst, source_text):
    """Rich 01-02.10: when the deal address has NO house number, the extractor sometimes takes
    a COMP/SOLD address as the deal (deal "SW 158th Pl" -> comp "7743 SW 157th Pl"). If the
    extracted address equals a comparable_sales[] address, recover the real deal street from
    the source ABOVE the first Comps/Comparable/Sold heading (number-less -> needs_address_review).
    SOURCE must be the ad's own complete_info (never the whole email_html: a "first street above
    Comps" from another ad would be wrong)."""
    a = (addr or "").strip()
    if not a:
        return addr
    comps = lst.get("comparable_sales") or []
    comp_keys = {_comp_street_key(c.get("address")) for c in comps
                 if isinstance(c, dict) and c.get("address")}
    if not comp_keys or _comp_street_key(a) not in comp_keys:
        return addr                              # deal address is not a comp -> leave as-is
    st = source_text or ""
    if not st:
        logging.warning("comp-guard: addr %r matched a comp but no ad complete_info to recover from; kept", a)
        return addr
    m = _COMPS_HEADING_RE_LD.search(st)
    head = st[:m.start()] if m else st
    _SUF = r"(?i)\b(st|street|ave|avenue|blvd|boulevard|rd|road|dr|drive|ln|lane|ct|court|pl|place|ter|terrace|way|cir|circle|hwy|pkwy|trl|trail)\b"
    _SKIP = r"(?i)\$|\bbeds?\b|\bbaths?\b|\bsq ?ft\b|\bSF\b|\bbuilt\b|\bHOA\b|asking|\bsold\b"
    for line in head.splitlines():
        line = line.strip()
        if not line:
            continue
        if re.search(_SUF, line) and not re.search(_SKIP, line):
            deal_street = line.split(",")[0].strip()
            logging.info("comp-guard: addr %r matched a comp; recovered deal street %r", a, deal_street)
            return deal_street
    logging.warning("comp-guard: addr %r matched a comp but no deal street above Comps; kept", a)
    return addr


def _strip_comps_sections(html: str) -> str:
    """Conservative pre-strip so the extractor never reads a comparable/sold line as the deal
    (Rich 29.09). Drops a line ONLY when it explicitly says "sold for" AND is short (<=200 chars):
    a short standalone comp line. A long/minified line (Constant Contact tables can put the deal
    and a comp on one line) is kept so we never delete the real deal. The "under a Comps heading"
    rule is handled in the prompt, which understands document structure. Low-risk / reversible."""
    if not html:
        return html
    kept = []
    for line in html.split("\n"):
        if len(line) <= 200 and re.search(r"(?i)\bsold\s+for\b", line):
            continue
        kept.append(line)
    return "\n".join(kept)


def extract_listings_from_email_html(email_html: str,
                                     model: Optional[str] = None,
                                     temperature: float = 0.0,
                                     use_nearest_image_rules: bool = False) -> Dict[str, Any]:
    """
    Parse a raw email HTML (no scripts/styles/comments) containing multiple property ads,
    and return structured listings using OpenAI Structured Outputs (strict JSON schema).

    Returns:
        dict: { "listings": [ ... ], "notes": [...] }
    """
    if not email_html or not email_html.strip():
        return {"listings": [], "notes": ["empty_input_html"]}

    model = model or OPENAI_MODEL

    # Optional: tiny cleanup to reduce obvious noise that sometimes slips through.
    compact_html = re.sub(r"\s+\n", "\n", email_html).strip()
    # B.3 option B (Blagojche 01.10): pre-strip DISABLED - the prompt now captures comps into
    # comparable_sales and never takes a comp as the deal address, so we keep the comp lines.
    # compact_html = _strip_comps_sections(compact_html)  # Rich 29.09: drop "sold for" comp lines

    system_prompt = build_system_prompt(use_nearest_image_rules=use_nearest_image_rules)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": _USER_INSTRUCTIONS_TEMPLATE.format(email_html=compact_html)}
    ]

    def _attach_usage(data: Dict[str, Any], chat) -> Dict[str, Any]:
        try:
            from observability.openai_usage import extract_usage_from_response
            data["_openai_usage"] = extract_usage_from_response(chat)
            data["_openai_model"] = model
        except Exception:
            pass
        return data

    # First try: Structured Outputs (json_schema strict)
    try:
        create_kwargs: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "response_format": _response_format(),
        }
        if _model_supports_temperature(model):
            create_kwargs["temperature"] = 0.3
        chat = client.chat.completions.create(**create_kwargs)
        content = chat.choices[0].message.content
        data = json.loads(content)
        data.setdefault("notes", [])
        return _attach_usage(data, chat)
    except Exception as e:
        # Fallback: JSON mode (still asks for JSON, not schema-validated)
        try:
            print("Inside expection",e)
            fallback_kwargs: Dict[str, Any] = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt + "\nYou must output valid JSON only."},
                    {"role": "user", "content": _USER_INSTRUCTIONS_TEMPLATE.format(email_html=compact_html)}
                ],
                "response_format": {"type": "json_object"},
            }
            if _model_supports_temperature(model):
                fallback_kwargs["temperature"] = temperature
            chat = client.chat.completions.create(**fallback_kwargs)
            content = chat.choices[0].message.content
            data = json.loads(content)
            data.setdefault("notes", [])
            return _attach_usage(data, chat)
        except Exception as e2:
            # Last resort: return a structured error
            return {"listings": [], "notes": [f"extraction_failed: {e}", f"fallback_failed: {e2}"]}


def _normalize_city_for_google(city: str) -> str:
    """
    Replace any standalone 'bch' (any casing) with 'Beach'.
    Examples:
      'Pompano Bch'      -> 'Pompano Beach'
      'POMPANO BCH'      -> 'POMPANO Beach'
      'bch'              -> 'Beach'
    """
    if not city:
        return city

    # \b = word boundary, re.I = case-insensitive
    return re.sub(r"\bbch\b", "Beach", city, flags=re.IGNORECASE)


def _clean_images(arr):
    out = []
    for u in arr or []:
        if isinstance(u, str):
            u2 = u.strip()
            if u2.lower().startswith(("http://", "https://")):
                out.append(u2)
    return out[:12]  # cap to 12

def _compose_raw_for_google(addr: str, city: str, state: str, zip_: str) -> str:
    parts = [p.strip() for p in [addr, city, state, zip_] if p and str(p).strip()]
    return ", ".join(parts + ["USA"]) if parts else ""

def _gallery_url_fallback(source_email_doc) -> Optional[str]:
    """If AI missed the gallery URL, take Drive/Dropbox/Photos from the body.

    JG Equity WA deals are often a Constant Contact page with no gallery host
    in the URL; fall back to the first http(s) link for that group only.
    """
    if not source_email_doc:
        return None
    bodies = getattr(source_email_doc, "bodies", None)
    text = ""
    if bodies:
        text = (
            getattr(bodies, "html_ai", None)
            or getattr(bodies, "html_full", None)
            or getattr(bodies, "text", None)
            or ""
        )
    found = gallery_url_from_text(text)
    if found:
        return found
    if getattr(source_email_doc, "account_label", "") != "whatsapp":
        return None
    subject = (getattr(source_email_doc, "subject", None) or "").strip()
    group = subject[3:].strip() if subject.upper().startswith("WA ") else subject
    if not (is_jg_equity_group(group) or is_jg_equity_group(subject)):
        return None
    return first_http_url(getattr(bodies, "text", None) or "" if bodies else "") or None


def _sender_email_safe(source_email_doc) -> str:
    """
    Safely get the sender email from FilteredListingEmail.
    Supports both attribute and dict-like access for from_info.
    """
    if not source_email_doc:
        return ""
    fi = getattr(source_email_doc, "from_info", None)
    if not fi:
        return ""
    # mongoengine EmbeddedDocument vs dict
    email = getattr(fi, "email", None)
    if not email and isinstance(fi, dict):
        email = fi.get("email")
    return (email or "").strip().lower()

def upsert_parsed_listings_from_html(
    email_html: str,
    account_label: str,
    gmail_message_id: str,
    source_email_doc: FilteredListingEmail,
    list_slice: Optional[tuple[int, int]] = None,   # NEW
) -> Dict[str, Any]:
    """
    Run extraction on given HTML, then upsert rows into parsed_listings.
    Returns: {"count": N, "ids": [...], "notes": [...]}
    """
    # result = extract_listings_from_email_html(email_html)
    # NEW: choose image-rule mode based on sender
    sender = _sender_email_safe(source_email_doc)
    use_nearest = (
        sender == "deals@aoinvestments.ccsend.com"
        or sender == "southfloridadispo@joehomebuyer.com"
    )

    result = extract_listings_from_email_html(
        email_html,
        use_nearest_image_rules=True,  # default False unless AO sender
    )
    listings = result.get("listings", []) or []
    saved_ids: List[str] = []
    wholesaler_map = get_wholesaler_map()
    
    # bounds
    start_i = 1
    end_i = len(listings)
    if list_slice:
        s, e = list_slice
        start_i = max(1, s)
        end_i = min(len(listings), e)

    for idx, lst in enumerate(listings, start=1):  # 1..N within this email
        # honor slice by original position, so list_index stays stable for this email
        # if not (start_i <= idx <= end_i):
        #     continue 
        if list_slice:
            if start_i <= idx <= end_i:
                status_for_insert = "not_processed"
            else:
                status_for_insert = "bypassed"
        else:
            # old behavior: everything is not_processed
            status_for_insert = "not_processed"
        try:
            addr  = (lst.get("address") or "").strip()
            # Carlos 52402 guard (29.09): never post a fabricated house number for a masked source.
            addr = _guard_masked_house_number(addr, (lst.get("complete_info") or "") or email_html)
            # Rich 01-02.10: never let a comp/SOLD address become the deal address. Use the ad's
            # own complete_info ONLY (not email_html) so recovery can't grab another ad's street.
            addr = _guard_comp_as_deal_address(addr, lst, lst.get("complete_info") or "")
            city  = (lst.get("city") or "").strip()
            state = (lst.get("state") or "").strip()
            zip_  = (lst.get("zip") or "").strip()

            # Skip bed/bath/size blurbs mistaken for street addresses
            if is_bed_bath_descriptor_address(addr):
                logging.info(
                    "Skipping bed/bath descriptor address @idx %s account=%s msg=%s: %r",
                    idx, account_label, gmail_message_id, addr,
                )
                continue

            # ✨ NEW: try to normalize with Google
            geo_js = None
            try:
                norm_city = _normalize_city_for_google(city)
                raw_line = _compose_raw_for_google(addr, norm_city, state, zip_)
                if raw_line:
                    # Google step 1 (Blagojche 2026-09-25): ONE Google call = Geocoding; derive
                    # street/city/zip from components (paid Address Validation removed).
                    geo_js = geocode_response(raw_line)
                    fa, fc, fz = street_city_zip_from_geocode(geo_js)
                    # B.1 (Blagojche 01.10): NEVER let the geocode overwrite a MASKED house
                    # number. Google collapses "2**0 NW 91st St" to street_number "0" ->
                    # "0 Northwest 91st Street". If the post-guard addr is masked, or the
                    # geocoded street itself collapsed to a leading "0", keep the source
                    # addr/city/zip (geo_js is still stored for debugging). Masked address
                    # then stays as-is and propagates to WhatsApp/email (which read address).
                    _lead = (addr or "").split(" ", 1)[0]
                    _addr_masked = bool(re.search(r"\d", _lead)) and bool(re.search(r"[*xX#_\u2022]", _lead))
                    _fa_collapsed = bool(fa) and re.match(r"^0\b", fa.strip()) is not None
                    if _addr_masked or _fa_collapsed:
                        logging.info("B.1 masked-guard: kept source addr %r (geocode gave %r / %r)", addr, fa, fc)
                    else:
                        if fa and fc:
                            addr, city = fa, fc   # overwrite with geocoded components
                        if fz and not zip_:
                            zip_ = fz
            except Exception as e:
                print(f"Exception in listing geo format: {e}")
                # fail-open: keep original addr/city
                pass

            resolved_addr = resolve_street_address_from_fields(addr, lst)
            if resolved_addr:
                addr = resolved_addr
                lst["address"] = addr
                if is_bed_bath_descriptor_address(addr):
                    logging.info(
                        "Skipping resolved bed/bath descriptor address @idx %s account=%s msg=%s: %r",
                        idx, account_label, gmail_message_id, addr,
                    )
                    continue

            extracted_special_prefs = finalize_extracted_special_preferences(lst)
            # Keep only in top-level DB field — not inside complete_info blob used for WhatsApp
            lst.pop("special_preferences_detected", None)

            price_val = None
            if lst.get("list_price_usd") is not None:
                try:
                    price_val = float(lst["list_price_usd"])
                except Exception:
                    price_val = None

            q = ParsedListing.objects(
                account_label=account_label,
                gmail_message_id=gmail_message_id,
                list_index=idx,  
            )

            # -------------------------------
            # NEW: direct_wholeseller logic
            # -------------------------------
            direct_wholeseller_flag = "not_found"
            dw_info = None
            if sender:
                # sender is lowercased by _sender_email_safe; also normalize Constant Contact relay
                # addresses (user@x.ccsend.com -> user@x.com) so the wholesaler_map - which is keyed
                # by normalize_email(sender_email) - matches Todd/Francesco. Fix 30.09 (Blagojche):
                # reuse the EXISTING normalizer, do not add a new one.
                dw_info = wholesaler_map.get(normalize_email(sender))

            if dw_info and isinstance(dw_info, dict):
                # Mark as not_processed for further handling elsewhere
                # direct_wholeseller_flag = "not_processed"
                if status_for_insert == "not_processed":
                    direct_wholeseller_flag = "not_processed"
                else:
                    direct_wholeseller_flag = "bypassed"

                # Overwrite agent contact inside the listing blob (complete_info)
                try:
                    name = dw_info.get("name")
                    phone = dw_info.get("phone")
                    email = dw_info.get("email")
                    updateFlagForPodio= dw_info.get("updateFlagForPodio")

                    if name:
                        lst["agent_name"] = name
                    if phone is not None:
                        # ensure string, but keep formatting flexible
                        lst["agent_phone"] = str(phone)
                    if email:
                        lst["agent_email"] = email
                    if updateFlagForPodio is not None:
                        lst["updateFlagForPodio"] = (
                            "true" if updateFlagForPodio else "false"
                        )

                    
                except Exception as e:
                    # If anything goes wrong, we log and keep the original listing intact
                    logging.exception("Failed to apply direct_wholeseller override for sender %s: %s", sender, e)
            # If no match, flag stays "not_found"


    
            updates = {
                "upsert": True,
                "set__source_email": source_email_doc,
                "set__address": addr,
                "set__city": city,
                "set__state": state,
                "set__zip": zip_,
                "set__price": price_val,
                "set__images": _clean_images(lst.get("images")),
                "set__other_images_source": (
                    (lst.get("other_images_source") or "").strip()
                    or _gallery_url_fallback(source_email_doc)
                    or None
                ),
                "set__complete_info": lst,
                "set__input_source": getattr(source_email_doc, "input_source", None) or (
                    "whatsapp" if account_label == "whatsapp" else "email"
                ),
                "set__source_website": getattr(source_email_doc, "source_website", None),
                "set_on_insert__web_publish_enabled": (
                    getattr(source_email_doc, "input_source", None) != "web"
                ),
                "set__extracted_special_preferences": extracted_special_prefs,
                "set_on_insert__status": status_for_insert,  # brand-new only
                "set__direct_wholeseller": direct_wholeseller_flag,
            }
            if geo_js is not None:
                updates["set__geo_code_response"] = geo_js

            q.update_one(**updates)

            saved = q.only("id").first()
            if saved:
                saved_ids.append(str(saved.id))
                try:
                    from observability.pipeline_metrics import record_listing_created
                    record_listing_created(str(saved.id))
                except Exception:
                    pass
                if addr and city:
                    try:
                        ADDRESS_KEYS_POOL.submit(_update_keys_async, str(saved.id), addr, city)
                    except Exception:
                        pass
        except Exception as e:
            print(f"[parsed_listings] upsert error @idx {idx}: {e}")

    usage = result.get("_openai_usage")
    if usage and saved_ids:
        try:
            from observability.openai_usage import allocate_usage_to_listings
            allocate_usage_to_listings(
                usage,
                model=result.get("_openai_model"),
                stage="parsed",
                call_name="extract_listings",
                listing_ids=saved_ids,
            )
        except Exception:
            pass
    elif usage and source_email_doc:
        try:
            from models import FilteredListingEmail
            FilteredListingEmail.objects(id=source_email_doc.id).update_one(
                set__pipeline_token_usage={
                    "stage": "parsed",
                    "call_name": "extract_listings",
                    "model": result.get("_openai_model"),
                    **usage,
                },
                set__updated_at=datetime.utcnow(),
            )
        except Exception:
            pass

    return {"count": len(saved_ids), "ids": saved_ids, "notes": result.get("notes", [])}
