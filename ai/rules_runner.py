# ai_nl_rules_runner.py
import yaml
from datetime import datetime
from typing import Dict, Any
from db.mongo_engine_conn import init_db
from models import ParsedListing
from ai.rules_judge import judge_listing_with_english_rules

def _facts_from_doc(pl: ParsedListing) -> Dict[str, Any]:
    # Prefer the extractor’s blob, then backfill basics from top-level
    facts = dict(pl.complete_info or {})
    def ensure(k, v):
        if facts.get(k) is None:
            facts[k] = v
    # Minimal backfills that rules may rely on:
    ensure("address", getattr(pl, "address", None))
    ensure("city", getattr(pl, "city", None))
    ensure("state", getattr(pl, "state", None))
    ensure("zip", getattr(pl, "zip", None))
    # If extractor didn't write list_price_usd, fallback to top-level price:
    if facts.get("list_price_usd") is None and getattr(pl, "price", None) is not None:
        facts["list_price_usd"] = float(pl.price)
    facts = _infer_single_family(facts)
    return facts

def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _is_tri_county(facts):
    if facts.get("region_bucket") == "south_florida_tri_county":
        return True
    if facts.get("tri_county_name") in ("miami_dade", "broward", "palm_beach"):
        return True
    c = (facts.get("county") or "").strip().lower()
    return c in ("miami-dade", "miami dade", "broward", "palm beach")


def _is_one_bath(baths):
    # "1-bath" = exactly 1 full bath. Rich (24.09): 3/1.5 (1 full + 1 half) is NOT a 1-bath.
    return _num(baths) == 1


def deterministic_rule_skip(facts):
    """Deterministic R6/R7 guard: return (rule_id, reason) on a structured violation, else None.
    Runs before the LLM so identical qualifying data is always rejected (the fuzzy rules stay on the LLM)."""
    if facts.get("water_exception_applicable") is True:
        return None
    beds = _num(facts.get("beds"))
    price = _num(facts.get("list_price_usd"))
    if not (_is_tri_county(facts) and beds == 3 and _is_one_bath(facts.get("baths")) and price is not None):
        return None
    under900 = facts.get("under_900_sqft")
    if under900 is None:
        s = _num(facts.get("sqft"))
        under900 = (s is not None and s < 900)
    if under900 and price > 325000:
        return ("R7", "Tri-County 3-bed/1-bath under 900 sqft at $%.0f > $325,000, not on water" % price)
    if price > 375000:
        return ("R6", "Tri-County 3-bed/1-bath at $%.0f > $375,000, not on water" % price)
    return None


# --- FIX (a) 2026-10-21 (Blagojche): property_type null -> treat as single-family for R1/R2,
# unless the listing text/flags indicate a non-single-family type. Done in code BEFORE the AI call
# (not by rewording the yaml). Root case: 2764 NW 31st St (2bd $337,500) posted as 54023 because
# property_type was null so the LLM did not apply the single-family-only R1/R2.
import re as _re_sf
_NONSF_RE = _re_sf.compile(r"(condo|town\s*-?\s*house|town\s*-?\s*home|duplex|triplex|tri-plex|fourplex|four-plex|quadplex|quad-plex|multi[\s-]*family|multifamily|([2-9]|\d{2,})\s*(?:units?|doors?)|(?:two|three|four)\s*(?:units?|doors?)|mobile\s*home|manufactured)", _re_sf.I)

def _infer_single_family(facts):
    """If property_type is null/unknown, set it to single_family UNLESS the text or structured
    flags say condo/townhouse/duplex/multi-family/unit-count>1 (or mobile/manufactured). Marks
    property_type_inferred=True for traceability. Returns facts (mutated)."""
    pt = str(facts.get("property_type") or "").strip().lower()
    if pt not in ("", "unknown"):
        return facts  # explicit type (incl. other/land/condo/...) -> leave as-is
    # Blagojche 21.10: scope to the 2-bed rules (R1/R2 are the ONLY single-family-only rules),
    # so inferring single_family cannot affect R3/R4/R5 on non-2-bed null-type listings.
    try:
        _beds = float(facts.get("bedrooms")) if facts.get("bedrooms") is not None else None
    except (TypeError, ValueError):
        _beds = None
    if _beds != 2:
        return facts
    if facts.get("is_condo") is True or facts.get("is_mobile_home") is True:
        return facts  # structured non-SF signal
    parts = [facts.get("raw_description_excerpt"), facts.get("source_title"), facts.get("marketing_tags")]
    text = " ".join(str(p) for p in parts if p)
    if _NONSF_RE.search(text):
        return facts  # text indicates a non-single-family type -> do NOT force single_family
    facts["property_type"] = "single_family"
    facts["property_type_inferred"] = True
    return facts

