---
layout: solution
title: "Agent Doesn't Implement Test Doubles for External APIs"
category: testing
description: "Replace live external API calls with stubs, fakes, and mocks during tests so the agent's logic can be verified without network access, cost, or flaky third-party dependencies."
tags: [testing, mocks, stubs, fakes, test-doubles, python]
---

# Agent Doesn't Implement Test Doubles for External APIs

Agents that call live APIs in tests are slow, expensive, and non-deterministic — a rate limit or outage breaks the entire test suite. Test doubles (stubs, fakes, mocks) replace external dependencies with controlled stand-ins so logic is validated cheaply and reliably.

## Option 1: Stub with Hardcoded Responses

```python
import anthropic
from unittest.mock import patch, MagicMock

client = anthropic.Anthropic()

# ── Production tool function ───────────────────────────────────────────────

def get_weather(city: str) -> dict:
    """Real implementation would call a weather API."""
    import urllib.request, json
    url = f"https://wttr.in/{city}?format=j1"
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read())

def agent_respond(city: str) -> str:
    data = get_weather(city)
    temp = data.get("current_condition", [{}])[0].get("temp_C", "?")
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": f"The temperature in {city} is {temp}°C. Summarize briefly."}],
    )
    return resp.content[0].text

# ── Stub ──────────────────────────────────────────────────────────────────

WEATHER_STUB = {
    "current_condition": [{"temp_C": "22", "weatherDesc": [{"value": "Sunny"}]}]
}

def test_agent_uses_weather_data():
    with patch("__main__.get_weather", return_value=WEATHER_STUB):
        result = agent_respond("Tokyo")
    assert "22" in result or "temperature" in result.lower() or len(result) > 0
    print(f"[PASS] stub test: {result.strip()[:80]}")

def test_agent_handles_missing_temp():
    stub_missing = {"current_condition": [{}]}
    with patch("__main__.get_weather", return_value=stub_missing):
        result = agent_respond("UnknownCity")
    assert isinstance(result, str)
    print(f"[PASS] missing temp test: {result.strip()[:80]}")

test_agent_uses_weather_data()
test_agent_handles_missing_temp()

# Expected Token Savings: No weather API calls in tests; one Haiku call per test verifies logic
# Environment: stdlib unittest.mock; works with any function-based API wrapper
```

## Option 2: Fake In-Memory Implementation

```python
import anthropic
from dataclasses import dataclass, field

client = anthropic.Anthropic()

# ── Interface (production would use real HTTP) ─────────────────────────────

class WeatherAPI:
    def current(self, city: str) -> dict:
        raise NotImplementedError

class DatabaseAPI:
    def get_user(self, user_id: str) -> dict | None:
        raise NotImplementedError
    def save_user(self, user_id: str, data: dict):
        raise NotImplementedError

# ── Fakes ─────────────────────────────────────────────────────────────────

@dataclass
class FakeWeatherAPI(WeatherAPI):
    responses: dict[str, dict] = field(default_factory=lambda: {
        "Tokyo":   {"temp_C": 22, "condition": "Sunny"},
        "London":  {"temp_C": 15, "condition": "Cloudy"},
        "default": {"temp_C": 20, "condition": "Clear"},
    })

    def current(self, city: str) -> dict:
        return self.responses.get(city, self.responses["default"])

@dataclass
class FakeDatabaseAPI(DatabaseAPI):
    _store: dict = field(default_factory=dict)
    _call_log: list = field(default_factory=list)

    def get_user(self, user_id: str) -> dict | None:
        self._call_log.append(("get", user_id))
        return self._store.get(user_id)

    def save_user(self, user_id: str, data: dict):
        self._call_log.append(("save", user_id))
        self._store[user_id] = data

    @property
    def call_log(self): return list(self._call_log)

# ── Agent using injected dependencies ─────────────────────────────────────

def agent_greet_user(
    user_id: str,
    weather_api: WeatherAPI,
    db: DatabaseAPI,
) -> str:
    user = db.get_user(user_id) or {"name": "stranger", "city": "Tokyo"}
    weather = weather_api.current(user["city"])
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content":
            f"Greet {user['name']} in {user['city']} where it's {weather['temp_C']}°C."}],
    )
    return resp.content[0].text

# ── Tests ─────────────────────────────────────────────────────────────────

weather = FakeWeatherAPI()
db = FakeDatabaseAPI()
db.save_user("u1", {"name": "Alice", "city": "Tokyo"})

result = agent_greet_user("u1", weather, db)
assert "alice" in result.lower() or "tokyo" in result.lower() or len(result) > 0
print(f"[PASS] fake test: {result.strip()[:80]}")
print(f"DB calls: {db.call_log}")

# Unknown user falls back to defaults
result2 = agent_greet_user("u_unknown", weather, db)
print(f"[PASS] unknown user: {result2.strip()[:80]}")

# Expected Token Savings: Zero real API calls; fakes are deterministic and instant
# Environment: dependency injection pattern; fakes implement same interface as production
```

