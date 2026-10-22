# FIX (b) 2026-10-21 (Blagojche): masked-unit duplicates pass dup-30d because the address STRINGS
# differ. Normalize before comparing: strip trailing *, treat Apt/Unit/#/Ste + x/* placeholders as
# "unit unknown", match on house# + street + zip. Unit-unknown on either side = same deal; two
# explicit different units stay different; a masked house number matches only when the visible
# digits are consistent AND street+zip match.
import re

_SUFFIX = {
    "ave":"ave","avenue":"ave","st":"st","street":"st","rd":"rd","road":"rd","dr":"dr","drive":"dr",
    "blvd":"blvd","boulevard":"blvd","ln":"ln","lane":"ln","ct":"ct","court":"ct","ter":"ter",
    "terr":"ter","terrace":"ter","pl":"pl","place":"pl","cir":"cir","circle":"cir","hwy":"hwy",
    "highway":"hwy","pkwy":"pkwy","parkway":"pkwy","way":"way","trl":"trl","trail":"trl","ct.":"ct",
    "sq":"sq","square":"sq","loop":"loop","run":"run","pt":"pt","point":"pt","cv":"cv","cove":"cv",
}
_MASK = re.compile(r"[*xX_#•]")
_UNIT_RE = re.compile(r"(?:,?\s*(?:#|apt\.?|unit|ste\.?|suite|bldg\.?|lot)\s*)([0-9a-zA-Z*xX_#\-]+)\s*$", re.I)
_TRAIL_STAR = re.compile(r"[\s*]+$")

def _is_masked_tok(tok):
    return bool(re.search(r"[0-9]", tok)) and bool(_MASK.search(tok))

def _unit_norm(u):
    """Return (kind, value): kind in (none|unknown|explicit)."""
    if u is None:
        return ("none", None)
    u = u.strip().strip(".,")
    if not u:
        return ("none", None)
    # placeholder / masked unit -> unknown
    if _MASK.search(u) or re.fullmatch(r"[xX]+", u) or u.lower() in ("xxx","xx","na","n/a","tbd"):
        return ("unknown", None)
    return ("explicit", u.lower())

def parse_addr(addr):
    if not addr or not isinstance(addr, str):
        return None
    s = addr.strip()
    # 1) pull a trailing unit token if any (#306, Apt Xxx, Unit 4, Ste 200, "918 Ocean Dr ***" handled below)
    unit_kind, unit_val = ("none", None)
    m = _UNIT_RE.search(s)
    if m:
        unit_kind, unit_val = _unit_norm(m.group(1))
        s = s[:m.start()].rstrip(" ,")
    # 2) trailing bare mask run (no Apt/#): "918 Ocean Dr ***" -> unit unknown, or "...W*" directional star
    #    Strip trailing * / whitespace; if what we stripped was a standalone mask run, mark unit unknown.
    mt = re.search(r"\s+([*xX_#•]{1,})\s*$", s)
    if mt and unit_kind == "none":
        unit_kind = "unknown"
        s = s[:mt.start()].rstrip(" ,")
    s = _TRAIL_STAR.sub("", s)   # strip any remaining trailing * (e.g. "W*")
    s = s.strip().strip(",")
    # 3) leading house-number token
    parts = s.split()
    if not parts:
        return None
    house = parts[0]
    street_toks = parts[1:]
    house_masked = _is_masked_tok(house)
    house_digits = re.sub(r"[^0-9]", "", house)  # visible digits only
    # 4) normalize street: lowercase, drop punctuation, canonical suffix
    stoks = []
    for t in street_toks:
        tl = re.sub(r"[.,]", "", t.lower())
        stoks.append(_SUFFIX.get(tl, tl))
    street = " ".join(stoks)
    return {"house": house.lower(), "house_masked": house_masked, "house_digits": house_digits,
            "house_pattern": re.sub(r"[^0-9]", "x", house) if house_masked else house_digits,
            "street": street, "unit_kind": unit_kind, "unit_val": unit_val}

