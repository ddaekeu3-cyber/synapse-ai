---
layout: solution
title: "Agent Doesn't Implement Integration Tests for Multi-Step Tool Flows"
category: testing
description: "The agent's multi-turn tool use pipeline is only tested manually — a model call triggers a tool, the result feeds back into the model, but no automated test verifies this loop end-to-end."
tags: [testing, integration, tool-use, multi-step, reliability]
---

## Symptom

Each individual component (API client, tool executor, message formatter) passes unit tests. But when they are wired together, bugs appear: the tool result is injected in the wrong message role, the second model call doesn't receive the tool output, or an exception in a tool call silently produces an empty `tool_result` that the model misinterprets. These bugs only surface during manual testing or in production.

## Root Cause

Multi-step tool flows have implicit contracts between components: the tool call must be extracted from `response.content`, executed, formatted as a `tool_result` block, and injected back into `messages` in the correct structure before the next model call. A unit test that mocks each step independently cannot verify these interface contracts. An integration test that wires all components together — with the API calls mocked at the network boundary — catches structural bugs without live API costs.

## Fix

### Option 1 — Sequential two-turn tool flow integration test

```python
import pytest
import anthropic
from unittest.mock import MagicMock

# ── tool definitions and executor ─────────────────────────────────────────────

CALCULATOR_TOOL = {
    "name": "calculate",
    "description": "Evaluate a math expression.",
    "input_schema": {
        "type": "object",
        "properties": {"expression": {"type": "string"}},
        "required": ["expression"],
    },
}

def execute_tool(name: str, tool_input: dict) -> str:
    if name == "calculate":
        try:
            return str(eval(tool_input["expression"], {"__builtins__": {}}))
        except Exception as e:
            return f"error: {e}"
    return f"unknown tool: {name}"

# ── agent loop under test ──────────────────────────────────────────────────────

def run_agent(user_query: str, client: anthropic.Anthropic) -> str:
    messages = [{"role": "user", "content": user_query}]

    for _ in range(5):  # max turns
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=[CALCULATOR_TOOL],
            messages=messages,
        )

        # Collect tool calls
        tool_calls = [b for b in resp.content if b.type == "tool_use"]
        if not tool_calls:
            # No more tool calls — return final text
            text_blocks = [b for b in resp.content if b.type == "text"]
            return text_blocks[0].text if text_blocks else ""

        # Execute tools and build result message
        messages.append({"role": "assistant", "content": resp.content})
        tool_results = [
            {
                "type": "tool_result",
                "tool_use_id": tc.id,
                "content": execute_tool(tc.name, tc.input),
            }
            for tc in tool_calls
        ]
        messages.append({"role": "user", "content": tool_results})

    return "max turns reached"

# ── integration test helpers ───────────────────────────────────────────────────

def make_client_sequence(*turn_specs) -> MagicMock:
    """Build a mock client that returns different responses per call."""
    responses = []
    for spec in turn_specs:
        resp = MagicMock()
        resp.content = []
        for block_spec in spec:
            if block_spec["type"] == "tool_use":
                b = MagicMock()
                b.type = "tool_use"
                b.name = block_spec["name"]
                b.input = block_spec["input"]
                b.id    = block_spec.get("id", "tool_abc")
                resp.content.append(b)
            elif block_spec["type"] == "text":
                b = MagicMock()
                b.type = "text"
                b.text = block_spec["text"]
                resp.content.append(b)
        responses.append(resp)
    client = MagicMock(spec=anthropic.Anthropic)
    client.messages.create.side_effect = responses
    return client

# ── tests ──────────────────────────────────────────────────────────────────────

def test_two_turn_tool_flow_returns_final_text():
    client = make_client_sequence(
        [{"type": "tool_use", "name": "calculate", "input": {"expression": "6 * 7"}, "id": "t1"}],
        [{"type": "text",     "text": "6 times 7 equals 42."}],
    )
    result = run_agent("What is 6 times 7?", client)
    assert "42" in result

def test_two_api_calls_made():
    client = make_client_sequence(
        [{"type": "tool_use", "name": "calculate", "input": {"expression": "100 / 4"}, "id": "t1"}],
        [{"type": "text",     "text": "100 divided by 4 is 25."}],
    )
    run_agent("Divide 100 by 4.", client)
    assert client.messages.create.call_count == 2

def test_tool_result_injected_in_second_call():
    client = make_client_sequence(
        [{"type": "tool_use", "name": "calculate", "input": {"expression": "5 + 5"}, "id": "t2"}],
        [{"type": "text",     "text": "5 plus 5 is 10."}],
    )
    run_agent("Add 5 and 5.", client)
    second_call_kwargs = client.messages.create.call_args_list[1].kwargs
    messages = second_call_kwargs["messages"]
    # Last user message must be tool_results
    last_user = messages[-1]
    assert last_user["role"] == "user"
    assert isinstance(last_user["content"], list)
    assert last_user["content"][0]["type"] == "tool_result"

def test_tool_result_contains_execution_output():
    client = make_client_sequence(
        [{"type": "tool_use", "name": "calculate", "input": {"expression": "3 ** 3"}, "id": "t3"}],
        [{"type": "text",     "text": "3 cubed is 27."}],
    )
    run_agent("What is 3 cubed?", client)
    second_call_messages = client.messages.create.call_args_list[1].kwargs["messages"]
    tool_result_content = second_call_messages[-1]["content"][0]["content"]
    assert tool_result_content == "27"  # execute_tool("calculate", {"expression": "3**3"})

def test_no_tool_call_returns_immediately():
    client = make_client_sequence(
        [{"type": "text", "text": "I already know the answer: 42."}],
    )
    result = run_agent("What is the answer?", client)
    assert "42" in result
    assert client.messages.create.call_count == 1
```

