---
layout: solution
title: "Agent Doesn't Implement API Mock Server for Hermetic Testing"
description: "How to run a local mock server that intercepts agent API calls — eliminating flaky tests, network dependencies, and API costs from your test suite."
tags: [testing, mocks, api, hermetic, deterministic, network, wiremock]
difficulty: intermediate
solution_count: 6
---

## Problem

Agent tests make real HTTP calls to the Anthropic API, vector databases, and external tools. Tests fail intermittently due to network issues, rate limits, or API outages. Tests are slow because every LLM call takes 1-5 seconds. Tests are expensive because every CI run burns API credits. Different test runs produce different results because LLM outputs are non-deterministic.

```python
# Bad: real API calls in tests — slow, flaky, expensive
async def test_agent_summarizes():
    response = await agent.process("Summarize this document: ...")
    # This makes a real $0.01 API call on every test run
    assert "summary" in response.lower()
```

---

## Solution 1 — httpx MockTransport: Intercept Requests In-Process

Replace the httpx transport layer with an in-process mock that returns pre-configured responses without making network calls.

```python
import pytest
import httpx
import json
from anthropic import AsyncAnthropic

def make_mock_anthropic_response(content: str, model: str = "claude-haiku-4-5-20251001") -> dict:
    """Build a valid Anthropic API response envelope."""
    return {
        "id": "msg_mock_001",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": content}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": len(content.split())},
    }

class MockAnthropicTransport(httpx.AsyncMockTransport):
    def __init__(self, responses: dict[str, str]):
        """responses: {prompt_substring -> response_text}"""
        self._responses = responses
        self._call_log: list[dict] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        messages = body.get("messages", [])
        user_content = messages[-1]["content"] if messages else ""

        # Find matching response
        response_text = "Default mock response."
        for trigger, text in self._responses.items():
            if trigger.lower() in str(user_content).lower():
                response_text = text
                break

        self._call_log.append({
            "url": str(request.url),
            "user_message": user_content[:100],
            "response": response_text,
        })
        return httpx.Response(
            200,
            json=make_mock_anthropic_response(response_text),
            headers={"Content-Type": "application/json"},
        )

    @property
    def calls(self) -> list[dict]:
        return self._call_log

@pytest.fixture
def mock_anthropic():
    """Fixture providing a mocked AsyncAnthropic client."""
    transport = MockAnthropicTransport({
        "summarize": "Here is a concise summary: The document covers key AI trends.",
        "classify": "Category: Technology",
        "translate": "Bonjour, comment puis-je vous aider?",
    })
    http_client = httpx.AsyncClient(transport=transport)
    client = AsyncAnthropic(api_key="mock-key", http_client=http_client)
    return client, transport

@pytest.mark.asyncio
async def test_agent_summarizes(mock_anthropic):
    client, transport = mock_anthropic
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": "Please summarize this article..."}],
    )
    assert "summary" in response.content[0].text.lower()
    assert len(transport.calls) == 1  # exactly one API call made

@pytest.mark.asyncio
async def test_agent_classifies(mock_anthropic):
    client, transport = mock_anthropic
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": "Please classify this text..."}],
    )
    assert "technology" in response.content[0].text.lower()
    assert transport.calls[0]["user_message"]  # call was recorded
```

---

## Solution 2 — respx: Route-Based HTTP Mocking for Complex Patterns

Use `respx` to define URL-pattern-based mock routes, enabling precise control over different endpoints with different response patterns.

