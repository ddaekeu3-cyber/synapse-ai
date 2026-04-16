---
layout: solution
title: "Agent Doesn't Implement Test Isolation Between Agent Sessions"
category: testing
description: "Ensure each agent test runs in a clean, isolated state — resetting shared memory, SQLite databases, global caches, and conversation history so tests don't bleed state into each other and failures are reproducible."
tags: [testing, isolation, fixtures, state-management, pytest, python]
---

# Agent Doesn't Implement Test Isolation Between Agent Sessions

Agent tests that share SQLite databases, global caches, or in-memory stores produce order-dependent failures — a test passes alone but fails in a suite because a prior test left stale data. Proper isolation gives each test a fresh slate: ephemeral databases, scoped fixtures, and explicit teardown that makes failures reproducible regardless of execution order.

## Option 1: Per-Test In-Memory SQLite via Fixture

```python
import anthropic
import sqlite3
import pytest
from unittest.mock import patch, MagicMock

# The module under test uses a global DB path
import agent.memory as memory_module

@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    """Replace the global DB path with a per-test temp file."""
    db_path = str(tmp_path / "test_memory.db")
    # Patch the module-level DB constant
    with patch.object(memory_module, "DB", db_path):
        memory_module.init_db()
        yield db_path
    # tmp_path is auto-cleaned by pytest

# Stub Anthropic client for all tests
@pytest.fixture(autouse=True)
def stub_client():
    mock = MagicMock()
    mock.messages.create.return_value = MagicMock(
        content=[MagicMock(text="stub response")],
        usage=MagicMock(input_tokens=10, output_tokens=5),
    )
    with patch("agent.memory.client", mock):
        yield mock

# Tests — each gets its own empty DB
def test_store_and_retrieve():
    memory_module.store("user-1", "Python is great")
    results = memory_module.search("user-1", "Python")
    assert len(results) == 1
    assert "Python" in results[0]

def test_no_cross_session_leakage():
    # user-1 data from test_store_and_retrieve is NOT here — isolated DB
    results = memory_module.search("user-1", "Python")
    assert len(results) == 0  # clean state

def test_duplicate_detection():
    memory_module.store("user-1", "asyncio is async")
    memory_module.store("user-1", "asyncio is async")  # duplicate
    results = memory_module.search("user-1", "asyncio")
    assert len(results) == 1  # dedup works

# Expected Token Savings: Stub client prevents API calls; isolated DB means no fixture ordering tricks needed
# Environment: pytest + tmp_path; patch.object works for module-level constants; autouse=True for silent isolation
```

## Option 2: Session Factory with Explicit Reset

```python
import anthropic
import sqlite3
import pytest
import uuid
from unittest.mock import MagicMock, patch

# In-memory session store (simulates agent state)
class AgentSession:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.history: list[dict] = []
        self.memory: dict[str, str] = {}
        self.tool_calls: list[str] = []

    def add_message(self, role: str, content: str):
        self.history.append({"role": role, "content": content})

    def reset(self):
        self.history.clear()
        self.memory.clear()
        self.tool_calls.clear()

@pytest.fixture
def session():
    """Create a fresh AgentSession per test."""
    s = AgentSession(session_id=str(uuid.uuid4()))
    yield s
    s.reset()  # explicit teardown for clarity

@pytest.fixture
def mock_client():
    m = MagicMock()
    m.messages.create.return_value = MagicMock(
        content=[MagicMock(text="mocked")],
        usage=MagicMock(input_tokens=5, output_tokens=3),
    )
    return m

def agent_respond(session: AgentSession, user_msg: str, client) -> str:
    session.add_message("user", user_msg)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=session.history,
    )
    reply = resp.content[0].text
    session.add_message("assistant", reply)
    return reply

# Tests — each gets its own session fixture instance
def test_session_starts_empty(session):
    assert session.history == []
    assert session.memory  == {}

def test_single_turn(session, mock_client):
    reply = agent_respond(session, "Hello", mock_client)
    assert reply == "mocked"
    assert len(session.history) == 2  # user + assistant

def test_multi_turn_accumulates(session, mock_client):
    agent_respond(session, "Message 1", mock_client)
    agent_respond(session, "Message 2", mock_client)
    assert len(session.history) == 4  # 2 user + 2 assistant

def test_sessions_are_independent(mock_client):
    s1 = AgentSession("s1")
    s2 = AgentSession("s2")
    agent_respond(s1, "Only in s1", mock_client)
    assert len(s2.history) == 0  # s2 unaffected

# Expected Token Savings: Mock client + session fixture = zero API calls; each test is O(1) isolated
# Environment: pytest; session fixture scope="function" (default) gives one instance per test
```

## Option 3: Environment Variable Isolation with monkeypatch

