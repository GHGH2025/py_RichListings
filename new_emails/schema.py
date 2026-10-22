"""Snapshot of the existing property schema, shared field contract only."""
from typing import Dict, Any

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
        "unit_count": {"type": ["number", "null"]},
        "building_count": {"type": ["number", "null"]},
        "monthly_rent_usd": {"type": ["number", "null"]},
        "annual_expenses_usd": {"type": ["number", "null"]},
        "estimated_arv": {"type": ["number", "null"]},
        "estimated_repairs": {"type": ["number", "null"]},
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

def response_format():
    return _response_format()