**Expected Token Savings:** Integration tests catch message-structure bugs (wrong role, missing tool_use_id) before live API calls; each caught bug saves 2+ API round-trips to reproduce manually.
**Environment:** Any agent with a tool use loop; the test structure above applies to any tool name, not just calculator.

---

### Option 2 — Parallel tool call integration: multiple tools in one turn

```python
import pytest
import anthropic
from unittest.mock import MagicMock

TOOLS = [
    {
        "name": "get_weather",
        "description": "Get weather for a city.",
        "input_schema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
    {
        "name": "get_time",
        "description": "Get current time in a timezone.",
        "input_schema": {
            "type": "object",
            "properties": {"timezone": {"type": "string"}},
            "required": ["timezone"],
        },
    },
]

TOOL_RESPONSES = {
    "get_weather": lambda inp: f"Sunny, 22°C in {inp['city']}",
    "get_time":    lambda inp: f"14:30 in {inp['timezone']}",
}

def execute_tools_parallel(tool_calls) -> list[dict]:
    return [
        {
            "type": "tool_result",
            "tool_use_id": tc.id,
            "content": TOOL_RESPONSES.get(tc.name, lambda _: "unknown")(tc.input),
        }
        for tc in tool_calls
    ]

def run_agent(query: str, client: anthropic.Anthropic) -> str:
    messages = [{"role": "user", "content": query}]
    for _ in range(4):
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=TOOLS,
            messages=messages,
        )
        tool_calls = [b for b in resp.content if b.type == "tool_use"]
        if not tool_calls:
            return next((b.text for b in resp.content if b.type == "text"), "")
        messages.append({"role": "assistant", "content": resp.content})
        results = execute_tools_parallel(tool_calls)
        messages.append({"role": "user", "content": results})
    return ""

def make_parallel_client(tool_specs: list[dict], final_text: str) -> MagicMock:
    tool_blocks = []
    for spec in tool_specs:
        b = MagicMock()
        b.type  = "tool_use"
        b.name  = spec["name"]
        b.input = spec["input"]
        b.id    = spec["id"]
        tool_blocks.append(b)
    r1 = MagicMock(); r1.content = tool_blocks
    tb = MagicMock(); tb.type = "text"; tb.text = final_text
    r2 = MagicMock(); r2.content = [tb]
    client = MagicMock(spec=anthropic.Anthropic)
    client.messages.create.side_effect = [r1, r2]
    return client

def test_parallel_tools_all_results_injected():
    client = make_parallel_client(
        [
            {"name": "get_weather", "input": {"city": "Paris"},  "id": "w1"},
            {"name": "get_time",    "input": {"timezone": "CET"}, "id": "t1"},
        ],
        "Paris is sunny at 22°C; local time is 14:30 CET.",
    )
    result = run_agent("Weather and time in Paris?", client)
    assert "22" in result or "14:30" in result

def test_all_tool_results_have_correct_ids():
    client = make_parallel_client(
        [
            {"name": "get_weather", "input": {"city": "Tokyo"}, "id": "wTOK"},
            {"name": "get_time",    "input": {"timezone": "JST"}, "id": "tJST"},
        ],
        "Tokyo: sunny, 09:00 JST.",
    )
    run_agent("Tokyo weather and time?", client)
    second_call = client.messages.create.call_args_list[1]
    results = second_call.kwargs["messages"][-1]["content"]
    result_ids = {r["tool_use_id"] for r in results}
    assert result_ids == {"wTOK", "tJST"}

def test_parallel_results_count_matches_tool_calls():
    specs = [
        {"name": "get_weather", "input": {"city": "NYC"}, "id": "w2"},
        {"name": "get_time",    "input": {"timezone": "EST"}, "id": "t2"},
    ]
    client = make_parallel_client(specs, "NYC: cloudy, 09:00 EST.")
    run_agent("NYC status?", client)
    results = client.messages.create.call_args_list[1].kwargs["messages"][-1]["content"]
    assert len(results) == len(specs)
```

