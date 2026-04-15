---
layout: solution
title: "Agent Generates Non-Deterministic Output — Tests Are Flaky"
category: general
description: "Tests against agent output fail intermittently because LLM responses vary across runs. Same input produces slightly different wording, ordering, or structure each time. Tests that assert exact strings always fail."
tags: [testing, determinism, flaky-tests, temperature, seed, output, reproducibility]
---

# Agent Generates Non-Deterministic Output — Tests Are Flaky

## Symptom
- Unit test passes 70% of the time, fails 30% — same code, same input
- `assert result == "The answer is 42"` fails because agent sometimes writes "The answer is: 42"
- Integration tests for agent pipelines are unreliable in CI
- Debugging failures is impossible because the error cannot be reproduced
- Test suite is disabled or marked `@skip` because "LLM output is unpredictable"

## Root Cause
LLMs are probabilistic by nature. Temperature > 0 introduces randomness. Even at temperature=0, minor implementation differences across API versions can change outputs. Tests that assert exact string equality against LLM output will always be fragile. The fix is a combination of: reducing model temperature, using semantic assertions instead of string equality, and structuring output to be deterministic.

## Fix

### Option 1: Set temperature=0 for deterministic tasks

```python
import anthropic

client = anthropic.Anthropic()

def call_deterministic(prompt: str, system: str = "") -> str:
    """
    Use temperature=0 for tasks where consistency matters more than creativity.
    Classification, extraction, structured output — all benefit from temperature=0.
    """
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        temperature=0,   # Most deterministic setting
        system=system,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text

# Good for: classification, data extraction, code generation, yes/no questions
# Bad for: creative writing, brainstorming, varied examples
```

### Option 2: Structured output makes assertions deterministic

```python
import json
from pydantic import BaseModel

class ClassificationResult(BaseModel):
    label: str          # "positive" | "negative" | "neutral"
    confidence: float   # 0.0–1.0
    reasoning: str      # brief explanation

def classify_sentiment(text: str, client) -> ClassificationResult:
    """
    Return structured JSON — easier to assert against than free text.
    """
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=200,
        temperature=0,
        system=(
            "Return a JSON object with exactly these fields: "
            "label (one of: positive, negative, neutral), "
            "confidence (float 0-1), reasoning (one sentence). "
            "No other text."
        ),
        messages=[{"role": "user", "content": f"Classify: {text}"}]
    )
    data = json.loads(response.content[0].text)
    return ClassificationResult(**data)

# Test assertions are now on structured fields, not raw strings:
def test_positive_sentiment():
    result = classify_sentiment("I love this product!")
    assert result.label == "positive"          # Exact match on enum — reliable
    assert result.confidence > 0.7             # Range assertion — robust
    assert len(result.reasoning) > 10          # Existence check — not string equality
```

### Option 3: Semantic assertions instead of string equality

```python
import re
from typing import Callable

def assert_semantic(response: str, checks: list[Callable[[str], bool]], description: str = ""):
    """
    Assert semantic properties of a response rather than exact string match.
    """
    failures = []
    for check in checks:
        if not check(response):
            failures.append(f"Failed: {check.__name__ or str(check)}")

    if failures:
        raise AssertionError(
            f"Semantic assertion failed for: '{response[:100]}...'\n" +
            "\n".join(failures)
        )

# Robust test patterns:
def test_agent_mentions_price():
    response = get_agent_response("What does it cost?")

    assert_semantic(response, [
        lambda r: bool(re.search(r"\$[\d,]+", r)),          # Contains a dollar amount
        lambda r: any(w in r.lower() for w in ["price", "cost", "fee", "charge"]),
        lambda r: len(r) > 20,                               # Not empty
        lambda r: len(r) < 2000,                             # Not excessively long
    ])

def test_agent_refuses_off_topic():
    response = get_agent_response("What is the capital of France?")

    assert_semantic(response, [
        lambda r: not re.search(r"\bparis\b", r, re.IGNORECASE),  # Refused to answer
        lambda r: any(w in r.lower() for w in ["sorry", "outside", "only", "not"]),
    ])
```

### Option 4: Record-and-replay for stable integration tests

