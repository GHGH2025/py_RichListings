import os
import json
import re
from typing import Dict, Any, List, Optional
from openai import OpenAI
from dotenv import load_dotenv
from models import ParsedListing, FilteredListingEmail
# -------------------------
# CONFIG
# -------------------------
# Load environment variables
load_dotenv()

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")  # supports structured outputs
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY)


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
You extract structured data from EMAIL HTML containing MULTIPLE property listings. 
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
- Map "CBS" or "concrete block structure" → build_material = concrete_block.
- Accept listings anywhere in the HTML; there may be separators or repeated blocks.
- For each listing, also include "complete_info":
  • Copy/paste the VERBATIM text content for that listing only (strip HTML tags, keep line breaks and punctuation).
  • Do NOT paraphrase or normalize wording; preserve numbers, currency symbols, and units as written.
  • If extremely long, keep the first ~1800–2000 characters and append an ellipsis (…) at the end.
  • Do not mix content from different listings.
- For each listing, populate image fields:
  • "images": collect direct image URLs (http/https) that *visually depict the property* within that listing's section.
    - Exclude logos, broker badges, agent headshots, social icons, QR codes, and tracking pixels.
    - Prefer images larger than 40x40 or clearly property photos.
    - If URLs are relative, include them as-is.
    - Do NOT include data: URIs.
    - Cap to the first 12 unique URLs per listing.
  • "other_images_source": if the listing includes a link to more photos (e.g., “View more photos”, “Gallery”, Google Drive, Dropbox, MLS), return that single URL; otherwise null.
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
            temperature=temperature,
            response_format=_response_format()
        )
        content = chat.choices[0].message.content
        print("content",content)
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

            q.update_one(
                upsert=True,
                set__source_email=source_email_doc,
                set__address=addr,
                set__city=city,
                set__state=state,
                set__zip=zip_,
                set__price=price_val,
                set__images=_clean_images(lst.get("images")),
                set__other_images_source=(lst.get("other_images_source") or "").strip() or None,
                set__complete_info=lst,
                set_on_insert__status="not_processed",  # brand-new only
            )

            saved = q.only("id").first()
            if saved:
                saved_ids.append(str(saved.id))
        except Exception as e:
            print(f"[parsed_listings] upsert error @idx {idx}: {e}")

    return {"count": len(saved_ids), "ids": saved_ids, "notes": result.get("notes", [])}