def _zip4(z):
    if not z: return None
    m = re.search(r"\d{5}", str(z))
    return m.group(0) if m else None

def _norm_city(c):
    if not c: return None
    c=re.sub(r"[^a-z0-9 ]"," ", str(c).lower()).strip()
    c=re.sub(r"\s+"," ",c)
    return c or None

def _zip_ok(a, b):
    za, zb = _zip4(a), _zip4(b)
    if za and zb: return za == zb
    return True  # one missing -> wildcard (noted)

def _house_ok(a, b):
    if not a["house_masked"] and not b["house_masked"]:
        return a["house_digits"] == b["house_digits"] and a["house_digits"] != ""
    # at least one masked: visible digits must be consistent position-wise
    pa, pb = a["house_pattern"], b["house_pattern"]
    da, db = a["house_digits"], b["house_digits"]
    # compare the clean side digits against the masked pattern (align by length)
    def consistent(mask_pat, clean_digits):
        if len(mask_pat) != len(clean_digits):
            return False
        for mc, cc in zip(mask_pat, clean_digits):
            if mc != "x" and mc != cc:
                return False
        return True
    if a["house_masked"] and not b["house_masked"]:
        return consistent(pa, db)
    if b["house_masked"] and not a["house_masked"]:
        return consistent(pb, da)
    # both masked: overlapping visible digits must agree (same length)
    if len(pa) != len(pb): return False
    for ca, cb in zip(pa, pb):
        if ca != "x" and cb != "x" and ca != cb:
            return False
    return True

def _unit_ok(a, b):
    if a["unit_kind"] == "explicit" and b["unit_kind"] == "explicit":
        return a["unit_val"] == b["unit_val"]
    return True  # unknown/none on either side = same deal

_DIRS={"n","s","e","w","ne","nw","se","sw","north","south","east","west","northeast","northwest","southeast","southwest"}
def street_name_token(addr):
    """First street token after the house number that is not a direction or a street suffix
    (e.g. '918 Ocean Dr'->ocean, '4449 Moncrief Rd W'->moncrief, '3xx4 E 4th Avenue'->4th).
    Used to narrow dedup candidates. Returns a lowercase token or None."""
    p=parse_addr(addr)
    if not p: return None
    for t in p["street"].split():
        if t in _DIRS: continue
        if t in _SUFFIX: continue
        return t
    return None

def same_deal(addr_a, zip_a, addr_b, zip_b, city_a=None, city_b=None, county_a=None, county_b=None, zip_cities=None):
    a, b = parse_addr(addr_a), parse_addr(addr_b)
    if not a or not b:
        return False
    if a["street"] != b["street"]:
        return False
    # Blagojche 21.10: zip present both sides -> must match; if missing on a side, require a
    # normalized CITY match (same house+street in two cities must NOT merge).
    za, zb = _zip4(zip_a), _zip4(zip_b)
    if za and zb:
        if za != zb:
            return False
    else:
        # one or both zips missing -> need a city/county relationship so two different cities do not merge.
        ca, cb = _norm_city(city_a), _norm_city(city_b)
        if ca and cb and ca == cb:
            pass  # exact city match (original guard)
        elif bool(za) ^ bool(zb):
            # Rich 22.10 (tightened): exactly one side has a zip. Merge ONLY if the zip-less city is a
            # known city for the present zip (zip_cities), OR the two counties match. City-name
            # containment alone is NOT enough (Miami vs Miami Beach/Gardens/Lakes).
            present_zip = za or zb
            zipless_city = cb if za else ca
            known = (zip_cities is not None and present_zip and zipless_city
                     and zipless_city in (zip_cities.get(present_zip) or ()))
            coa, cob = _norm_city(county_a), _norm_city(county_b)
            county_match = bool(coa and cob and coa == cob)
            if known or county_match:
                pass
            else:
                return False
        else:
            return False  # both zips missing and cities differ -> do not merge
    if not _house_ok(a, b):
        return False
    if not _unit_ok(a, b):
        return False
    return True