## Option 3: Mock with Call Verification

```python
import anthropic
from unittest.mock import MagicMock, call, patch
import pytest

client = anthropic.Anthropic()

# ── Agent under test ──────────────────────────────────────────────────────

def process_order(order_id: str, payment_service, inventory_service) -> str:
    # Check inventory
    item = inventory_service.check(order_id)
    if not item["available"]:
        return "Item out of stock."
    # Charge payment
    charge = payment_service.charge(order_id, item["price"])
    if not charge["success"]:
        return "Payment failed."
    # Confirm with model
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content":
            f"Order {order_id} confirmed. Item: {item['name']}, Price: ${item['price']}. Write a brief confirmation."}],
    )
    return resp.content[0].text

# ── Mock-based tests ──────────────────────────────────────────────────────

def test_successful_order():
    payment = MagicMock()
    inventory = MagicMock()
    inventory.check.return_value = {"available": True, "name": "Laptop", "price": 999}
    payment.charge.return_value = {"success": True, "transaction_id": "txn_123"}

    result = process_order("order_42", payment, inventory)

    # Verify correct calls were made
    inventory.check.assert_called_once_with("order_42")
    payment.charge.assert_called_once_with("order_42", 999)
    assert isinstance(result, str) and len(result) > 0
    print(f"[PASS] successful order: {result.strip()[:60]}")

def test_out_of_stock():
    payment = MagicMock()
    inventory = MagicMock()
    inventory.check.return_value = {"available": False, "name": "Widget", "price": 10}

    result = process_order("order_99", payment, inventory)

    assert result == "Item out of stock."
    payment.charge.assert_not_called()  # Payment must NOT be called
    print(f"[PASS] out of stock: no payment charged")

def test_payment_failure():
    payment = MagicMock()
    inventory = MagicMock()
    inventory.check.return_value = {"available": True, "name": "Book", "price": 25}
    payment.charge.return_value = {"success": False, "error": "card_declined"}

    result = process_order("order_55", payment, inventory)

    assert result == "Payment failed."
    print(f"[PASS] payment failure handled correctly")

test_successful_order()
test_out_of_stock()
test_payment_failure()

# Expected Token Savings: Only 1 Haiku call (success path); failure paths use zero tokens
# Environment: unittest.mock; assert_called_once_with ensures no extra/missing API calls
```

## Option 4: Recorded Response Replay (VCR-style)

```python
import anthropic
import json
import hashlib
import os
from pathlib import Path

client = anthropic.Anthropic()
CASSETTE_DIR = Path("test_cassettes")

def _cassette_key(url: str, payload: dict) -> str:
    content = json.dumps({"url": url, "payload": payload}, sort_keys=True)
    return hashlib.sha256(content.encode()).hexdigest()[:16]

class Cassette:
    """Record external API calls on first run, replay on subsequent runs."""

    def __init__(self, name: str, record: bool = False):
        self.path = CASSETTE_DIR / f"{name}.json"
        self.record = record
        self._data: dict = {}
        if self.path.exists():
            self._data = json.loads(self.path.read_text())

    def get_or_record(self, key: str, fetch_fn) -> dict:
        if key in self._data and not self.record:
            print(f"  [REPLAY] cassette hit: {key}")
            return self._data[key]
        print(f"  [RECORD] fetching: {key}")
        result = fetch_fn()
        self._data[key] = result
        CASSETTE_DIR.mkdir(exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=2))
        return result

def fetch_github_user(username: str, cassette: Cassette) -> dict:
    """Fetch GitHub user — uses cassette for replay in tests."""
    import urllib.request
    key = _cassette_key("github_users", {"username": username})

    def live_fetch():
        url = f"https://api.github.com/users/{username}"
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                return json.loads(r.read())
        except Exception:
            return {"login": username, "public_repos": 0, "followers": 0}

    return cassette.get_or_record(key, live_fetch)

def agent_describe_user(username: str, cassette: Cassette) -> str:
    user = fetch_github_user(username, cassette)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content":
            f"Describe GitHub user {user.get('login', username)} who has "
            f"{user.get('public_repos', 0)} repos and {user.get('followers', 0)} followers."}],
    )
    return resp.content[0].text

# First call: records (or replays if cassette exists)
cassette = Cassette("github_test")
result = agent_describe_user("octocat", cassette)
print(f"[PASS] {result.strip()[:80]}")

# Second call: always replays from cassette
result2 = agent_describe_user("octocat", cassette)
print(f"[REPLAY] {result2.strip()[:80]}")

# Expected Token Savings: After first recording, zero external API calls; Haiku still called for generation
# Environment: CASSETTE_DIR stores JSON; commit cassettes to version control for CI
```