demo_html = """
<!DOCTYPE HTML>
<html lang="en-US"> <head> <title>Email from Lexico Realty</title> <meta http-equiv="Content-Type" content="text/html; charset=utf-8"> <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
</head> <body class="body template template--en-US" data-template-version="1.46.10" data-canonical-name="CPE-PT16282" lang="en-US" align="center" style="-ms-text-size-adjust: 100%; -webkit-text-size-adjust: 100%; min-width: 100%; width: 100%; margin: 0px; padding: 0px;"> <div id="preheader" style="color: transparent; display: none; font-size: 1px; line-height: 1px; max-height: 0px; max-width: 0px; opacity: 0; overflow: hidden;"><span data-entity-ref="preheader">Lexico : (954) 328 - 6498 ! The Investment Property Solution in South Florida!</span></div> <div id="tracking-image" style="color: transparent; display: none; font-size: 1px; line-height: 1px; max-height: 0px; max-width: 0px; opacity: 0; overflow: hidden;"><img src="https://9vn5xrabb.cc.rs6.net/on.jsp?ca=e8bb1958-5bfc-4ae6-8260-8bfb64254058&a=1134466905786&c=7f7092e6-ba40-11ea-8629-d4ae528442b5&ch=f9d326bc-8d59-11ee-981c-fa163e25e9d8" / alt=""></div> <div class="shell" lang="en-US" dir="ltr" style="background-color: #c03e30;"> <table class="shell_panel-row" width="100%" border="0" cellpadding="0" cellspacing="0" style="background-image: url('https://imgssl.constantcontact.com/letters/images/backgrounds/darkwood.png');" background="https://imgssl.constantcontact.com/letters/images/backgrounds/darkwood.png"> <tr class=""> <td class="shell_panel-cell" style="" align="center" valign="top"> <table class="shell_width-row scale" style="width: 630px;" align="center" border="0" cellpadding="0" cellspacing="0"> <tr> <td class="shell_width-cell" style="padding: 15px 10px;" align="center" valign="top"> <table class="shell_content-row" width="100%" align="center" border="0" cellpadding="0" cellspacing="0"> <tr> <td class="shell_content-cell" style="border-radius: 0px; background-color: #ffffff; padding: 0; border: 5px solid #000000;" align="center" valign="top" bgcolor="#ffffff"> <table class="layout layout--1-column" style="background-color: #F0F0F7; table-layout: fixed;" width="100%" border="0" cellpadding="0" cellspacing="0" bgcolor="#F0F0F7"> <tr> <td class="column column--1 scale stack" style="width: 100%;" align="center" valign="top"> <table class="image image--padding-vertical image--mobile-scale image--mobile-center" width="100%" border="0" cellpadding="0" cellspacing="0"> <tr> <td class="image_container content-padding-horizontal" style="padding: 10px 20px;" align="center" valign="top"> <table class="image_container-caption text" border="0" cellpadding="0" cellspacing="0" style="table-layout: fixed;"> <tr> <td class="text_content-cell" style="text-align: center; font-family: Arial,Verdana,Helvetica,sans-serif; color: #505362; font-size: 14px; line-height: 1.2; display: block; word-wrap: break-word; padding: 0px;" align="center"> <img data-image-content class="image_content" style="display: block; height: auto; max-width: 100%;" width="345" src="https://files.constantcontact.com/ab63f732801/8db2372c-0d7c-4a4f-9d99-76986629c7ab.png?rdr=true" alt=""> </td> </tr> </table> </td> </tr> </table> </td> </tr> </table> <table class="layout layout--1-column" style="table-layout: fixed;" width="100%" border="0" cellpadding="0" cellspacing="0"> <tr> <td class="column column--1 scale stack" style="width: 100%;" align="center" valign="top">
<table class="divider" width="100%" cellpadding="0" cellspacing="0" border="0"> <tr> <td class="divider_container content-padding-horizontal" style="padding: 10px 20px;" width="100%" align="center" valign="top"> <table class="divider_content-row" style="height: 1px; width: 100%;" cellpadding="0" cellspacing="0" border="0"> <tr> <td class="divider_content-cell" style="padding-bottom: 10px; background-color: #646464; height: 1px; line-height: 1px; border-bottom-width: 0px;" height="1" align="center" bgcolor="#646464"> <img alt="" width="5" height="1" border="0" hspace="0" vspace="0" src="https://imgssl.constantcontact.com/letters/images/1101116784221/S.gif" style="display: block; height: 1px; width: 5px;"> </td> </tr> </table> </td> </tr> </table> </td> </tr> </table> <table class="layout layout--1-column" style="table-layout: fixed;" width="100%" border="0" cellpadding="0" cellspacing="0"> <tr> <td class="column column--1 scale stack" style="width: 100%;" align="center" valign="top"> <table class="image image--padding-vertical image--mobile-scale image--mobile-center" width="100%" border="0" cellpadding="0" cellspacing="0"> <tr> <td class="image_container content-padding-horizontal" style="padding: 10px 20px;" align="center" valign="top"> <table class="image_container-caption text" border="0" cellpadding="0" cellspacing="0" style="table-layout: fixed;"> <tr> <td class="text_content-cell" style="text-align: center; font-family: Arial,Verdana,Helvetica,sans-serif; color: #505362; font-size: 14px; line-height: 1.2; display: block; word-wrap: break-word; padding: 0px;" align="center"> <img data-image-content class="image_content" style="display: block; height: auto; max-width: 100%;" width="560" src="https://files.constantcontact.com/ab63f732801/a7b1e1a2-72e4-4da7-936e-4931a4bade28.jpg?rdr=true" alt=""> </td> </tr> </table> </td> </tr> </table> </td> </tr> </table> <table class="layout-margin" style="" width="100%" border="0" cellpadding="0" cellspacing="0"> <tr> <td class="layout-margin_cell" style="padding: 0px 20px;" align="center" valign="top"> <table class="layout layout--2-column layout--divided" style="table-layout: fixed;" width="100%" border="0" cellpadding="0" cellspacing="0"> <tr> <td class="column column--1 scale stack" style="width: 50%;" align="center" valign="top"> <table class="image image--padding-vertical image--mobile-scale image--mobile-center" width="100%" border="0" cellpadding="0" cellspacing="0"> <tr> <td class="image_container content-padding-horizontal" style="padding: 10px 20px;" align="center" valign="top"> <table class="image_container-caption text" border="0" cellpadding="0" cellspacing="0" style="table-layout: fixed;"> <tr> <td class="text_content-cell" style="text-align: center; font-family: Arial,Verdana,Helvetica,sans-serif; color: #505362; font-size: 14px; line-height: 1.2; display: block; word-wrap: break-word; padding: 0px;" align="center"> <img data-image-content class="image_content" style="display: block; height: auto; max-width: 100%;" width="228" src="https://files.constantcontact.com/ab63f732801/26f1be37-9774-4eca-873f-fc4707431eaa.jpg?rdr=true" alt=""> </td> </tr> </table> </td> </tr> </table> </td> <td class="column-divider scale stack" style="height: 1px; line-height: 1px;" width="20" align="center" valign="top"> <img alt="" width="20" height="20" border="0" hspace="0" vspace="0" src="https://imgssl.constantcontact.com/letters/images/1101116784221/S.gif"> </td> <td class="column column-border column--2 scale stack" style="width: 50%; border: 2px solid #CCCCCC;" align="center" valign="top"> <table class="image image--padding-vertical image--mobile-scale image--mobile-center" width="100%" border="0" cellpadding="0" cellspacing="0"> <tr> <td class="image_container content-padding-horizontal" style="padding: 10px 20px;" align="center" valign="top"> <table class="image_container-caption text" border="0" cellpadding="0" cellspacing="0" style="table-layout: fixed;"> <tr> <td class="text_content-cell" style="text-align: center; font-family: Arial,Verdana,Helvetica,sans-serif; color: #505362; font-size: 14px; line-height: 1.2; display: block; word-wrap: break-word; padding: 0px;" align="center"> <img data-image-content class="image_content" style="display: block; height: auto; max-width: 100%;" width="228" src="https://files.constantcontact.com/ab63f732801/3ded214c-a31d-43f9-a0d5-4135098cfcc6.jpg?rdr=true" alt=""> </td> </tr> </table> </td> </tr> </table> </td> </tr> </table> </td> </tr> </table> <table class="layout layout--1-column" style="table-layout: fixed;" width="100%" border="0" cellpadding="0" cellspacing="0"> <tr> <td class="column column--1 scale stack" style="width: 100%;" align="center" valign="top"> <table class="image image--padding-vertical image--mobile-scale image--mobile-center" width="100%" border="0" cellpadding="0" cellspacing="0"> <tr> <td class="image_container content-padding-horizontal" style="padding: 10px 20px;" align="center" valign="top"> <table class="image_container-caption text" border="0" cellpadding="0" cellspacing="0" style="table-layout: fixed;"> <tr> <td class="text_content-cell" style="text-align: center; font-family: Arial,Verdana,Helvetica,sans-serif; color: #505362; font-size: 14px; line-height: 1.2; display: block; word-wrap: break-word; padding: 0px;" align="center"> <img data-image-content class="image_content" style="display: block; height: auto; max-width: 100%;" width="560" src="https://files.constantcontact.com/ab63f732801/4ae853bb-25bf-44c3-93b7-aa62d2fa0bf3.jpg?rdr=true" alt=""> </td> </tr> </table> </td> </tr> </table> </td> </tr> </table> <table class="layout layout--heading layout--1-column" style="background-color: #F0F0F7; table-layout: fixed;" width="100%" border="0" cellpadding="0" cellspacing="0" bgcolor="#F0F0F7"> <tr> <td class="column column--1 scale stack" style="width: 100%;" align="center" valign="top"> <table class="text text--padding-vertical" width="100%" border="0" cellpadding="0" cellspacing="0" style="table-layout: fixed;"> <tr> <td class="text_content-cell content-padding-horizontal" style="text-align: center; font-family: Arial,Verdana,Helvetica,sans-serif; color: #505362; font-size: 14px; line-height: 1.2; display: block; word-wrap: break-word; padding: 10px 20px;" align="center" valign="top">
<p style="margin: 0;"><span style="font-size: 23px; color: rgb(54, 97, 189); font-weight: bold;">3230 N 23 Ave</span></p>
<p style="margin: 0;"><span style="font-size: 23px; color: rgb(54, 97, 189); font-weight: bold;">Hollywood, FL 33020</span></p>
<p style="margin: 0;"><br></p>
<p style="margin: 0;"><br></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;">5 Bedrooms / 2 Bathrooms + Garage </span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;">Corner Lot</span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;">1,791 Sq Ft</span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;">5,119 Sq ft Lot </span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;">CBS - Built 1972</span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;"> Spacious Family Room </span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;">Large Den</span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;"> Dated but Needs Updates</span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;">Great Investment Area </span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;">See Comps Below </span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;">Vacant on Supra</span></p>
<p style="margin: 0;"><br></p>
<p style="text-align: left; margin: 0;" align="left"><br></p>
<p style="text-align: left; margin: 0;" align="left"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;">Comp 1) 2412 Raleigh St - SOLD- $530,000</span><span style="font-size: 20px; color: rgb(255, 0, 0); font-weight: bold;"> </span></p>
<p style="text-align: left; margin: 0;" align="left"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;">Comp 2) 2516 Raleigh St - SOLD - $529,000</span></p>
<p style="text-align: left; margin: 0;" align="left"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;">Comp 3) 2245 Cody St - SOLD - $505,000 </span></p>
<p style="text-align: left; margin: 0;" align="left"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;">Comp 4) 2331 Coolidge St - SOLD by Lexico - $495,000</span></p>
<p style="margin: 0;"><br></p>
<p style="margin: 0;"><br></p>
<p style="margin: 0;"><br></p>
<p style="margin: 0;"><span style="font-size: 24px; color: rgb(54, 97, 189); font-weight: bold;">Asking Price</span><span style="font-size: 24px; color: rgb(34, 34, 34);">&#xa0;</span><span style="font-size: 26px; color: rgb(255, 0, 0); font-weight: bold;">$355,000</span></p>
<p style="margin: 0;"><br></p>
<p style="margin: 0;"><br></p>
<h1 style="font-family: Arial,Verdana,Helvetica,sans-serif; color: #00BBD2; font-size: 26px; font-weight: bold; margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34);">&#xa0;Call/Text Alex: (954) 328-6498</span></h1>
<h1 style="font-family: Arial,Verdana,Helvetica,sans-serif; color: #00BBD2; font-size: 26px; font-weight: bold; margin: 0;"><br></h1>
</td> </tr> </table> </td> </tr> </table> <table class="layout layout--1-column" style="table-layout: fixed;" width="100%" border="0" cellpadding="0" cellspacing="0"> <tr> <td class="column column--1 scale stack" style="width: 100%;" align="center" valign="top">
<table class="divider" width="100%" cellpadding="0" cellspacing="0" border="0"> <tr> <td class="divider_container content-padding-horizontal" style="padding: 10px 20px;" width="100%" align="center" valign="top"> <table class="divider_content-row" style="height: 1px; width: 100%;" cellpadding="0" cellspacing="0" border="0"> <tr> <td class="divider_content-cell" style="padding-bottom: 10px; background-color: #646464; height: 1px; line-height: 1px; border-bottom-width: 0px;" height="1" align="center" bgcolor="#646464"> <img alt="" width="5" height="1" border="0" hspace="0" vspace="0" src="https://imgssl.constantcontact.com/letters/images/1101116784221/S.gif" style="display: block; height: 1px; width: 5px;"> </td> </tr> </table> </td> </tr> </table> </td> </tr> </table> <table class="layout-margin" style="" width="100%" border="0" cellpadding="0" cellspacing="0"> <tr> <td class="layout-margin_cell" style="padding: 0px 20px;" align="center" valign="top"> <table class="layout layout--2-column layout--divided" style="table-layout: fixed;" width="100%" border="0" cellpadding="0" cellspacing="0"> <tr> <td class="column column--1 scale stack" style="width: 50%;" align="center" valign="top"> <table class="image image--padding-vertical image--mobile-scale image--mobile-center" width="100%" border="0" cellpadding="0" cellspacing="0"> <tr> <td class="image_container content-padding-horizontal" style="padding: 10px 20px;" align="center" valign="top"> <table class="image_container-caption text" border="0" cellpadding="0" cellspacing="0" style="table-layout: fixed;"> <tr> <td class="text_content-cell" style="text-align: center; font-family: Arial,Verdana,Helvetica,sans-serif; color: #505362; font-size: 14px; line-height: 1.2; display: block; word-wrap: break-word; padding: 0px;" align="center"> <img data-image-content class="image_content" style="display: block; height: auto; max-width: 100%;" width="228" src="https://files.constantcontact.com/ab63f732801/516a7c6c-39c1-4303-bca3-4d34701217d3.jpg?rdr=true" alt=""> </td> </tr> </table> </td> </tr> </table> </td> <td class="column-divider scale stack" style="height: 1px; line-height: 1px;" width="20" align="center" valign="top"> <img alt="" width="20" height="20" border="0" hspace="0" vspace="0" src="https://imgssl.constantcontact.com/letters/images/1101116784221/S.gif"> </td> <td class="column column-border column--2 scale stack" style="width: 50%; border: 2px solid #CCCCCC;" align="center" valign="top"> <table class="image image--padding-vertical image--mobile-scale image--mobile-center" width="100%" border="0" cellpadding="0" cellspacing="0"> <tr> <td class="image_container content-padding-horizontal" style="padding: 10px 20px;" align="center" valign="top"> <table class="image_container-caption text" border="0" cellpadding="0" cellspacing="0" style="table-layout: fixed;"> <tr> <td class="text_content-cell" style="text-align: center; font-family: Arial,Verdana,Helvetica,sans-serif; color: #505362; font-size: 14px; line-height: 1.2; display: block; word-wrap: break-word; padding: 0px;" align="center"> <img data-image-content class="image_content" style="display: block; height: auto; max-width: 100%;" width="226" src="https://files.constantcontact.com/ab63f732801/fb49787c-3bd2-4f87-b32a-06897f51775f.jpg?rdr=true" alt=""> </td> </tr> </table> </td> </tr> </table> </td> </tr> </table> </td> </tr> </table> <table class="layout-margin" style="" width="100%" border="0" cellpadding="0" cellspacing="0"> <tr> <td class="layout-margin_cell" style="padding: 0px 20px;" align="center" valign="top"> <table class="layout layout--2-column layout--divided" style="table-layout: fixed;" width="100%" border="0" cellpadding="0" cellspacing="0"> <tr> <td class="column column--1 scale stack" style="width: 50%;" align="center" valign="top"> <table class="image image--padding-vertical image--mobile-scale image--mobile-center" width="100%" border="0" cellpadding="0" cellspacing="0"> <tr> <td class="image_container content-padding-horizontal" style="padding: 10px 20px;" align="center" valign="top"> <table class="image_container-caption text" border="0" cellpadding="0" cellspacing="0" style="table-layout: fixed;"> <tr> <td class="text_content-cell" style="text-align: center; font-family: Arial,Verdana,Helvetica,sans-serif; color: #505362; font-size: 14px; line-height: 1.2; display: block; word-wrap: break-word; padding: 0px;" align="center"> <img data-image-content class="image_content" style="display: block; height: auto; max-width: 100%;" width="209" src="https://files.constantcontact.com/ab63f732801/049d3865-5964-4937-8267-8e6d43849004.jpg?rdr=true" alt=""> </td> </tr> </table> </td> </tr> </table> </td> <td class="column-divider scale stack" style="height: 1px; line-height: 1px;" width="20" align="center" valign="top"> <img alt="" width="20" height="20" border="0" hspace="0" vspace="0" src="https://imgssl.constantcontact.com/letters/images/1101116784221/S.gif"> </td> <td class="column column-border column--2 scale stack" style="width: 50%; border: 2px solid #CCCCCC;" align="center" valign="top"> <table class="image image--padding-vertical image--mobile-scale image--mobile-center" width="100%" border="0" cellpadding="0" cellspacing="0"> <tr> <td class="image_container content-padding-horizontal" style="padding: 10px 20px;" align="center" valign="top"> <table class="image_container-caption text" border="0" cellpadding="0" cellspacing="0" style="table-layout: fixed;"> <tr> <td class="text_content-cell" style="text-align: center; font-family: Arial,Verdana,Helvetica,sans-serif; color: #505362; font-size: 14px; line-height: 1.2; display: block; word-wrap: break-word; padding: 0px;" align="center"> <img data-image-content class="image_content" style="display: block; height: auto; max-width: 100%;" width="228" src="https://files.constantcontact.com/ab63f732801/2fc3901c-b9ee-4203-853d-2b20c38e9dcc.jpg?rdr=true" alt=""> </td> </tr> </table> </td> </tr> </table> </td> </tr> </table> </td> </tr> </table> <table class="layout layout--heading layout--1-column" style="background-color: #F0F0F7; table-layout: fixed;" width="100%" border="0" cellpadding="0" cellspacing="0" bgcolor="#F0F0F7"> <tr> <td class="column column--1 scale stack" style="width: 100%;" align="center" valign="top"> <table class="text text--padding-vertical" width="100%" border="0" cellpadding="0" cellspacing="0" style="table-layout: fixed;"> <tr> <td class="text_content-cell content-padding-horizontal" style="text-align: center; font-family: Arial,Verdana,Helvetica,sans-serif; color: #505362; font-size: 14px; line-height: 1.2; display: block; word-wrap: break-word; padding: 10px 20px;" align="center" valign="top">
<p style="margin: 0;"><br></p>
<p style="margin: 0;"><span style="font-size: 23px; color: rgb(54, 97, 189); font-weight: bold;">742 - 746 NW 70 ST</span></p>
<p style="margin: 0;"><span style="font-size: 23px; color: rgb(54, 97, 189); font-weight: bold;">MIAMI, FL 33127</span></p>
<p style="margin: 0;"><span style="font-size: 23px; color: rgb(255, 0, 0);">NEW CONSTRUCTION </span></p>
<p style="margin: 0;"><span style="font-size: 23px; color: rgb(255, 0, 0);">TRIPLEX</span></p>
<p style="margin: 0;"><br></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(255, 0, 0);">Video:</span><span style="font-size: 16px; color: rgb(255, 0, 0); background-color: rgb(240, 240, 247); font-weight: bold;"> </span><span style="font-size: 16px; color: rgb(54, 97, 189); background-color: rgb(240, 240, 247); font-weight: bold;">https://youtu.be/Yhvl5SgHl-o?feature=shared</span></p>
<p style="margin: 0;"><br></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;">Sq. Ft Lot: 6,400</span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;">2 Story Triplex</span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;">Unit 1) 3 Bd / 2 Ba </span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;">Unit 2) 3 Bd / 2 Ba</span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;">Unit 3) 3Bd / 2 Ba</span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;">Subdivision: Henry Ford Sub</span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;">3 Electric Meters</span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;">3 Kitchens</span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;">3 Central A/C's</span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;">Only $440/ppsf</span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;">Accepting FHA, Conventional, Hard Money</span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;">VACANT - Showing by Appointment</span></p>
<p style="margin: 0;"><br></p>
<p style="margin: 0;"><br></p>
<p style="margin: 0;"><span style="font-size: 24px; color: rgb(54, 97, 189); font-weight: bold;">Asking Price</span><span style="font-size: 24px; color: rgb(34, 34, 34);">&#xa0;</span><span style="font-size: 26px; color: rgb(255, 0, 0); font-weight: bold;">$1,200,000</span></p>
<p style="margin: 0;"><br></p>
<h1 style="font-family: Arial,Verdana,Helvetica,sans-serif; color: #00BBD2; font-size: 26px; font-weight: bold; margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34);">&#xa0;Call/Text Alex: (954) 328-6498</span></h1>
</td> </tr> </table> </td> </tr> </table> <table class="layout layout--1-column" style="table-layout: fixed;" width="100%" border="0" cellpadding="0" cellspacing="0"> <tr> <td class="column column--1 scale stack" style="width: 100%;" align="center" valign="top">
<table class="divider" width="100%" cellpadding="0" cellspacing="0" border="0"> <tr> <td class="divider_container content-padding-horizontal" style="padding: 10px 20px;" width="100%" align="center" valign="top"> <table class="divider_content-row" style="height: 1px; width: 100%;" cellpadding="0" cellspacing="0" border="0"> <tr> <td class="divider_content-cell" style="padding-bottom: 10px; background-color: #646464; height: 1px; line-height: 1px; border-bottom-width: 0px;" height="1" align="center" bgcolor="#646464"> <img alt="" width="5" height="1" border="0" hspace="0" vspace="0" src="https://imgssl.constantcontact.com/letters/images/1101116784221/S.gif" style="display: block; height: 1px; width: 5px;"> </td> </tr> </table> </td> </tr> </table> </td> </tr> </table> <table class="layout layout--1-column" style="table-layout: fixed;" width="100%" border="0" cellpadding="0" cellspacing="0"> <tr> <td class="column column--1 scale stack" style="width: 100%;" align="center" valign="top"> <table class="image image--padding-vertical image--mobile-scale image--mobile-center" width="100%" border="0" cellpadding="0" cellspacing="0"> <tr> <td class="image_container content-padding-horizontal" style="padding: 10px 20px;" align="center" valign="top"> <table class="image_container-caption text" border="0" cellpadding="0" cellspacing="0" style="table-layout: fixed;"> <tr> <td class="text_content-cell" style="text-align: center; font-family: Arial,Verdana,Helvetica,sans-serif; color: #505362; font-size: 14px; line-height: 1.2; display: block; word-wrap: break-word; padding: 0px;" align="center"> <img data-image-content class="image_content" style="display: block; height: auto; max-width: 100%;" width="558" src="https://files.constantcontact.com/ab63f732801/bb49bf47-0d88-4a7c-bf7e-f2a3c83136e9.jpg?rdr=true" alt=""> </td> </tr> </table> </td> </tr> </table> </td> </tr> </table> <table class="layout-margin" style="" width="100%" border="0" cellpadding="0" cellspacing="0"> <tr> <td class="layout-margin_cell" style="padding: 0px 20px;" align="center" valign="top"> <table class="layout layout--1-column layout--divided" style="table-layout: fixed;" width="100%" border="0" cellpadding="0" cellspacing="0"> <tr> <td class="column column-border column--1 scale stack" style="width: 100%; border: 2px solid #CCCCCC;" align="center" valign="top"> <table class="image image--padding-vertical image--mobile-scale image--mobile-center" width="100%" border="0" cellpadding="0" cellspacing="0"> <tr> <td class="image_container content-padding-horizontal" style="padding: 10px 20px;" align="center" valign="top"> <table class="image_container-caption text" border="0" cellpadding="0" cellspacing="0" style="table-layout: fixed;"> <tr> <td class="text_content-cell" style="text-align: center; font-family: Arial,Verdana,Helvetica,sans-serif; color: #505362; font-size: 14px; line-height: 1.2; display: block; word-wrap: break-word; padding: 0px;" align="center"> <img data-image-content class="image_content" style="display: block; height: auto; max-width: 100%;" width="516" src="https://files.constantcontact.com/ab63f732801/71a7fb01-01d6-4900-acab-6a11adc33017.jpg?rdr=true" alt=""> </td> </tr> </table> </td> </tr> </table> </td> </tr> </table> </td> </tr> </table> <table class="layout layout--heading layout--1-column" style="background-color: #F0F0F7; table-layout: fixed;" width="100%" border="0" cellpadding="0" cellspacing="0" bgcolor="#F0F0F7"> <tr> <td class="column column--1 scale stack" style="width: 100%;" align="center" valign="top"> <table class="text text--padding-vertical" width="100%" border="0" cellpadding="0" cellspacing="0" style="table-layout: fixed;"> <tr> <td class="text_content-cell content-padding-horizontal" style="text-align: center; font-family: Arial,Verdana,Helvetica,sans-serif; color: #505362; font-size: 14px; line-height: 1.2; display: block; word-wrap: break-word; padding: 10px 20px;" align="center" valign="top">
<p style="margin: 0;"><span style="font-size: 23px; color: rgb(54, 97, 189); font-weight: bold;">8** NW&#xa0;56 St</span></p>
<p style="margin: 0;"><span style="font-size: 23px; color: rgb(54, 97, 189); font-weight: bold;">Miami, FL 33127</span></p>
<p style="margin: 0;"><span style="font-size: 23px; color: rgb(255, 0, 0); font-weight: bold;">*Duplex*</span></p>
<p style="margin: 0;"><br></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;">5 Bedrooms / 3 Bathrooms Total </span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;">Unit A: 3Bed/2Bath </span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;"><span class="ql-cursor">&#xfeff;</span>Unit B: 2Bed/1Bath</span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;">1,776 Sq Ft</span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;"> 5,200 Sq Ft Lot </span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;">CBS - Built 1949 </span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;"> Duplex in City of Miami</span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;">Zoned T3 0</span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;">2 Legal Living Units</span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;">Electrical Meter Installed </span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;">Needs Full Rehab</span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;">Ready for renovation</span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;">Excellent Investment Area </span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;">See Comps Below</span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;">Vacant on Lockbox!</span></p>
<p style="margin: 0;"><br></p>
<p style="margin: 0;"><br></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;">Comp 1) 545 NW 45 St- SOLD- $910,000</span><span style="font-size: 20px; color: rgb(255, 0, 0); font-weight: bold;"> </span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;"> Comp 2) 529 NW 43 St - SOLD - $780,000 </span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;"> Comp 3) 1030 NW 57 St - SOLD - $630,000 </span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;"> Comp 4) 729 NW 55 St - SOLD - $579,900 </span></p>
<p style="margin: 0;"><br></p>
<p style="margin: 0;"><br></p>
<p style="margin: 0;"><span style="font-size: 24px; color: rgb(54, 97, 189); font-weight: bold;">Asking Price</span><span style="font-size: 24px; color: rgb(34, 34, 34);">&#xa0;</span><span style="font-size: 26px; color: rgb(255, 0, 0); font-weight: bold;">$338,000</span></p>
<p style="margin: 0;"><br></p>
<p style="margin: 0;"><br></p>
<h1 style="font-family: Arial,Verdana,Helvetica,sans-serif; color: #00BBD2; font-size: 26px; font-weight: bold; margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34);">&#xa0;Call/Text Alex: (954) 328-6498</span></h1>
<h1 style="font-family: Arial,Verdana,Helvetica,sans-serif; color: #00BBD2; font-size: 26px; font-weight: bold; margin: 0;"><br></h1>
</td> </tr> </table> </td> </tr> </table> <table class="layout layout--1-column" style="table-layout: fixed;" width="100%" border="0" cellpadding="0" cellspacing="0"> <tr> <td class="column column--1 scale stack" style="width: 100%;" align="center" valign="top">
<table class="divider" width="100%" cellpadding="0" cellspacing="0" border="0"> <tr> <td class="divider_container content-padding-horizontal" style="padding: 10px 20px;" width="100%" align="center" valign="top"> <table class="divider_content-row" style="height: 1px; width: 100%;" cellpadding="0" cellspacing="0" border="0"> <tr> <td class="divider_content-cell" style="padding-bottom: 10px; background-color: #646464; height: 1px; line-height: 1px; border-bottom-width: 0px;" height="1" align="center" bgcolor="#646464"> <img alt="" width="5" height="1" border="0" hspace="0" vspace="0" src="https://imgssl.constantcontact.com/letters/images/1101116784221/S.gif" style="display: block; height: 1px; width: 5px;"> </td> </tr> </table> </td> </tr> </table> </td> </tr> </table> <table class="layout layout--1-column" style="table-layout: fixed;" width="100%" border="0" cellpadding="0" cellspacing="0"> <tr> <td class="column column--1 scale stack" style="width: 100%;" align="center" valign="top"> <table class="image image--padding-vertical image--mobile-scale image--mobile-center" width="100%" border="0" cellpadding="0" cellspacing="0"> <tr> <td class="image_container content-padding-horizontal" style="padding: 10px 20px;" align="center" valign="top"> <table class="image_container-caption text" border="0" cellpadding="0" cellspacing="0" style="table-layout: fixed;"> <tr> <td class="text_content-cell" style="text-align: center; font-family: Arial,Verdana,Helvetica,sans-serif; color: #505362; font-size: 14px; line-height: 1.2; display: block; word-wrap: break-word; padding: 0px;" align="center"> <img data-image-content class="image_content" style="display: block; height: auto; max-width: 100%;" width="396" src="https://files.constantcontact.com/ab63f732801/aee42816-dafa-409f-a9fd-8381cb31f603.jpg?rdr=true" alt=""> </td> </tr> </table> </td> </tr> </table> </td> </tr> </table> <table class="layout layout--heading layout--1-column" style="background-color: #F0F0F7; table-layout: fixed;" width="100%" border="0" cellpadding="0" cellspacing="0" bgcolor="#F0F0F7"> <tr> <td class="column column--1 scale stack" style="width: 100%;" align="center" valign="top"> <table class="text text--padding-vertical" width="100%" border="0" cellpadding="0" cellspacing="0" style="table-layout: fixed;"> <tr> <td class="text_content-cell content-padding-horizontal" style="text-align: center; font-family: Arial,Verdana,Helvetica,sans-serif; color: #505362; font-size: 14px; line-height: 1.2; display: block; word-wrap: break-word; padding: 10px 20px;" align="center" valign="top">
<p style="margin: 0;"><span style="font-size: 23px; color: rgb(54, 97, 189); font-weight: bold;">Tropical Flower Farm</span></p>
<p style="margin: 0;"><span style="font-size: 23px; color: rgb(54, 97, 189); font-weight: bold;">Ocala, FL 32668</span></p>
<p style="margin: 0;"><span style="font-size: 23px; color: rgb(255, 0, 0);">*12 ACRES*</span></p>
<p style="margin: 0;"><br></p>
<p style="margin: 0;"><br></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;">Fully Equipped 12 Acre Farm w/ Landing Airstrip</span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;">FA30 Airstrip</span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;">Landing Runway length is 2,700Ft (823 Meters)</span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;">Agricultural Land with Licenses</span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;">Large indoor building w/ AC</span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;">4 Greenhouses</span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;">100% Turnkey - 100% Temperature Regulated</span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;">County : Levy</span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;">Call for Private Showing!</span></p>
<p style="margin: 0;"><br></p>
<p style="margin: 0;"><br></p>
<p style="margin: 0;"><span style="font-size: 24px; color: rgb(54, 97, 189); font-weight: bold;">Asking Price</span><span style="font-size: 24px; color: rgb(34, 34, 34);">&#xa0;</span><span style="font-size: 26px; color: rgb(255, 0, 0); font-weight: bold;">$955,000</span></p>
<p style="margin: 0;"><br></p>
<h1 style="font-family: Arial,Verdana,Helvetica,sans-serif; color: #00BBD2; font-size: 26px; font-weight: bold; margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34);">&#xa0;Call/Text Alex: (954) 328-6498</span></h1>
</td> </tr> </table> </td> </tr> </table> <table class="layout layout--1-column" style="table-layout: fixed;" width="100%" border="0" cellpadding="0" cellspacing="0"> <tr> <td class="column column--1 scale stack" style="width: 100%;" align="center" valign="top">
<table class="divider" width="100%" cellpadding="0" cellspacing="0" border="0"> <tr> <td class="divider_container content-padding-horizontal" style="padding: 10px 20px;" width="100%" align="center" valign="top"> <table class="divider_content-row" style="height: 1px; width: 100%;" cellpadding="0" cellspacing="0" border="0"> <tr> <td class="divider_content-cell" style="padding-bottom: 10px; background-color: #5A5A5A; height: 1px; line-height: 1px; border-bottom-width: 0px;" height="1" align="center" bgcolor="#5A5A5A"> <img alt="" width="5" height="1" border="0" hspace="0" vspace="0" src="https://imgssl.constantcontact.com/letters/images/1101116784221/S.gif" style="display: block; height: 1px; width: 5px;"> </td> </tr> </table> </td> </tr> </table> </td> </tr> </table> <table class="layout layout--1-column" style="table-layout: fixed;" width="100%" border="0" cellpadding="0" cellspacing="0"> <tr> <td class="column column--1 scale stack" style="width: 100%;" align="center" valign="top"> <table class="image image--padding-vertical image--mobile-scale image--mobile-center" width="100%" border="0" cellpadding="0" cellspacing="0"> <tr> <td class="image_container content-padding-horizontal" style="padding: 10px 20px;" align="center" valign="top"> <table class="image_container-caption text" border="0" cellpadding="0" cellspacing="0" style="table-layout: fixed;"> <tr> <td class="text_content-cell" style="text-align: center; font-family: Arial,Verdana,Helvetica,sans-serif; color: #505362; font-size: 14px; line-height: 1.2; display: block; word-wrap: break-word; padding: 0px;" align="center"> <img data-image-content class="image_content" style="display: block; height: auto; max-width: 100%;" width="502" src="https://files.constantcontact.com/ab63f732801/205859cc-a54e-4f47-86b6-c74cf76a57eb.jpg?rdr=true" alt=""> </td> </tr> </table> </td> </tr> </table> </td> </tr> </table> <table class="layout layout--1-column" style="table-layout: fixed;" width="100%" border="0" cellpadding="0" cellspacing="0"> <tr> <td class="column column--1 scale stack" style="width: 100%;" align="center" valign="top"> <table class="image image--padding-vertical image--mobile-scale image--mobile-center" width="100%" border="0" cellpadding="0" cellspacing="0"> <tr> <td class="image_container content-padding-horizontal" style="padding: 10px 20px;" align="center" valign="top"> <table class="image_container-caption text" border="0" cellpadding="0" cellspacing="0" style="table-layout: fixed;"> <tr> <td class="text_content-cell" style="text-align: center; font-family: Arial,Verdana,Helvetica,sans-serif; color: #505362; font-size: 14px; line-height: 1.2; display: block; word-wrap: break-word; padding: 0px;" align="center"> <img data-image-content class="image_content" style="display: block; height: auto; max-width: 100%;" width="511" src="https://files.constantcontact.com/ab63f732801/ee6c87ef-be08-4625-9773-54f421ac0792.jpg?rdr=true" alt=""> </td> </tr> </table> </td> </tr> </table> </td> </tr> </table> <table class="layout layout--heading layout--1-column" style="background-color: #F0F0F7; table-layout: fixed;" width="100%" border="0" cellpadding="0" cellspacing="0" bgcolor="#F0F0F7"> <tr> <td class="column column--1 scale stack" style="width: 100%;" align="center" valign="top"> <table class="text text--padding-vertical" width="100%" border="0" cellpadding="0" cellspacing="0" style="table-layout: fixed;"> <tr> <td class="text_content-cell content-padding-horizontal" style="text-align: center; font-family: Arial,Verdana,Helvetica,sans-serif; color: #505362; font-size: 14px; line-height: 1.2; display: block; word-wrap: break-word; padding: 10px 20px;" align="center" valign="top">
<p style="margin: 0;"><span style="font-size: 23px; color: rgb(54, 97, 189); font-weight: bold;">729 NW 55 TERR.</span></p>
<p style="margin: 0;"><span style="font-size: 23px; color: rgb(54, 97, 189); font-weight: bold;">Miami, FL 33127</span></p>
<p style="margin: 0;"><span style="font-size: 23px; color: rgb(54, 97, 189); font-weight: bold;">*8-UNIT MULTIFAMILY - Off Market*</span></p>
<p style="margin: 0;"><br></p>
<p style="margin: 0;"><span style="font-size: 23px; color: rgb(255, 0, 0);">*8-PLEX*</span></p>
<p style="margin: 0;"><br></p>
<p style="margin: 0;"><br></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(36, 36, 36); font-weight: bold;">Off Market</span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(36, 36, 36); font-weight: bold;">&#xfeff;8 Living Units</span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(36, 36, 36); font-weight: bold;">Sq.Ft : 4,232</span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(36, 36, 36); font-weight: bold;">Sq.Ft Lot : 13,100</span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(36, 36, 36); font-weight: bold;">3 Separate CBS Buildings</span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(36, 36, 36); font-weight: bold;">4) 2 Bed / 1 Bath</span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(36, 36, 36); font-weight: bold;">4) 1 Bed / 1 Bath</span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(36, 36, 36); font-weight: bold;">Newly Remodeled - Fenced In</span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(36, 36, 36); font-weight: bold;">New Roof, New Impact Windows, New Plumbing</span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(36, 36, 36); font-weight: bold;">8 Electric Meters</span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(36, 36, 36); font-weight: bold;">2 Water Meters</span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(36, 36, 36); font-weight: bold;">Zoning Permits 10 Units</span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(36, 36, 36); font-weight: bold;">Vacant on Supra</span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(36, 36, 36); font-weight: bold;">Year Remodeled: 2024</span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(36, 36, 36); font-weight: bold;">Call for Access!</span></p>
<p style="margin: 0;"><br></p>
<p style="margin: 0;"><br></p>
<p style="margin: 0;"><br></p>
<p style="margin: 0;"><span style="font-size: 24px; color: rgb(54, 97, 189); font-weight: bold;">Asking Price</span><span style="font-size: 24px; color: rgb(0, 187, 210);">&#xa0;</span><span style="font-size: 26px; color: rgb(255, 0, 0); font-weight: bold;">$1.75M</span></p>
<p style="margin: 0;"><br></p>
<h1 style="font-family: Arial,Verdana,Helvetica,sans-serif; color: #00BBD2; font-size: 26px; font-weight: bold; margin: 0;"><span style="font-size: 20px; color: rgb(0, 0, 0);">&#xa0;Call/Text Alex: (954) 328-6498</span></h1>
</td> </tr> </table> </td> </tr> </table> <table class="layout layout--1-column" style="table-layout: fixed;" width="100%" border="0" cellpadding="0" cellspacing="0"> <tr> <td class="column column--1 scale stack" style="width: 100%;" align="center" valign="top">
<table class="divider" width="100%" cellpadding="0" cellspacing="0" border="0"> <tr> <td class="divider_container content-padding-horizontal" style="padding: 10px 20px;" width="100%" align="center" valign="top"> <table class="divider_content-row" style="height: 1px; width: 100%;" cellpadding="0" cellspacing="0" border="0"> <tr> <td class="divider_content-cell" style="padding-bottom: 10px; background-color: #5A5A5A; height: 1px; line-height: 1px; border-bottom-width: 0px;" height="1" align="center" bgcolor="#5A5A5A"> <img alt="" width="5" height="1" border="0" hspace="0" vspace="0" src="https://imgssl.constantcontact.com/letters/images/1101116784221/S.gif" style="display: block; height: 1px; width: 5px;"> </td> </tr> </table> </td> </tr> </table> </td> </tr> </table> <table class="layout-margin" style="" width="100%" border="0" cellpadding="0" cellspacing="0"> <tr> <td class="layout-margin_cell" style="padding: 0px 20px;" align="center" valign="top"> <table class="layout layout--2-column layout--divided" style="table-layout: fixed;" width="100%" border="0" cellpadding="0" cellspacing="0"> <tr> <td class="column column-border column--1 scale stack" style="width: 50%; border: 2px solid #CCCCCC;" align="center" valign="top"> <table class="image image--padding-vertical image--mobile-scale image--mobile-center" width="100%" border="0" cellpadding="0" cellspacing="0"> <tr> <td class="image_container content-padding-horizontal" style="padding: 10px 20px;" align="center" valign="top"> <table class="image_container-caption text" border="0" cellpadding="0" cellspacing="0" style="table-layout: fixed;"> <tr> <td class="text_content-cell" style="text-align: center; font-family: Arial,Verdana,Helvetica,sans-serif; color: #505362; font-size: 14px; line-height: 1.2; display: block; word-wrap: break-word; padding: 0px;" align="center"> <img data-image-content class="image_content" style="display: block; height: auto; max-width: 100%;" width="215" src="https://files.constantcontact.com/ab63f732801/53ad2790-aabe-42e8-a5da-5d260f1182bb.jpg?rdr=true" alt=""> </td> </tr> </table> </td> </tr> </table> </td> <td class="column-divider scale stack" style="height: 1px; line-height: 1px;" width="20" align="center" valign="top"> <img alt="" width="20" height="20" border="0" hspace="0" vspace="0" src="https://imgssl.constantcontact.com/letters/images/1101116784221/S.gif"> </td> <td class="column column-border column--2 scale stack" style="width: 50%; border: 2px solid #CCCCCC;" align="center" valign="top"> <table class="image image--padding-vertical image--mobile-scale image--mobile-center" width="100%" border="0" cellpadding="0" cellspacing="0"> <tr> <td class="image_container content-padding-horizontal" style="padding: 10px 20px;" align="center" valign="top"> <table class="image_container-caption text" border="0" cellpadding="0" cellspacing="0" style="table-layout: fixed;"> <tr> <td class="text_content-cell" style="text-align: center; font-family: Arial,Verdana,Helvetica,sans-serif; color: #505362; font-size: 14px; line-height: 1.2; display: block; word-wrap: break-word; padding: 0px;" align="center"> <img data-image-content class="image_content" style="display: block; height: auto; max-width: 100%;" width="215" src="https://files.constantcontact.com/ab63f732801/38f21bc0-40e9-4af3-9543-e6e6b3d33ef6.jpg?rdr=true" alt=""> </td> </tr> </table> </td> </tr> </table> </td> </tr> </table> </td> </tr> </table> <table class="layout layout--heading layout--1-column" style="background-color: #F0F0F7; table-layout: fixed;" width="100%" border="0" cellpadding="0" cellspacing="0" bgcolor="#F0F0F7"> <tr> <td class="column column--1 scale stack" style="width: 100%;" align="center" valign="top"> <table class="text text--padding-vertical" width="100%" border="0" cellpadding="0" cellspacing="0" style="table-layout: fixed;"> <tr> <td class="text_content-cell content-padding-horizontal" style="text-align: center; font-family: Arial,Verdana,Helvetica,sans-serif; color: #505362; font-size: 14px; line-height: 1.2; display: block; word-wrap: break-word; padding: 10px 20px;" align="center" valign="top">
<p style="margin: 0;"><span style="font-size: 23px; color: rgb(54, 97, 189); font-weight: bold;">400 Sunny Isles Blvd #1005</span></p>
<p style="margin: 0;"><span style="font-size: 23px; color: rgb(54, 97, 189); font-weight: bold;">Sunny Isles Beach, FL 33160</span></p>
<p style="margin: 0;"><br></p>
<p style="margin: 0;"><br></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;">&#xfeff;3 Bedrooms / 2.5 Bathrooms </span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;">Sq. Ft: 1,705</span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;">North &amp; South Water Views</span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;">Walk to the Beaches of Sunny Isles</span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;">Heated Infinity Pool</span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;">Dry Storage for Boats/Yachts Marina w/ Restaurant</span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;">Free Valet for Guests</span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;">Water front Tennis, Pool, Jacuzzi, Steam Room, Sauna, Gym</span></p>
<p style="margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34); font-weight: bold;"> Call for Private Showing!</span></p>
<p style="margin: 0;"><br></p>
<p style="margin: 0;"><br></p>
<p style="margin: 0;"><span style="font-size: 24px; color: rgb(54, 97, 189); font-weight: bold;">Asking Price</span><span style="font-size: 24px; color: rgb(34, 34, 34);">&#xa0;</span><span style="font-size: 26px; color: rgb(255, 0, 0); font-weight: bold;">$1,200,000</span></p>
<p style="margin: 0;"><br></p>
<p style="margin: 0;"><br></p>
<h1 style="font-family: Arial,Verdana,Helvetica,sans-serif; color: #00BBD2; font-size: 26px; font-weight: bold; margin: 0;"><span style="font-size: 20px; color: rgb(34, 34, 34);">&#xa0;Call/Text Alex: (954) 328-6498</span></h1>
</td> </tr> </table> </td> </tr> </table> <table class="layout layout--1-column" style="table-layout: fixed;" width="100%" border="0" cellpadding="0" cellspacing="0"> <tr> <td class="column column--1 scale stack" style="width: 100%;" align="center" valign="top"> <table class="image image--padding-vertical image--mobile-scale image--mobile-center" width="100%" border="0" cellpadding="0" cellspacing="0"> <tr> <td class="image_container" style="padding-top: 10px; padding-bottom: 10px;" align="center" valign="top"> <table class="image_container-caption text" border="0" cellpadding="0" cellspacing="0" style="table-layout: fixed;"> <tr> <td class="text_content-cell" style="text-align: center; font-family: Arial,Verdana,Helvetica,sans-serif; color: #505362; font-size: 14px; line-height: 1.2; display: block; word-wrap: break-word; padding: 0px;" align="center"> <img data-image-content class="image_content" style="display: block; height: auto; max-width: 100%;" width="600" src="https://imgssl.constantcontact.com/letters/images/PT1594/divider.png" alt=""> </td> </tr> </table> </td> </tr> </table> </td> </tr> </table> <table class="layout layout--feature layout--2-column" style="background-color: #FFFFFF; table-layout: fixed;" width="100%" border="0" cellpadding="0" cellspacing="0" bgcolor="#FFFFFF"> <tr> <td class="column column--1 scale stack" style="width: 50%;" align="center" valign="top"> <table class="text text--padding-vertical" width="100%" border="0" cellpadding="0" cellspacing="0" style="table-layout: fixed;"> <tr> <td class="text_content-cell content-padding-horizontal" style="text-align: center; font-family: Arial,Verdana,Helvetica,sans-serif; color: #505362; font-size: 14px; line-height: 1.2; display: block; word-wrap: break-word; padding: 10px 10px 10px 20px;" align="center" valign="top"><p style="margin: 0;"><span style="font-size: 16px; color: rgb(54, 97, 189); font-weight: bold;">If any property sparks your interest, contact Lexico Realty!</span></p></td> </tr> </table> <table class="divider" width="100%" cellpadding="0" cellspacing="0" border="0"> <tr> <td class="divider_container" style="padding-top: 9px; padding-bottom: 9px;" width="100%" align="center" valign="top"> <table class="divider_content-row" style="width: 99%; height: 1px;" cellpadding="0" cellspacing="0" border="0"> <tr> <td class="divider_content-cell" style="padding-bottom: 0px; background-color: #3661BD; height: 1px; line-height: 1px; border-bottom-width: 0px;" height="1" align="center" bgcolor="#3661BD"> <img alt="" width="5" height="1" border="0" hspace="0" vspace="0" src="https://imgssl.constantcontact.com/letters/images/1101116784221/S.gif" style="display: block; height: 1px; width: 5px;"> </td> </tr> </table> </td> </tr> </table> <table class="text text--padding-vertical" width="100%" border="0" cellpadding="0" cellspacing="0" style="table-layout: fixed;"> <tr> <td class="text_content-cell content-padding-horizontal" style="line-height: 1; text-align: center; font-family: Arial,Verdana,Helvetica,sans-serif; color: #505362; font-size: 14px; display: block; word-wrap: break-word; padding: 10px 10px 10px 20px;" align="center" valign="top">
<p style="margin: 0;"><span style="font-size: 16px; color: rgb(54, 97, 189); font-weight: bold;">Alejandro Patino</span></p>
<p style="margin: 0;"><br></p>
<p style="margin: 0;"><span style="font-size: 16px; color: rgb(54, 97, 189); font-weight: bold;">Licensed Real Estate Broker</span></p>
<p style="margin: 0;"><br></p>
<p style="margin: 0;"><span style="font-size: 16px; color: rgb(54, 97, 189); font-weight: bold;">Alex@LexicoRealty.com </span></p>
<p style="margin: 0;"><span style="font-size: 16px; color: rgb(54, 97, 189); font-weight: bold;">&#xfeff;</span></p>
<p style="margin: 0;"><span style="font-size: 22px; color: rgb(54, 97, 189); font-weight: bold;">(954)328-6498</span></p>
</td> </tr> </table> </td> <td class="column column--2 scale stack" style="width: 50%;" align="center" valign="top"> <table class="image image--padding-vertical image--mobile-scale image--mobile-center" width="100%" border="0" cellpadding="0" cellspacing="0"> <tr> <td class="image_container" style="padding-top: 10px; padding-bottom: 10px;" align="center" valign="top"> <table class="image_container-caption text" border="0" cellpadding="0" cellspacing="0" style="table-layout: fixed;"> <tr> <td class="text_content-cell" style="text-align: center; font-family: Arial,Verdana,Helvetica,sans-serif; color: #505362; font-size: 14px; line-height: 1.2; display: block; word-wrap: break-word; padding: 0px;" align="center"> <img data-image-content class="image_content" style="display: block; height: auto; max-width: 100%;" width="300" src="https://files.constantcontact.com/ab63f732801/8db2372c-0d7c-4a4f-9d99-76986629c7ab.png?rdr=true" alt=""> </td> </tr> </table> </td> </tr> </table> </td> </tr> </table> <table class="layout layout--1-column" style="table-layout: fixed;" width="100%" border="0" cellpadding="0" cellspacing="0"> <tr> <td class="column column--1 scale stack" style="width: 100%;" align="center" valign="top"> <table class="socialFollow socialFollow--padding-vertical" width="100%" cellpadding="0" cellspacing="0" border="0"> <tr> <td class="socialFollow_container content-padding-horizontal" style="height: 1px; line-height: 1px; padding: 10px 20px;" width="100%" align="center" valign="top"> <a href="https://9vn5xrabb.cc.rs6.net/tn.jsp?f=001VFBbpKuIWGWciNdVUgPaxKMUKaV_VtjCkVh4IF9YAZCU0XjH4rItxpI2ei7zW89gAG57ExrjfwI1dSVgmI9pOOQxCoi4QF-v-D-NPtLGbKCpSgacr0iqfA0EBvBNEzrTV3HQMZndA08HxoPG01S2J2qUqyS-6gh7TrCY6RAZXa6ccyVfVnhDLA==&c=HOxKu4lVZ1_RrWBhdB_E0Ev45m1BOzE9a8fwBJRbNWFej8XzoYgT0g==&ch=1KbpNw6x3-F9jSrzYoRCSyLiPvg1KtEXSK-KUGyaYLzGEySwngR6nQ==" data-trackable="true"><img class="socialFollow_icon" alt="Instagram" width="32" border="0" src="https://imgssl.constantcontact.com/letters/images/CPE/SocialIcons/circles/circleColor_Instagram.png" style="display: inline-block; margin: 0; padding: 0;"></a> &#xa0; </td> </tr> </table> </td> </tr> </table> <table class="layout layout--1-column" style="background-color: #000000; table-layout: fixed;" width="100%" border="0" cellpadding="0" cellspacing="0" bgcolor="#000000"> <tr> <td class="column column--1 scale stack" style="width: 100%;" align="center" valign="top"><div class="spacer" style="line-height: 10px; height: 10px;">&#x200a;</div></td> </tr> </table> </td> </tr> </table> </td> </tr> </table> </td> </tr> <tr class=""> <td class="shell_panel-cell shell_panel-cell--footer" style="background-color: #FFFFFF;" align="center" valign="top" bgcolor="#FFFFFF"> <table class="shell_width-row scale" style="width: 630px;" align="center" border="0" cellpadding="0" cellspacing="0"> <tr> <td class="shell_width-cell" style="padding: 0px 10px;" align="center" valign="top"> <table class="shell_content-row" width="100%" align="center" border="0" cellpadding="0" cellspacing="0"> <tr> <td class="shell_content-cell" style="background-color: transparent; padding: 0; border: 0 solid #000000;" align="center" valign="top" bgcolor="transparent"> <table class="layout layout--1-column" style="table-layout: fixed;" width="100%" border="0" cellpadding="0" cellspacing="0"> <tr> <td class="column column--1 scale stack" style="width: 100%;" align="center" valign="top"> <table class="complianceAddress complianceAddress--padding-vertical" width="100%" border="0" cellpadding="0" cellspacing="0"> <tr> <td class="complianceAddress_content-cell content-padding-horizontal" align="center" style="color: #595959; font-family: Verdana,Geneva,sans-serif; font-size: 12px; line-height: 1.2; padding: 10px 20px;"> <p style="margin: 0;"><span>Lexico Realty | </span><span>7900 Harbor Island Dr. </span><span> SUITE #908A | </span><span>Miami Beach, FL 33141 US</span></p> </td> </tr> </table> <table class="complianceLinks complianceLinks--padding-vertical" width="100%" border="0" cellpadding="0" cellspacing="0"> <tr> <td class="complianceLinks_content-cell content-padding-horizontal" align="center" style="color: #595959; font-family: Verdana,Geneva,sans-serif; font-size: 12px; line-height: 1.2; padding: 10px 20px;"> <p style="margin: 0;"> <a href="https://visitor.constantcontact.com/do?p=un&m=001QZnlnRx424I_j1ifNkQT1A%3D&ch=f9d326bc-8d59-11ee-981c-fa163e25e9d8&ca=e8bb1958-5bfc-4ae6-8260-8bfb64254058" data-track="false" style="color: inherit;">Unsubscribe<span></span></a><span> | </span><span></span><span><a href="https://visitor.constantcontact.com/do?p=oo&m=001QZnlnRx424I_j1ifNkQT1A%3D&ch=f9d326bc-8d59-11ee-981c-fa163e25e9d8&ca=e8bb1958-5bfc-4ae6-8260-8bfb64254058" data-track="false" style="color: inherit;">Update Profile</a></span><span> | </span><span><span></span><a href="https://www.constantcontact.com/legal/customer-contact-data-notice" data-track="false" style="color: inherit;">Constant Contact Data Notice</a></span> </p> </td> </tr> </table> </td> </tr> </table> </td> </tr> </table> </td> </tr> </table> </td> </tr> <tr class=""> <td class="shell_panel-cell shell_panel-cell--footer" style="background-color: #FFFFFF;" align="center" valign="top" bgcolor="#FFFFFF"> <table class="shell_width-row scale" style="width: 630px;" align="center" border="0" cellpadding="0" cellspacing="0"> <tr> <td class="shell_width-cell" style="padding: 0px 10px;" align="center" valign="top"> <table class="shell_content-row" width="100%" align="center" border="0" cellpadding="0" cellspacing="0"> <tr> <td class="shell_content-cell" style="background-color: transparent; padding: 0; border: 0 solid #000000;" align="center" valign="top" bgcolor="transparent"> <table class="layout layout--1-column" style="table-layout: fixed;" width="100%" border="0" cellpadding="0" cellspacing="0"> <tr> <td class="column column--1 scale stack" style="width: 100%;" align="center" valign="top"> <table class="image image--mobile-center" width="100%" border="0" cellpadding="0" cellspacing="0"> <tr> <td class="image_container" style="" align="center" valign="top"> <table class="image_container-caption text" border="0" cellpadding="0" cellspacing="0" style="table-layout: fixed;"> <tr> <td class="text_content-cell" style="text-align: center; font-family: Arial,Verdana,Helvetica,sans-serif; color: #505362; font-size: 14px; line-height: 1.2; display: block; word-wrap: break-word; padding: 0px;" align="center"> <a href="https://www.constantcontact.com/landing1/vr/home?cc=nge&utm_campaign=nge&rmc=VF21_CPE&utm_medium=VF21_CPE&utm_source=viral&pn=ROVING&nav=e8bb1958-5bfc-4ae6-8260-8bfb64254058" data-trackable="false" style="color: #3ABFD7; font-weight: bold; text-decoration: underline;"><img data-image-content class="image_content" style="display: block; height: auto; max-width: 100%;" width="220" src="https://imgssl.constantcontact.com/letters/images/CPE/referralLogos/H-Stacked-FC-WhiteBG-Email-Footer.png" alt="Constant Contact"></a> </td> </tr> </table> </td> </tr> </table> </td> </tr> </table> </td> </tr> </table> </td> </tr> </table> </td> </tr> </table> </div> </body> </html>
"""
# data = extract_listings_from_email_html(demo_html)
# print(json.dumps(data, indent=2))