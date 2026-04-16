---
title: "Agent Doesn't Implement Response Validation with Retry on Malformed Output"
slug: agent-doesnt-implement-response-validation-with-retry-on-malformed-output
category: reliability
tags: [validation, retry, json, structured-output, pydantic, anthropic-sdk]
description: >
  The agent passes model responses directly to downstream code without
  validating structure or content. When the model returns malformed JSON,
  missing required fields, or out-of-range values the entire request fails
  with an opaque error instead of retrying with a corrective prompt.
symptoms:
  - JSONDecodeError or KeyError tracebacks appear randomly in production
  - Downstream pipelines receive None or empty strings silently
  - No distinction between model refusal and malformed response
  - Retry logic exists but re-sends the original prompt, getting the same bad output
related_solutions:
  - agent-doesnt-implement-model-tiering-by-task-complexity
  - agent-doesnt-implement-cooperative-cancellation-with-structured-concurrency
  - agent-doesnt-implement-chain-of-draft-prompting
---

## Problem

LLMs occasionally produce outputs that violate the expected schema: truncated
JSON, extra prose around a code block, wrong field types, or hallucinated keys.
Naïve retry logic sends the same prompt again and usually gets the same error.
Effective validation-with-retry requires: (a) detecting exactly what is wrong,
(b) including the error and the bad output in the retry prompt so the model can
fix itself, and (c) escalating to a stricter model or a fallback if repeated
attempts fail.

---

## Solution 1 — JSON Extract-and-Repair with One Retry

Extract JSON from anywhere in the response (handles markdown code fences),
attempt `json.loads`, and on failure send the bad output back to the model
with an explicit repair instruction.

```python
import anthropic
import asyncio
import json
import re


def extract_json(text: str) -> str | None:
    """Try to pull JSON out of a string that may have surrounding prose."""
    # Strip markdown code fences
    fence_match = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
    if fence_match:
        return fence_match.group(1).strip()
    # Try the whole string
    text = text.strip()
    if text.startswith("{") or text.startswith("["):
        return text
    # Find the first { or [
    start = min(
        (text.find("{") if "{" in text else len(text)),
        (text.find("[") if "[" in text else len(text)),
    )
    if start < len(text):
        return text[start:]
    return None


async def json_create_with_repair(
    messages: list,
    schema_description: str,
    model: str = "claude-sonnet-4-6",
    max_retries: int = 2,
) -> dict:
    client = anthropic.AsyncAnthropic()
    history = list(messages)

    for attempt in range(max_retries + 1):
        resp = await client.messages.create(
            model=model, max_tokens=1024, messages=history
        )
        raw = resp.content[0].text

        candidate = extract_json(raw)
        if candidate:
            try:
                return json.loads(candidate)
            except json.JSONDecodeError as e:
                parse_error = str(e)
        else:
            parse_error = "No JSON found in response"

        if attempt == max_retries:
            raise ValueError(
                f"JSON validation failed after {max_retries + 1} attempts. "
                f"Last error: {parse_error}\nLast output: {raw[:200]}"
            )

        # Add assistant turn + repair instruction to history
        history.append({"role": "assistant", "content": raw})
        history.append({
            "role": "user",
            "content": (
                f"Your previous response could not be parsed as JSON.\n"
                f"Parse error: {parse_error}\n"
                f"Your output was:\n{raw[:300]}\n\n"
                f"Please respond with ONLY valid JSON matching this schema:\n"
                f"{schema_description}\n"
                f"No prose, no markdown fences — just raw JSON."
            ),
        })
        print(f"[repair] attempt {attempt + 1} — retrying with repair prompt")

    raise RuntimeError("Unreachable")


async def demo():
    schema = '{"name": "string", "score": "integer 0-100", "tags": ["string"]}'
    result = await json_create_with_repair(
        messages=[{
            "role": "user",
            "content": (
                "Evaluate the concept of 'idempotency' and return a JSON object "
                "with fields: name (string), score (0-100), tags (list of strings)."
            ),
        }],
        schema_description=schema,
    )
    print(f"Parsed result: {json.dumps(result, indent=2)}")


asyncio.run(demo())
```

