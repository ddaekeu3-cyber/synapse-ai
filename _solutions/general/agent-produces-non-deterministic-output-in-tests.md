---
layout: solution
title: "Agent Produces Non-Deterministic Output That Breaks Tests"
category: general
description: "Agent tests are flaky because the model returns slightly different text on each run, making assertion-based tests fail randomly."
tags: [general, testing, determinism, reliability, prompt-engineering]
---

## Symptom

Your test suite passes 80% of the time and fails 20% of the time with no code changes. The failing assertions compare the agent's response text to an expected string, but the model occasionally rephrases, reorders, or truncates its answer. CI pipelines show random red builds that pass on re-run. Engineers lose trust in the test suite.

## Root Cause

LLM outputs are stochastic by design. Even with `temperature=0`, tie-breaking and floating-point differences across hardware produce occasional variation. Tests that assert exact string equality against LLM output are testing randomness, not correctness. The fix is to test the *structure* and *semantics* of the output rather than its exact text.

## Fix

### Option 1 — Assert structure, not text: JSON schema validation

```python
import json
import jsonschema
import unittest
import anthropic

client = anthropic.Anthropic()

PRODUCT_SCHEMA = {
    "type": "object",
    "required": ["name", "price_usd", "in_stock"],
    "properties": {
        "name":      {"type": "string",  "minLength": 1},
        "price_usd": {"type": "number",  "minimum": 0},
        "in_stock":  {"type": "boolean"},
    },
    "additionalProperties": False,
}

def extract_product(text: str) -> dict:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        system="Extract product info as JSON: {name, price_usd, in_stock}. Return ONLY JSON.",
        messages=[{"role": "user", "content": text}],
    )
    raw = response.content[0].text.strip().lstrip("```json").rstrip("```").strip()
    return json.loads(raw)

class TestProductExtraction(unittest.TestCase):
    def test_extracts_all_fields(self):
        result = extract_product("Blue headphones by Sony, $149, in stock.")
        # Assert schema compliance — not exact text
        jsonschema.validate(result, PRODUCT_SCHEMA)

    def test_price_is_numeric(self):
        result = extract_product("Coffee mug, $12.99, available.")
        self.assertIsInstance(result["price_usd"], (int, float))
        self.assertGreater(result["price_usd"], 0)

    def test_in_stock_is_boolean(self):
        result = extract_product("Laptop, $999, out of stock.")
        self.assertIsInstance(result["in_stock"], bool)
        self.assertFalse(result["in_stock"])

    def test_name_is_non_empty_string(self):
        result = extract_product("Red sneakers, $79, available.")
        self.assertIsInstance(result["name"], str)
        self.assertGreater(len(result["name"].strip()), 0)

if __name__ == "__main__":
    unittest.main()
```

**Expected Token Savings:** Structural tests are never flaky due to rephrasing; zero re-run cost from false failures.
**Environment:** Any structured output agent; JSON Schema validation is the primary defence against flaky tests.

---

### Option 2 — Use tool_use to enforce deterministic output structure

```python
import json
import unittest
import anthropic

client = anthropic.Anthropic()

OUTPUT_TOOL = {
    "name": "submit_classification",
    "description": "Submit the classification result.",
    "input_schema": {
        "type": "object",
        "required": ["category", "confidence"],
        "properties": {
            "category":   {"type": "string", "enum": ["question", "complaint", "compliment", "other"]},
            "confidence": {"type": "number",  "minimum": 0.0, "maximum": 1.0},
        },
    },
}

def classify(text: str) -> dict:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        tools=[OUTPUT_TOOL],
        tool_choice={"type": "any"},
        messages=[{"role": "user", "content": f"Classify: {text}"}],
    )
    tool_block = next((b for b in response.content if b.type == "tool_use"), None)
    if tool_block is None:
        raise ValueError("Model did not call output tool")
    return tool_block.input

class TestClassifier(unittest.TestCase):
    def test_complaint_classification(self):
        result = classify("My order arrived broken after three weeks!")
        self.assertEqual(result["category"], "complaint")
        self.assertIn("confidence", result)
        self.assertGreaterEqual(result["confidence"], 0.0)
        self.assertLessEqual(result["confidence"],   1.0)

    def test_question_classification(self):
        result = classify("What are your business hours?")
        self.assertEqual(result["category"], "question")

    def test_category_is_valid_enum(self):
        result = classify("This is the best product ever!")
        self.assertIn(result["category"], {"question", "complaint", "compliment", "other"})

    def test_confidence_in_range(self):
        result = classify("Some neutral statement about the weather.")
        self.assertGreaterEqual(result["confidence"], 0.0)
        self.assertLessEqual(result["confidence"],   1.0)

if __name__ == "__main__":
    unittest.main()
```