**Expected Token Savings:** Parallel tool tests verify that all tool results are injected — a common bug is only injecting the first result when multiple tools fire simultaneously.
**Environment:** Agents that call multiple tools in a single turn; the test pattern catches "only first tool result injected" bugs that are invisible in unit tests.

---

### Option 3 — Error recovery flow: tool failure triggers agent retry

```python
import pytest
import anthropic
from unittest.mock import MagicMock

SEARCH_TOOL = {
    "name": "search",
    "description": "Search the web.",
    "input_schema": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
}

def execute_search(query: str, should_fail: bool) -> dict:
    if should_fail:
        return {"type": "tool_result", "content": "error: service unavailable", "is_error": True}
    return {"type": "tool_result", "content": f"Results for: {query}"}

def run_agent_with_error_handling(query: str, client: anthropic.Anthropic, fail_first: bool) -> str:
    messages = [{"role": "user", "content": query}]
    call_count = 0
    for _ in range(5):
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=[SEARCH_TOOL],
            messages=messages,
        )
        tool_calls = [b for b in resp.content if b.type == "tool_use"]
        if not tool_calls:
            return next((b.text for b in resp.content if b.type == "text"), "")
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for tc in tool_calls:
            call_count += 1
            result = execute_search(tc.input["query"], should_fail=(fail_first and call_count == 1))
            result["tool_use_id"] = tc.id
            results.append(result)
        messages.append({"role": "user", "content": results})
    return ""

def make_error_recovery_client(final_text: str) -> MagicMock:
    make_tool = lambda id_: type("B", (), {"type": "tool_use", "name": "search",
                                           "input": {"query": "test"}, "id": id_})()
    r1 = MagicMock(); r1.content = [make_tool("s1")]
    r2 = MagicMock(); r2.content = [make_tool("s2")]
    tb = MagicMock(); tb.type = "text"; tb.text = final_text
    r3 = MagicMock(); r3.content = [tb]
    client = MagicMock(spec=anthropic.Anthropic)
    client.messages.create.side_effect = [r1, r2, r3]
    return client

def test_agent_retries_after_tool_error():
    client = make_error_recovery_client("I found the information using a second search.")
    result = run_agent_with_error_handling("Find Python docs.", client, fail_first=True)
    assert client.messages.create.call_count == 3  # initial + retry turn + final

def test_error_result_has_is_error_flag():
    client = make_error_recovery_client("Found it on retry.")
    run_agent_with_error_handling("Search for X.", client, fail_first=True)
    # First user message with tool_result should contain is_error=True
    first_result_msg = client.messages.create.call_args_list[1].kwargs["messages"][-1]
    results = first_result_msg["content"]
    assert any(r.get("is_error") for r in results)
```