---

## Solution 2 — Pydantic Schema Validation with Field-Level Error Feedback

Parse the JSON into a Pydantic model and, on `ValidationError`, include the
structured field errors in the retry prompt so the model knows exactly which
fields need fixing.

```python
import anthropic
import asyncio
import json
import re
from pydantic import BaseModel, Field, ValidationError
from typing import Literal


class ResearchSummary(BaseModel):
    topic:        str
    confidence:   float = Field(ge=0.0, le=1.0)
    key_points:   list[str] = Field(min_length=1, max_length=10)
    verdict:      Literal["proven", "disputed", "unknown"]
    sources_needed: bool


def extract_json_block(text: str) -> str:
    m = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
    if m:
        return m.group(1)
    stripped = text.strip()
    if stripped.startswith("{"):
        return stripped
    idx = stripped.find("{")
    return stripped[idx:] if idx >= 0 else stripped


async def validated_pydantic_create(
    prompt: str,
    model: str = "claude-sonnet-4-6",
    max_retries: int = 3,
) -> ResearchSummary:
    client = anthropic.AsyncAnthropic()

    system = (
        "You are a research assistant. Always respond with ONLY a JSON object — "
        "no prose, no markdown fences. The JSON must match the required schema exactly."
    )
    schema_hint = ResearchSummary.model_json_schema()
    base_prompt = (
        f"{prompt}\n\nRespond with JSON matching this schema:\n"
        f"{json.dumps(schema_hint, indent=2)}"
    )
    history = [{"role": "user", "content": base_prompt}]

    for attempt in range(max_retries + 1):
        resp = await client.messages.create(
            model=model,
            max_tokens=1024,
            system=system,
            messages=history,
        )
        raw = resp.content[0].text

        try:
            parsed = json.loads(extract_json_block(raw))
            return ResearchSummary(**parsed)
        except json.JSONDecodeError as e:
            error_msg = f"JSON parse error: {e}"
        except ValidationError as e:
            # Include structured Pydantic errors in next prompt
            field_errors = [
                f"  - {'.'.join(str(loc) for loc in err['loc'])}: {err['msg']}"
                for err in e.errors()
            ]
            error_msg = "Validation errors:\n" + "\n".join(field_errors)

        if attempt == max_retries:
            raise ValueError(f"Validation failed after {max_retries + 1} attempts: {error_msg}")

        history.append({"role": "assistant", "content": raw})
        history.append({
            "role": "user",
            "content": (
                f"Your response had errors:\n{error_msg}\n\n"
                f"Your previous output:\n{raw[:400]}\n\n"
                f"Fix these errors and respond with valid JSON only."
            ),
        })
        print(f"[pydantic-retry] attempt {attempt + 1}  error: {error_msg[:80]}")

    raise RuntimeError("Unreachable")


async def demo():
    result = await validated_pydantic_create(
        "Summarise whether quantum computing threatens current RSA encryption."
    )
    print(result.model_dump_json(indent=2))


asyncio.run(demo())
```

---

## Solution 3 — Content Rule Validator with Escalating Model

Apply a set of content rules (length, forbidden phrases, required sections)
after each generation. On failure, escalate to a more capable model tier
rather than repeating with the same model.

