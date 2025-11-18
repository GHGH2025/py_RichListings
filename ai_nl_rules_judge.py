# # --- add (or keep) these imports/helpers at top of ai_nl_rules_judge.py ---
# import os, json, yaml, re
# from typing import Dict, Any
# from dotenv import load_dotenv
# load_dotenv()

# # LangChain (newer/older compatibility)
# try:
#     from langchain_openai import ChatOpenAI
# except Exception:
#     from langchain.chat_models import ChatOpenAI
# from langchain.schema import SystemMessage, HumanMessage

# def _response_format() -> Dict[str, Any]:
#     return {
#         "type": "json_schema",
#         "json_schema": {
#                 "name": "listing_rule_decision",
#                 "strict": True,
#                 "schema": {
#                     "type": "object",
#                     "additionalProperties": False,
#                     "properties": {
#                         "listing_status": {"type": "string", "enum": ["Passed", "Skipped"]},
#                         "skip_reason": {"type": ["string", "null"]},
#                         "matched_rule_id": {"type": ["string", "null"]}
#                     },
#                     "required": ["listing_status", "skip_reason", "matched_rule_id"]
#                 }
#         }
#     }

# def _extract_json(text: str) -> Dict[str, Any]:
#     try:
#         return json.loads(text)
#     except Exception:
#         pass
#     try:
#         m = re.search(r"\{.*\}", text, re.DOTALL)
#         if m:
#             return json.loads(m.group(0))
#     except Exception:
#         pass
#     # bracket-balance fallback
#     start = text.find("{")
#     if start != -1:
#         depth = 0
#         for i, ch in enumerate(text[start:], start=start):
#             if ch == "{":
#                 depth += 1
#             elif ch == "}":
#                 depth -= 1
#                 if depth == 0:
#                     candidate = text[start:i+1]
#                     return json.loads(candidate)
#     raise ValueError("LLM did not return valid JSON")

# # --- DROP-IN: same name/signature/output ---
# def judge_listing_with_english_rules(listing_facts: Dict[str, Any], rules_yaml: Dict[str, Any]) -> Dict[str, Any]:
#     policy = rules_yaml.get("policy", "")
#     rules  = rules_yaml.get("rules", []) or []
#     rule_text = "\n".join([f"- [{r['id']}] {r['text'].strip()}" for r in rules if r.get("id") and r.get("text")])

#     # === CONSTRUCT PROMPT (unchanged from your tested code) ===
#     system_prompt = f"""
# You are a real estate compliance assistant. You will receive:

# 1. A listing JSON object.
# 2. A policy and a set of rules to evaluate in order.

# NUMERIC RULES:
# - "Over $X" means strictly greater than X (price > X)
# - "Under $X" means strictly less than X (price < X)
# - "At least X" means price >= X
# - "At most X" means price <= X

# Do not confuse "over" with "greater than or equal to".


# # Evaluate the listing against the rules. Follow these instructions:
# # - Apply rules in order.
# # - Use only information in the listing.
# # - Do not guess missing fields. If a field is missing and required for a rule, skip the rule.
# # - Return only a JSON object matching the schema below.

# Evaluation Instructions:
# - Apply rules in order.
# - Stop at the first rule that applies and is not exempted.
# - Use only information in the listing.
# - Do not guess missing fields. If a field is missing and required for a rule, skip that rule.
# - Return a JSON object that matches the schema.

# Important:
# - If listing is "Skipped", include a clear, detailed explanation in `skip_reason` showing why it was skipped and how the rule applies.
# - Mention the rule logic (e.g., price threshold, property type, location).
# - If listing is "Passed", set `skip_reason` and `matched_rule_id` to null.

# {_response_format()}

# Here is the decision policy:
# {policy}

# Rules:
# {rule_text}
# """.strip()

#     listing_str = json.dumps(listing_facts, indent=2, ensure_ascii=False)
#     human_prompt = f"""
# Evaluate this listing:

# {listing_str}

# Respond ONLY with the JSON object matching the schema. Do not include any text, explanation, or markdown — just the raw JSON.

