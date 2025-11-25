import os
import json
import re
from typing import Dict, Any, List, Optional
from openai import OpenAI
from dotenv import load_dotenv
from models import ParsedListing, FilteredListingEmail
from ai_address_search_keys import update_parsed_listing_address_keys
from google_formatter import get_street_and_city

from concurrent.futures import ThreadPoolExecutor
import logging
# -------------------------
# CONFIG
# -------------------------
# Load environment variables
load_dotenv()

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")  # supports structured outputs
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
# client = OpenAI(api_key=OPENAI_API_KEY)
client = OpenAI(
    api_key=OPENAI_API_KEY,
    timeout=800.0,        # 30s hard timeout for network+read
    max_retries=0        # keep low; you can set 0 or 1
)


ADDRESS_KEYS_POOL = ThreadPoolExecutor(max_workers=6)  # tune as you like

def _update_keys_async(listing_id: str, addr: str, city: str) -> None:
    try:
        from ai_address_search_keys import update_parsed_listing_address_keys
        ok = update_parsed_listing_address_keys(listing_id, addr, city)
        if not ok:
            logging.warning("address_search_keys update returned False for %s", listing_id)
    except Exception as e:
        logging.exception("address_search_keys async failed for %s: %s", listing_id, e)



# -------------------------
# JSON-SCHEMA (STRICT)
# -------------------------
# def _listing_schema() -> Dict[str, Any]:
#     # Schema matches the keys we agreed on; nullables are allowed to avoid hallucination.
#     return {
#         "type": "object",
#         "additionalProperties": False,
#         "properties": {
#             "complete_info": {
#                 "type": ["string", "null"],
#                 # Verbose description helps the model obey
#                 "description": "Verbatim text for this listing exactly as written in the email. Strip HTML tags but keep original wording, numbers, symbols, and line breaks. Do not paraphrase. If very long, truncate to ~2000 chars."
#             },
#             # 1) Identification
#             "source_title": {"type": ["string", "null"]},
#             "listing_url": {"type": ["string", "null"]},
#             "mls_id": {"type": ["string", "null"]},
#             "agent_name": {"type": ["string", "null"]},
#             "agent_phone": {"type": ["string", "null"]},
#             "agent_email": {"type": ["string", "null"]},

#             # 2) Location
#             "address_line": {"type": ["string", "null"]},
#             "city": {"type": ["string", "null"]},
#             "county": {"type": ["string", "null"]},
#             "state": {"type": ["string", "null"]},
#             "zip": {"type": ["string", "null"]},
#             "latitude": {"type": ["number", "null"]},
#             "longitude": {"type": ["number", "null"]},

#             # 3) Price & Fees
#             "list_price_usd": {"type": ["number", "null"]},
#             "hoa_fee_monthly_usd": {"type": ["number", "null"]},
#             "hoa_assessment_monthly_usd": {"type": ["number", "null"]},
#             "hoa_total_monthly_usd": {"type": ["number", "null"]},
#             "taxes_annual_usd": {"type": ["number", "null"]},

#             # 4) Property Type & Basics
#             "property_type": {
#                 "type": ["string", "null"],
#                 "enum": [
#                     "single_family", "condo", "townhouse", "multi_family",
#                     "land", "mobile_home", "manufactured", "other", None
#                 ]
#             },
#             "bedrooms": {"type": ["number", "null"]},
#             "bathrooms_full": {"type": ["number", "null"]},
#             "bathrooms_half": {"type": ["number", "null"]},
#             "living_area_sqft": {"type": ["number", "null"]},
#             "year_built": {"type": ["number", "null"]},
#             "is_condo": {"type": ["boolean", "null"]},

#             # 5) Lot / Land
#             "lot_size_sqft": {"type": ["number", "null"]},
#             "lot_size_acres": {"type": ["number", "null"]},
#             "is_land_only": {"type": ["boolean", "null"]},

#             # 6) Waterfront / Water Access
#             "water_feature": {
#                 "type": ["string", "null"],
#                 "enum": [
#                     "oceanfront", "ocean_access", "intracoastal",
#                     "bayfront", "canal", "lakefront", "riverfront",
#                     "water_view_only", "none", "unknown", None
#                 ]
#             },
#             "is_on_water": {"type": ["boolean", "null"]},
#             "water_notes": {"type": ["string", "null"]},