```python
import anthropic
import asyncio
import re
from dataclasses import dataclass


@dataclass
class ContentRule:
    name:        str
    check:       callable    # (text: str) -> bool  (True = PASS)
    description: str


RULES: list[ContentRule] = [
    ContentRule("min_length",   lambda t: len(t.strip()) >= 50,
                "Response must be at least 50 characters"),
    ContentRule("no_refusal",   lambda t: not re.search(
        r"\b(cannot|can't|I'm sorry|I apologize|I'm unable)\b", t, re.I),
        "Response must not contain refusal language"),
    ContentRule("has_conclusion", lambda t: re.search(
        r"\b(in conclusion|to summarize|therefore|thus|in summary)\b", t, re.I) is not None,
        "Response must contain a concluding statement"),
    ContentRule("no_placeholder", lambda t: "[" not in t and "TODO" not in t,
                "Response must not contain placeholder text"),
]


MODEL_LADDER = [
    "claude-haiku-4-5-20251001",
    "claude-sonnet-4-6",
    "claude-opus-4-6",
]


def validate(text: str) -> list[str]:
    """Returns list of failed rule descriptions."""
    return [rule.description for rule in RULES if not rule.check(text)]


async def escalating_validated_create(
    messages: list,
    start_model_idx: int = 0,
) -> tuple[str, str]:
    """Returns (text, model_used)."""
    client = anthropic.AsyncAnthropic()
    history = list(messages)

    for model_idx in range(start_model_idx, len(MODEL_LADDER)):
        model = MODEL_LADDER[model_idx]

        for attempt in range(2):  # 2 attempts per model tier
            resp = await client.messages.create(
                model=model, max_tokens=1024, messages=history
            )
            text = resp.content[0].text
            failures = validate(text)

            if not failures:
                print(f"[validate] PASS on model={model} attempt={attempt}")
                return text, model

            print(f"[validate] FAIL model={model} attempt={attempt} rules={failures}")

            if attempt == 0:
                history.append({"role": "assistant", "content": text})
                history.append({
                    "role": "user",
                    "content": (
                        f"Your response failed these quality checks:\n"
                        + "\n".join(f"  - {f}" for f in failures)
                        + "\n\nPlease rewrite your response addressing all issues."
                    ),
                })

        # Escalate to next model
        if model_idx + 1 < len(MODEL_LADDER):
            print(f"[validate] escalating from {model} to {MODEL_LADDER[model_idx + 1]}")
            # Reset history to original for clean escalation
            history = list(messages)

    raise ValueError("All model tiers exhausted without valid response")


async def demo():
    messages = [{"role": "user", "content": "Explain why eventual consistency matters in distributed databases."}]
    text, model = await escalating_validated_create(messages, start_model_idx=0)
    print(f"\nModel used: {model}")
    print(f"Response: {text[:120]}")


asyncio.run(demo())
```

---

## Solution 4 — Tool-Use Structured Output with Schema Enforcement

Force structured output by declaring a single tool whose `input_schema`
describes the desired shape. The model must call the tool with valid arguments,
giving you schema enforcement without JSON extraction or regex hacks.

```python
import anthropic
import asyncio
from typing import Any


EXTRACTOR_TOOL = {
    "name": "extract_analysis",
    "description": "Extract structured analysis from your reasoning.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary":     {"type": "string", "description": "1-2 sentence summary"},
            "pros":        {"type": "array",  "items": {"type": "string"}, "description": "List of advantages"},
            "cons":        {"type": "array",  "items": {"type": "string"}, "description": "List of disadvantages"},
            "score":       {"type": "integer", "minimum": 1, "maximum": 10},
            "recommended": {"type": "boolean"},
        },
        "required": ["summary", "pros", "cons", "score", "recommended"],
        "additionalProperties": False,
    },
}


async def tool_use_validated_create(
    prompt: str,
    model: str = "claude-sonnet-4-6",
    max_retries: int = 2,
) -> dict[str, Any]:
    client = anthropic.AsyncAnthropic()
    messages = [{"role": "user", "content": prompt}]

    for attempt in range(max_retries + 1):
        resp = await client.messages.create(
            model=model,
            max_tokens=1024,
            tools=[EXTRACTOR_TOOL],
            tool_choice={"type": "any"},  # force tool use
            messages=messages,
        )

        tool_use_block = next(
            (b for b in resp.content if b.type == "tool_use"), None
        )

        if tool_use_block is not None:
            return tool_use_block.input  # already validated by SDK against schema

        # Model didn't call the tool (shouldn't happen with tool_choice=any)
        raw = next((b.text for b in resp.content if hasattr(b, "text")), "")
        if attempt == max_retries:
            raise ValueError(f"Model did not call tool after {max_retries + 1} attempts. Last output: {raw[:200]}")

        messages.append({"role": "assistant", "content": resp.content})
        messages.append({
            "role": "user",
            "content": "You must call the extract_analysis tool. Please try again.",
        })
        print(f"[tool-validated] attempt {attempt + 1} — model skipped tool, retrying")

    raise RuntimeError("Unreachable")


async def demo():
    result = await tool_use_validated_create(
        "Analyse the trade-offs of using microservices vs monolith architecture for a 5-person startup."
    )
    import json
    print(json.dumps(result, indent=2))


asyncio.run(demo())
```

