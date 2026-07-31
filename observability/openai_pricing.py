"""
OpenAI list prices used to estimate deal AI cost at call time.
USD per 1M tokens. Update when OpenAI publishes new rates.
Source: https://developers.openai.com/api/docs/pricing (as of RATES_AS_OF).
"""
from __future__ import annotations

import json
import os
from typing import Dict, Optional, Tuple

# YYYY-MM-DD — bump when you edit MODEL_RATES
RATES_AS_OF = "2026-07-31"

# model -> (input_per_1m, output_per_1m, cached_input_per_1m)
MODEL_RATES: Dict[str, Tuple[float, float, float]] = {
    "gpt-4o-mini": (0.15, 0.60, 0.075),
    "gpt-4.1-mini": (0.40, 1.60, 0.10),
    "gpt-4.1": (2.00, 8.00, 0.50),
    "gpt-5-mini": (0.25, 2.00, 0.025),
    "gpt-5": (1.25, 10.00, 0.125),
    "gpt-5.1": (1.25, 10.00, 0.125),
    "gpt-5.4-mini": (0.75, 4.50, 0.075),
    "gpt-5.4-nano": (0.20, 1.25, 0.02),
    "gpt-5.6-luna": (0.20, 1.20, 0.02),
    "gpt-5.6-terra": (2.00, 12.00, 0.20),
    "gpt-5.6-sol": (5.00, 30.00, 0.50),
}

# Fallback when model is unknown (conservative mid-tier)
_DEFAULT_RATES = (1.25, 10.00, 0.125)


def _load_override_rates() -> Dict[str, Tuple[float, float, float]]:
    """Optional JSON override via OPENAI_PRICE_TABLE_JSON env (model -> {input,output,cached})."""
    raw = (os.getenv("OPENAI_PRICE_TABLE_JSON") or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    out: Dict[str, Tuple[float, float, float]] = {}
    if not isinstance(data, dict):
        return out
    for model, vals in data.items():
        if not isinstance(vals, dict):
            continue
        try:
            out[str(model)] = (
                float(vals.get("input", vals.get("input_per_1m", 0))),
                float(vals.get("output", vals.get("output_per_1m", 0))),
                float(vals.get("cached", vals.get("cached_input_per_1m", 0))),
            )
        except (TypeError, ValueError):
            continue
    return out


def get_rates(model: Optional[str]) -> Tuple[float, float, float]:
    key = (model or "").strip()
    overrides = _load_override_rates()
    if key in overrides:
        return overrides[key]
    if key in MODEL_RATES:
        return MODEL_RATES[key]
    # snapshot ids like gpt-4o-mini-2024-07-18
    for known, rates in {**MODEL_RATES, **overrides}.items():
        if key.startswith(known):
            return rates
    return _DEFAULT_RATES


def estimate_cost_usd(
    *,
    model: Optional[str],
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cached_tokens: int = 0,
) -> float:
    inp, out, cached = get_rates(model)
    # Cached tokens are billed at cached rate; uncached prompt = prompt - cached
    uncached_prompt = max(0, int(prompt_tokens) - int(cached_tokens or 0))
    cost = (
        (uncached_prompt / 1_000_000.0) * inp
        + (int(cached_tokens or 0) / 1_000_000.0) * cached
        + (int(completion_tokens) / 1_000_000.0) * out
    )
    return round(cost, 8)


def pricing_snapshot() -> dict:
    return {
        "as_of": RATES_AS_OF,
        "currency": "USD",
        "unit": "per_1m_tokens",
        "models": {
            m: {"input": r[0], "output": r[1], "cached_input": r[2]}
            for m, r in MODEL_RATES.items()
        },
    }