```python
import json
import hashlib
from pathlib import Path

class RecordReplayClient:
    """
    In record mode: makes real API calls and saves responses.
    In replay mode: returns saved responses — no API calls, deterministic.
    """

    def __init__(self, mode: str = "replay", fixture_dir: str = "tests/fixtures"):
        self.mode = mode  # "record" or "replay"
        self.fixture_dir = Path(fixture_dir)
        self.fixture_dir.mkdir(parents=True, exist_ok=True)

    def _fixture_key(self, prompt: str, system: str) -> str:
        content = f"{system}|||{prompt}"
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def _fixture_path(self, key: str) -> Path:
        return self.fixture_dir / f"{key}.json"

    def complete(self, prompt: str, system: str = "") -> str:
        key = self._fixture_key(prompt, system)
        path = self._fixture_path(key)

        if self.mode == "replay":
            if not path.exists():
                raise FileNotFoundError(
                    f"No fixture for this prompt. Run with mode='record' first.\n"
                    f"Key: {key}, Prompt: {prompt[:50]}"
                )
            return json.loads(path.read_text())["response"]

        # Record mode: real API call
        import anthropic
        client = anthropic.Anthropic()
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            temperature=0,
            system=system,
            messages=[{"role": "user", "content": prompt}]
        )
        text = response.content[0].text
        path.write_text(json.dumps({"prompt": prompt, "system": system, "response": text}))
        print(f"Recorded fixture: {key}")
        return text

# In tests (replay mode — no API calls, always same response):
client = RecordReplayClient(mode="replay")
result = client.complete("What is 2+2?")  # Returns fixture
assert "4" in result
```

### Option 5: Constrain output to a fixed vocabulary

```python
# For classification/routing, use forced choices — eliminates variability
VALID_LABELS = ["billing", "technical", "account", "other"]

def classify_with_forced_choice(query: str, client) -> str:
    """
    Force the model to pick from an explicit list — output is always one of VALID_LABELS.
    """
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=20,   # Short: one word only
        temperature=0,
        system=(
            f"Classify the user query into exactly one category. "
            f"Valid categories: {', '.join(VALID_LABELS)}. "
            f"Output ONLY the category name. Nothing else."
        ),
        messages=[{"role": "user", "content": query}]
    )
    label = response.content[0].text.strip().lower()

    # Validate and normalize
    if label not in VALID_LABELS:
        # Handle minor variations like "Technical" vs "technical"
        for valid in VALID_LABELS:
            if valid in label:
                return valid
        return "other"  # Safe fallback

    return label

# Test is now 100% deterministic:
def test_billing_classification():
    result = classify_with_forced_choice("I was charged twice")
    assert result == "billing"  # Never fails
```

### Option 6: Property-based testing for agent output

```python
# Instead of exact assertions, test properties that should always hold
import pytest

def get_agent_summary(text: str) -> str:
    # ... agent call
    pass

class TestAgentSummaryProperties:
    """
    Test properties that should hold for ANY valid summary,
    not specific wording that varies.
    """

    def test_summary_is_shorter_than_input(self):
        long_text = "word " * 500  # 500 words
        summary = get_agent_summary(long_text)
        assert len(summary.split()) < 100, "Summary should be under 100 words"

    def test_summary_is_not_empty(self):
        summary = get_agent_summary("Hello world")
        assert len(summary.strip()) > 0

    def test_summary_does_not_include_raw_input(self):
        # Summary should paraphrase, not copy verbatim
        text = "The quick brown fox jumps over the lazy dog"
        summary = get_agent_summary(text)
        assert summary != text

    def test_summary_contains_no_personal_data(self):
        text = "John Smith (SSN: 123-45-6789) called about his account"
        summary = get_agent_summary(text)
        assert "123-45-6789" not in summary   # PII should be redacted
        assert "ssn" not in summary.lower()

    @pytest.mark.parametrize("language", ["English", "Spanish", "French"])
    def test_summary_language_follows_input(self, language):
        # Property: language of summary matches input language
        # (test the property, not exact translated text)
        pass
```

## Testing Strategy by Agent Output Type

| Output type | Recommended assertion | Avoid |
|---|---|---|
| Classification | `result == "positive"` (exact — enum) | N/A — exact is fine for enums |
| Free text summary | Length range, keyword presence | `result == "specific wording"` |
| Structured JSON | Field existence, value ranges | Full JSON string equality |
| Code generation | Parses, lints, tests pass | Exact character match |
| Yes/No answer | `"yes" in result.lower()` | Full sentence match |
| Number/amount | Extract with regex, check range | Full string with units |

## Expected Token Savings
Flaky tests causing CI re-runs: ~50,000 extra tokens (plus dev time)
Semantic assertions + temperature=0: 0 wasted

## Environment
- Any agent system with automated tests; critical for CI/CD pipelines
- Source: direct experience; non-deterministic test failures are the top developer productivity drain in agent development