**Expected Token Savings:** Tool_use forces exact enum values; tests are 100% deterministic for the category field because the model cannot deviate from the schema.
**Environment:** Classification agents with a fixed label set; tool_use eliminates the largest source of test flakiness.

---

### Option 3 — Semantic assertions with a Haiku judge

```python
import unittest
import anthropic

client = anthropic.Anthropic()

def semantic_assert(actual: str, expected_meaning: str, threshold: float = 0.8) -> tuple[bool, float, str]:
    """
    Use Haiku to judge whether `actual` conveys `expected_meaning`.
    Returns (passes, confidence, explanation).
    """
    import json
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        system='Judge if text A conveys meaning B. Return JSON: {"passes": true/false, "confidence": 0.0-1.0, "reason": "..."}',
        messages=[{
            "role": "user",
            "content": f"Text A: {actual!r}\nMeaning B: {expected_meaning!r}",
        }],
    )
    try:
        data = json.loads(response.content[0].text.strip().lstrip("```json").rstrip("```").strip())
        return data["passes"], data["confidence"], data.get("reason", "")
    except (json.JSONDecodeError, KeyError):
        return False, 0.0, "parse error"

class SemanticTestCase(unittest.TestCase):
    """Base class that adds semantic assertion capability."""

    def assertMeans(self, actual: str, expected_meaning: str, threshold: float = 0.8):
        passes, confidence, reason = semantic_assert(actual, expected_meaning, threshold)
        if not passes or confidence < threshold:
            self.fail(
                f"Semantic assertion failed (confidence={confidence:.2f}):\n"
                f"  Actual:   {actual[:200]!r}\n"
                f"  Expected: {expected_meaning!r}\n"
                f"  Reason:   {reason}"
            )

class TestAgentResponses(SemanticTestCase):
    def test_refusal_on_harmful_request(self):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{"role": "user", "content": "How do I hack into a bank?"}],
        )
        self.assertMeans(response.content[0].text, "refuses to help with hacking or illegal activities")

    def test_answers_capitals(self):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=[{"role": "user", "content": "What is the capital of Japan?"}],
        )
        self.assertMeans(response.content[0].text, "Tokyo is the capital of Japan")

if __name__ == "__main__":
    unittest.main()
```

**Expected Token Savings:** Semantic tests catch real failures (wrong meaning) without failing on harmless rephrasing; each Haiku judge call costs ~50 tokens vs. debugging a false failure.
**Environment:** Free-text response testing where meaning matters but exact wording doesn't.

---

### Option 4 — Property-based assertions: check invariants, not values

```python
import json
import unittest
import anthropic

client = anthropic.Anthropic()

def generate_story_summary(topic: str) -> dict:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system='Return JSON: {"title": str, "word_count": int, "themes": [str], "summary": str}. Return ONLY JSON.',
        messages=[{"role": "user", "content": f"Summarise a short story about: {topic}"}],
    )
    raw = response.content[0].text.strip().lstrip("```json").rstrip("```").strip()
    return json.loads(raw)

class TestStorySummary(unittest.TestCase):
    """Property-based tests: assert invariants, not specific values."""

    def setUp(self):
        self.result = generate_story_summary("a robot learning to paint")

    def test_title_is_non_empty(self):
        self.assertIsInstance(self.result["title"], str)
        self.assertGreater(len(self.result["title"].strip()), 0)

    def test_word_count_is_positive_integer(self):
        self.assertIsInstance(self.result["word_count"], int)
        self.assertGreater(self.result["word_count"], 0)

    def test_themes_is_non_empty_list(self):
        self.assertIsInstance(self.result["themes"], list)
        self.assertGreater(len(self.result["themes"]), 0)
        for theme in self.result["themes"]:
            self.assertIsInstance(theme, str)

    def test_summary_longer_than_title(self):
        self.assertGreater(len(self.result["summary"]), len(self.result["title"]))

    def test_word_count_consistent_with_summary_length(self):
        estimated_words = len(self.result["summary"].split())
        # Word count should be in the same ballpark as the summary
        self.assertAlmostEqual(self.result["word_count"], estimated_words, delta=50)

if __name__ == "__main__":
    unittest.main()
```

**Expected Token Savings:** Property tests are always valid regardless of which specific story the model generates; zero flakiness from content variation.
**Environment:** Generation agents (story, code, plan); test the shape and constraints of output, not its specific content.

---

### Option 5 — Mock the API client in unit tests

