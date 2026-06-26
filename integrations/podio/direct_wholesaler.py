# podio_direct_wholeseller.py

import os
import time
import logging
import re
from typing import List, Optional, Dict, Any, Set

import requests
from dotenv import load_dotenv

from models import ParsedListing
from pipeline.address_utils import resolve_street_address

load_dotenv()

# -------------------------------------------------------------------
# Podio config & constants
# -------------------------------------------------------------------

PODIO_BASE_URL = "https://api.podio.com"

# App IDs (you can override via env if needed, but defaults are your IDs)
PROPERTIES_APP_ID = int(os.getenv("PODIO_PROPERTIES_APP_ID", "18339388"))
WHOLESELLERS_APP_ID = int(os.getenv("PODIO_WHOLESELLERS_APP_ID", "18339395"))

# Field IDs from your JSON snippets
PROPERTY_STATUS_FIELD_ID = 144394555     # "Status" category field in Properties app
WHOLESELLER_REF_FIELD_ID = 144394554     # "Wholeseller" app reference field in Properties app
WHOLESELLER_EMAIL_FIELD_ID = 144394623   # "Email" text field in Wholesellers app

# Auth env vars (matching your Node code exactly)
PODIO_CLIENT_ID = os.getenv("PodioClientId")
PODIO_CLIENT_SECRET = os.getenv("PodioClientSecret")
PODIO_USERNAME = os.getenv("podioUsername")
PODIO_PASSWORD = os.getenv("podioPassword")
PODIO_REDIRECT_URI = os.getenv("redirectUri")

# Token cache
_PODIO_ACCESS_TOKEN: Optional[str] = None
_PODIO_ACCESS_TOKEN_EXPIRES_AT: float = 0.0

IGNORE_PODIO_STATUS_FOR_TEST = os.getenv("IGNORE_PODIO_STATUS_FOR_TEST", "false").lower() == "true"



# -------------------------------------------------------------------
# Auth + low-level Podio request helper
# -------------------------------------------------------------------

def get_podio_access_token(force_refresh: bool = False) -> str:
    """
    Password grant, same as your Node getAccessToken, but with caching
    so we don't hammer the auth endpoint.
    """
    global _PODIO_ACCESS_TOKEN, _PODIO_ACCESS_TOKEN_EXPIRES_AT

    now = time.time()
    if (
        not force_refresh
        and _PODIO_ACCESS_TOKEN
        and now < _PODIO_ACCESS_TOKEN_EXPIRES_AT - 60
    ):
      
        return _PODIO_ACCESS_TOKEN


    if not all(
        [PODIO_CLIENT_ID, PODIO_CLIENT_SECRET, PODIO_USERNAME, PODIO_PASSWORD, PODIO_REDIRECT_URI]
    ):
        logging.error("Missing Podio OAuth environment variables, cannot request token")
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
        logging.error("Error fetching Podio access token: %s %s", resp.status_code, resp.text)
        resp.raise_for_status()

    data = resp.json()
    token = data.get("access_token")
    print("token=====", token)  # kept as-is
    if not token:
        logging.error("Podio auth response missing access_token: %r", data)
        raise RuntimeError(f"Podio auth response missing access_token: {data}")

    expires_in = data.get("expires_in", 3600)
    _PODIO_ACCESS_TOKEN = token
    _PODIO_ACCESS_TOKEN_EXPIRES_AT = now + expires_in

    return token


def _podio_request(
    method: str,
    path: str,
    *,
    token: Optional[str] = None,
    retry_on_401: bool = True,
    **kwargs,
) -> Optional[Any]:
    """
    Generic thin wrapper around requests.request for Podio.

    - Injects Authorization header
    - Retries once on 401 by refreshing the token
    - On error (4xx/5xx) logs and returns None
    - On 204 or empty body returns {} so callers can treat it as success
    """
    if token is None:
        token = get_podio_access_token()

    headers = kwargs.pop("headers", {})
    headers.setdefault("Authorization", f"Bearer {token}")
    headers.setdefault("Content-Type", "application/json")

    url = f"{PODIO_BASE_URL}{path}"
 

    resp = requests.request(method, url, headers=headers, timeout=30, **kwargs)



    if resp.status_code == 401 and retry_on_401:
        logging.warning("Podio 401 on %s %s, refreshing token and retrying once", method, path)
        token = get_podio_access_token(force_refresh=True)
        headers["Authorization"] = f"Bearer {token}"
        resp = requests.request(method, url, headers=headers, timeout=30, **kwargs)
    

    if not resp.ok:
        return None

    # 204 or no content -> treat as success with empty payload
    if resp.status_code == 204 or not resp.text.strip():
        return {}

    try:
        data = resp.json()
        return data
    except ValueError:
        logging.warning("Podio %s %s returned non-JSON response", method, path)
        return {"raw": resp.text}


