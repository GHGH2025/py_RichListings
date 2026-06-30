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
    return facts

def apply_ai_english_rules(rules_path: str, limit: int = 100) -> Dict[str, int]:
    # init_db()
    with open(rules_path, "r", encoding="utf-8") as f:
        rules_yaml = yaml.safe_load(f)

    total = passed = skipped = 0

    # Pull a batch ready to screen
    q = ParsedListing.objects(status="processed").limit(limit)

    for pl in q:
        total += 1
        facts = _facts_from_doc(pl)
        try:
            result = judge_listing_with_english_rules(facts, rules_yaml)
        except Exception as e:
            # Keep doc for retry; annotate error for visibility
            pl.update(set__complete_info__rules_ai_error=str(e), set__updated_at=datetime.utcnow())
            continue

        status = result.get("listing_status")
        reason = result.get("skip_reason")
        reasonP = result.get("pass_reason")
        ruleid = result.get("matched_rule_id")
        rules_version = str(rules_yaml.get("version")) if rules_yaml.get("version") is not None else None

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