**Expected Token Savings:** Testing the error recovery path catches "agent silently ignores tool errors" bugs; these bugs cause infinite loops in production that burn thousands of tokens.
**Environment:** Agents with fallback retry logic on tool failure; testing that `is_error: true` in tool results is correctly handled by the agent loop.

---

### Option 4 — Stateful multi-session integration test

```python
import pytest
import anthropic
from unittest.mock import MagicMock
from dataclasses import dataclass, field

NOTE_TOOL = {
    "name": "save_note",
    "description": "Save a note.",
    "input_schema": {
        "type": "object",
        "properties": {"title": {"type": "string"}, "content": {"type": "string"}},
        "required": ["title", "content"],
    },
}

@dataclass
class NoteStorage:
    notes: dict[str, str] = field(default_factory=dict)

    def save(self, title: str, content: str) -> str:
        self.notes[title] = content
        return f"Saved note: {title}"

storage = NoteStorage()

def execute_note_tool(tc) -> str:
    return storage.save(tc.input["title"], tc.input["content"])

def run_session(messages: list[dict], client: anthropic.Anthropic) -> tuple[str, list[dict]]:
    """Returns (final_text, updated_messages)."""
    for _ in range(4):
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=[NOTE_TOOL],
            messages=messages,
        )
        tool_calls = [b for b in resp.content if b.type == "tool_use"]
        if not tool_calls:
            text = next((b.text for b in resp.content if b.type == "text"), "")
            return text, messages
        messages.append({"role": "assistant", "content": resp.content})
        results = [
            {"type": "tool_result", "tool_use_id": tc.id, "content": execute_note_tool(tc)}
            for tc in tool_calls
        ]
        messages.append({"role": "user", "content": results})
    return "", messages

def make_single_tool_client(tool_name: str, tool_input: dict, tool_id: str, final_text: str) -> MagicMock:
    tb_tool = MagicMock()
    tb_tool.type  = "tool_use"
    tb_tool.name  = tool_name
    tb_tool.input = tool_input
    tb_tool.id    = tool_id
    r1 = MagicMock(); r1.content = [tb_tool]
    tb_text = MagicMock(); tb_text.type = "text"; tb_text.text = final_text
    r2 = MagicMock(); r2.content = [tb_text]
    client = MagicMock(spec=anthropic.Anthropic)
    client.messages.create.side_effect = [r1, r2]
    return client

def test_note_saved_to_storage():
    storage.notes.clear()
    client = make_single_tool_client(
        "save_note", {"title": "Test Note", "content": "Hello"}, "n1", "Note saved!"
    )
    messages = [{"role": "user", "content": "Save a note titled 'Test Note' with content 'Hello'."}]
    run_session(messages, client)
    assert "Test Note" in storage.notes
    assert storage.notes["Test Note"] == "Hello"

def test_session_history_grows_correctly():
    storage.notes.clear()
    client = make_single_tool_client(
        "save_note", {"title": "Log", "content": "entry"}, "n2", "Done."
    )
    messages = [{"role": "user", "content": "Save a log entry."}]
    _, updated = run_session(messages, client)
    # user → assistant (tool_use) → user (tool_result) → [final assistant not appended]
    roles = [m["role"] for m in updated]
    assert roles == ["user", "assistant", "user"]
```

**Expected Token Savings:** Stateful tests verify that tool side effects (database writes, file saves) actually happen — unit tests with mocked executors cannot catch bugs where the agent constructs the right call but the executor is never invoked.
**Environment:** Agents with persistent tool side effects (note saving, database writes, file operations); multi-session agents where conversation history grows across turns.

---

### Option 5 — pytest parametrize across multiple tool scenarios

