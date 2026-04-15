---
layout: solution
title: "Agent uses large model for output validation"
category: token-cost
description: "Agent uses Claude Sonnet or Opus to validate, check, or score its own outputs — a task that Haiku handles equally well at 15× lower cost. Validation tasks (format checks, length checks, tone checks, JSON validity) are classification problems that don't need the full capability of a large model."
tags: [token-cost, validation, model-routing, haiku, classification, output-quality]
---

## Symptom

After generating a 500-token response, the agent calls Sonnet again with the response as input to check "is this response complete and well-formatted?" The validation call costs the same as the generation call — doubling the per-turn cost. For 1000 requests/day, this wastes ~$30/day on validation that Haiku could perform at $2/day.

## Root Cause

The agent's validation step was added after the generation step without reconsidering the model choice. The same `client` and `model` used for generation is reused for validation. Validation is fundamentally a simpler task — it reads existing text and produces a binary or categorical judgment — not a generation task. The model capability required is much lower.

## Fix

Route validation, checking, and scoring tasks to `claude-haiku-4-5-20251001`. Reserve `claude-sonnet-4-6` for tasks that require generation, reasoning, or nuanced judgment. Use deterministic validators (regex, JSON parsing, length check) for simple format checks — zero model cost.

---

### Option 1 — Route validation to Haiku instead of Sonnet

```python
import anthropic
import json

client = anthropic.Anthropic(api_key="sk-live-...")

# Dedicated validation client pointing to Haiku
def validate_response(response_text: str, validation_criteria: str) -> dict:
    """
    Use Haiku for validation — same accuracy for classification tasks at 15× lower cost.
    Sonnet: ~$3/M input tokens. Haiku: ~$0.25/M input tokens.
    """
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",   # ← Haiku, not Sonnet
        max_tokens=128,
        system=(
            "You are a response validator. Check if the response meets the criteria. "
            "Reply with JSON: {\"valid\": true/false, \"issues\": [list of issues if any], "
            "\"score\": 1-5}"
        ),
        messages=[{
            "role": "user",
            "content": f"Criteria: {validation_criteria}\n\nResponse to validate:\n{response_text}",
        }],
    )
    try:
        return json.loads(response.content[0].text)
    except json.JSONDecodeError:
        return {"valid": True, "issues": [], "score": 3}


def generate_and_validate(user_message: str) -> str:
    """Generate with Sonnet, validate with Haiku."""
    # Step 1: Generate (Sonnet — needs full capability)
    gen_response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": user_message}],
    )
    generated_text = gen_response.content[0].text

    # Step 2: Validate (Haiku — classification task only)
    validation = validate_response(
        generated_text,
        "Response must be complete, answer the question directly, and be under 500 words",
    )

    if not validation["valid"]:
        print(f"[Validation] Failed: {validation['issues']}")
        # Re-generate with issues as context
        fix_prompt = (
            f"Original question: {user_message}\n\n"
            f"Previous response had issues: {', '.join(validation['issues'])}\n"
            f"Please improve your response."
        )
        gen_response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[{"role": "user", "content": fix_prompt}],
        )
        generated_text = gen_response.content[0].text

    print(f"[Validation] Score: {validation['score']}/5, Valid: {validation['valid']}")
    return generated_text


result = generate_and_validate("Explain async programming in Python")
print(result[:200])

# Cost comparison for 1000 requests/day:
# BEFORE: 1000 × Sonnet validation = $3.00/day (at $3/M input tokens, ~1k tokens/call)
# AFTER:  1000 × Haiku validation  = $0.25/day (at $0.25/M input tokens, ~1k tokens/call)
# Savings: $2.75/day, $1,004/year
```

**Expected Token Savings:** Zero token volume change — same prompts, same task; Haiku at $0.25/M vs Sonnet at $3/M = 12× cost reduction for the validation step; for 1000 validations/day at 1k tokens each, saves $2.75/day.
**Environment:** Any agent with a separate validation/checking step; the single model change from `claude-sonnet-4-6` to `claude-haiku-4-5-20251001` in the validation call captures the full savings.

---

### Option 2 — Deterministic validation for format checks (zero model cost)