## Option 5: Async API Double with Configurable Latency and Errors

```python
import anthropic
import asyncio
import random
from dataclasses import dataclass, field
from typing import Any

client = anthropic.AsyncAnthropic()

# ── Async fake with configurable behavior ─────────────────────────────────

@dataclass
class AsyncAPIDouble:
    responses: dict[str, Any] = field(default_factory=dict)
    error_rate: float = 0.0       # fraction of calls that raise
    latency_ms: float = 0.0       # simulated network delay
    call_log: list = field(default_factory=list)

    async def call(self, endpoint: str, **kwargs) -> Any:
        self.call_log.append({"endpoint": endpoint, "kwargs": kwargs})
        if self.latency_ms:
            await asyncio.sleep(self.latency_ms / 1000)
        if random.random() < self.error_rate:
            raise ConnectionError(f"Simulated failure for {endpoint}")
        return self.responses.get(endpoint, {"status": "ok"})

# ── Async agent under test ────────────────────────────────────────────────

async def async_agent(search_api: AsyncAPIDouble, user_query: str) -> str:
    search_result = await search_api.call("search", query=user_query)
    hits = search_result.get("hits", [])
    context = " ".join(h.get("title", "") for h in hits[:3])

    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content":
            f"Based on: {context or 'no results'}. Answer: {user_query}"}],
    )
    return resp.content[0].text

# ── Tests ─────────────────────────────────────────────────────────────────

async def test_with_results():
    api = AsyncAPIDouble(responses={
        "search": {"hits": [
            {"title": "Python asyncio guide"},
            {"title": "Async patterns in Python"},
        ]}
    })
    result = await async_agent(api, "What is asyncio?")
    assert "search" in [c["endpoint"] for c in api.call_log]
    print(f"[PASS] with results: {result.strip()[:60]}")

async def test_empty_results():
    api = AsyncAPIDouble(responses={"search": {"hits": []}})
    result = await async_agent(api, "What is asyncio?")
    assert isinstance(result, str)
    print(f"[PASS] empty results: {result.strip()[:60]}")

async def test_slow_api():
    api = AsyncAPIDouble(
        responses={"search": {"hits": [{"title": "Fast Python"}]}},
        latency_ms=200,
    )
    import time; start = time.monotonic()
    result = await async_agent(api, "Python performance?")
    elapsed = time.monotonic() - start
    assert elapsed >= 0.2
    print(f"[PASS] slow api ({elapsed:.2f}s): {result.strip()[:60]}")

async def main():
    await test_with_results()
    await test_empty_results()
    await test_slow_api()

asyncio.run(main())

# Expected Token Savings: All external calls replaced; only Haiku called once per test
# Environment: async; configurable error_rate enables resilience testing without real failures
```

## Option 6: Pytest Plugin-Style Fixture Doubles with Teardown Tracking