```python
import pytest
import anthropic
from unittest.mock import MagicMock

TRANSLATE_TOOL = {
    "name": "translate",
    "description": "Translate text to a target language.",
    "input_schema": {
        "type": "object",
        "properties": {
            "text":   {"type": "string"},
            "target": {"type": "string"},
        },
        "required": ["text", "target"],
    },
}

TRANSLATIONS = {
    ("hello", "French"):  "bonjour",
    ("hello", "German"):  "hallo",
    ("goodbye", "French"): "au revoir",
}

def translate(text: str, target: str) -> str:
    return TRANSLATIONS.get((text.lower(), target), f"{text} [{target}]")

def run_translate_agent(query: str, client: anthropic.Anthropic) -> str:
    messages = [{"role": "user", "content": query}]
    for _ in range(4):
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            tools=[TRANSLATE_TOOL],
            messages=messages,
        )
        tool_calls = [b for b in resp.content if b.type == "tool_use"]
        if not tool_calls:
            return next((b.text for b in resp.content if b.type == "text"), "")
        messages.append({"role": "assistant", "content": resp.content})
        results = [
            {"type": "tool_result", "tool_use_id": tc.id,
             "content": translate(tc.input["text"], tc.input["target"])}
            for tc in tool_calls
        ]
        messages.append({"role": "user", "content": results})
    return ""

def make_translate_client(text: str, target: str, final: str) -> MagicMock:
    tc = MagicMock()
    tc.type  = "tool_use"
    tc.name  = "translate"
    tc.input = {"text": text, "target": target}
    tc.id    = "tr1"
    r1 = MagicMock(); r1.content = [tc]
    tb = MagicMock(); tb.type = "text"; tb.text = final
    r2 = MagicMock(); r2.content = [tb]
    client = MagicMock(spec=anthropic.Anthropic)
    client.messages.create.side_effect = [r1, r2]
    return client

@pytest.mark.parametrize("text,target,expected_in_result", [
    ("hello",   "French",  "bonjour"),
    ("hello",   "German",  "hallo"),
    ("goodbye", "French",  "au revoir"),
])
def test_translation_flows(text, target, expected_in_result):
    client = make_translate_client(
        text, target,
        final=f"'{text}' in {target} is '{expected_in_result}'."
    )
    result = run_translate_agent(f"Translate '{text}' to {target}.", client)
    assert expected_in_result in result

@pytest.mark.parametrize("text,target", [
    ("hello", "French"),
    ("hello", "German"),
])
def test_tool_called_with_correct_arguments(text, target):
    client = make_translate_client(text, target, f"Done.")
    run_translate_agent(f"Translate '{text}' to {target}.", client)
    first_call = client.messages.create.call_args_list[0]
    # Verify tools are passed
    assert "tools" in first_call.kwargs
    assert any(t["name"] == "translate" for t in first_call.kwargs["tools"])
```

**Expected Token Savings:** Parametrized integration tests cover multiple scenarios cheaply; adding a new language pair or translation scenario costs one line, not a new test file.
**Environment:** Agents with parameterisable tool calls; testing tool behavior across many input combinations efficiently.

---

### Option 6 — Full pipeline test with fixtures for complex agent workflows