```python
import anthropic
import json
import re

client = anthropic.Anthropic(api_key="sk-live-...")


class ResponseValidator:
    """
    Hierarchical validation: deterministic first (free), LLM only as fallback.
    Most format checks don't need an LLM at all.
    """

    @staticmethod
    def is_valid_json(text: str) -> tuple[bool, str]:
        try:
            json.loads(text)
            return True, ""
        except json.JSONDecodeError as e:
            return False, f"JSON parse error: {e}"

    @staticmethod
    def is_within_length(text: str, max_words: int = 500) -> tuple[bool, str]:
        word_count = len(text.split())
        if word_count > max_words:
            return False, f"Too long: {word_count} words (max {max_words})"
        return True, ""

    @staticmethod
    def contains_required_sections(text: str, sections: list[str]) -> tuple[bool, str]:
        missing = [s for s in sections if s.lower() not in text.lower()]
        if missing:
            return False, f"Missing sections: {missing}"
        return True, ""

    @staticmethod
    def no_forbidden_phrases(text: str, forbidden: list[str]) -> tuple[bool, str]:
        found = [p for p in forbidden if p.lower() in text.lower()]
        if found:
            return False, f"Contains forbidden phrases: {found}"
        return True, ""

    @staticmethod
    def matches_pattern(text: str, pattern: str) -> tuple[bool, str]:
        if re.search(pattern, text, re.IGNORECASE):
            return True, ""
        return False, f"Does not match required pattern: {pattern}"

    @staticmethod
    def llm_semantic_check(text: str, criterion: str) -> tuple[bool, str]:
        """Only called when deterministic checks can't answer the question."""
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",   # Haiku for classification
            max_tokens=64,
            system="Answer only YES or NO. Does the text meet the criterion?",
            messages=[{
                "role": "user",
                "content": f"Criterion: {criterion}\n\nText: {text[:1000]}",
            }],
        )
        passed = "yes" in response.content[0].text.lower()
        return passed, "" if passed else f"Failed semantic check: {criterion}"


def validate_agent_output(
    output: str,
    expected_format: str = "json",
    max_words: int = 300,
    required_sections: list[str] | None = None,
    semantic_criteria: list[str] | None = None,
) -> tuple[bool, list[str]]:
    """
    Validate output using cheapest applicable method.
    Returns (is_valid, list_of_issues).
    """
    validator = ResponseValidator()
    issues = []
    llm_calls = 0

    # Deterministic checks (free)
    if expected_format == "json":
        ok, err = validator.is_valid_json(output)
        if not ok:
            issues.append(err)

    ok, err = validator.is_within_length(output, max_words)
    if not ok:
        issues.append(err)

    if required_sections:
        ok, err = validator.contains_required_sections(output, required_sections)
        if not ok:
            issues.append(err)

    # LLM semantic checks — only if deterministic checks passed and criteria specified
    if not issues and semantic_criteria:
        for criterion in semantic_criteria:
            ok, err = validator.llm_semantic_check(output, criterion)
            llm_calls += 1
            if not ok:
                issues.append(err)

    print(f"[Validator] {len(issues)} issue(s), LLM calls: {llm_calls}")
    return len(issues) == 0, issues


# Generate with Sonnet
response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=512,
    messages=[{"role": "user", "content": "Return a JSON object with name and age fields"}],
)
output = response.content[0].text

# Validate — deterministic first, LLM only if needed
valid, issues = validate_agent_output(
    output,
    expected_format="json",
    max_words=100,
    semantic_criteria=["The response contains both name and age fields with appropriate values"],
)
print(f"Valid: {valid}, Issues: {issues}")
```

**Expected Token Savings:** JSON validation, length check, and pattern matching cost zero tokens; semantic LLM check only fires when deterministic checks can't answer — for typical format-validation workloads, 70–90% of checks are handled deterministically, reducing validation LLM calls by 70–90%.
**Environment:** Agents with structured output requirements; the validation hierarchy maximizes deterministic coverage before falling back to any LLM call.

---

### Option 3 — Validation routing based on check type

