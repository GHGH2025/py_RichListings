# podio_web_form_submissions.py
import os
import time
import logging
from typing import Optional, Any, Dict, List
import requests
from dotenv import load_dotenv

load_dotenv()

PODIO_BASE_URL = "https://api.podio.com"

WEB_FORM_SUBMISSIONS_APP_ID = int(os.getenv("PODIO_WEB_FORM_SUBMISSIONS_APP_ID", "30585451"))

# Podio OAuth env vars (same names you already use)
PODIO_CLIENT_ID = os.getenv("PodioClientId")
PODIO_CLIENT_SECRET = os.getenv("PodioClientSecret")
PODIO_USERNAME = os.getenv("podioUsername")
PODIO_PASSWORD = os.getenv("podioPassword")
PODIO_REDIRECT_URI = os.getenv("redirectUri")

# Token cache
_PODIO_ACCESS_TOKEN: Optional[str] = None
_PODIO_ACCESS_TOKEN_EXPIRES_AT: float = 0.0


def get_podio_access_token(force_refresh: bool = False) -> str:
    global _PODIO_ACCESS_TOKEN, _PODIO_ACCESS_TOKEN_EXPIRES_AT

    now = time.time()
    if (
        not force_refresh
        and _PODIO_ACCESS_TOKEN
        and now < _PODIO_ACCESS_TOKEN_EXPIRES_AT - 60
    ):
        return _PODIO_ACCESS_TOKEN

    if not all([PODIO_CLIENT_ID, PODIO_CLIENT_SECRET, PODIO_USERNAME, PODIO_PASSWORD, PODIO_REDIRECT_URI]):
        raise RuntimeError("Missing Podio OAuth environment variables")

    auth_url = f"{PODIO_BASE_URL}/oauth/token/v2"
    payload = {
        "grant_type": "password",
        "username": PODIO_USERNAME,
        "password": PODIO_PASSWORD,
        "client_id": PODIO_CLIENT_ID,
        "client_secret": PODIO_CLIENT_SECRET,
        "redirect_uri": PODIO_REDIRECT_URI,
    }

    resp = requests.post(auth_url, json=payload, timeout=20)
    if resp.status_code != 200:
        logging.error("Podio auth error: %s %s", resp.status_code, resp.text)
        resp.raise_for_status()

    data = resp.json()
    token = data.get("access_token")
    if not token:
        raise RuntimeError(f"Podio auth response missing access_token: {data}")

    expires_in = data.get("expires_in", 3600)
    _PODIO_ACCESS_TOKEN = token
    _PODIO_ACCESS_TOKEN_EXPIRES_AT = now + expires_in
    return token


def _podio_request(method: str, path: str, *, token: Optional[str] = None, retry_on_401: bool = True, **kwargs) -> Optional[Any]:
    if token is None:
        token = get_podio_access_token()

    headers = kwargs.pop("headers", {})
    headers.setdefault("Authorization", f"Bearer {token}")
    headers.setdefault("Content-Type", "application/json")

    url = f"{PODIO_BASE_URL}{path}"
    resp = requests.request(method, url, headers=headers, timeout=30, **kwargs)

    if resp.status_code == 401 and retry_on_401:
        token = get_podio_access_token(force_refresh=True)
        headers["Authorization"] = f"Bearer {token}"
        resp = requests.request(method, url, headers=headers, timeout=30, **kwargs)

    if not resp.ok:
        logging.error("Podio request failed %s %s: %s %s", method, path, resp.status_code, resp.text)
        return None

    if resp.status_code == 204 or not resp.text.strip():
        return {}

    try:
        return resp.json()
    except ValueError:
        return {"raw": resp.text}


# Field IDs (from your Podio app structure)
FIELD_NAME = 275184365
FIELD_COMPANY = 275184418
FIELD_PHONE_CALL = 275184419
FIELD_TEXT_NUMBER = 275184420
FIELD_EMAIL = 275184421

FIELD_MULTI_FAMILY = 275184422
FIELD_CONDO = 275184423
FIELD_LAND = 275184424
FIELD_COMMERCIAL = 275184425
FIELD_SINGLE_FAMILY = 275184426
FIELD_TOWN_HOUSE = 275184427

FIELD_COUNTY = 275184428
FIELD_CITY = 275184429
FIELD_MONGO_OBJECT_ID = 275184431


def _field(field_id: int, value: Any) -> Dict[str, Any]:
    return {"field_id": field_id, "values": [{"value": value}]}


def _phone_field(field_id: int, number: str, phone_type: str = "mobile") -> Dict[str, Any]:
    return {"field_id": field_id, "values": [{"value": number, "type": phone_type}]}


def _email_field(field_id: int, email: str, email_type: str = "work") -> Dict[str, Any]:
    return {"field_id": field_id, "values": [{"value": email, "type": email_type}]}


def create_web_form_submission_item(
    *,
    name: str,
    company: str,
    email: str,
    phone_call: str,
    text_number: str,
    county: str,
    city: str,
    mongo_object_id: str,
    property_html: Dict[str, str],
) -> Optional[int]:
    fields: List[Dict[str, Any]] = []

    # Contact
    if name: fields.append(_field(FIELD_NAME, name))
    if company: fields.append(_field(FIELD_COMPANY, company))
    if email: fields.append(_email_field(FIELD_EMAIL, email))
    if phone_call: fields.append(_phone_field(FIELD_PHONE_CALL, phone_call))
    if text_number: fields.append(_phone_field(FIELD_TEXT_NUMBER, text_number))

    # Geo
    if county: fields.append(_field(FIELD_COUNTY, county))
    if city: fields.append(_field(FIELD_CITY, city))

    # Mongo Object ID
    if mongo_object_id: fields.append(_field(FIELD_MONGO_OBJECT_ID, mongo_object_id))

    # Property text areas (ONLY if non-empty)
    if property_html.get("multiFamily"): fields.append(_field(FIELD_MULTI_FAMILY, property_html["multiFamily"]))
    if property_html.get("condo"): fields.append(_field(FIELD_CONDO, property_html["condo"]))
    if property_html.get("land"): fields.append(_field(FIELD_LAND, property_html["land"]))
    if property_html.get("commercial"): fields.append(_field(FIELD_COMMERCIAL, property_html["commercial"]))
    if property_html.get("singleFamily"): fields.append(_field(FIELD_SINGLE_FAMILY, property_html["singleFamily"]))
    if property_html.get("townhouse"): fields.append(_field(FIELD_TOWN_HOUSE, property_html["townhouse"]))

    payload = {"fields": fields}

    data = _podio_request("POST", f"/item/app/{WEB_FORM_SUBMISSIONS_APP_ID}/", json=payload)
    if not data:
        return None

    # Podio usually returns {"item_id": ...}
    item_id = data.get("item_id") if isinstance(data, dict) else None
    return item_id