```python
import pytest
import respx
import httpx
import json
from anthropic import AsyncAnthropic

MOCK_MESSAGES_RESPONSE = {
    "id": "msg_mock",
    "type": "message",
    "role": "assistant",
    "model": "claude-haiku-4-5-20251001",
    "content": [{"type": "text", "text": "Mocked response: I can help with that!"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 15, "output_tokens": 8},
}

MOCK_TOOL_USE_RESPONSE = {
    "id": "msg_tool_mock",
    "type": "message",
    "role": "assistant",
    "model": "claude-haiku-4-5-20251001",
    "content": [{
        "type": "tool_use",
        "id": "tool_001",
        "name": "web_search",
        "input": {"query": "current weather in Paris"},
    }],
    "stop_reason": "tool_use",
    "usage": {"input_tokens": 20, "output_tokens": 12},
}

@pytest.fixture
def mock_routes():
    with respx.mock(base_url="https://api.anthropic.com", assert_all_called=False) as router:
        # Standard message response
        router.post("/v1/messages").mock(
            return_value=httpx.Response(200, json=MOCK_MESSAGES_RESPONSE)
        )
        yield router

@pytest.fixture
def mock_tool_routes():
    with respx.mock(base_url="https://api.anthropic.com") as router:
        call_count = {"n": 0}

        def tool_then_answer(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            if call_count["n"] == 1:
                return httpx.Response(200, json=MOCK_TOOL_USE_RESPONSE)
            else:
                final = {**MOCK_MESSAGES_RESPONSE,
                         "content": [{"type": "text", "text": "The weather in Paris is 18°C."}]}
                return httpx.Response(200, json=final)

        router.post("/v1/messages").mock(side_effect=tool_then_answer)
        yield router, call_count

@pytest.mark.asyncio
async def test_simple_response(mock_routes):
    client = AsyncAnthropic(api_key="mock-key")
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": "Hello!"}],
    )
    assert response.content[0].text == "Mocked response: I can help with that!"
    assert mock_routes["POST https://api.anthropic.com/v1/messages"].called

@pytest.mark.asyncio
async def test_tool_use_flow(mock_tool_routes):
    router, call_count = mock_tool_routes
    client = AsyncAnthropic(api_key="mock-key")

    # First call: returns tool_use
    r1 = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        tools=[{"name": "web_search", "description": "Search",
                "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}}}],
        messages=[{"role": "user", "content": "What's the weather in Paris?"}],
    )
    assert r1.stop_reason == "tool_use"
    assert r1.content[0].name == "web_search"

    # Second call: returns final answer
    r2 = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[
            {"role": "user", "content": "What's the weather in Paris?"},
            {"role": "assistant", "content": r1.content},
            {"role": "user", "content": [{"type": "tool_result",
                                          "tool_use_id": "tool_001",
                                          "content": "18°C, partly cloudy"}]},
        ],
    )
    assert "18°C" in r2.content[0].text
    assert call_count["n"] == 2
```

---

## Solution 3 — WireMock-Style Local HTTP Server

Run a real HTTP server in-process (using FastAPI) that handles agent requests. Supports recording, playback, and stateful scenario testing.