def apply_ai_english_rules(
    rules_path: str,
    limit: int = 100,
    gmail_message_id: str | None = None,
) -> Dict[str, int]:
    # init_db()
    with open(rules_path, "r", encoding="utf-8") as f:
        rules_yaml = yaml.safe_load(f)

    total = passed = skipped = 0

    # Pull a batch ready to screen
    q = ParsedListing.objects(status="processed")
    if gmail_message_id:
        q = q.filter(gmail_message_id=gmail_message_id)
    else:
        q = q.filter(gmail_message_id__not__startswith="test_")
    q = q.limit(limit)

    for pl in q:
        total += 1
        facts = _facts_from_doc(pl)
        det = deterministic_rule_skip(facts)
        if det:
            ruleid, reason = det
            skipped += 1
            pl.update(
                set__status="skipped",
                set__rules_ai_rule_id=ruleid,
                set__rules_ai_version=str(rules_yaml.get("version")) if rules_yaml.get("version") is not None else None,
                set__rules_ai_reason=reason,
                set__skipped_or_posted_at=datetime.utcnow(),
                set__updated_at=datetime.utcnow(),
            )
            try:
                from observability.pipeline_metrics import record_listing_stage
                record_listing_stage(str(pl.id), "rules_skipped", listing_status="skipped", skip_reason=reason)
            except Exception:
                pass
            continue
        try:
            result = judge_listing_with_english_rules(facts, rules_yaml, listing_id=str(pl.id))
        except Exception as e:
            # Keep doc for retry; annotate error for visibility
            pl.update(set__complete_info__rules_ai_error=str(e), set__updated_at=datetime.utcnow())
            continue

        status = result.get("listing_status")
        reason = result.get("skip_reason")
        reasonP = result.get("pass_reason")
        ruleid = result.get("matched_rule_id")
        rules_version = str(rules_yaml.get("version")) if rules_yaml.get("version") is not None else None

        # Rich 30.09: the 2-bed rules R1/R2 apply to single-family homes ONLY. If the LLM flagged
        # R1/R2 on a duplex/multi-family/condo/townhouse/manufactured, override to Passed.
        _ptype = str((getattr(pl, "complete_info", None) or {}).get("property_type") or "").lower()
        if status == "Skipped" and ruleid in ("R1", "R2") and _ptype in (
                "multi_family", "multifamily", "duplex", "triplex", "fourplex", "quadplex",
                "condo", "townhouse", "town_home", "manufactured", "mobile_home"):
            status = "Passed"; ruleid = None; reason = None
            reasonP = "R1/R2 (2-bed rule) does not apply: property_type=%s is not single-family" % _ptype

        if status == "Skipped":
            skipped += 1
            pl.update(
                set__status="skipped",
                set__rules_ai_rule_id=ruleid,
                set__rules_ai_version=rules_version,
                set__rules_ai_reason=reason,
                set__skipped_or_posted_at=datetime.utcnow(),
                set__updated_at=datetime.utcnow(),
            )
            try:
                from observability.pipeline_metrics import record_listing_stage
                record_listing_stage(str(pl.id), "rules_skipped", listing_status="skipped", skip_reason=reason)
            except Exception:
                pass
        else:
            passed += 1
            # Keep status for downstream steps, but store the AI pass decision
            pl.update(
                set__status="passed",
                set__rules_ai_rule_id=None,
                set__rules_ai_version=rules_version,
                set__rules_ai_reason=reasonP,
                set__skipped_or_posted_at=datetime.utcnow(),
                set__updated_at=datetime.utcnow(),
            )
            try:
                from observability.pipeline_metrics import record_listing_stage
                record_listing_stage(str(pl.id), "rules", listing_status="passed")
            except Exception:
                pass

    return {"total": total, "passed": passed, "skipped": skipped}

# if __name__ == "__main__":
#     # Example: python ai_nl_rules_runner.py
#     stats = apply_ai_english_rules("ai_listing_rules.yaml", limit=100)
#     print(stats)