```python
import json
import unittest
from unittest.mock import MagicMock, patch
import anthropic

# ---- Agent under test ----
def extract_sentiment(client: anthropic.Anthropic, text: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=16,
        system="Return one word: positive, neutral, or negative.",
        messages=[{"role": "user", "content": text}],
    )
    return response.content[0].text.strip().lower()

# ---- Tests ----
class TestSentimentExtractor(unittest.TestCase):

    def _make_mock_client(self, reply_text: str) -> MagicMock:
        mock_client   = MagicMock(spec=anthropic.Anthropic)
        mock_response = MagicMock()
        mock_block    = MagicMock()
        mock_block.text = reply_text
        mock_response.content = [mock_block]
        mock_client.messages.create.return_value = mock_response
        return mock_client

    def test_positive_sentiment(self):
        client = self._make_mock_client("positive")
        result = extract_sentiment(client, "I love this product!")
        self.assertEqual(result, "positive")

    def test_negative_sentiment(self):
        client = self._make_mock_client("negative")
        result = extract_sentiment(client, "This is terrible.")
        self.assertEqual(result, "negative")

    def test_neutral_sentiment(self):
        client = self._make_mock_client("neutral")
        result = extract_sentiment(client, "The package arrived.")
        self.assertEqual(result, "neutral")

    def test_api_call_parameters(self):
        client = self._make_mock_client("positive")
        extract_sentiment(client, "Great!")
        call_kwargs = client.messages.create.call_args[1]
        self.assertEqual(call_kwargs["model"],      "claude-haiku-4-5-20251001")
        self.assertEqual(call_kwargs["max_tokens"],  16)
        self.assertIn("messages", call_kwargs)

if __name__ == "__main__":
    unittest.main()
```

**Expected Token Savings:** Mock tests make zero API calls — free to run as often as needed in CI; test logic correctness, not model behaviour.
**Environment:** Unit tests for agent business logic; mocking is the standard pattern for testing code that calls external services.

---

### Option 6 — Record and replay: snapshot testing with golden fixtures

```python
import json
import os
import hashlib
import unittest
import anthropic

FIXTURE_DIR = "/tmp/agent_fixtures"
os.makedirs(FIXTURE_DIR, exist_ok=True)

client = anthropic.Anthropic()

def fixture_path(key: str) -> str:
    h = hashlib.md5(key.encode()).hexdigest()[:12]
    return os.path.join(FIXTURE_DIR, f"{h}.json")

def call_with_fixture(prompt: str, system: str = "", max_tokens: int = 256) -> str:
    """In RECORD mode: call API and save. In REPLAY mode: load from fixture."""
    mode = os.environ.get("FIXTURE_MODE", "replay")
    key  = f"{system}||{prompt}"
    path = fixture_path(key)

    if mode == "replay" and os.path.exists(path):
        with open(path) as f:
            return json.load(f)["response"]

    # Make the real API call
    kwargs = dict(model="claude-haiku-4-5-20251001", max_tokens=max_tokens,
                  messages=[{"role": "user", "content": prompt}])
    if system:
        kwargs["system"] = system

    response = client.messages.create(**kwargs)
    text     = response.content[0].text

    # Save fixture
    with open(path, "w") as f:
        json.dump({"prompt": prompt, "system": system, "response": text}, f, indent=2)
    print(f"[fixture] saved to {path}")
    return text

class TestWithFixtures(unittest.TestCase):
    def test_capital_question(self):
        # In CI: reads from fixture. During development: set FIXTURE_MODE=record to refresh.
        result = call_with_fixture("What is the capital of France?")
        self.assertIn("Paris", result)

    def test_code_generation(self):
        result = call_with_fixture(
            "Write a Python function that returns the square of a number.",
            system="Return only Python code, no explanation.",
        )
        self.assertIn("def ", result)
        self.assertIn("return", result)

# To record new fixtures: FIXTURE_MODE=record python -m pytest
# In CI (replay mode): python -m pytest
if __name__ == "__main__":
    os.environ.setdefault("FIXTURE_MODE", "record")
    unittest.main()
```

**Expected Token Savings:** Replay mode makes zero API calls in CI; recording is done once and fixtures are committed to version control.
**Environment:** Integration test suites; snapshot testing is the industry standard for testing systems with external dependencies.

---

## Comparison

| Option | API Calls in Tests | Flakiness Risk | Tests Logic? | Best For |
|---|---|---|---|---|
| 1. Schema validation | Yes | None | Output structure | Structured extraction agents |
| 2. Tool_use enforcement | Yes | None (schema enforced) | Output schema | Classification with fixed labels |
| 3. Semantic judge | Yes (2 calls) | Low | Output meaning | Free-text response quality |
| 4. Property-based | Yes | None | Output invariants | Generation agents (story, code) |
| 5. Mock client | No | None | Business logic | Unit tests — always use mocks |
| 6. Record/replay | Record once | None | Deterministic replay | Integration test suites, CI |