```python
import asyncio
import json
import threading
import uvicorn
import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from anthropic import AsyncAnthropic

class MockAPIServer:
    """A local HTTP server that mocks the Anthropic API."""

    def __init__(self, port: int = 18080):
        self.port = port
        self._app = FastAPI()
        self._scenarios: list[dict] = []  # queue of responses
        self._recording: list[dict] = []  # recorded calls
        self._default_response = "Default mock response."
        self._setup_routes()
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    def _setup_routes(self):
        @self._app.post("/v1/messages")
        async def mock_messages(request: Request):
            body = await request.json()
            user_content = body.get("messages", [{}])[-1].get("content", "")

            if self._scenarios:
                scenario = self._scenarios.pop(0)
                response_text = scenario["response"]
                stop_reason = scenario.get("stop_reason", "end_turn")
                content = scenario.get("content", [{"type": "text", "text": response_text}])
            else:
                response_text = self._default_response
                stop_reason = "end_turn"
                content = [{"type": "text", "text": response_text}]

            self._recording.append({
                "user_message": str(user_content)[:200],
                "response": response_text,
            })

            return JSONResponse({
                "id": f"msg_mock_{len(self._recording)}",
                "type": "message",
                "role": "assistant",
                "model": body.get("model", "claude-haiku-4-5-20251001"),
                "content": content,
                "stop_reason": stop_reason,
                "usage": {"input_tokens": 10, "output_tokens": 5},
            })

    def queue_response(self, response: str, stop_reason: str = "end_turn") -> None:
        """Add a response to the scenario queue."""
        self._scenarios.append({"response": response, "stop_reason": stop_reason})

    def queue_tool_use(self, tool_name: str, tool_input: dict) -> None:
        """Queue a tool_use response."""
        self._scenarios.append({
            "response": "",
            "stop_reason": "tool_use",
            "content": [{
                "type": "tool_use",
                "id": "tool_mock_001",
                "name": tool_name,
                "input": tool_input,
            }],
        })

    def start(self) -> None:
        config = uvicorn.Config(self._app, host="127.0.0.1", port=self.port,
                                log_level="error")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        import time; time.sleep(0.3)  # wait for server to start

    def stop(self) -> None:
        if self._server:
            self._server.should_exit = True

    def make_client(self) -> AsyncAnthropic:
        return AsyncAnthropic(
            api_key="mock-key",
            base_url=f"http://127.0.0.1:{self.port}",
            http_client=httpx.AsyncClient(base_url=f"http://127.0.0.1:{self.port}"),
        )

    @property
    def recorded_calls(self) -> list[dict]:
        return self._recording

@pytest.fixture(scope="session")
def mock_server():
    server = MockAPIServer(port=18080)
    server.start()
    yield server
    server.stop()

@pytest.mark.asyncio
async def test_agent_with_mock_server(mock_server):
    mock_server.queue_response("The capital of France is Paris.")
    client = mock_server.make_client()
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": "What is the capital of France?"}],
    )
    assert "Paris" in response.content[0].text
    assert len(mock_server.recorded_calls) >= 1
```

---

## Solution 4 — Cassette Recording and Playback (VCR-Style)

Record real API interactions to YAML/JSON cassettes on first run; replay them on subsequent runs — real responses, zero network cost after recording.

```python
import asyncio
import json
import os
from pathlib import Path
from typing import Any
import httpx
import pytest
from anthropic import AsyncAnthropic

CASSETTE_DIR = Path("tests/cassettes")

class CassetteTransport(httpx.AsyncBaseTransport):
    def __init__(self, cassette_path: Path, mode: str = "playback"):
        self._path = cassette_path
        self._mode = mode  # "record" or "playback"
        self._recordings: list[dict] = []
        self._playback_index = 0
        if mode == "playback" and cassette_path.exists():
            with open(cassette_path) as f:
                self._recordings = json.load(f)
        self._real_transport = httpx.AsyncHTTPTransport() if mode == "record" else None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self._mode == "playback":
            if self._playback_index >= len(self._recordings):
                raise RuntimeError(
                    f"Cassette exhausted: {self._path} has only {len(self._recordings)} calls"
                )
            recording = self._recordings[self._playback_index]
            self._playback_index += 1
            return httpx.Response(
                recording["status_code"],
                json=recording["response_body"],
                headers=recording.get("headers", {}),
            )
        else:  # record mode
            response = await self._real_transport.handle_async_request(request)
            body = json.loads(await response.aread())
            self._recordings.append({
                "url": str(request.url),
                "method": request.method,
                "status_code": response.status_code,
                "response_body": body,
            })
            # Rebuild response (it was consumed)
            return httpx.Response(response.status_code, json=body)

    def save_cassette(self) -> None:
        if self._mode == "record":
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "w") as f:
                json.dump(self._recordings, f, indent=2)
            print(f"Cassette saved: {self._path} ({len(self._recordings)} interactions)")

class cassette:
    """Context manager for cassette recording/playback."""
    def __init__(self, name: str, mode: str = None):
        self._path = CASSETTE_DIR / f"{name}.json"
        # Auto-detect mode: record if cassette doesn't exist
        self._mode = mode or ("playback" if self._path.exists() else "record")
        self._transport: CassetteTransport | None = None

    def __enter__(self) -> AsyncAnthropic:
        self._transport = CassetteTransport(self._path, self._mode)
        http_client = httpx.AsyncClient(transport=self._transport)
        return AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", "mock"),
                              http_client=http_client)

    def __exit__(self, *args) -> None:
        if self._transport:
            self._transport.save_cassette()

@pytest.mark.asyncio
async def test_with_cassette():
    with cassette("test_summarize") as client:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": "Summarize: AI is transforming work."}],
        )
        assert len(response.content[0].text) > 0
    # First run: records real API call to tests/cassettes/test_summarize.json
    # Subsequent runs: plays back from cassette — zero cost, zero network
```

