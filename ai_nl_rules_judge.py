# ai_nl_rules_judge.py
import os, json, yaml, re
from typing import Dict, Any
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY)

JUDGE_SYSTEM_PROMPT = """\
You are a strict listing screener.

INPUTS YOU WILL RECEIVE:
1) A human-written screening policy containing plain-English rules in a specific order.
2) JSON facts for ONE listing, using keys like: property_type, bedrooms, bathrooms_full, list_price_usd,
   region_bucket, city, state, is_on_water, water_feature, is_land_only, lot_size_sqft, lot_size_acres,
   land_under_5000_sqft, under_900_sqft, is_frame_or_wood, is_teardown_or_redevelopment, is_mobile_home,
   hoa_total_monthly_usd (fee + assessments), etc. Missing fields may be null.

HOW TO DECIDE:
- Evaluate rules IN THE GIVEN ORDER. Stop at the FIRST rule that applies and is not exempted by its “Unless” clause.
- If a rule has an exception ("Unless ...") and the exception is satisfied, that rule does NOT cause a skip. Continue to the next rule.
- If information to evaluate a rule is missing/uncertain, do NOT skip on that rule; continue to the next rule.
- Use only the listing facts provided. Do not guess or add information.
- Interpret “on the water” as true if the listing explicitly shows waterfront or these features: oceanfront, ocean access, intracoastal, bayfront, canal, lakefront, riverfront.
- “South Florida Tri-County” = Miami-Dade, Broward, Palm Beach.
- “Rest of Florida” = anywhere in Florida outside South Florida Tri-County (includes St. Lucie and Fort Pierce).
- For condos: if both HOA fee and assessments exist, sum them for “hoa_total_monthly_usd”.

OUTPUT FORMAT (STRICT):
Return a JSON object with EXACTLY these keys:
- "listing_status": "Passed" or "Skipped"
- "skip_reason": string or null
- "matched_rule_id": string or null

If Skipped, supply a short skip_reason paraphrasing the matching rule. If Passed, both reason and rule id must be null.
"""

def _response_format() -> Dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "listing_rule_decision",
            "strict": True,
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "listing_status": {"type": "string", "enum": ["Passed", "Skipped"]},
                    "skip_reason": {"type": ["string", "null"]},
                    "matched_rule_id": {"type": ["string", "null"]}
                },
                "required": ["listing_status", "skip_reason", "matched_rule_id"]
            }
        }
    }

def judge_listing_with_english_rules(listing_facts: Dict[str, Any], rules_yaml: Dict[str, Any]) -> Dict[str, Any]:
    # Render policy text
    policy = rules_yaml.get("policy", "").strip()
    rules_lines = []
    for r in rules_yaml.get("rules", []):
        rid = r.get("id", "")
        text = (r.get("text") or "").strip()
        rules_lines.append(f"[{rid}] {text}")
    rules_text = f"POLICY:\n{policy}\n\nRULES (ordered):\n" + "\n".join(f"- {line}" for line in rules_lines)

    messages = [
        {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
        {"role": "user",
         "content": (
            f"{rules_text}\n\n"
            f"LISTING FACTS (strict JSON):\n{json.dumps(listing_facts, ensure_ascii=False)}\n\n"
            "TASK: Apply rules in order. Stop at the first applicable rule (unless its exception applies). "
            "If a rule applies and no exception applies, return Skipped with a short reason and the rule id. "
            "If none apply, return Passed with null reason and null rule id."
         )
        }
    ]

    chat = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=messages,
        temperature=0.0,
        response_format=_response_format(),
        max_tokens=700
    )
    return json.loads(chat.choices[0].message.content)