```python
import anthropic
import json
import re

client = anthropic.Anthropic(api_key="sk-live-...")

# Classify validation checks by required model capability
DETERMINISTIC_CHECKS = {
    "json_valid", "length_under", "length_over", "contains_text",
    "not_empty", "starts_with", "ends_with", "matches_regex",
}
HAIKU_CHECKS = {
    "tone_appropriate", "complete_answer", "on_topic",
    "no_hallucination_obvious", "format_matches", "readable",
}
SONNET_CHECKS = {
    "factually_correct", "reasoning_sound", "no_subtle_errors",
    "expert_level_quality",
}


def route_validation(check_type: str, output: str, parameter: str = "") -> tuple[bool, str]:
    """Route each validation check to the cheapest capable method."""

    if check_type in DETERMINISTIC_CHECKS:
        # Free — no LLM needed
        if check_type == "json_valid":
            try:
                json.loads(output)
                return True, ""
            except json.JSONDecodeError as e:
                return False, str(e)
        elif check_type == "length_under":
            n = int(parameter)
            return len(output.split()) <= n, f"Too long: {len(output.split())} > {n} words"
        elif check_type == "not_empty":
            return bool(output.strip()), "Response is empty"
        elif check_type == "contains_text":
            found = parameter.lower() in output.lower()
            return found, f"Missing required text: '{parameter}'"
        elif check_type == "matches_regex":
            match = bool(re.search(parameter, output))
            return match, f"Does not match pattern: {parameter}"
        return True, ""   # unknown deterministic check — pass

    elif check_type in HAIKU_CHECKS:
        # Haiku — cheap LLM classification
        model = "claude-haiku-4-5-20251001"
        prompt = f"Check: Is the following text '{check_type}'? {f'(Context: {parameter})' if parameter else ''}\n\nText: {output[:1500]}\n\nAnswer YES or NO."

    elif check_type in SONNET_CHECKS:
        # Sonnet — expensive but necessary for high-stakes checks
        model = "claude-sonnet-4-6"
        prompt = f"Carefully evaluate: {check_type}. {f'Criteria: {parameter}' if parameter else ''}\n\nText: {output[:3000]}\n\nAnswer YES or NO."

    else:
        return True, ""   # unknown check — pass

    response = client.messages.create(
        model=model,
        max_tokens=64,
        messages=[{"role": "user", "content": prompt}],
    )
    result_text = response.content[0].text.lower().strip()
    passed = "yes" in result_text
    return passed, "" if passed else f"Failed {check_type}"


def multi_check_validation(output: str, checks: list[tuple[str, str]]) -> dict:
    """Run multiple validation checks with optimal routing."""
    results = {"passed": [], "failed": [], "total_llm_calls": 0}
    model_calls = {"haiku": 0, "sonnet": 0, "deterministic": 0}

    for check_type, parameter in checks:
        passed, error = route_validation(check_type, output, parameter)

        if check_type in HAIKU_CHECKS:
            model_calls["haiku"] += 1
            results["total_llm_calls"] += 1
        elif check_type in SONNET_CHECKS:
            model_calls["sonnet"] += 1
            results["total_llm_calls"] += 1
        else:
            model_calls["deterministic"] += 1

        if passed:
            results["passed"].append(check_type)
        else:
            results["failed"].append(f"{check_type}: {error}")

    print(
        f"[Routing] deterministic={model_calls['deterministic']}, "
        f"haiku={model_calls['haiku']}, sonnet={model_calls['sonnet']}"
    )
    return results


# Example validation suite
response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=512,
    messages=[{"role": "user", "content": "Explain recursion briefly"}],
)
output_text = response.content[0].text

results = multi_check_validation(output_text, [
    ("not_empty", ""),
    ("length_under", "200"),
    ("complete_answer", ""),
    ("on_topic", "recursion in programming"),
    ("readable", ""),
])
print(f"Passed: {results['passed']}, Failed: {results['failed']}")
```

**Expected Token Savings:** Routing sends ~60% of checks to deterministic (free), ~35% to Haiku ($0.25/M), ~5% to Sonnet ($3/M); vs sending all to Sonnet, the routing approach reduces validation cost by ~85%.
**Environment:** Agents with diverse validation requirements; the routing table makes the cost/quality trade-off explicit and auditable.

---

### Option 4 — Batch validation: one Haiku call for multiple criteria