---

## Solution 5 — Stateful Mock with Conversation Simulation

Build a mock that simulates realistic multi-turn conversations: tracks message history, generates contextually relevant responses, and enforces tool-call/tool-result ordering.

```python
import json
import re
from dataclasses import dataclass, field
from typing import Any
import httpx
from anthropic import AsyncAnthropic

@dataclass
class ConversationMock:
    """Stateful mock that generates responses based on conversation context."""
    response_rules: list[tuple[str, str]] = field(default_factory=list)  # (pattern, response)
    tool_responses: dict[str, Any] = field(default_factory=dict)  # tool_name -> result
    call_count: int = 0
    max_calls: int = 20

    def add_rule(self, pattern: str, response: str) -> "ConversationMock":
        self.response_rules.append((pattern, response))
        return self

    def add_tool(self, tool_name: str, result: Any) -> "ConversationMock":
        self.tool_responses[tool_name] = result
        return self

    def generate_response(self, messages: list[dict]) -> dict:
        self.call_count += 1
        if self.call_count > self.max_calls:
            raise RuntimeError(f"Mock exceeded max_calls={self.max_calls}")

        # Check if last message is a tool result
        last = messages[-1] if messages else {}
        if last.get("role") == "user" and isinstance(last.get("content"), list):
            for block in last["content"]:
                if block.get("type") == "tool_result":
                    return {
                        "content": [{"type": "text", "text": f"Based on the tool result: {str(block.get('content', ''))[:100]}"}],
                        "stop_reason": "end_turn",
                    }

        # Match against rules
        full_text = " ".join(
            str(m.get("content", "")) for m in messages if m.get("role") == "user"
        )
        for pattern, response in self.response_rules:
            if re.search(pattern, full_text, re.IGNORECASE):
                # Check if response is a tool call spec
                if response.startswith("TOOL:"):
                    parts = response[5:].split(":", 1)
                    tool_name = parts[0]
                    return {
                        "content": [{
                            "type": "tool_use",
                            "id": f"tool_{self.call_count}",
                            "name": tool_name,
                            "input": json.loads(parts[1]) if len(parts) > 1 else {},
                        }],
                        "stop_reason": "tool_use",
                    }
                return {
                    "content": [{"type": "text", "text": response}],
                    "stop_reason": "end_turn",
                }

        return {
            "content": [{"type": "text", "text": "I understand. How can I help further?"}],
            "stop_reason": "end_turn",
        }

class StatefulMockTransport(httpx.AsyncBaseTransport):
    def __init__(self, mock: ConversationMock):
        self._mock = mock

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        messages = body.get("messages", [])
        generated = self._mock.generate_response(messages)
        return httpx.Response(200, json={
            "id": f"msg_mock_{self._mock.call_count}",
            "type": "message",
            "role": "assistant",
            "model": body.get("model", "claude-haiku-4-5-20251001"),
            "stop_reason": generated["stop_reason"],
            "content": generated["content"],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        })

# Usage
import pytest

@pytest.mark.asyncio
async def test_multi_turn_with_stateful_mock():
    mock = (ConversationMock()
        .add_rule(r"weather", "TOOL:get_weather:{\"location\": \"Paris\"}")
        .add_rule(r"thank", "You're welcome! Is there anything else I can help with?"))

    transport = StatefulMockTransport(mock)
    client = AsyncAnthropic(
        api_key="mock",
        http_client=httpx.AsyncClient(transport=transport),
    )

    # Turn 1: tool call
    r1 = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": "What's the weather like?"}],
    )
    assert r1.stop_reason == "tool_use"

    # Turn 2: tool result
    r2 = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[
            {"role": "user", "content": "What's the weather like?"},
            {"role": "assistant", "content": r1.content},
            {"role": "user", "content": [{"type": "tool_result",
                                          "tool_use_id": r1.content[0].id,
                                          "content": "Sunny, 22°C"}]},
        ],
    )
    assert "22°C" in r2.content[0].text or "tool result" in r2.content[0].text.lower()
    assert mock.call_count == 2
```

