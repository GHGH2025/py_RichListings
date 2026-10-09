"""AI availability-email generator (server 01). Called by server 02 /richEmailScheduled.
Token-guarded. Returns {subject, body(HTML)} following Rich's rules, with CODE validation
(regenerate-or-fail) so the rules are enforced deterministically, not only in the prompt.

23.09.2026 (Rich's 22.09 findings): (a) at most TWO questions per email, enforced in code;
(b) a rotating pool of question pairs so wholesalers do not all get the same "showings /
access / offers" pair, with per-wholesaler avoidance via used_questions (server 02 memo);
(c) phone numbers can never be sent as addresses (defensive filter on the input)."""
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
from typing import List, Tuple
import os, re, json, logging, itertools, threading, random

router = APIRouter(tags=["availability-email"])
STREET_SUFFIX = r"(?:St|Street|Ave|Avenue|Rd|Road|Dr|Drive|Ct|Court|Ln|Lane|Blvd|Way|Ter|Terrace|Cir|Circle|Pl|Place|Loop|Hwy|Pkwy)"
PHONE_RE = re.compile(r"^\s*\(?\d{3}\)?[-. ]?\d{3}[-. ]?\d{4}\s*$")  # a line that is only a phone number
MAX_QUESTIONS = 2


class GenReq(BaseModel):
    name: str = ""
    addresses: List[str] = []
    property_count: int = 0
    used_subjects: List[str] = []
    used_openers: List[str] = []
    used_questions: List[str] = []      # question keys already sent to this wholesaler (server 02 memo)
    name_in_subject_ok: bool = True
    is_land: bool = False   # Rich 30.09: land/lot -> never ask about repairs/condition


def _check_token(request: Request):
    tok = request.query_params.get("t") or request.headers.get("x-alert-token") or ""
    want = os.getenv("INTERNAL_ALERT_TOKEN", "")
    if not want or tok != want:
        raise HTTPException(status_code=403, detail="forbidden")


def _sanitize(s: str) -> str:
    # em/en dash -> " - "; curly quotes -> straight; (always applied, deterministic)
    s = re.sub(r"\s*[—–]\s*", " - ", s or "")
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return s


def _delist(body: str) -> str:
    """Rich 30.09: addresses as plain <p> lines, never <ul>/<li> and never a leading bullet/'-'."""
    if not body:
        return body
    b = re.sub(r"(?is)</?ul[^>]*>", "", body)
    b = re.sub(r"(?is)<li[^>]*>\s*", "<p>", b)
    b = re.sub(r"(?is)\s*</li>", "</p>", b)
    b = re.sub(r"(?im)(<p[^>]*>)\s*[-\u2022*]\s+", r"\1", b)
    return b


def _singularize(text: str) -> str:
    """Deterministic plural->single fix for a one-property email (Rich 30.09: these->this),
    used only as a last-attempt fallback so we never drop to the old GlobiFlow template."""
    if not text:
        return text
    # Blagojche 30.09: PHRASE-level only, so the English stays correct (never "Are this").
    # No digits, no bare these->this, no both change. Anything still plural is left for
    # _violations -> 422 -> server 02 ai_pending retry (+30 min).
    def _cap(rep, m):  # Blagojche 30.09: keep sentence-start capital ("Are these"->"Is this")
        return (rep[0].upper() + rep[1:]) if m.group(0)[:1].isupper() else rep
    text = re.sub(r"\bare\s+these\b", lambda m: _cap("is this", m), text, flags=re.I)
    text = re.sub(r"\bare\s+those\b", lambda m: _cap("is this", m), text, flags=re.I)
    text = re.sub(r"\bare\s+they\b", lambda m: _cap("is it", m), text, flags=re.I)
    _sn = {"properties": "property", "property": "property", "homes": "home", "home": "home",
           "houses": "house", "house": "house", "deals": "deal", "deal": "deal",
           "listings": "listing", "listing": "listing", "ones": "one", "one": "one"}
    text = re.sub(r"\b(?:these|those)\s+(properties|property|homes|home|houses|house|deals|deal|listings|listing|ones|one)\b",
                  lambda m: _cap("this ", m) + _sn.get(m.group(1).lower(), m.group(1)), text, flags=re.I)
    return text