# """.strip()
#     print(">",system_prompt)
#     print(">>",human_prompt)

#     # === CALL LLM (model name can come from env; default matches your test) ===
#     llm = ChatOpenAI(model=os.getenv("OPENAI_MODEL", "gpt-5.1"))

#     # resp = llm([SystemMessage(content=system_prompt), HumanMessage(content=human_prompt)])

#     # result = _extract_json(resp.content)

#     # # Return exactly what the model produced (same as your tested flow)
#     # return result

#     messages = [SystemMessage(content=system_prompt),HumanMessage(content=human_prompt)]

#     # Prefer the new API, fall back if running on an older LangChain
#     invoke = getattr(llm, "invoke", None)
#     if callable(invoke):
#         resp = llm.invoke(messages)   # ✅ no deprecation warning
#     else:
#         resp = llm(messages)          # legacy fallback
#     print("resp.content",resp.content)
#     result = _extract_json(resp.content)
#     return result



# ai_nl_rules_judge.py

import os, json, yaml, re
from typing import Dict, Any
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Create a single shared client
client = OpenAI(
    api_key=OPENAI_API_KEY,
    timeout=800.0,        # 30s hard timeout for network+read
    max_retries=0        # keep low; you can set 0 or 1
)

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

def _extract_json(text: str) -> Dict[str, Any]:
    try:
        return json.loads(text)
    except Exception:
        pass
    try:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            return json.loads(m.group(0))
    except Exception:
        pass
    # bracket-balance fallback
    start = text.find("{")
    if start != -1:
        depth = 0
        for i, ch in enumerate(text[start:], start=start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start:i+1]
                    return json.loads(candidate)
    raise ValueError("LLM did not return valid JSON")


def judge_listing_with_english_rules(
    listing_facts: Dict[str, Any],
    rules_yaml: Dict[str, Any]
) -> Dict[str, Any]:
    policy = rules_yaml.get("policy", "")
    rules  = rules_yaml.get("rules", []) or []
    rule_text = "\n".join(
        [f"- [{r['id']}] {r['text'].strip()}" for r in rules if r.get("id") and r.get("text")]
    )

    system_prompt = f"""
You are a real estate compliance assistant. You will receive:

1. A listing JSON object.
2. A policy and a set of rules to evaluate in order.

NUMERIC RULES:
- "Over $X" means strictly greater than X (price > X)
- "Under $X" means strictly less than X (price < X)
- "At least X" means price >= X
- "At most X" means price <= X

Do not confuse "over" with "greater than or equal to".


# Evaluate the listing against the rules. Follow these instructions:
# - Apply rules in order.
# - Use only information in the listing.
# - Do not guess missing fields. If a field is missing and required for a rule, skip the rule.
# - Return only a JSON object matching the schema below.

Evaluation Instructions:
- Apply rules in order.
- Stop at the first rule that applies and is not exempted.
- Use only information in the listing.
- Do not guess missing fields. If a field is missing and required for a rule, skip that rule.
- Return a JSON object that matches the schema.

Important:
- If listing is "Skipped", include a clear, detailed explanation in `skip_reason` showing why it was skipped and how the rule applies.
- Mention the rule logic (e.g., price threshold, property type, location).
- If listing is "Passed", set `skip_reason` and `matched_rule_id` to null.

{_response_format()}

Here is the decision policy:
{policy}

Rules:
{rule_text}
""".strip()

    listing_str = json.dumps(listing_facts, indent=2, ensure_ascii=False)
    human_prompt = f"""
Evaluate this listing:

{listing_str}

Respond ONLY with the JSON object matching the schema. Do not include any text, explanation, or markdown — just the raw JSON.
""".strip()


    model_name = os.getenv("OPENAI_MODEL", "gpt-5.1")

    # --- Direct OpenAI call (no LangChain) ---
    resp = client.chat.completions.create(
        model=model_name,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": human_prompt},
        ],
        response_format=_response_format()
    )

    content = resp.choices[0].message.content
    print("resp.content", content)

    result = _extract_json(content)
    return result
