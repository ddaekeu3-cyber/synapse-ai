---
layout: solution
title: "Agent Doesn't Mock the Anthropic API in Unit Tests"
category: testing
description: "Unit tests make real Anthropic API calls, making the test suite slow, expensive, flaky on network issues, and impossible to run offline or in CI without credentials."
tags: [testing, mocking, unit-tests, ci, reliability]
---

## Symptom

The test suite takes 30–120 seconds to run because every test makes a live API call. Tests fail intermittently due to rate limits, network timeouts, or API unavailability. Running tests costs real money. The CI pipeline requires production API keys. Developers can't run tests offline. A model update changes response format and breaks dozens of tests simultaneously.

## Root Cause

Without mocking, unit tests couple business logic verification to the availability and behavior of an external service. Unit tests should test *your code's logic* in isolation. The Anthropic API response format is a contract — verifying your code handles specific response shapes correctly requires deterministic, controllable inputs, not live inference.

## Fix

### Option 1 — unittest.mock.patch for synchronous client

```python
import anthropic
import unittest
from unittest.mock import MagicMock, patch

# Agent function under test
def summarise(text: str, client: anthropic.Anthropic) -> str:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": f"Summarise: {text}"}],
    )
    return resp.content[0].text

class TestSummarise(unittest.TestCase):

    @patch("anthropic.Anthropic")
    def test_returns_model_text(self, MockAnthropic):
        # Build a fake response that mirrors the real SDK structure
        fake_content = MagicMock()
        fake_content.text = "This is a mock summary."
        fake_response = MagicMock()
        fake_response.content = [fake_content]
        fake_response.stop_reason = "end_turn"

        mock_client = MockAnthropic.return_value
        mock_client.messages.create.return_value = fake_response

        result = summarise("Long document...", mock_client)
        self.assertEqual(result, "This is a mock summary.")

    @patch("anthropic.Anthropic")
    def test_passes_correct_model(self, MockAnthropic):
        fake_content = MagicMock()
        fake_content.text = "ok"
        fake_response = MagicMock()
        fake_response.content = [fake_content]
        mock_client = MockAnthropic.return_value
        mock_client.messages.create.return_value = fake_response

        summarise("test", mock_client)

        call_kwargs = mock_client.messages.create.call_args.kwargs
        self.assertEqual(call_kwargs["model"], "claude-haiku-4-5-20251001")
        self.assertLessEqual(call_kwargs["max_tokens"], 256)

if __name__ == "__main__":
    unittest.main()
```

**Expected Token Savings:** Zero live API calls in unit tests; test suite runs in milliseconds with no spend.
**Environment:** Any synchronous agent; standard Python unittest or pytest projects.

---

### Option 2 — pytest fixtures with a reusable mock client factory

```python
import pytest
import anthropic
from unittest.mock import MagicMock

# ── agent code ──────────────────────────────────────────────────────────────

def classify_sentiment(text: str, client: anthropic.Anthropic) -> str:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=8,
        messages=[{"role": "user", "content": f"Sentiment (positive/negative/neutral): {text}"}],
    )
    return resp.content[0].text.strip().lower()

def extract_keywords(text: str, client: anthropic.Anthropic) -> list[str]:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": f"List 5 keywords from: {text}"}],
    )
    return [kw.strip() for kw in resp.content[0].text.split(",")]

# ── fixtures ─────────────────────────────────────────────────────────────────

def make_mock_client(response_text: str) -> MagicMock:
    content = MagicMock()
    content.text = response_text
    response = MagicMock()
    response.content = [content]
    response.stop_reason = "end_turn"
    response.usage = MagicMock(input_tokens=10, output_tokens=5)
    client = MagicMock(spec=anthropic.Anthropic)
    client.messages.create.return_value = response
    return client

@pytest.fixture
def positive_client():
    return make_mock_client("positive")

@pytest.fixture
def keyword_client():
    return make_mock_client("python, asyncio, testing, mock, pytest")

# ── tests ────────────────────────────────────────────────────────────────────

def test_classify_positive(positive_client):
    result = classify_sentiment("I love this product!", positive_client)
    assert result == "positive"

def test_classify_calls_correct_model(positive_client):
    classify_sentiment("great", positive_client)
    kwargs = positive_client.messages.create.call_args.kwargs
    assert kwargs["model"] == "claude-haiku-4-5-20251001"

def test_extract_keywords_returns_list(keyword_client):
    result = extract_keywords("Testing Python agents with asyncio", keyword_client)
    assert isinstance(result, list)
    assert len(result) == 5

def test_extract_keywords_strips_whitespace(keyword_client):
    result = extract_keywords("any text", keyword_client)
    assert all(kw == kw.strip() for kw in result)
```

**Expected Token Savings:** Fixture factory reuse means one mock definition covers dozens of tests; adding `spec=anthropic.Anthropic` catches typos in attribute access.
**Environment:** pytest-based projects; teams that share fixtures across multiple test modules.