def _clean_addresses(addresses: List[str]) -> List[str]:
    """Drop empties and anything that is only a phone number (22.09: Rich's own number was
    listed as a property in all 35 emails because the extractor picked it up)."""
    out = []
    for a in addresses or []:
        a = (a or "").strip()
        if not a or PHONE_RE.match(a):
            continue
        out.append(a)
    # Rich 30.09: same property listed twice (e.g. "Southeast Placita Court" and
    # "9 Southeast Placita Court"). Drop a number-less address that is a suffix of a numbered one
    # in the same list, keeping the numbered (more specific) one.
    numbered = [x for x in out if re.match(r"^\s*\d", x)]
    deduped = []
    for a in out:
        al = a.strip().lower()
        if not re.match(r"^\s*\d", a) and any(
                n.strip().lower().endswith(al) and n.strip().lower() != al for n in numbered):
            continue
        deduped.append(a)
    return deduped


def _count_questions(body: str) -> int:
    text = re.sub(r"<[^>]+>", " ", body or "")
    return text.count("?")


def _violations(subject: str, body: str, req: GenReq) -> List[str]:
    v = []
    text = subject + "\n" + body
    # no state/zip: 5-digit zip, ", FL", or "Florida"
    if re.search(r"\b\d{5}(?:-\d{4})?\b(?!\s+[A-Za-z])", text) or re.search(r"(,\s*FL\b|\bFlorida\b)", text, re.I):
        v.append("state_or_zip")
    # address in subject only if exactly 1 property
    subj_has_addr = bool(re.search(r"\d+\s+\w+.*\b" + STREET_SUFFIX + r"\b", subject, re.I)
                         or re.search(r"\b" + STREET_SUFFIX + r"\b", subject))
    if req.property_count != 1 and subj_has_addr:
        v.append("address_in_subject_but_multi")
    # no name in subject unless allowed
    if not req.name_in_subject_ok and req.name and re.search(r"\b" + re.escape(req.name) + r"\b", subject, re.I):
        v.append("name_in_subject_not_allowed")
    # >5 properties: no story (little prose outside the list)
    if req.property_count > 5:
        prose = re.sub(r"<ul>.*?</ul>", " ", body, flags=re.S | re.I)
        prose = re.sub(r"<[^>]+>", " ", prose)
        sentences = [x for x in re.split(r"[.!?]+", prose) if len(x.strip()) > 12]
        if len(sentences) > 3:
            v.append("story_with_many_properties")
    # Rich 18.09 (1): never tell them our plans/purpose
    if re.search(r"(off|from) my (sheet|list)|my sheet|updat\w* (my|the) (list|sheet)|so i can update|clean(ing)? up my list", text, re.I):
        v.append("mentions_our_plans")
    # Rich 18.09 (3): "still open" / "open" is not correct terminology
    if re.search(r"\bopen\b", text, re.I):
        v.append("uses_open")
    # Rich 18.09 (2): 1-5 properties: ask in a roundabout way, not "is it still available"
    if req.property_count <= 5 and re.search(
            r"still (available|for sale|on the market|around|active|there)|(is|are) (it|this|they|these|both(?: of these)?) (still )?available|gone yet|already (sold|gone)",
            text, re.I):
        v.append("direct_availability_question")
    # Rich 22.09: at most two questions, and at least one (it is a check-in, not a statement)
    nq = _count_questions(body)
    if nq > MAX_QUESTIONS:
        v.append("too_many_questions")
    if nq == 0:
        v.append("no_question")
    # a phone number must never appear as a property line
    if re.search(r"<li>\s*\(?\d{3}\)?[-. ]?\d{3}[-. ]?\d{4}\s*</li>", body):
        v.append("phone_as_address")
    # Rich 23.09 (video): never invent a first name when we have none
    if not (req.name or "").strip():
        first_line = re.sub(r"<[^>]+>", " ", body).strip()[:60]
        if re.match(r"(Hi|Hey|Hello|Dear)\s+[A-Z][a-z]+\b", first_line):
            v.append("invented_name")
    # Rich 23.09 (video): singular/plural must match the number of properties ("this" vs "these")
    if req.property_count == 1:
        if re.search(r"\b(these|those|both|couple of|two|three|2|3)\b(?![^<]*</li>)", text, re.I):
            v.append("plural_for_single_property")
    elif req.property_count >= 2:
        if re.search(r"\b(this (one|address|property|deal|listing))\b", text, re.I):
            v.append("singular_for_multiple_properties")
    return v


