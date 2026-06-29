"""
rules_step – AI English rules gate.

Loads ai_listing_rules.yaml once at import time (the YAML path comes from
core.paths.data_path, same as v1). Calls judge_listing_with_english_rules and
applies the Passed / Skipped decision.

Outcomes:
    CONTINUE  – listing passes all rules; status → 'passed'
    EXIT      – listing skipped by a rule; status → 'skipped'
"""
import logging
from datetime import datetime
from functools import lru_cache
from typing import Any, Dict

import yaml

from ai.rules_judge import judge_listing_with_english_rules
from ai.rules_runner import _facts_from_doc
from core.paths import data_path
from models import ParsedListing
from observability.pipeline_metrics import record_listing_stage
from pipeline_v2.steps import StepResult

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _load_rules() -> Dict[str, Any]:
    path = data_path("ai_listing_rules.yaml")
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def run(pl: ParsedListing) -> StepResult:
    """
    Apply AI English rules to a single listing.

    Returns StepResult.CONTINUE on pass, StepResult.EXIT on skip.
    """
    now = datetime.utcnow()
    rules_yaml = _load_rules()

    facts = _facts_from_doc(pl)
    try:
        result = judge_listing_with_english_rules(facts, rules_yaml)
    except Exception as exc:
        pl.update(
            set__complete_info__rules_ai_error=str(exc),
            set__updated_at=now,
        )
        logger.warning("rules_step: listing=%s judge raised %s – retaining for retry", pl.id, exc)
        return StepResult.EXIT

    status = result.get("listing_status")
    reason = result.get("skip_reason")
    pass_reason = result.get("pass_reason")
    rule_id = result.get("matched_rule_id")
    rules_version = str(rules_yaml.get("version")) if rules_yaml.get("version") is not None else None

    if status == "Skipped":
        pl.update(
            set__status="skipped",
            set__rules_ai_rule_id=rule_id,
            set__rules_ai_version=rules_version,
            set__rules_ai_reason=reason,
            set__skipped_or_posted_at=now,
            set__updated_at=now,
        )
        try:
            record_listing_stage(str(pl.id), "rules_skipped", listing_status="skipped", skip_reason=reason)
        except Exception:
            pass
        logger.info("rules_step: listing=%s EXIT (rule=%s)", pl.id, rule_id)
        return StepResult.EXIT

    pl.update(
        set__status="passed",
        set__rules_ai_rule_id=None,
        set__rules_ai_version=rules_version,
        set__rules_ai_reason=pass_reason,
        set__skipped_or_posted_at=now,
        set__updated_at=now,
    )
    try:
        record_listing_stage(str(pl.id), "rules", listing_status="passed")
    except Exception:
        pass
    logger.info("rules_step: listing=%s CONTINUE (passed)", pl.id)
    return StepResult.CONTINUE