```python
import pytest
import anthropic
from unittest.mock import MagicMock
from typing import Generator

# ── complex agent: research → summarise → store ────────────────────────────────

RESEARCH_TOOL  = {"name": "research",  "description": "Search for information.", "input_schema": {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]}}
SUMMARISE_TOOL = {"name": "summarise", "description": "Summarise text.",         "input_schema": {"type": "object", "properties": {"text":  {"type": "string"}}, "required": ["text"]}}
STORE_TOOL     = {"name": "store",     "description": "Store a result.",          "input_schema": {"type": "object", "properties": {"key": {"type": "string"}, "value": {"type": "string"}}, "required": ["key", "value"]}}

_storage: dict[str, str] = {}

def execute(name: str, inp: dict) -> str:
    if name == "research":  return f"Research on {inp['topic']}: lots of data found"
    if name == "summarise": return f"Summary: {inp['text'][:30]}..."
    if name == "store":     _storage[inp["key"]] = inp["value"]; return "stored"
    return "unknown"

def run_pipeline(query: str, client: anthropic.Anthropic) -> str:
    messages = [{"role": "user", "content": query}]
    for _ in range(8):
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=[RESEARCH_TOOL, SUMMARISE_TOOL, STORE_TOOL],
            messages=messages,
        )
        tool_calls = [b for b in resp.content if b.type == "tool_use"]
        if not tool_calls:
            return next((b.text for b in resp.content if b.type == "text"), "")
        messages.append({"role": "assistant", "content": resp.content})
        results = [{"type": "tool_result", "tool_use_id": tc.id, "content": execute(tc.name, tc.input)} for tc in tool_calls]
        messages.append({"role": "user", "content": results})
    return ""

def _tc(type_: str, name: str, inp: dict, id_: str):
    b = MagicMock(); b.type = type_; b.name = name; b.input = inp; b.id = id_; return b

def _text(t: str):
    b = MagicMock(); b.type = "text"; b.text = t; return b

def _resp(*blocks):
    r = MagicMock(); r.content = list(blocks); return r

@pytest.fixture(autouse=True)
def clear_storage():
    _storage.clear()
    yield
    _storage.clear()

@pytest.fixture
def pipeline_client() -> MagicMock:
    client = MagicMock(spec=anthropic.Anthropic)
    client.messages.create.side_effect = [
        _resp(_tc("tool_use", "research",  {"topic": "AI agents"}, "r1")),
        _resp(_tc("tool_use", "summarise", {"text": "Research on AI agents: lots of data found"}, "s1")),
        _resp(_tc("tool_use", "store",     {"key": "ai_agents", "value": "Summary: Research on AI..."}, "st1")),
        _resp(_text("Research complete and stored.")),
    ]
    return client

def test_full_pipeline_completes(pipeline_client):
    result = run_pipeline("Research AI agents and store a summary.", pipeline_client)
    assert pipeline_client.messages.create.call_count == 4
    assert "stored" in result or "complete" in result

def test_full_pipeline_stores_result(pipeline_client):
    run_pipeline("Research AI agents and store a summary.", pipeline_client)
    assert "ai_agents" in _storage

def test_pipeline_tool_sequence(pipeline_client):
    run_pipeline("Research AI agents and store a summary.", pipeline_client)
    calls = pipeline_client.messages.create.call_args_list
    # Verify tool sequence via injected tool_result messages
    def get_tool_names_from_results(call_idx):
        msgs = calls[call_idx].kwargs["messages"]
        for m in reversed(msgs):
            if m["role"] == "user" and isinstance(m["content"], list):
                return [r.get("content", "") for r in m["content"] if r.get("type") == "tool_result"]
        return []
    assert len(get_tool_names_from_results(1)) == 1  # research result
    assert len(get_tool_names_from_results(2)) == 1  # summarise result
```

**Expected Token Savings:** Full pipeline test with fixtures verifies the entire research→summarise→store workflow; catches bugs in tool sequencing that only appear when all three tools are wired together.
**Environment:** Complex multi-tool agents with sequential dependencies; agents where tool B depends on the output of tool A.

---

## Comparison

| Option | Flow Type | Tools Tested | Side Effects | Parametrized | Best For |
|---|---|---|---|---|---|
| 1. Sequential two-turn | A→result→B | Single | No | No | Baseline tool loop testing |
| 2. Parallel tools | A+B→results→C | Multiple simultaneous | No | No | Multi-tool-per-turn agents |
| 3. Error recovery | A(fail)→B | Single with error | No | No | Retry and error handling paths |
| 4. Stateful session | A→side effect | Single with storage | Yes | No | Persistent tool side effects |
| 5. Parametrize scenarios | A→result | Single | No | Yes | Multiple input/output scenarios |
| 6. Full pipeline fixture | A→B→C→done | Sequential chain | Yes | No | Complex multi-step workflows |