SYS = """You write ONE short, friendly availability check-in email that sounds like Rich (a real wholesale
real-estate investor) personally typed it - never like a template or marketing blast. Rules:
- Human and warm but not over-the-top. Short. Correct English.
- Ask ONLY the questions given in questions_to_ask (at most two), in your own natural words. Do not add
  any other question. Never more than two question marks in the whole email.
- 1 to 5 properties: do NOT ask directly whether it is still available / still for sale / gone. Ask in a
  roundabout way, the way Rich would: buyers are asking about these deals he sent, then the questions
  from questions_to_ask. (If it is gone, the wholesaler will say so on his own.)
- Never mention Rich's plans or purpose: no "I'll take it off my sheet", no "updating my list", no
  "so I can update them".
- Never use the word "open" for a property's status (no "still open").
- Different phrasing for ONE property vs MULTIPLE. With ONE property say "this address" / "this one", never
  "these", "both", "two". With several, say "these", never "this address". The subject must match too.
- If wholesaler_first_name is EMPTY, open without any name ("Hi," / "Hey there,"). NEVER invent a name.
- If MORE THAN 5 properties: NO story at all - one short line asking which of the listed ones are still
  available, then the list. For the subject, use or vary Rich's own ideas from subject_ideas_many.
- Addresses: ONLY street, or street + city. NEVER include state, zip or country. Mix it up. Never write
  a phone number anywhere.
- If exactly one property, you MAY put the address in the subject.
- Do NOT use the wholesaler's name in the subject unless name_in_subject_allowed is true.
- Vary the wording; do NOT reuse any subject/opener listed in avoid_subjects / avoid_openers.
- Use straight quotes only. Do NOT use em dashes or en dashes; use a comma or a hyphen.
- Sign off simply as Rich. No company signature, no links, no phone numbers.
- body must be simple HTML: one <p> per line and one <p> per property (NEVER <ul>/<li> lists, and no leading "-" or bullet characters). No <html>/<head>/<body>.
Return ONLY JSON: {"subject": "...", "body": "<html fragment>"}."""

# Question angles Rich would use (22.09: "asking the same question to all" + "limit to 2 questions").
# key -> wording hint for the model. Pairs are rotated per call and avoided per wholesaler.
QUESTIONS = {
    "showings":   "whether he has any showings planned for these",
    "access":     "what access looks like (lockbox, appointment, tenant)",
    "offers":     "whether he currently has any offers on them",
    "news":       "what the latest news is on these",
    "price":      "whether there is any flexibility on the price",
    "timeline":   "what the closing timeline looks like",
    "photos":     "whether he has more photos or a walkthrough video",
    "occupancy":  "what the occupancy situation is right now",
    "condition":  "anything new on condition or repairs since he sent it",
}
PAIRS: List[Tuple[str, str]] = [
    ("showings", "offers"), ("news", "access"), ("price", "timeline"), ("photos", "occupancy"),
    ("offers", "condition"), ("access", "price"), ("showings", "photos"), ("news", "timeline"),
    ("occupancy", "offers"), ("condition", "showings"), ("timeline", "access"), ("price", "news"),
]
_pair_cycle = itertools.cycle(range(len(PAIRS)))
_pair_lock = threading.Lock()


def _pick_pair(used: List[str]) -> Tuple[str, str]:
    """Round-robin over PAIRS so consecutive calls (one batch) get different pairs; skip pairs that
    contain a key this wholesaler already got (last 6 keys from the memo). Falls back to the plain
    round-robin pair if every pair is excluded."""
    avoid = set((used or [])[-6:])
    with _pair_lock:
        for _ in range(len(PAIRS)):
            i = next(_pair_cycle)
            p = PAIRS[i]
            if p[0] not in avoid and p[1] not in avoid:
                return p
        return PAIRS[next(_pair_cycle)]


EXAMPLES = ["Got a couple buyers asking about these deals you sent me. Do you have any showings planned?",
            "What's the latest news on these?",
            "Buyer interested in your deal at (address). What does access look like?",
            "A few clients are interested in these. Do you currently have any offers?"]
SUBJECT_IDEAS_MANY = ["Inquiry about your listings", "Question about deals you sent me",
                      "RE: regarding your Wholesale deals", "Question about your inventory",
                      "What do you have for sale?", "I got some buyers interested", "Still for sale?",
                      "Can we set up a showing for these?", "Your Latest Deal List"]


def _call_model(user: dict):
    from openai import OpenAI
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    model = os.getenv("AVAILABILITY_EMAIL_MODEL", "gpt-6-luna")
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": SYS},
                  {"role": "user", "content": json.dumps(user, ensure_ascii=False)}],
        response_format={"type": "json_object"},
    )
    d = json.loads(resp.choices[0].message.content)
    return _sanitize((d.get("subject") or "").strip()), _sanitize((d.get("body") or "").strip())


