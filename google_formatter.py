import os
import requests
from dotenv import load_dotenv

load_dotenv()
# Put your API key in an env var: GOOGLE_ADDRESS_API_KEY
GOOGLE_ADDRESS_API_KEY = os.getenv("GOOGLE_ADDRESS_API_KEY")

if not GOOGLE_ADDRESS_API_KEY:
    raise RuntimeError("Set GOOGLE_ADDRESS_API_KEY in your environment.")

ENDPOINT = (
    f"https://addressvalidation.googleapis.com/v1:validateAddress"
    f"?key={GOOGLE_ADDRESS_API_KEY}"
)

def get_street_and_city(raw_address: str, region_code: str = "US"):
    """
    Takes a raw address string like:
      "3427 Barstow St, Sarasota, FL 34235, USA"

    Returns:
      (street, city) -> ("3427 Barstow St", "Sarasota")
    or (None, None) if parsing fails.
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

        if not street or not city:
            return None, None

        return street, city

    except (KeyError, IndexError):
        return None, None
    except requests.RequestException as e:
        print(f"Request error: {e}")
        return None, None


# addr = "3527 Sw 23rd St, Delray Beach, FL 33445, USA"
# formatted = get_street_and_city(addr)

# print(formatted)