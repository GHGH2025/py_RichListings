"""
Capture OpenAI token usage per listing and estimate USD cost.
Best-effort — never raises into the pipeline.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence

from observability.openai_pricing import estimate_cost_usd

logger = logging.getLogger(__name__)

_MAX_AI_CALLS = 80


def _now() -> datetime:
    return datetime.utcnow()


def extract_usage_from_response(response: Any) -> Dict[str, int]:
    """Normalize Chat Completions usage into plain ints."""
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    if usage is None:
        return {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cached_tokens": 0,
            "total_tokens": 0,
        }

    def _get(obj: Any, key: str, default: int = 0) -> int:
        if obj is None:
            return default
        if isinstance(obj, dict):
            val = obj.get(key, default)
        else:
            val = getattr(obj, key, default)
        try:
            return int(val or 0)
        except (TypeError, ValueError):
            return default

    prompt = _get(usage, "prompt_tokens")
    completion = _get(usage, "completion_tokens")
    total = _get(usage, "total_tokens") or (prompt + completion)

    cached = 0
    details = None
    if isinstance(usage, dict):
        details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details")
    else:
        details = getattr(usage, "prompt_tokens_details", None) or getattr(
            usage, "input_tokens_details", None
        )
    if details is not None:
        cached = _get(details, "cached_tokens")

    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cached_tokens": cached,
        "total_tokens": total,
    }


def build_usage_record(
    *,
    model: Optional[str],
    stage: str,
    call_name: str,
    usage: Dict[str, int],
    share: float = 1.0,
) -> Dict[str, Any]:
    share = float(share) if share and share > 0 else 1.0
    prompt = int(round(usage.get("prompt_tokens", 0) * share))
    completion = int(round(usage.get("completion_tokens", 0) * share))
    cached = int(round(usage.get("cached_tokens", 0) * share))
    total = int(round(usage.get("total_tokens", 0) * share)) or (prompt + completion)
    cost = estimate_cost_usd(
        model=model,
        prompt_tokens=prompt,
        completion_tokens=completion,
        cached_tokens=cached,
    )
    return {
        "stage": stage,
        "call_name": call_name,
        "model": model or "",
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cached_tokens": cached,
        "total_tokens": total,
        "cost_usd": cost,
        "at": _now().isoformat() + "Z",
        "share": share if share != 1.0 else None,
    }


def _persist_call(listing_id: str, call: Dict[str, Any]) -> None:
    from models import ListingPipelineMetric

    lid = str(listing_id).strip()
    if not lid:
        return

    metric = ListingPipelineMetric.objects(listing_id=lid).first()
    if not metric:
        try:
            from observability.pipeline_metrics import record_listing_created

            record_listing_created(lid)
            metric = ListingPipelineMetric.objects(listing_id=lid).first()
        except Exception:
            metric = None
    if not metric:
        return

    metric.ai_prompt_tokens = int(metric.ai_prompt_tokens or 0) + int(call["prompt_tokens"])
    metric.ai_completion_tokens = int(metric.ai_completion_tokens or 0) + int(
        call["completion_tokens"]
    )
    metric.ai_cached_tokens = int(metric.ai_cached_tokens or 0) + int(call["cached_tokens"])
    metric.ai_total_tokens = int(metric.ai_total_tokens or 0) + int(call["total_tokens"])
    metric.ai_cost_usd = round(float(metric.ai_cost_usd or 0.0) + float(call["cost_usd"]), 8)

    calls = list(metric.ai_calls or [])
    # drop None share for cleaner storage
    stored = {k: v for k, v in call.items() if v is not None}
    calls.append(stored)
    if len(calls) > _MAX_AI_CALLS:
        calls = calls[-_MAX_AI_CALLS:]
    metric.ai_calls = calls
    metric.updated_at = _now()
    metric.save()


def record_openai_usage(
    response: Any = None,
    *,
    model: Optional[str] = None,
    stage: str,
    call_name: str,
    listing_id: Optional[str] = None,
    listing_ids: Optional[Sequence[str]] = None,
    usage: Optional[Dict[str, int]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Record usage against one listing, or split evenly across listing_ids
    (e.g. one email parse → N deals).
    """
    try:
        usage_data = usage or extract_usage_from_response(response)
        if not any(usage_data.get(k, 0) for k in ("prompt_tokens", "completion_tokens", "total_tokens")):
            return None

        targets: List[str] = []
        if listing_ids:
            targets = [str(x).strip() for x in listing_ids if str(x).strip()]
        elif listing_id:
            targets = [str(listing_id).strip()]

        if not targets:
            return usage_data

        n = len(targets)
        share = 1.0 / n
        for lid in targets:
            call = build_usage_record(
                model=model,
                stage=stage,
                call_name=call_name,
                usage=usage_data,
                share=share,
            )
            _persist_call(lid, call)
        return usage_data
    except Exception:
        logger.exception(
            "record_openai_usage failed stage=%s call=%s listing_id=%s",
            stage,
            call_name,
            listing_id,
        )
        return None


def tracked_chat_create(
    client: Any,
    *,
    stage: str,
    call_name: str,
    listing_id: Optional[str] = None,
    listing_ids: Optional[Sequence[str]] = None,
    **create_kwargs: Any,
) -> Any:
    """Wrap client.chat.completions.create and record usage."""
    resp = client.chat.completions.create(**create_kwargs)
    try:
        record_openai_usage(
            resp,
            model=create_kwargs.get("model"),
            stage=stage,
            call_name=call_name,
            listing_id=listing_id,
            listing_ids=listing_ids,
        )
    except Exception:
        logger.exception("tracked_chat_create record failed call=%s", call_name)
    return resp


def allocate_usage_to_listings(
    usage: Dict[str, int],
    *,
    model: Optional[str],
    stage: str,
    call_name: str,
    listing_ids: Iterable[str],
) -> None:
    record_openai_usage(
        model=model,
        stage=stage,
        call_name=call_name,
        listing_ids=list(listing_ids),
        usage=usage,
    )