```python
import anthropic
import json

client = anthropic.Anthropic(api_key="sk-live-...")


def batch_validate(output: str, criteria: list[str]) -> dict[str, bool]:
    """
    Check multiple criteria in a single Haiku call.
    Cheaper than one call per criterion.
    """
    criteria_list = "\n".join(f"{i+1}. {c}" for i, c in enumerate(criteria))

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        system=(
            "Check each criterion against the text. "
            "Reply with a JSON object where keys are criterion numbers (as strings) "
            "and values are true (passes) or false (fails)."
        ),
        messages=[{
            "role": "user",
            "content": f"Text:\n{output[:2000]}\n\nCriteria:\n{criteria_list}",
        }],
    )

    try:
        scores = json.loads(response.content[0].text.strip())
        return {criteria[int(k)-1]: v for k, v in scores.items() if k.isdigit() and int(k)-1 < len(criteria)}
    except (json.JSONDecodeError, KeyError, ValueError):
        return {c: True for c in criteria}   # pass on parse failure


def generate_and_batch_validate(user_message: str) -> str:
    # Generate with Sonnet
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": user_message}],
    )
    output = response.content[0].text

    # Validate ALL criteria in ONE Haiku call
    criteria = [
        "The response directly answers the question",
        "The response is appropriately concise (not excessively long)",
        "The response uses clear, accessible language",
        "The response includes concrete examples if applicable",
        "The response does not make unsupported claims",
    ]

    validation_results = batch_validate(output, criteria)
    failed = [c for c, passed in validation_results.items() if not passed]

    if failed:
        print(f"[BatchValidate] Failed {len(failed)}/{len(criteria)} criteria")
        # One Haiku call to fix all issues simultaneously
        fix_prompt = (
            f"Improve this response to fix these issues: {'; '.join(failed)}\n\n"
            f"Original question: {user_message}\n\n"
            f"Original response: {output}"
        )
        fix_response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[{"role": "user", "content": fix_prompt}],
        )
        return fix_response.content[0].text

    print(f"[BatchValidate] All {len(criteria)} criteria passed")
    return output


result = generate_and_batch_validate("Explain the CAP theorem")
print(result[:300])
```

**Expected Token Savings:** Batching 5 criteria into one Haiku call costs ~400 tokens vs 5 separate Haiku calls at ~5 × 300 = 1500 tokens — 73% reduction for multi-criterion validation; and Haiku vs Sonnet pricing gives an additional 12× cost reduction.
**Environment:** Agents with multi-criteria quality gates; batch validation is the most efficient approach when 3+ criteria must be checked simultaneously.

---

### Option 5 — Self-evaluation with structured scoring rubric

```python
import anthropic
import json

client = anthropic.Anthropic(api_key="sk-live-...")

RUBRIC_SYSTEM = """\
You are a response quality scorer. Given a question and a response, score the response
on each dimension. Reply with ONLY a JSON object, no other text.

Scoring dimensions (1-5 each):
- relevance: Does it directly answer the question?
- accuracy: Are the facts correct?
- completeness: Does it cover the key points?
- clarity: Is it easy to understand?
- conciseness: Is it appropriately brief?
"""


def score_response(question: str, response_text: str) -> dict:
    """Score a response on multiple dimensions using Haiku."""
    result = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        system=RUBRIC_SYSTEM,
        messages=[{
            "role": "user",
            "content": f"Question: {question}\n\nResponse: {response_text[:1500]}",
        }],
    )
    try:
        scores = json.loads(result.content[0].text.strip())
        overall = sum(scores.values()) / len(scores) if scores else 3.0
        scores["overall"] = round(overall, 1)
        return scores
    except json.JSONDecodeError:
        return {"overall": 3.0}


def generate_with_quality_gate(user_message: str, min_score: float = 3.5) -> str:
    """Generate and retry once if quality is below threshold."""
    for attempt in range(2):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[{"role": "user", "content": user_message}],
        )
        output = response.content[0].text
        scores = score_response(user_message, output)

        print(
            f"[Attempt {attempt+1}] Score: {scores.get('overall', '?')}/5 "
            f"(relevance={scores.get('relevance','?')}, "
            f"completeness={scores.get('completeness','?')})"
        )

        if scores.get("overall", 0) >= min_score:
            return output

        # Below threshold — add guidance for the retry
        user_message = (
            f"{user_message}\n\n"
            f"Please provide a more thorough answer (previous attempt scored "
            f"{scores.get('overall', 0):.1f}/5 — low on "
            f"{', '.join(k for k, v in scores.items() if k != 'overall' and isinstance(v, (int, float)) and v < 3.5)})."
        )

    return output   # return best attempt


result = generate_with_quality_gate("What is the difference between concurrency and parallelism?")
print(result[:400])
```