---

### Option 3 — Async agent mocking with AsyncMock

```python
import asyncio
import pytest
import anthropic
from unittest.mock import AsyncMock, MagicMock

# ── async agent code ──────────────────────────────────────────────────────────

async def translate(text: str, target_lang: str, client: anthropic.AsyncAnthropic) -> str:
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"Translate to {target_lang}: {text}"
        }],
    )
    return resp.content[0].text

# ── helpers ───────────────────────────────────────────────────────────────────

def make_async_mock_client(response_text: str) -> MagicMock:
    content = MagicMock()
    content.text = response_text
    response = MagicMock()
    response.content = [content]
    response.stop_reason = "end_turn"

    client = MagicMock(spec=anthropic.AsyncAnthropic)
    # AsyncMock makes the coroutine awaitable
    client.messages.create = AsyncMock(return_value=response)
    return client

# ── tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_translate_returns_text():
    client = make_async_mock_client("Bonjour le monde")
    result = await translate("Hello world", "French", client)
    assert result == "Bonjour le monde"

@pytest.mark.asyncio
async def test_translate_includes_target_lang():
    client = make_async_mock_client("Hola mundo")
    await translate("Hello world", "Spanish", client)
    call_args = client.messages.create.call_args
    user_content = call_args.kwargs["messages"][0]["content"]
    assert "Spanish" in user_content

@pytest.mark.asyncio
async def test_translate_concurrent_calls():
    client = make_async_mock_client("output")
    results = await asyncio.gather(
        translate("hi", "French", client),
        translate("bye", "German", client),
        translate("yes", "Italian", client),
    )
    assert len(results) == 3
    assert client.messages.create.call_count == 3
```

**Expected Token Savings:** AsyncMock tests async agents without event loop complexity; concurrent tests verify gather() behavior deterministically.
**Environment:** Async agents using `anthropic.AsyncAnthropic`; pytest-asyncio projects.

---

### Option 4 — Recorded response fixtures for regression testing

```python
import json
import os
import anthropic
from unittest.mock import MagicMock

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")

# ── fixture recorder (run once, commit JSON to repo) ─────────────────────────

def record_fixture(name: str, prompt: str) -> None:
    """Call the real API once and save the response as a fixture."""
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": prompt}],
    )
    os.makedirs(FIXTURES_DIR, exist_ok=True)
    data = {
        "content": [{"text": block.text} for block in resp.content],
        "stop_reason": resp.stop_reason,
        "usage": {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens},
    }
    with open(os.path.join(FIXTURES_DIR, f"{name}.json"), "w") as f:
        json.dump(data, f, indent=2)
    print(f"Recorded fixture: {name}.json")

# ── fixture loader ────────────────────────────────────────────────────────────

def load_fixture(name: str) -> MagicMock:
    path = os.path.join(FIXTURES_DIR, f"{name}.json")
    with open(path) as f:
        data = json.load(f)

    content_blocks = []
    for block_data in data["content"]:
        block = MagicMock()
        block.text = block_data["text"]
        content_blocks.append(block)

    response = MagicMock()
    response.content = content_blocks
    response.stop_reason = data["stop_reason"]
    response.usage = MagicMock(**data["usage"])

    client = MagicMock(spec=anthropic.Anthropic)
    client.messages.create.return_value = response
    return client

# ── agent under test ──────────────────────────────────────────────────────────

def generate_title(topic: str, client: anthropic.Anthropic) -> str:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": f"Write a blog post title about: {topic}"}],
    )
    return resp.content[0].text.strip()

# ── tests using fixtures ──────────────────────────────────────────────────────

import pytest

@pytest.fixture
def title_client(tmp_path, monkeypatch):
    """Build a fixture inline for this example (normally loaded from disk)."""
    content = MagicMock()
    content.text = "10 Reasons Python Async Will Change How You Code"
    response = MagicMock()
    response.content = [content]
    response.stop_reason = "end_turn"
    client = MagicMock(spec=anthropic.Anthropic)
    client.messages.create.return_value = response
    return client

def test_generate_title_returns_string(title_client):
    result = generate_title("async Python programming", title_client)
    assert isinstance(result, str)
    assert len(result) > 0

def test_generate_title_strips_whitespace(title_client):
    result = generate_title("machine learning", title_client)
    assert result == result.strip()
```

**Expected Token Savings:** Recorded fixtures capture real model responses once; replaying them in tests costs zero tokens and produces deterministic output for regression detection.
**Environment:** Regression test suites; teams that want to pin response format and detect when output changes after a model upgrade.

---

### Option 5 — Dependency injection with a fake client interface