```python
import anthropic
import os
import pytest
from unittest.mock import MagicMock, patch

# Agent reads config from env vars at import time — tricky to isolate
def get_agent_config() -> dict:
    return {
        "model":       os.environ.get("AGENT_MODEL",   "claude-haiku-4-5-20251001"),
        "max_tokens":  int(os.environ.get("MAX_TOKENS", "512")),
        "environment": os.environ.get("ENVIRONMENT",   "production"),
        "session_id":  os.environ.get("SESSION_ID",    "default"),
    }

def make_agent_call(prompt: str, client) -> str:
    cfg = get_agent_config()
    resp = client.messages.create(
        model=cfg["model"],
        max_tokens=cfg["max_tokens"],
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text

@pytest.fixture
def mock_client():
    m = MagicMock()
    m.messages.create.return_value = MagicMock(
        content=[MagicMock(text="stub")],
        usage=MagicMock(input_tokens=10, output_tokens=5),
    )
    return m

# Tests using monkeypatch for env var isolation
def test_uses_configured_model(monkeypatch, mock_client):
    monkeypatch.setenv("AGENT_MODEL", "claude-sonnet-4-6")
    monkeypatch.setenv("MAX_TOKENS", "256")
    make_agent_call("test", mock_client)
    call_kwargs = mock_client.messages.create.call_args.kwargs
    assert call_kwargs["model"] == "claude-sonnet-4-6"
    assert call_kwargs["max_tokens"] == 256

def test_default_model_when_env_unset(monkeypatch, mock_client):
    monkeypatch.delenv("AGENT_MODEL", raising=False)
    monkeypatch.delenv("MAX_TOKENS",  raising=False)
    make_agent_call("test", mock_client)
    call_kwargs = mock_client.messages.create.call_args.kwargs
    assert call_kwargs["model"] == "claude-haiku-4-5-20251001"
    assert call_kwargs["max_tokens"] == 512

def test_env_vars_do_not_leak_between_tests(monkeypatch, mock_client):
    # monkeypatch auto-restores after each test — this test gets a clean slate
    current_model = os.environ.get("AGENT_MODEL", "claude-haiku-4-5-20251001")
    assert current_model == "claude-haiku-4-5-20251001"  # not set by prior test

# Expected Token Savings: monkeypatch restores env after each test; no global state mutation
# Environment: pytest monkeypatch; works for os.environ, module attributes, and class attributes
```

## Option 4: SQLite Per-Test Isolation with Parameterized Scenarios

```python
import anthropic
import sqlite3
import pytest
import os
from unittest.mock import MagicMock, patch

DB_PATH_VAR = "AGENT_DB_PATH"

def init_schema(db_path: str):
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE IF NOT EXISTS conversations (session_id TEXT, role TEXT, content TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS tool_calls (session_id TEXT, tool TEXT, result TEXT)")
    con.commit(); con.close()

def save_message(db_path: str, session_id: str, role: str, content: str):
    con = sqlite3.connect(db_path)
    con.execute("INSERT INTO conversations VALUES (?,?,?)", (session_id, role, content))
    con.commit(); con.close()

def get_history(db_path: str, session_id: str) -> list[dict]:
    con = sqlite3.connect(db_path)
    rows = con.execute(
        "SELECT role, content FROM conversations WHERE session_id=?", (session_id,)
    ).fetchall()
    con.close()
    return [{"role": r[0], "content": r[1]} for r in rows]

@pytest.fixture
def isolated_db(tmp_path):
    db_path = str(tmp_path / f"test_{os.getpid()}.db")
    init_schema(db_path)
    old = os.environ.get(DB_PATH_VAR)
    os.environ[DB_PATH_VAR] = db_path
    yield db_path
    if old is None:
        os.environ.pop(DB_PATH_VAR, None)
    else:
        os.environ[DB_PATH_VAR] = old

# Parameterized tests — each runs with isolated DB
@pytest.mark.parametrize("session_id,messages", [
    ("sess-a", ["Hello", "How are you?"]),
    ("sess-b", ["What is Python?", "Tell me more."]),
    ("sess-c", ["Goodbye"]),
])
def test_history_isolation(isolated_db, session_id, messages):
    db = isolated_db
    for i, msg in enumerate(messages):
        save_message(db, session_id, "user", msg)
        save_message(db, session_id, "assistant", f"reply-{i}")

    history = get_history(db, session_id)
    assert len(history) == len(messages) * 2

    # Other sessions in SAME test run — still isolated because different session_id
    other_history = get_history(db, "sess-other")
    assert len(other_history) == 0

def test_no_data_from_prior_test(isolated_db):
    # This test gets a FRESH db — no data from parameterized runs above
    con = sqlite3.connect(isolated_db)
    count = con.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    con.close()
    assert count == 0

# Expected Token Savings: Parameterized + isolated DB = comprehensive coverage with no inter-test interference
# Environment: tmp_path is pytest's temp dir fixture; auto-cleaned after test session
```

## Option 5: Global Cache Reset with autouse Fixture