---

## Solution 6 — Pytest Plugin: Auto-Mock Based on Test Markers

Create a pytest plugin that automatically mocks the Anthropic API when tests are marked with `@pytest.mark.hermetic`, injecting configurable responses without any fixture boilerplate.

```python
# conftest.py — install as project-level pytest plugin
import pytest
import httpx
import json
from anthropic import AsyncAnthropic

HERMETIC_RESPONSES = {
    "default": "Mocked agent response for hermetic testing.",
    "summarize": "Summary: The text covers the main topic concisely.",
    "classify": "Classification: Category A",
    "translate": "Translation: [translated content]",
    "search": "Search results: [relevant information found]",
}

def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "hermetic: mark test as hermetic (uses mock API, no real network calls)",
    )

def _make_mock_response(text: str) -> dict:
    return {
        "id": "msg_hermetic",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-4-5-20251001",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 5, "output_tokens": len(text.split())},
    }

class AutoMockTransport(httpx.AsyncBaseTransport):
    def __init__(self, responses: dict[str, str]):
        self._responses = responses
        self.calls: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        messages = body.get("messages", [])
        user_text = str(messages[-1].get("content", "")).lower() if messages else ""

        response_text = self._responses["default"]
        for key, text in self._responses.items():
            if key != "default" and key in user_text:
                response_text = text
                break

        self.calls.append(user_text[:50])
        return httpx.Response(200, json=_make_mock_response(response_text))

@pytest.fixture(autouse=True)
def auto_hermetic_mock(request, monkeypatch):
    """Automatically inject mock client for @pytest.mark.hermetic tests."""
    if not request.node.get_closest_marker("hermetic"):
        return  # not hermetic — use real API

    transport = AutoMockTransport(HERMETIC_RESPONSES)
    http_client = httpx.AsyncClient(transport=transport)
    mock_client = AsyncAnthropic(api_key="hermetic-mock", http_client=http_client)

    # Patch AsyncAnthropic constructor
    monkeypatch.setattr("anthropic.AsyncAnthropic", lambda **kwargs: mock_client)
    request.node._hermetic_transport = transport

# Usage in test files — zero boilerplate
@pytest.mark.hermetic
@pytest.mark.asyncio
async def test_agent_summarizes_hermetically():
    # AsyncAnthropic() is automatically mocked — no real API call
    client = AsyncAnthropic()
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": "Please summarize this article..."}],
    )
    assert "summary" in response.content[0].text.lower()

@pytest.mark.hermetic
@pytest.mark.asyncio
async def test_agent_classifies_hermetically():
    client = AsyncAnthropic()
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": "Classify this text: AI news"}],
    )
    assert "category" in response.content[0].text.lower()
```

---

## Comparison

| Approach | Setup | Stateful | Recording | Realistic Responses | Zero Network | Best For |
|---|---|---|---|---|---|---|
| httpx MockTransport | **Low** | No | No | Low | **Yes** | Quick unit tests |
| respx routes | **Low** | Partial | No | Medium | **Yes** | Route-specific mocking |
| WireMock-style server | Medium | **Yes** | No | Medium | **Yes** | Integration test scenarios |
| Cassette recording | Medium | No | **Yes** | **High** (real) | After first run | Regression tests |
| Stateful conversation mock | Medium | **Yes** | No | Medium | **Yes** | Multi-turn flow testing |
| Hermetic pytest plugin | **Low** (after setup) | No | No | Low | **Yes** | Zero-boilerplate hermetic tests |