```python
import anthropic
from typing import Protocol
from dataclasses import dataclass
import pytest

# ── protocol / interface ──────────────────────────────────────────────────────

class MessageClient(Protocol):
    def create_message(self, prompt: str, max_tokens: int) -> str:
        ...

# ── real adapter ──────────────────────────────────────────────────────────────

@dataclass
class AnthropicAdapter:
    _client: anthropic.Anthropic
    model: str = "claude-haiku-4-5-20251001"

    def create_message(self, prompt: str, max_tokens: int) -> str:
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text

# ── fake adapter for tests ────────────────────────────────────────────────────

@dataclass
class FakeMessageClient:
    responses: dict[str, str]  # prompt substring → response
    call_count: int = 0

    def create_message(self, prompt: str, max_tokens: int) -> str:
        self.call_count += 1
        for key, response in self.responses.items():
            if key in prompt:
                return response
        return "default fake response"

# ── agent using the interface ─────────────────────────────────────────────────

def answer_questions(questions: list[str], client: MessageClient) -> list[str]:
    return [client.create_message(q, max_tokens=64) for q in questions]

# ── tests ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def fake_client():
    return FakeMessageClient(responses={
        "capital":    "Paris is the capital of France.",
        "population": "France has approximately 68 million people.",
        "language":   "The official language of France is French.",
    })

def test_answer_questions_returns_all(fake_client):
    questions = ["What is the capital?", "What is the population?", "What language?"]
    results = answer_questions(questions, fake_client)
    assert len(results) == 3

def test_answer_questions_call_count(fake_client):
    questions = ["capital", "population"]
    answer_questions(questions, fake_client)
    assert fake_client.call_count == 2

def test_answer_questions_routing(fake_client):
    results = answer_questions(["Tell me about the capital of France"], fake_client)
    assert "Paris" in results[0]
```

**Expected Token Savings:** Protocol-based DI decouples agent logic from the SDK entirely; tests never touch the network regardless of test count or complexity.
**Environment:** Large codebases; teams that want to test agent logic independently of Anthropic SDK internals.

---

### Option 6 — Parametrized tests with multiple mock response scenarios

```python
import pytest
import anthropic
from unittest.mock import MagicMock

# ── agent with error handling ─────────────────────────────────────────────────

def safe_classify(text: str, client: anthropic.Anthropic) -> dict:
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=16,
            messages=[{"role": "user", "content": f"Classify as spam/ham: {text}"}],
        )
        label = resp.content[0].text.strip().lower()
        return {"label": label, "error": None}
    except anthropic.RateLimitError:
        return {"label": None, "error": "rate_limited"}
    except anthropic.APIConnectionError:
        return {"label": None, "error": "connection_error"}

# ── mock helpers ──────────────────────────────────────────────────────────────

def mock_success(text: str) -> MagicMock:
    content = MagicMock(); content.text = text
    resp = MagicMock(); resp.content = [content]
    client = MagicMock(spec=anthropic.Anthropic)
    client.messages.create.return_value = resp
    return client

def mock_error(error_class) -> MagicMock:
    client = MagicMock(spec=anthropic.Anthropic)
    client.messages.create.side_effect = error_class(
        message="mock error", request=MagicMock(), response=MagicMock()
    )
    return client

# ── parametrized tests ────────────────────────────────────────────────────────

@pytest.mark.parametrize("response_text,expected_label", [
    ("spam",   "spam"),
    ("ham",    "ham"),
    ("SPAM",   "spam"),   # tests lower() normalisation
    ("  ham ", "ham"),    # tests strip()
])
def test_classify_label_normalisation(response_text, expected_label):
    client = mock_success(response_text)
    result = safe_classify("Buy cheap meds now!", client)
    assert result["label"] == expected_label
    assert result["error"] is None

@pytest.mark.parametrize("error_class,expected_error", [
    (anthropic.RateLimitError,     "rate_limited"),
    (anthropic.APIConnectionError, "connection_error"),
])
def test_classify_error_handling(error_class, expected_error):
    client = mock_error(error_class)
    result = safe_classify("some text", client)
    assert result["label"] is None
    assert result["error"] == expected_error
```

**Expected Token Savings:** Parametrized mocks test N scenarios (success, edge cases, each error type) with zero API calls; adding a new scenario costs nothing.
**Environment:** Agents with error handling branches; any code path that needs to be tested against both success and failure modes.

---

## Comparison

| Option | Approach | Async Support | Fixture Reuse | Error Scenario Testing | Best For |
|---|---|---|---|---|---|
| 1. unittest.mock.patch | Inline patch decorator | No | Low | Manual | Quick, self-contained test files |
| 2. pytest fixtures | Shared factory fixture | No | High | Manual | Shared test suites with many tests |
| 3. AsyncMock | AsyncMock coroutine | Yes | Medium | Manual | Async agents; pytest-asyncio |
| 4. Recorded fixtures | JSON snapshots on disk | No | High | No | Regression detection after model updates |
| 5. DI + Protocol | Fake implementation | Protocol-based | High | Via fake logic | Large codebases; clean architecture |
| 6. Parametrized | Parametrize decorator | No | High | Yes | Edge cases; error handler coverage |