```python
import anthropic
import pytest
from unittest.mock import MagicMock, patch

# Module-level caches (common source of test bleed)
_response_cache: dict[str, str] = {}
_tool_results:   dict[str, object] = {}
_session_store:  dict[str, list]   = {}

def get_cached_response(prompt: str) -> str | None:
    return _response_cache.get(prompt)

def cache_response(prompt: str, response: str):
    _response_cache[prompt] = response

def get_session(session_id: str) -> list:
    return _session_store.setdefault(session_id, [])

def add_to_session(session_id: str, msg: dict):
    _session_store.setdefault(session_id, []).append(msg)

@pytest.fixture(autouse=True)
def reset_global_caches():
    """Clear all module-level caches before every test."""
    _response_cache.clear()
    _tool_results.clear()
    _session_store.clear()
    yield
    # Post-test clear too (defensive)
    _response_cache.clear()
    _tool_results.clear()
    _session_store.clear()

@pytest.fixture
def mock_client():
    call_count = [0]
    def side_effect(**kwargs):
        call_count[0] += 1
        return MagicMock(
            content=[MagicMock(text=f"response-{call_count[0]}")],
            usage=MagicMock(input_tokens=5, output_tokens=3),
        )
    m = MagicMock()
    m.messages.create.side_effect = side_effect
    return m

def test_cache_starts_empty():
    assert get_cached_response("any prompt") is None

def test_cache_stores_and_retrieves():
    cache_response("hello", "world")
    assert get_cached_response("hello") == "world"

def test_cache_cleared_between_tests():
    # Prior test set "hello" -> "world" but autouse fixture cleared it
    assert get_cached_response("hello") is None

def test_session_starts_empty():
    assert get_session("user-1") == []

def test_session_accumulates_within_test():
    add_to_session("user-1", {"role": "user", "content": "hi"})
    add_to_session("user-1", {"role": "assistant", "content": "hello"})
    assert len(get_session("user-1")) == 2

def test_session_empty_again():
    # autouse fixture reset it
    assert len(get_session("user-1")) == 0

# Expected Token Savings: autouse fixture eliminates setup boilerplate in each test; no state from prior tests
# Environment: autouse=True applies to all tests in scope; use scope="session" for expensive one-time setup
```

## Option 6: Async Test Isolation with Event Loop Reset

```python
import anthropic
import asyncio
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch

# Async agent state
class AsyncAgentState:
    def __init__(self):
        self.history: list[dict] = []
        self.pending_tasks: list[asyncio.Task] = []
        self._lock = asyncio.Lock()

    async def add_message(self, role: str, content: str):
        async with self._lock:
            self.history.append({"role": role, "content": content})

    async def reset(self):
        for task in self.pending_tasks:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        async with self._lock:
            self.history.clear()
            self.pending_tasks.clear()

@pytest_asyncio.fixture
async def agent_state():
    state = AsyncAgentState()
    yield state
    await state.reset()

@pytest_asyncio.fixture
async def async_mock_client():
    m = MagicMock()
    m.messages.create = AsyncMock(return_value=MagicMock(
        content=[MagicMock(text="async stub")],
        usage=MagicMock(input_tokens=8, output_tokens=4),
    ))
    return m

async def async_agent_call(state: AsyncAgentState, prompt: str, client) -> str:
    await state.add_message("user", prompt)
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=state.history,
    )
    text = resp.content[0].text
    await state.add_message("assistant", text)
    return text

@pytest.mark.asyncio
async def test_async_session_starts_empty(agent_state):
    assert agent_state.history == []

@pytest.mark.asyncio
async def test_async_single_turn(agent_state, async_mock_client):
    reply = await async_agent_call(agent_state, "Hello", async_mock_client)
    assert reply == "async stub"
    assert len(agent_state.history) == 2

@pytest.mark.asyncio
async def test_async_state_not_shared(agent_state, async_mock_client):
    # Different fixture instance — starts empty regardless of prior test
    assert agent_state.history == []
    await async_agent_call(agent_state, "First", async_mock_client)
    assert len(agent_state.history) == 2

@pytest.mark.asyncio
async def test_concurrent_messages_are_thread_safe(agent_state, async_mock_client):
    await asyncio.gather(
        async_agent_call(agent_state, "Msg A", async_mock_client),
        async_agent_call(agent_state, "Msg B", async_mock_client),
        async_agent_call(agent_state, "Msg C", async_mock_client),
    )
    assert len(agent_state.history) == 6  # 3 user + 3 assistant

# Expected Token Savings: AsyncMock client = zero real API calls; lock ensures no race conditions in test assertions
# Environment: pip install pytest-asyncio; set asyncio_mode = "auto" in pytest.ini to avoid @pytest.mark.asyncio
```

## Comparison

| Option | Isolation Mechanism | Scope | Async | DB Isolation |
|--------|-------------------|-------|-------|-------------|
| 1 — tmp_path DB | Patched DB path | Per-test | No | Yes (file) |
| 2 — Session Factory | Fixture instance | Per-test | No | In-memory |
| 3 — monkeypatch Env | Env var scope | Per-test | No | No |
| 4 — Parameterized SQLite | tmp_path per test | Per-test | No | Yes (file) |
| 5 — Global Cache Reset | autouse clear | Per-test | No | In-memory |
| 6 — Async Event Loop | pytest-asyncio fixture | Per-test | Yes | In-memory |