#             # 7) Structure / Build
#             "build_material": {
#                 "type": ["string", "null"],
#                 "enum": ["frame", "wood", "concrete_block", "brick", "stucco", "mixed", "unknown", None]
#             },
#             "is_frame_or_wood": {"type": ["boolean", "null"]},

#             # 8) Keywords & Exceptional Flags
#             "is_teardown_or_redevelopment": {"type": ["boolean", "null"]},
#             "marketing_tags": {"type": "array", "items": {"type": "string"}},
#             "raw_description_excerpt": {"type": ["string", "null"]},

#             # 9) Region Classification
#             "region_bucket": {
#                 "type": ["string", "null"],
#                 "enum": [
#                     "south_florida_tri_county", "st_lucie", "fort_pierce",
#                     "rest_of_florida", "outside_florida", "unknown", None
#                 ]
#             },
#             "tri_county_name": {
#                 "type": ["string", "null"],
#                 "enum": ["miami_dade", "broward", "palm_beach", None]
#             },

#             # 10) Mobile Home
#             "is_mobile_home": {"type": ["boolean", "null"]},

#             # 11) Derived convenience flags
#             "bath_combo_label": {"type": ["string", "null"]},
#             "has_hoa": {"type": ["boolean", "null"]},
#             "under_900_sqft": {"type": ["boolean", "null"]},
#             "land_under_5000_sqft": {"type": ["boolean", "null"]},
#             "water_exception_applicable": {"type": ["boolean", "null"]},

#             # NEW: Images found in the email for this listing
#             "images": {
#                 "type": "array",
#                 "items": {"type": "string"},
#                 "description": "Direct image URLs (http/https) that appear in the email for this listing. Exclude logos, agent headshots, social icons, QR codes, and tracking pixels."
#             },

#             # NEW: External gallery link (if any)
#             "other_images_source": {
#                 "type": ["string", "null"],
#                 "description": "Single external link to additional photos (e.g., Google Drive, Dropbox, MLS gallery) found in the listing block."
#             },
#         }
#     }


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

        # 4) Property Type & Basics
        "property_type": {
            "type": ["string", "null"],
            "enum": ["single_family", "condo", "townhouse", "multi_family", "land", "mobile_home", "manufactured", "other", None]
        },
        "bedrooms": {"type": ["number", "null"]},
        "bathrooms_full": {"type": ["number", "null"]},
        "bathrooms_half": {"type": ["number", "null"]},
        "living_area_sqft": {"type": ["number", "null"]},
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