@router.post("/availability-email/generate")
def generate(req: GenReq, request: Request):
    _check_token(request)
    addrs = _clean_addresses(req.addresses)
    if not addrs:
        raise HTTPException(status_code=422, detail={"error": "no_addresses"})
    req.property_count = len(addrs)   # count what we actually list, not what the extractor counted
    pair = _pick_pair(req.used_questions)
    q_keys = list(pair)
    if req.is_land:   # Rich 30.09: never ask about repairs/condition for land/lot
        q_keys = [k for k in q_keys if k != "condition"] or ["news"]
    if len(q_keys) > 1 and random.choice((True, False)):   # Rich 30.09: vary 1 or 2 questions
        q_keys = q_keys[:1]
    # >5 properties: Rich's rule is one line "which of these are still available" - that is the only question
    questions = [QUESTIONS[k] for k in q_keys] if req.property_count <= 5 else ["which of the listed ones are still available"]
    user = {
        "wholesaler_first_name": (req.name or "").strip(),
        "property_count": req.property_count,
        "addresses": addrs,
        "name_in_subject_allowed": bool(req.name_in_subject_ok),
        "avoid_subjects": (req.used_subjects or [])[-8:],
        "avoid_openers": (req.used_openers or [])[-8:],
        "questions_to_ask": questions,
        "example_phrasings_pool": EXAMPLES,
        "subject_ideas_many": SUBJECT_IDEAS_MANY,
    }
    last = None
    subject, body = "", ""
    for attempt in range(3):  # regenerate-or-fail
        subject, body = _call_model(user)
        if not subject or not body:
            last = ["empty"]; continue
        v = _violations(subject, body, req)
        if not v:
            return {"subject": subject, "body": _delist(body), "questions": q_keys, "addresses": addrs}
        last = v
        logging.warning("availability gen attempt %d violations=%s", attempt + 1, v)
    # Rich 30.09 (Blagojche): if the ONLY remaining problem is plural-for-single, fix it
    # deterministically (these->this) and return - never drop to the old GlobiFlow template.
    if last == ["plural_for_single_property"] and subject and body:
        subject, body = _singularize(subject), _singularize(body)
        if not _violations(subject, body, req):
            return {"subject": subject, "body": _delist(body), "questions": q_keys, "addresses": addrs}
    raise HTTPException(status_code=422, detail={"error": "validation_failed", "violations": last})


# --- Opt-out lookup for the availability queue (Blagojche/Rich 01.10) ---
# Server-02 queue calls this before enqueuing a Saturday availability email: a wholesaler with
# Opted Out = Yes OR Special List = "Exclude from ALL" must never get the Tuesday email.
# Token in the X-Alert-Token header (same INTERNAL_ALERT_TOKEN). On any Podio error -> 502 so the
# caller can fail-open (send) rather than silently treat an error as "not opted out".
_OPT_FIELD_ID = 276480365     # "Opted Out" (Yes/No)
_SPECIAL_LIST_FIELD_ID = 166938662  # "Special List" (e.g. "Exclude from ALL")


def _wh_field_text(item, field_id):
    for f in (item.get("fields") or []):
        if f.get("field_id") == field_id:
            vals = f.get("values") or []
            if not vals:
                return None
            v = vals[0].get("value")
            return v.get("text") if isinstance(v, dict) else v
    return None


@router.get("/wholesaler/optout")
def wholesaler_optout(email: str = "", request: Request = None):
    _check_token(request)
    e = (email or "").strip().lower()
    if not e:
        return {"email": e, "opted_out": False, "reason": "no_email", "item_id": None}
    try:
        from integrations.podio.direct_wholesaler import (
            get_podio_access_token, find_wholeseller_item_by_email, _get_item)
        tok = get_podio_access_token()
        iid = find_wholeseller_item_by_email(tok, e)
        if not iid:
            return {"email": e, "opted_out": False, "reason": "not_found", "item_id": None}
        item = _get_item(tok, int(iid))
        if not item:
            raise HTTPException(status_code=502, detail="podio_item_unavailable")
        opt = _wh_field_text(item, _OPT_FIELD_ID)
        spec = _wh_field_text(item, _SPECIAL_LIST_FIELD_ID)
        opted = (str(opt).strip().lower() == "yes") or \
                (str(spec or "").strip().lower() == "exclude from all")
        if str(opt).strip().lower() == "yes":
            reason = "opted_out"
        elif opted:
            reason = "exclude_from_all"
        else:
            reason = "active"
        return {"email": e, "opted_out": bool(opted), "reason": reason, "item_id": int(iid)}
    except HTTPException:
        raise
    except Exception:
        import logging as _lg
        _lg.exception("wholesaler_optout failed email=%s", e)
        raise HTTPException(status_code=502, detail="podio_lookup_failed")