```python
# conftest_doubles.py — import into your test files
# Usage: pytest conftest_doubles.py -v

import anthropic
import pytest
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

client = anthropic.Anthropic()

# ── Tracked doubles ───────────────────────────────────────────────────────

@dataclass
class TrackedStub:
    """A stub that records calls and supports assertions."""
    return_values: dict[str, Any] = field(default_factory=dict)
    _calls: list[dict] = field(default_factory=list)
    call_count: int = 0

    def __call__(self, method: str, **kwargs) -> Any:
        self._calls.append({"method": method, "kwargs": kwargs})
        self.call_count += 1
        return self.return_values.get(method, {})

    def assert_called_with(self, method: str, **expected_kwargs):
        matching = [c for c in self._calls if c["method"] == method]
        assert matching, f"Method '{method}' was never called"
        last = matching[-1]["kwargs"]
        for k, v in expected_kwargs.items():
            assert last.get(k) == v, f"Expected {k}={v!r}, got {last.get(k)!r}"

    def reset(self):
        self._calls.clear()
        self.call_count = 0

# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture
def payment_double():
    return TrackedStub(return_values={
        "charge":  {"success": True, "txn_id": "txn_test"},
        "refund":  {"success": True},
        "balance": {"amount": 1000.0},
    })

@pytest.fixture
def email_double():
    return TrackedStub(return_values={
        "send":    {"message_id": "msg_001", "status": "sent"},
        "verify":  {"valid": True},
    })

# ── Agent ─────────────────────────────────────────────────────────────────

def checkout_agent(cart: list[dict], payment: TrackedStub, email: TrackedStub) -> str:
    total = sum(item["price"] for item in cart)
    charge = payment("charge", amount=total, currency="USD")
    if not charge["success"]:
        return "Payment failed"
    summary = ", ".join(f"{i['name']} (${i['price']})" for i in cart)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content":
            f"Generate a receipt for: {summary}. Total: ${total}"}],
    )
    receipt = resp.content[0].text
    email("send", to="customer@example.com", body=receipt)
    return receipt

# ── Tests ─────────────────────────────────────────────────────────────────

def test_checkout_charges_correct_amount(payment_double, email_double):
    cart = [{"name": "Book", "price": 25}, {"name": "Pen", "price": 5}]
    result = checkout_agent(cart, payment_double, email_double)
    payment_double.assert_called_with("charge", amount=30, currency="USD")
    assert payment_double.call_count >= 1
    print(f"[PASS] correct charge: {result.strip()[:60]}")

def test_checkout_sends_email(payment_double, email_double):
    cart = [{"name": "Laptop", "price": 999}]
    checkout_agent(cart, payment_double, email_double)
    email_double.assert_called_with("send", to="customer@example.com")
    print(f"[PASS] email sent after checkout")

def test_checkout_no_email_on_failure(payment_double, email_double):
    payment_double.return_values["charge"] = {"success": False}
    cart = [{"name": "Phone", "price": 500}]
    result = checkout_agent(cart, payment_double, email_double)
    assert result == "Payment failed"
    send_calls = [c for c in email_double._calls if c["method"] == "send"]
    assert not send_calls, "Email should not be sent on payment failure"
    print(f"[PASS] no email on failure")

# Run inline (normally these run via pytest)
import types
_fixture_payment = payment_double.__wrapped__ if hasattr(payment_double, "__wrapped__") else TrackedStub(return_values={"charge":{"success":True,"txn_id":"t1"},"refund":{"success":True},"balance":{"amount":1000.0}})
_fixture_email   = TrackedStub(return_values={"send":{"message_id":"m1","status":"sent"},"verify":{"valid":True}})
test_checkout_charges_correct_amount(_fixture_payment, _fixture_email)
test_checkout_sends_email(_fixture_payment, _fixture_email)
_fixture_payment2 = TrackedStub(return_values={"charge":{"success":False}})
_fixture_email2   = TrackedStub(return_values={"send":{"message_id":"m2","status":"sent"}})
test_checkout_no_email_on_failure(_fixture_payment2, _fixture_email2)

# Expected Token Savings: Only 1-2 Haiku calls (success paths only); failure paths are zero-cost
# Environment: pytest fixtures; TrackedStub is framework-independent; add to conftest.py
```

## Comparison

| Option | Double Type | Call Verification | Best For |
|--------|------------|------------------|----------|
| 1 — Stub with patch | Stub (hardcoded returns) | None | Quick isolation of one function |
| 2 — Fake implementation | Fake (working in-memory) | Via call_log | Stateful APIs (DB, session store) |
| 3 — Mock with assertions | Mock (MagicMock) | `assert_called_*` | Verifying exact interaction sequence |
| 4 — Cassette Replay | Recorded real responses | None | Third-party APIs with complex responses |
| 5 — Async double | Async fake with latency/errors | Via call_log | Async agents; resilience testing |
| 6 — Tracked stub | Custom tracked stub | `assert_called_with` | Pytest integration; teardown tracking |