# -------------------------
# PROMPT UTILS
# -------------------------
_SYSTEM_PROMPT = """\
You extract structured data from EMAIL Markdown Content containing MULTIPLE property listings, Process and return ALL listings addresses.
Don't skip any address to process. 
Make sure if listing have images include in images.
VERY IMPORTANT: Use the field names EXACTLY as defined in the JSON schema.

OUTPUT CONTRACT (must follow exactly):
- Use the field names EXACTLY as in the JSON schema. No aliases, no renames, no extra fields.
- Every field in the schema MUST be present in every listing. If unknown, put null (or "unknown" for enums).
- Do not invent fields like "address_line", "property_address", "price", "price_usd", etc. The only valid keys are in the schema. Valid keys for locations are "address", "city", "state", "county", "zip" and for property price its should be "list_price_usd" only.

Rules:
- DO NOT GUESS. Only return values explicitly present in the HTML (or safe numeric conversions/derivations described below).
- If a field is missing/unclear, return null or "unknown" (for enums).
- Normalize numbers: strip $ and commas. Convert acres->sqft (1 acre = 43560 sqft) when only acres given.
- Compute `hoa_total_monthly_usd` = fee + assessments (if both present).
- Compute convenience booleans (is_condo, is_land_only, under_900_sqft, land_under_5000_sqft, has_hoa, water_exception_applicable).
- Water exception applies only for water_feature in {oceanfront, ocean_access, intracoastal}.
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

- For each listing, populate image fields:
  • "images": collect direct image URLs (http/https) that *visually depict the property* within that listing's section, might present under img tag.
    - If URLs are relative, include them as-is.
    - Cap to the first 12 unique URLs per listing.
  • "other_images_source": if the listing includes a link to more photos (e.g., “View more photos”, "Click Here For Pictures", “Gallery”, Google Drive, Dropbox, MLS), return that single URL; otherwise null.
Output MUST strictly match the provided JSON schema.
"""

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
def extract_listings_from_email_html(email_html: str,
                                     model: Optional[str] = None,
                                     temperature: float = 0.0) -> Dict[str, Any]:
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

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": _USER_INSTRUCTIONS_TEMPLATE.format(email_html=compact_html)}
    ]

    # First try: Structured Outputs (json_schema strict)
    try:
        chat = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.3,
            response_format=_response_format()
        )
        content = chat.choices[0].message.content
        data = json.loads(content)
        data.setdefault("notes", [])
        return data
        # return json.loads(content)
    except Exception as e:
        # Fallback: JSON mode (still asks for JSON, not schema-validated)
        try:
            print("Inside expection",e)
            chat = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT + "\nYou must output valid JSON only."},
                    {"role": "user", "content": _USER_INSTRUCTIONS_TEMPLATE.format(email_html=compact_html)}
                ],
                temperature=temperature,
                response_format={"type": "json_object"}
            )
            content = chat.choices[0].message.content
            # return json.loads(content)
            data = json.loads(content)
            data.setdefault("notes", [])
            return data
        except Exception as e2:
            # Last resort: return a structured error
            return {"listings": [], "notes": [f"extraction_failed: {e}", f"fallback_failed: {e2}"]}





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
    result = extract_listings_from_email_html(email_html)
    listings = result.get("listings", []) or []
    saved_ids: List[str] = []
    
    # bounds
    start_i = 1
    end_i = len(listings)
    if list_slice:
        s, e = list_slice
        start_i = max(1, s)
        end_i = min(len(listings), e)

    for idx, lst in enumerate(listings, start=1):  # 1..N within this email
        # honor slice by original position, so list_index stays stable for this email
        if not (start_i <= idx <= end_i):
            continue 
        try:
            addr  = (lst.get("address") or "").strip()
            city  = (lst.get("city") or "").strip()
            state = (lst.get("state") or "").strip()
            zip_  = (lst.get("zip") or "").strip()
            # ✨ NEW: try to normalize with Google
            geo_js = None
            try:
                raw_line = _compose_raw_for_google(addr, city, state, zip_)
                if raw_line:
                    fa, fc = get_street_and_city(raw_line)  # returns (street, city) or (None, None)
                    if fa and fc:
                        addr, city = fa, fc   # overwrite with formatted values
                    # geocode full result (non-blocking/fail-open)
                    geo_js = geocode_response(raw_line)
            except Exception:
                # fail-open: keep original addr/city
                pass

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

            # q.update_one(
            #     upsert=True,
            #     set__source_email=source_email_doc,
            #     set__address=addr,
            #     set__city=city,
            #     set__state=state,
            #     set__zip=zip_,
            #     set__price=price_val,
            #     set__images=_clean_images(lst.get("images")),
            #     set__other_images_source=(lst.get("other_images_source") or "").strip() or None,
            #     set__complete_info=lst,
            #     set_on_insert__status="not_processed",  # brand-new only
            # )
            updates = {
                "upsert": True,
                "set__source_email": source_email_doc,
                "set__address": addr,
                "set__city": city,
                "set__state": state,
                "set__zip": zip_,
                "set__price": price_val,
                "set__images": _clean_images(lst.get("images")),
                "set__other_images_source": (lst.get("other_images_source") or "").strip() or None,
                "set__complete_info": lst,
                "set_on_insert__status": "not_processed",  # brand-new only
            }
            if geo_js is not None:
                updates["set__geo_code_response"] = geo_js

            q.update_one(**updates)

            saved = q.only("id").first()
            if saved:
                saved_ids.append(str(saved.id))
                if addr and city:
                    try:
                        ADDRESS_KEYS_POOL.submit(_update_keys_async, str(saved.id), addr, city)
                    except Exception:
                        pass
        except Exception as e:
            print(f"[parsed_listings] upsert error @idx {idx}: {e}")

    return {"count": len(saved_ids), "ids": saved_ids, "notes": result.get("notes", [])}