**Expected Token Savings:** Structured rubric scoring with Haiku costs ~200 tokens; provides actionable failure details (which specific dimension failed) that let the retry be targeted — reducing the probability of a second retry needed and saving ~1500 tokens of unnecessary Sonnet calls.
**Environment:** Quality-critical agents where low-quality outputs have downstream consequences; the rubric provides debugging visibility into exactly why an output failed.

---

### Option 6 — Cost accounting: measure validation spend vs generation spend

```python
import anthropic
from dataclasses import dataclass, field

client = anthropic.Anthropic(api_key="sk-live-...")

# Pricing per 1M tokens (approximate)
PRICES = {
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "claude-haiku-4-5-20251001": {"input": 0.25, "output": 1.25},
    "claude-opus-4-6": {"input": 15.00, "output": 75.00},
}


@dataclass
class CostTracker:
    calls: dict[str, int] = field(default_factory=dict)
    input_tokens: dict[str, int] = field(default_factory=dict)
    output_tokens: dict[str, int] = field(default_factory=dict)

    def record(self, model: str, usage):
        self.calls[model] = self.calls.get(model, 0) + 1
        self.input_tokens[model] = self.input_tokens.get(model, 0) + usage.input_tokens
        self.output_tokens[model] = self.output_tokens.get(model, 0) + usage.output_tokens

    def cost(self, model: str) -> float:
        prices = PRICES.get(model, {"input": 3.0, "output": 15.0})
        return (
            self.input_tokens.get(model, 0) * prices["input"] / 1_000_000
            + self.output_tokens.get(model, 0) * prices["output"] / 1_000_000
        )

    def report(self) -> str:
        lines = ["=== Cost Report ==="]
        total = 0.0
        for model in self.calls:
            c = self.cost(model)
            total += c
            lines.append(
                f"  {model}: {self.calls[model]} calls, "
                f"{self.input_tokens.get(model,0):,}+{self.output_tokens.get(model,0):,} tokens, "
                f"${c:.4f}"
            )
        lines.append(f"  Total: ${total:.4f}")
        return "\n".join(lines)

    def validation_fraction(self) -> float:
        """What fraction of total cost is validation?"""
        gen_cost = sum(
            self.cost(m) for m in ["claude-sonnet-4-6", "claude-opus-4-6"]
            if m in self.calls
        )
        haiku_cost = self.cost("claude-haiku-4-5-20251001")
        total = gen_cost + haiku_cost
        return haiku_cost / total if total > 0 else 0.0


tracker = CostTracker()


def tracked_generate(prompt: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    tracker.record("claude-sonnet-4-6", response.usage)
    return response.content[0].text


def tracked_validate(output: str, criterion: str) -> bool:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001", max_tokens=32,
        messages=[{"role": "user", "content": f"Does this text '{criterion}'? YES or NO.\n\n{output[:1000]}"}],
    )
    tracker.record("claude-haiku-4-5-20251001", response.usage)
    return "yes" in response.content[0].text.lower()


# Comparison table
# | Option | Validation Method | Cost/1k validations | Accuracy |
# |--------|-----------------|--------------------|---------:|
# | 1 Haiku routing | Model switch | ~$0.25 | High |
# | 2 Deterministic | Regex/JSON/length | $0.00 | Exact |
# | 3 Type routing | Check-type dispatch | ~$0.10 | High |
# | 4 Batch criteria | One multi-check call | ~$0.05 | High |
# | 5 Rubric scoring | Structured 1-5 | ~$0.25 | High |
# | 6 Cost accounting | Measurement | N/A | N/A |

# Run a few requests and check the cost split
for i in range(3):
    output = tracked_generate(f"Explain concept {i}")
    tracked_validate(output, "is clear and informative")

print(tracker.report())
print(f"\nValidation fraction of total cost: {tracker.validation_fraction():.0%}")
```

**Expected Token Savings:** The cost tracker quantifies how much validation is costing relative to generation — if validation is 40% of total cost but Haiku handles it equally well, switching saves 40% × (1 - 1/12) = ~37% of total spend.
**Environment:** Production agents where cost visibility drives optimization decisions; measuring before optimizing prevents premature optimization of the wrong thing.