# -------------------------------------------------------------------
# Helpers to search apps, read items & fields
# -------------------------------------------------------------------

def _search_app_for_items(
    app_id: int,
    query: str,
    *,
    token: str,
    search_fields: Optional[List[str]] = None,
    limit: int = 10,
) -> List[int]:
    """
    Use Podio 'Search in app':
      POST /search/app/{app_id}/

    NOTE: This is the fix for your 404: no `/v2` here and method is POST.
    """
    if not query:
        logging.debug("Empty query passed to _search_app_for_items for app_id=%s", app_id)
        return []

    logging.info(
        "Searching app %s for query='%s' (limit=%s, search_fields=%s)",
        app_id,
        query,
        limit,
        search_fields,
    )

    payload: Dict[str, Any] = {
        "query": query,
        "ref_type": "item",
        "limit": limit,
        "offset": 0,
    }
    if search_fields:
        payload["search_fields"] = search_fields

    data = _podio_request(
        "POST",
        f"/search/app/{app_id}/",
        token=token,
        json=payload,
    )
    if not data:
        logging.info("No search results from Podio for app %s and query '%s'", app_id, query)
        return []

    if not isinstance(data, list):
        logging.warning("Unexpected search/app response for app %s and query '%s': %r", app_id, query, data)
        return []

    item_ids: List[int] = []
    for entry in data:
        if isinstance(entry, dict) and entry.get("type") == "item":
            item_id = entry.get("id")
            if isinstance(item_id, int):
                item_ids.append(item_id)

    logging.info(
        "Search in app %s for query='%s' returned item_ids=%s",
        app_id,
        query,
        item_ids,
    )
    return item_ids


def _get_item(token: str, item_id: int) -> Optional[Dict[str, Any]]:
    data = _podio_request("GET", f"/item/{item_id}", token=token)
    if not isinstance(data, dict):
        logging.warning("Unexpected get item response for %s: %r", item_id, data)
        return None
    return data


