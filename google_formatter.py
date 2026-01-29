import os
import requests
from dotenv import load_dotenv
from typing import Optional, Dict, Any, List
load_dotenv()
# Put your API key in an env var: GOOGLE_ADDRESS_API_KEY
GOOGLE_ADDRESS_API_KEY = os.getenv("GOOGLE_ADDRESS_API_KEY")

if not GOOGLE_ADDRESS_API_KEY:
    raise RuntimeError("Set GOOGLE_ADDRESS_API_KEY in your environment.")

ENDPOINT = (
    f"https://addressvalidation.googleapis.com/v1:validateAddress"
    f"?key={GOOGLE_ADDRESS_API_KEY}"
)

# ---- Geocoding API (for full geocode result) ----
GOOGLE_GEOCODE_API_KEY = os.getenv("GOOGLE_GEOCODE_API_KEY")
GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"

DEFAULT_TIMEOUT = 12  # seconds

def get_street_and_city(raw_address: str, region_code: str = "US"):
    """
    Takes a raw address string like:
      "3427 Barstow St, Sarasota, FL 34235, USA"

    Returns:
      (street, city, zip_)
    or (None, None, None) if parsing fails.
    """
    payload = {
        "address": {
            "regionCode": region_code,
            "addressLines": [raw_address],
        }
    }

    try:
        resp = requests.post(ENDPOINT, json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        postal = data["result"]["address"]["postalAddress"]

        street = postal["addressLines"][0].strip()         # "3427 Barstow St"
        city   = postal.get("locality", "").strip()        # "Sarasota"
        fz     = postal.get("postalCode", "").strip()

        if not street or not city:
            return None, None, None

        return street, city, fz

    except (KeyError, IndexError):
        return None, None, None
    except requests.RequestException as e:
        print(f"Request error: {e}")
        return None, None, None

def geocode_response(raw_address: str) -> Optional[Dict[str, Any]]:
    """
    Call Google Geocoding API and return the FIRST result object (dict) on success, else None.

    Notes:
      - Requires env var GOOGLE_GEOCODE_API_KEY.
      - Adds components=country:US to keep searches constrained.
    """
    if not GOOGLE_GEOCODE_API_KEY:
        print("[google_formatter] GOOGLE_GEOCODE_API_KEY not set; geocode_response skipped.")
        return None

    params = {
        "address": raw_address,
        "components": "country:US",
        "key": GOOGLE_GEOCODE_API_KEY,
    }
    try:
        resp = requests.get(GEOCODE_URL, params=params, timeout=DEFAULT_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") == "OK" and data.get("results"):
            return data["results"][0]
        return None
    except requests.RequestException as e:
        print(f"[google_formatter] Geocoding request error: {e}")
        return None
# addr = "3527 Sw 23rd St, Delray Beach, FL 33445, USA"
# formatted = get_street_and_city(addr)

# print(formatted)