---

## Solution 5 — Semantic Validation with a Judge Model

After generation, send the output to a cheap judge model that scores semantic
quality (completeness, accuracy, safety). Retry with corrective feedback if the
judge score is below a threshold.

```python
import anthropic
import asyncio
import json
import re


async def judge_response(
    original_prompt: str,
    response: str,
    judge_model: str = "claude-haiku-4-5-20251001",
) -> tuple[int, str]:
    """Returns (score 1-10, reason)."""
    client = anthropic.AsyncAnthropic()
    judge_resp = await client.messages.create(
        model=judge_model,
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": (
                f"Rate this response on a scale of 1-10 for quality, completeness, and accuracy.\n\n"
                f"ORIGINAL QUESTION:\n{original_prompt}\n\n"
                f"RESPONSE TO EVALUATE:\n{response}\n\n"
                f"Respond with JSON only: {{\"score\": <1-10>, \"reason\": \"<brief reason>\"}}"
            ),
        }],
    )
    raw = judge_resp.content[0].text
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group())
            return int(data["score"]), data.get("reason", "")
        except Exception:
            pass
    return 5, "Could not parse judge score"


async def judge_validated_create(
    prompt: str,
    model: str = "claude-sonnet-4-6",
    min_score: int = 7,
    max_retries: int = 2,
) -> str:
    client = anthropic.AsyncAnthropic()
    history = [{"role": "user", "content": prompt}]

    for attempt in range(max_retries + 1):
        resp = await client.messages.create(
            model=model, max_tokens=1024, messages=history
        )
        text = resp.content[0].text

        score, reason = await judge_response(prompt, text)
        print(f"[judge] attempt={attempt}  score={score}/10  reason={reason[:60]}")

        if score >= min_score:
            return text

        if attempt == max_retries:
            raise ValueError(f"Response quality below {min_score}/10 after all retries. Final score: {score}")

        history.append({"role": "assistant", "content": text})
        history.append({
            "role": "user",
            "content": (
                f"A quality reviewer rated your response {score}/10 with this feedback:\n"
                f"{reason}\n\n"
                f"Please improve your response addressing the feedback."
            ),
        })

    raise RuntimeError("Unreachable")


async def demo():
    result = await judge_validated_create(
        "Explain the differences between optimistic and pessimistic locking in databases.",
        min_score=7,
    )
    print(f"\nFinal response ({len(result)} chars):\n{result[:150]}")


asyncio.run(demo())
```

---

## Solution 6 — Multi-Aspect Validation Pipeline

Chain multiple validators (format → schema → semantic → safety) in a pipeline.
Each validator returns a typed result so the retry prompt includes exactly which
layer failed and why.

```python
import anthropic
import asyncio
import json
import re
from dataclasses import dataclass
from typing import Callable, Awaitable


@dataclass
class ValidationResult:
    passed: bool
    layer:  str
    error:  str = ""
    detail: str = ""


ValidatorFn = Callable[[str, str], Awaitable[ValidationResult]]


async def format_validator(text: str, _prompt: str) -> ValidationResult:
    if len(text.strip()) < 20:
        return ValidationResult(False, "format", "Too short", f"Got {len(text)} chars")
    if text.count("```") % 2 != 0:
        return ValidationResult(False, "format", "Unclosed code fence")
    return ValidationResult(True, "format")