def _find_field(
    fields: List[Dict[str, Any]],
    *,
    field_id: Optional[int] = None,
    external_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
  
    for f in fields:
        if field_id is not None and f.get("field_id") == field_id:
            logging.debug("Matched field_id=%s", field_id)
            return f
        if external_id is not None and f.get("external_id") == external_id:
            logging.debug("Matched external_id=%s", external_id)
            return f
    return None


def _get_property_status(item: Dict[str, Any]) -> Optional[str]:
    """
    Reads the 'Status' category field and returns its text, e.g. 'Active'.
    """
    fields = item.get("fields") or []
    field = _find_field(fields, field_id=PROPERTY_STATUS_FIELD_ID, external_id="status")
    if not field:
        return None
    values = field.get("values") or []
    if not values:
        return None
    first_val = values[0].get("value") or {}
    if isinstance(first_val, dict):
        status_text = first_val.get("text")
        logging.debug("Property status extracted: %s", status_text)
        return status_text
    return None


def _get_wholeseller_reference_item_id(item: Dict[str, Any]) -> Optional[int]:
    """
    Reads the 'Wholeseller' app reference field on the Properties item and returns the referenced item_id, if any.
    """
    fields = item.get("fields") or []
    field = _find_field(fields, field_id=WHOLESELLER_REF_FIELD_ID, external_id="wholeseller")
    if not field:
        return None
    values = field.get("values") or []
    if not values:
        return None
    first_val = values[0].get("value") or {}
    if isinstance(first_val, dict):
        item_id = first_val.get("item_id")
        if isinstance(item_id, int):
            return item_id
    return None


def _get_wholeseller_email_from_item(item: Dict[str, Any]) -> Optional[str]:
    """
    Reads the 'Email' text field from a Wholesellers item.
    """
    fields = item.get("fields") or []
    field = _find_field(fields, field_id=WHOLESELLER_EMAIL_FIELD_ID, external_id="cash-buyer-emal")
    if not field:
        return None
    values = field.get("values") or []
    if not values:
        return None
    raw_value = values[0].get("value")
    if isinstance(raw_value, dict):
        email = raw_value.get("value")
    else:
        email = raw_value
    if not isinstance(email, str):
        return None
    normalized = email.strip().lower()
    logging.debug("Wholeseller Email extracted: %s", normalized)
    return normalized


# -------------------------------------------------------------------
# Wholeseller lookup & assignment
# -------------------------------------------------------------------

def find_wholeseller_item_by_email(token: str, email: str) -> Optional[int]:
    """
    Search Wholesellers app by email, confirm via the Email field, and return the correct item_id.
    """
    if not email:
        return None
    normalized = email.strip().lower()

    candidate_ids = _search_app_for_items(
        WHOLESELLERS_APP_ID,
        query=normalized,
        token=token,
        search_fields=["text_values"],
        limit=10,
    )


    for item_id in candidate_ids:
        item = _get_item(token, item_id)
        if not item:
            continue
        wh_email = _get_wholeseller_email_from_item(item)
  
        if wh_email and wh_email == normalized:
            # logging.info("Matched wholeseller item %s for email '%s'", item_id, normalized)
            return item_id

    # logging.warning("No Wholeseller item found with email '%s'", email)
    return None
# new with true false for podio update
def set_wholeseller_reference_on_property(
    token: str,
    property_item_id: int,
    wholeseller_item_id: int,
    *,
    allow_update: bool = True,
) -> bool:
    """
    Update only the Wholeseller reference field on the given property item.

    Uses:
      PUT /item/{item_id}/value/{field_id}
      Body: [ { "value": { "item_id": <wholeseller_item_id> } } ]

    allow_update:
      - If False, we SKIP the Podio update and just log it.
    """

    # 🚫 If the listing says "do NOT update Podio", skip the PUT call entirely
    if not allow_update:
        logging.info(
            "Skipping Wholeseller reference update on property item %s "
            "because updateFlagForPodio is not 'true'",
            property_item_id,
        )
        # Treat as "success, nothing to change"
        return True

    logging.info(
        "Setting Wholeseller reference on property item %s to wholeseller item %s",
        property_item_id,
        wholeseller_item_id,
    )

    payload = [wholeseller_item_id]

    data = _podio_request(
        "PUT",
        f"/item/{property_item_id}/value/{WHOLESELLER_REF_FIELD_ID}",
        token=token,
        json=payload,
    )

    if data is None:
        # _podio_request already logged the error
        logging.error(
            "Failed to update Wholeseller reference on property item %s",
            property_item_id,
        )
        return False

    logging.info(
        "Updated Wholeseller reference on property item %s to wholeseller item %s",
        property_item_id,
        wholeseller_item_id,
    )
    return True



# -------------------------------------------------------------------
# Property search using address, city, address_search_keys
# -------------------------------------------------------------------


# -------------------------------------------------------------------
# Address normalization & matching helpers NEW HElpers
# -------------------------------------------------------------------

def _normalize_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


# Map "North East" / "N.E." / "NE" etc. to standard short codes
DIRECTION_PATTERNS = [
    (re.compile(r"\b(north[ -]?east|n\.?e\.?)\b", re.I), "ne"),
    (re.compile(r"\b(south[ -]?east|s\.?e\.?)\b", re.I), "se"),
    (re.compile(r"\b(north[ -]?west|n\.?w\.?)\b", re.I), "nw"),
    (re.compile(r"\b(south[ -]?west|s\.?w\.?)\b", re.I), "sw"),
    (re.compile(r"\b(north|n\.?)\b", re.I), "n"),
    (re.compile(r"\b(south|s\.?)\b", re.I), "s"),
    (re.compile(r"\b(east|e\.?)\b", re.I), "e"),
    (re.compile(r"\b(west|w\.?)\b", re.I), "w"),
]

FORT_PATTERNS = [
    (re.compile(r"\bfort\b", re.I), "ft"),
    (re.compile(r"\bft\.?\b", re.I), "ft"),
]

def _normalize_address_for_compare(s: str) -> str:
    s = s.lower()
    # directions (already there)
    for pattern, repl in DIRECTION_PATTERNS:
        s = pattern.sub(f" {repl} ", s)
    # NEW: fort / ft
    for pattern, repl in FORT_PATTERNS:
        s = pattern.sub(f" {repl} ", s)
    # rest of your cleanup (punctuation, spaces, etc.)
    s = re.sub(r"[^0-9a-z\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s



def _extract_house_number(s: str) -> Optional[str]:
    """
    Returns the leading integer in an address string (e.g. '1820' from '1820 NE 59th Ct').
    """
    m = re.match(r"^\s*(\d+)", s or "")
    return m.group(1) if m else None


def _extract_direction_token(s: str) -> Optional[str]:
    """
    Returns the primary direction token (ne, se, n, s, e, w) if present.
    """
    m = re.search(r"\b(ne|se|nw|sw|n|s|e|w)\b", s or "")
    return m.group(1) if m else None


def _tokenize(s: str) -> List[str]:
    return [t for t in (s or "").split(" ") if t]


def _cities_match(c1: str, c2: str) -> bool:
    """
    City check. If both are present and normalized cities differ, treat as mismatch.
    If one is missing, we don't disqualify.
    """
    if not c1 or not c2:
        return True  # can't prove mismatch
    n1 = _normalize_address_for_compare(c1)
    n2 = _normalize_address_for_compare(c2)
    return n1 == n2


def _get_property_location_components(item: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """
    Extracts street, city, state, zip, full string from the first location field on a property item.
    Works even if Podio slightly changes internal keys (we try multiple common ones).
    """
    fields = item.get("fields") or []
    loc_field = None
    for f in fields:
        if f.get("type") == "location":
            loc_field = f
            break

    if not loc_field:
        return {"street": None, "city": None, "state": None, "zip": None, "full": None}

    vals = loc_field.get("values") or []
    if not vals:
        return {"street": None, "city": None, "state": None, "zip": None, "full": None}

    raw_val = vals[0].get("value")
    full = street = city = state = zip_code = None

    if isinstance(raw_val, str):
        # Sometimes Podio returns just a string
        full = raw_val
    elif isinstance(raw_val, dict):
        # Typical Podio location structure
        full = raw_val.get("value") or raw_val.get("formatted") or raw_val.get("text")
        street = raw_val.get("street_address") or raw_val.get("address")
        city = raw_val.get("city") or raw_val.get("town")
        state = raw_val.get("state") or raw_val.get("region")
        zip_code = raw_val.get("postal_code") or raw_val.get("zip")

    return {
        "street": street,
        "city": city,
        "state": state,
        "zip": zip_code,
        "full": full,
    }


def addresses_clearly_match(
    listing_addr: str,
    listing_city: str,
    prop_street: Optional[str],
    prop_city: Optional[str],
    prop_full: Optional[str],
) -> bool:
    """
    Strong, deterministic check that a Podio property address matches the listing.

    - Normalizes both addresses (lowercase, strip punctuation, NE/SE standardization)
    - Requires city to not contradict (if both present)
    - Requires house numbers to match if both known
    - Requires directions to match if both present (so NE != SE)
    - Requires at least 2 non-city tokens in common (e.g., 1820 + 59th)
    """
    la = _normalize_address_for_compare(listing_addr or "")
    pa = _normalize_address_for_compare(prop_street or "") or _normalize_address_for_compare(prop_full or "")

    if not la or not pa:
        return False

    # City must not contradict (if we know both)
    if not _cities_match(listing_city or "", prop_city or ""):
        return False

    print("la===============",la)
    print("pa===============",pa)



    # House number check (if both present)
    ln = _extract_house_number(la)
    pn = _extract_house_number(pa)
    if ln and pn and ln != pn:
        return False

    # Direction check (NE vs SE etc.)
    ld = _extract_direction_token(la)
    pd = _extract_direction_token(pa)
    if ld and pd and ld != pd:
        return False

    # Shared non-city tokens requirement
    ltoks: Set[str] = set(_tokenize(la))
    ptoks: Set[str] = set(_tokenize(pa))

    city_norm = _normalize_address_for_compare(listing_city or "")
    for t in _tokenize(city_norm):
        ltoks.discard(t)
        ptoks.discard(t)

    shared = ltoks & ptoks

    # Require at least 2 shared non-city tokens (e.g., "1820" and "59th")
    return len(shared) >= 2



# newest
def search_properties_app_for_listing(token: str, listing: ParsedListing) -> Optional[int]:
    """
    Use (address, city) and address_search_keys to find the correct Properties item,
    restricted to Status = 'Active', with strict address matching to avoid wrong updates.

    In TEST mode (IGNORE_PODIO_STATUS_FOR_TEST=True), we:
      - ignore Status filtering, and
      - if multiple strict matches remain, we pick the first one so that
        we can mark the listing as "found" for coverage purposes.
    """
    logging.info("Starting property search for listing %s", listing.id)

    addr = (resolve_street_address(listing) or "").strip()
    city = (listing.city or "").strip()
    complete = listing.complete_info or {}

    # Fallback to complete_info if direct fields are missing
    if not addr:
        addr = (complete.get("address") or "").strip()
        logging.debug("Listing %s fallback address from complete_info: '%s'", listing.id, addr)
    if not city:
        city = (complete.get("city") or "").strip()
        logging.debug("Listing %s fallback city from complete_info: '%s'", listing.id, city)

    raw_keys = listing.address_search_keys or []
    addr_keys = [k.strip() for k in raw_keys if isinstance(k, str) and k.strip()]

    search_queries: List[str] = []

    # Main "address, city" combo
    if addr and city:
        search_queries.append(f"{addr}, {city}")

    # All variations from address_search_keys
    for key in addr_keys:
        search_queries.append(key)

    # De-duplicate, case-insensitive, order-preserving
    seen = set()
    unique_queries: List[str] = []
    for q in search_queries:
        low = q.lower()
        if low in seen:
            continue
        seen.add(low)
        unique_queries.append(q)

    if not unique_queries:
        logging.warning(
            "Listing %s has no usable address/city/address_search_keys for Podio lookup",
            listing.id,
        )
        return None

    logging.info(
        "Searching Podio Properties app for listing %s using %d queries",
        listing.id,
        len(unique_queries),
    )

    # STEP 1: Collect candidate items returned by any query
    # NOTE: in production we only keep Active; in test mode we keep all and just log.
    active_candidates: Dict[int, Dict[str, Optional[str]]] = {}
    seen_item_ids: Set[int] = set()

    for q in unique_queries:
        item_ids = _search_app_for_items(
            PROPERTIES_APP_ID,
            query=q,
            token=token,
            search_fields=["location_values"],
            limit=2,   # <-- your "only top 2 candidates" optimization
        )

        for item_id in item_ids:
            # Avoid re-fetching the same item for repeated queries
            if item_id in seen_item_ids:
                continue
            seen_item_ids.add(item_id)

            item = _get_item(token, item_id)
            if not item:
                continue

            status = _get_property_status(item)

            if not IGNORE_PODIO_STATUS_FOR_TEST:
                # Normal behavior: require Active
                if not status or status.lower() != "active":
                    continue
            else:
                # Test behavior: just log, but do NOT filter by status
                logging.debug(
                    "TEST MODE: ignoring status '%s' for property item %s (listing %s)",
                    status,
                    item_id,
                    listing.id,
                )

            loc = _get_property_location_components(item)
            active_candidates[item_id] = loc

    if not active_candidates:
        if IGNORE_PODIO_STATUS_FOR_TEST:
            logging.warning(
                "No property items found in Podio for listing %s (addr='%s', city='%s') [TEST MODE, status ignored]",
                listing.id,
                addr,
                city,
            )
        else:
            logging.warning(
                "No ACTIVE property items found in Podio for listing %s (addr='%s', city='%s')",
                listing.id,
                addr,
                city,
            )
        return None

    # STEP 2: Strict address match against listing to avoid wrong item selection
    match_candidates: Dict[int, Dict[str, Optional[str]]] = {}
    for item_id, loc in active_candidates.items():
        if addresses_clearly_match(
            addr,
            city,
            loc.get("street"),
            loc.get("city"),
            loc.get("full"),
        ):
            match_candidates[item_id] = loc
        else:
            logging.debug(
                "Property item %s failed strict address match for listing %s",
                item_id,
                listing.id,
            )

    # Nothing passed strict check -> true "not found"
    if not match_candidates:
        return None

    # If exactly one strong match, we're safe to use it
    if len(match_candidates) == 1:
        chosen_id = next(iter(match_candidates.keys()))
        return chosen_id

    # At this point we have MULTIPLE strict matches
    if IGNORE_PODIO_STATUS_FOR_TEST:
        # TEST MODE ONLY:
        # For debugging coverage, we don't care *which* one wins –
        # we just want to know that Podio has at least one clear match.
        chosen_id = next(iter(match_candidates.keys()))
        logging.debug(
            "TEST MODE: %d strict matches %s for listing %s; choosing %s for coverage only",
            len(match_candidates),
            list(match_candidates.keys()),
            listing.id,
            chosen_id,
        )
        return chosen_id

    # STEP 3 (production-only): Tie-breaker using normalized 'street + city'
    def _normalize_full_for_compare(
        street_val: Optional[str],
        city_val: Optional[str],
        full_val: Optional[str],
    ) -> str:
        if street_val or city_val:
            base = ", ".join([p for p in [street_val, city_val] if p])
        else:
            base = full_val or ""
        return _normalize_address_for_compare(base)

    listing_norm = _normalize_full_for_compare(addr, city, None)

    exact_ids: List[int] = []
    for item_id, loc in match_candidates.items():
        prop_norm = _normalize_full_for_compare(
            loc.get("street"),
            loc.get("city"),
            loc.get("full"),
        )
        if prop_norm and prop_norm == listing_norm:
            exact_ids.append(item_id)

    if len(exact_ids) == 1:
        chosen_id = exact_ids[0]
        return chosen_id

    # Still ambiguous after all checks -> safer to not auto-link than to update wrong property
    return None


# -------------------------------------------------------------------
# Orchestration for a single listing & batch processor
# -------------------------------------------------------------------

def process_single_listing_direct_wholeseller(listing: ParsedListing, token: str) -> bool:
    """
    Process one ParsedListing for the direct_wholeseller flow:

    - Find property in Properties app (Status = Active) by address/city/address_search_keys
    - Inspect Wholeseller reference field
    - If empty: find wholeseller by complete_info.agent_email and set it
    - If existing:
        * If email matches agent_email -> nothing to change, just mark processed
        * Else -> find correct wholeseller by email and reassign
    - If everything is correct, set direct_wholeseller = 'processed'
    """
    # logging.info("Processing single listing %s for direct_wholeseller", listing.id)

    complete = listing.complete_info or {}
    agent_email = (complete.get("agent_email") or "").strip().lower()

    # NEW: read updateFlagForPodio from complete_info (string: 'true' / 'false')
    update_flag_raw = str(complete.get("updateFlagForPodio", "")).strip().lower()
    allow_podio_update = (update_flag_raw == "true")


    if not agent_email:
   
        # Mark it so it won't be picked again in the batch query
        # Update only the flag, bypassing full document validation
        ParsedListing.objects(id=listing.id).update_one(
            set__direct_wholeseller="no_agent_email"
        )
        return False
# revert after test
    property_item_id = search_properties_app_for_listing(token, listing)
    if not property_item_id:
        # We tried to find a matching property but couldn't.
        # Mark this as not_found so it doesn't block future batches.
        ParsedListing.objects(id=listing.id).update_one(
            set__FoundInPodioViaSearch="not_found",
            set__direct_wholeseller="property_not_found",
        )
        logging.info(
            "Listing %s: no matching property found in Podio; "
            "marking direct_wholeseller='not_found'",
            listing.id,
        )
 
        return False

# revert after test

# # comment after test
#     property_item_id = search_properties_app_for_listing(token, listing)

#     if not property_item_id:
#         # Mark as NOT found via Podio search
#         # Mark as NOT found via Podio search (targeted update)
#         ParsedListing.objects(id=listing.id).update_one(
#             set__FoundInPodioViaSearch="not_found"
#         )
#         # Mark as NOT found via Podio search (targeted update)
#         ParsedListing.objects(id=listing.id).update_one(
#             set__direct_wholeseller="processed"
#         )
#         logging.info("not_found------- Listing %s: no ACTIVE property item matched in Podio", listing.id)

#         # Keep direct_wholeseller as-is so it can be retried later
#         logging.warning(
#             "Listing %s: no matching ACTIVE property item found in Podio; leaving as not_processed",
#             listing.id,
#         )
#         return False

#     # We DID find an ACTIVE property via search
#     # We DID find a property via search
#     ParsedListing.objects(id=listing.id).update_one(
#         set__FoundInPodioViaSearch="found"
#     )
#     logging.info(
#         "found-------- Listing %s matched to Podio property item %s via search",
#         listing.id,
#         property_item_id,
#     )

#     logging.info(
#         "Listing %s matched to Podio property item %s",
#         listing.id,
#         property_item_id,
#     )
# # comment after test


    property_item = _get_item(token, property_item_id)
    if not property_item:
  
        return False

    existing_wholeseller_item_id = _get_wholeseller_reference_item_id(property_item)
    logging.debug(
        "Listing %s property item %s existing_wholeseller_item_id=%s",
        listing.id,
        property_item_id,
        existing_wholeseller_item_id,
    )

    # Case 1: There is already a wholeseller set on the property
    if existing_wholeseller_item_id:
        wh_item = _get_item(token, existing_wholeseller_item_id)
        if wh_item:
            current_email = _get_wholeseller_email_from_item(wh_item)
        else:
            current_email = None

        if current_email and current_email.lower() == agent_email:
            logging.info(
                "Property %s already has correct Wholeseller (email %s); marking listing %s as processed",
                property_item_id,
                agent_email,
                listing.id,
            )
            ParsedListing.objects(id=listing.id).update_one(
            set__direct_wholeseller="processed"
            )
            try:
                from observability.pipeline_metrics import record_listing_stage
                record_listing_stage(str(listing.id), "podio_linked", direct_wholeseller="processed")
            except Exception:
                pass
            return True

        
    # Case 2: No wholeseller or mismatched -> find the right one by email
    target_wholeseller_item_id = find_wholeseller_item_by_email(token, agent_email)
   
    if not target_wholeseller_item_id:
        # We couldn't find a matching wholeseller record; leave as 'not_processed'
        logging.warning(
            "Listing %s: no wholeseller found in Podio with email '%s'; leaving as not_processed",
            listing.id,
            agent_email,
        )
        ParsedListing.objects(id=listing.id).update_one(
            set__direct_wholeseller="wholeseller_not_found",
            
        )
        return False

    if not set_wholeseller_reference_on_property(token, property_item_id, target_wholeseller_item_id,allow_update=allow_podio_update):
        logging.error(
            "Listing %s: failed to set wholeseller reference on property %s",
            listing.id,
            property_item_id,
        )
        return False

    ParsedListing.objects(id=listing.id).update_one(
        set__direct_wholeseller="processed"
    )
    try:
        from observability.pipeline_metrics import record_listing_stage
        record_listing_stage(str(listing.id), "podio_linked", direct_wholeseller="processed")
    except Exception:
        pass
    logging.info(
        "Listing %s marked as direct_wholeseller='processed'",
        listing.id,
    )
    return True


def process_direct_wholeseller_batch(batch_limit: int = 3) -> None:
    """
    Fetch a small batch of ParsedListing docs with direct_wholeseller='not_processed'
    and attempt to link them to the correct Wholeseller in Podio.

    Default batch_limit=3 (you can safely increase to 5 if desired).
    """
 

    # Clamp to [1, 5] as per your "3-5 items" requirement
    limit = max(1, min(batch_limit, 5))
    logging.debug("Effective batch limit after clamp: %s", limit)

    listings = list(
        ParsedListing.objects(direct_wholeseller="not_processed")[:limit]
    )
    print("listings==========", listings)  # kept as-is
    if not listings:
        logging.info("No ParsedListing items with direct_wholeseller='not_processed' found.")
        return

    logging.info(
        "Processing %d ParsedListing items for direct_wholeseller linking",
        len(listings),
    )
    logging.debug(
        "Listing IDs selected for batch: %s",
        [str(l.id) for l in listings],
    )

    token = get_podio_access_token()

    for listing in listings:
        try:
            logging.info("Beginning processing for listing %s", listing.id)
            success = process_single_listing_direct_wholeseller(listing, token)
           
        except Exception:
            logging.exception(
                "Unexpected error while processing direct_wholeseller for listing %s",
                listing.id,
            )

def initialize_direct_wholeseller_flag() -> None:
    """
    ONE-TIME HELPER:
    Set direct_wholeseller = 'not_processed' on all ParsedListing docs.

    Call this manually from your main/server_runner once,
    then comment/remove the call.
    """
    # If you ONLY want to touch docs where the field is missing, use:
    # qs = ParsedListing.objects(direct_wholeseller__exists=False)
    qs = ParsedListing.objects  # all documents

    updated = qs.update(set__direct_wholeseller="not_processed")
    logging.info(
        "initialize_direct_wholeseller_flag: set direct_wholeseller='not_processed' on %s ParsedListing docs",
        updated,
    )