async def json_schema_validator(text: str, _prompt: str) -> ValidationResult:
    m = re.search(r"\{[\s\S]+\}", text)
    if not m:
        return ValidationResult(True, "json_schema")  # Not a JSON task
    try:
        json.loads(m.group())
        return ValidationResult(True, "json_schema")
    except json.JSONDecodeError as e:
        return ValidationResult(False, "json_schema", "Invalid JSON", str(e))


async def safety_validator(text: str, _prompt: str) -> ValidationResult:
    danger = re.findall(r"\b(hack|exploit|malware|ransomware|bypass authentication)\b", text, re.I)
    if danger:
        return ValidationResult(False, "safety", "Unsafe content detected",
                                f"Flagged terms: {danger}")
    return ValidationResult(True, "safety")


VALIDATORS: list[ValidatorFn] = [
    format_validator,
    json_schema_validator,
    safety_validator,
]


async def pipeline_validated_create(
    messages: list,
    model: str = "claude-sonnet-4-6",
    max_retries: int = 2,
) -> str:
    client = anthropic.AsyncAnthropic()
    history = list(messages)
    original_prompt = messages[-1]["content"] if messages else ""

    for attempt in range(max_retries + 1):
        resp = await client.messages.create(
            model=model, max_tokens=1024, messages=history
        )
        text = resp.content[0].text

        # Run all validators
        failures: list[ValidationResult] = []
        for validator in VALIDATORS:
            result = await validator(text, original_prompt)
            if not result.passed:
                failures.append(result)

        if not failures:
            print(f"[pipeline] all validators PASS on attempt {attempt}")
            return text

        print(f"[pipeline] attempt {attempt} FAIL — layers: {[f.layer for f in failures]}")

        if attempt == max_retries:
            errors = "\n".join(f"  [{f.layer}] {f.error}: {f.detail}" for f in failures)
            raise ValueError(f"Validation pipeline failed:\n{errors}")

        error_summary = "\n".join(
            f"- [{f.layer}] {f.error}" + (f": {f.detail}" if f.detail else "")
            for f in failures
        )
        history.append({"role": "assistant", "content": text})
        history.append({
            "role": "user",
            "content": (
                f"Your response failed these validation checks:\n{error_summary}\n\n"
                f"Please rewrite your response correcting all issues."
            ),
        })

    raise RuntimeError("Unreachable")


async def demo():
    messages = [{"role": "user", "content": "Explain connection pooling in 3 paragraphs."}]
    result = await pipeline_validated_create(messages)
    print(f"Validated response ({len(result)} chars):\n{result[:120]}")


asyncio.run(demo())
```

---

## Comparison

| Approach | Validation type | Retry feedback quality | Model escalation | Complexity |
|---|---|---|---|---|
| JSON extract-and-repair | Syntax only | Error message + bad output | No | Very low |
| Pydantic field-level | Schema + types | Structured field errors | No | Low |
| Content rules + escalation | Prose quality | Rule list | Yes — model ladder | Medium |
| Tool-use enforcement | Schema (SDK-level) | N/A — model must use tool | No | Low |
| Judge model scoring | Semantic quality | Score + qualitative reason | No | Medium |
| Multi-layer pipeline | Format + schema + safety | Layer-specific error | No | Medium-high |

**Rule of thumb:**
- Structured JSON output → tool-use enforcement (Solution 4) first; JSON extract-and-repair (Solution 1) as fallback
- Pydantic models already in codebase → Solution 2 for free field-level feedback
- Prose quality matters → judge model (Solution 5) catches semantic failures that schema validators miss
- Safety-critical output → pipeline validator (Solution 6) with a safety layer always last
