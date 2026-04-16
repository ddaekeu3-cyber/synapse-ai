---
title: "Agent Doesn't Implement Async Lock for Shared Resource Access"
description: "Six solutions for protecting shared state, files, and external resources in concurrent agent systems using asyncio locks, RW locks, and distributed locking."
difficulty: intermediate
category: concurrency
tags: [asyncio, lock, mutex, shared-state, concurrency, race-condition]
---

# Agent Doesn't Implement Async Lock for Shared Resource Access

When multiple async agent tasks access shared state simultaneously—a counter, a file, a cache, a DB connection—they race. One overwrites the other's update. The result is silent data corruption: totals that don't add up, files half-written, caches in impossible states. Async locks serialize access without blocking the event loop.

## Solution 1: Basic asyncio.Lock for Shared In-Memory State

Protect a shared dictionary or counter with a simple asyncio.Lock.

```python
import asyncio
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic


@dataclass
class SharedAgentState:
    """Mutable state shared across concurrent agent tasks."""
    conversation_history: list[dict] = field(default_factory=list)
    token_count: int = 0
    request_count: int = 0
    last_model_used: str = ""
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    async def append_message(self, role: str, content: str):
        async with self._lock:
            self.conversation_history.append({"role": role, "content": content})

    async def record_usage(self, input_tokens: int, output_tokens: int, model: str):
        async with self._lock:
            self.token_count += input_tokens + output_tokens
            self.request_count += 1
            self.last_model_used = model

    async def get_snapshot(self) -> dict:
        async with self._lock:
            return {
                "history_len": len(self.conversation_history),
                "total_tokens": self.token_count,
                "requests": self.request_count,
                "last_model": self.last_model_used,
            }


class SharedStateAgent:
    def __init__(self, state: SharedAgentState):
        self.client = AsyncAnthropic()
        self.state = state

    async def chat(self, message: str, model: str = "claude-haiku-4-5-20251001") -> str:
        await self.state.append_message("user", message)
        async with self.state._lock:
            history = list(self.state.conversation_history[-10:])

        response = await self.client.messages.create(
            model=model,
            max_tokens=1024,
            messages=history,
        )
        text = response.content[0].text
        await self.state.append_message("assistant", text)
        await self.state.record_usage(
            response.usage.input_tokens,
            response.usage.output_tokens,
            model,
        )
        return text


async def demo_basic_lock():
    state = SharedAgentState()
    agents = [SharedStateAgent(state) for _ in range(3)]

    # All agents share the same state — lock prevents corruption
    messages = [f"Agent says: {i}" for i in range(9)]
    tasks = [agents[i % 3].chat(msg) for i, msg in enumerate(messages)]
    await asyncio.gather(*tasks)

    snapshot = await state.get_snapshot()
    print(f"Shared state: {snapshot}")
    assert snapshot["requests"] == 9, f"Expected 9 requests, got {snapshot['requests']}"
```

## Solution 2: Reader-Writer Lock for Read-Heavy Shared Data

Allow multiple concurrent readers; exclusive access only for writers. Maximizes throughput when reads dominate.

```python
import asyncio
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic


class AsyncRWLock:
    """
    Reader-Writer lock: multiple simultaneous readers, exclusive writers.
    Writers wait for all readers to finish; readers wait for active writers.
    """

    def __init__(self):
        self._readers = 0
        self._reader_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()

    class _ReadCtx:
        def __init__(self, rwlock: "AsyncRWLock"):
            self._rwlock = rwlock

        async def __aenter__(self):
            async with self._rwlock._reader_lock:
                self._rwlock._readers += 1
                if self._rwlock._readers == 1:
                    await self._rwlock._write_lock.acquire()
            return self

        async def __aexit__(self, *args):
            async with self._rwlock._reader_lock:
                self._rwlock._readers -= 1
                if self._rwlock._readers == 0:
                    self._rwlock._write_lock.release()

    class _WriteCtx:
        def __init__(self, rwlock: "AsyncRWLock"):
            self._rwlock = rwlock

        async def __aenter__(self):
            await self._rwlock._write_lock.acquire()
            return self

        async def __aexit__(self, *args):
            self._rwlock._write_lock.release()

    def reader(self) -> "_ReadCtx":
        return self._ReadCtx(self)

    def writer(self) -> "_WriteCtx":
        return self._WriteCtx(self)


@dataclass
class SharedKnowledgeBase:
    """Config/facts cache: read often, updated rarely."""
    facts: dict[str, str] = field(default_factory=dict)
    _rwlock: AsyncRWLock = field(default_factory=AsyncRWLock, init=False)
    _read_count: int = 0
    _write_count: int = 0

    async def get(self, key: str) -> str | None:
        async with self._rwlock.reader():
            self._read_count += 1
            return self.facts.get(key)

    async def set(self, key: str, value: str):
        async with self._rwlock.writer():
            self._write_count += 1
            self.facts[key] = value

    async def bulk_get(self, keys: list[str]) -> dict[str, str | None]:
        async with self._rwlock.reader():
            self._read_count += 1
            return {k: self.facts.get(k) for k in keys}

    def stats(self) -> dict:
        return {"reads": self._read_count, "writes": self._write_count}


class KnowledgeBaseAgent:
    def __init__(self, kb: SharedKnowledgeBase):
        self.client = AsyncAnthropic()
        self.kb = kb

    async def answer_with_context(self, question: str) -> str:
        # Read-heavy: multiple agents can read simultaneously
        context_keys = ["company_info", "product_docs", "faq"]
        context = await self.kb.bulk_get(context_keys)
        context_str = "\n".join(
            f"{k}: {v}" for k, v in context.items() if v
        )
        response = await self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=f"Use this context:\n{context_str}" if context_str else "Answer helpfully.",
            messages=[{"role": "user", "content": question}],
        )
        text = response.content[0].text
        # Occasionally update KB (write — exclusive)
        if "fact:" in question.lower():
            key, _, val = question.partition("fact:")
            await self.kb.set(key.strip(), val.strip())
        return text


async def demo_rwlock():
    kb = SharedKnowledgeBase()
    await kb.set("company_info", "Acme Corp makes widgets.")
    await kb.set("faq", "Returns accepted within 30 days.")

    agents = [KnowledgeBaseAgent(kb) for _ in range(5)]
    questions = [
        "What does the company do?",
        "What is the return policy?",
        "How do I contact support?",
    ] * 3
    await asyncio.gather(*[agents[i % 5].answer_with_context(q) for i, q in enumerate(questions)])
    print(f"KB stats: {kb.stats()}")
```

## Solution 3: Lock-Protected File Writing with Atomic Rename

Prevent concurrent agents from corrupting a shared log or state file.

```python
import asyncio
import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from anthropic import AsyncAnthropic

_FILE_LOCKS: dict[str, asyncio.Lock] = {}
_LOCK_REGISTRY = asyncio.Lock()


async def get_file_lock(path: str) -> asyncio.Lock:
    """Get or create a per-file lock."""
    async with _LOCK_REGISTRY:
        if path not in _FILE_LOCKS:
            _FILE_LOCKS[path] = asyncio.Lock()
        return _FILE_LOCKS[path]


async def atomic_json_update(path: str, update_fn):
    """Read → transform → write atomically with file lock + temp file rename."""
    lock = await get_file_lock(path)
    async with lock:
        # Read existing
        try:
            with open(path) as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}

        # Apply update
        new_data = update_fn(data)

        # Write to temp file then rename (atomic on POSIX)
        dir_ = os.path.dirname(path) or "."
        with tempfile.NamedTemporaryFile(
            mode="w", dir=dir_, suffix=".tmp", delete=False
        ) as tf:
            json.dump(new_data, tf, indent=2)
            tmp_path = tf.name

        os.replace(tmp_path, path)
        return new_data


class FileProtectedAgent:
    STATE_FILE = "/tmp/agent_shared_state.json"

    def __init__(self, agent_id: str):
        self.client = AsyncAnthropic()
        self.agent_id = agent_id

    async def chat_and_log(self, message: str) -> str:
        response = await self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": message}],
        )
        text = response.content[0].text

        # Safely update shared JSON state file
        def update(data: dict) -> dict:
            data.setdefault("sessions", {})
            data["sessions"].setdefault(self.agent_id, [])
            data["sessions"][self.agent_id].append({
                "timestamp": time.time(),
                "message": message[:50],
                "tokens": response.usage.output_tokens,
            })
            data["total_requests"] = data.get("total_requests", 0) + 1
            return data

        await atomic_json_update(self.STATE_FILE, update)
        return text


async def demo_file_lock():
    agents = [FileProtectedAgent(f"agent_{i}") for i in range(4)]
    messages = [f"Question {i}" for i in range(12)]
    await asyncio.gather(*[agents[i % 4].chat_and_log(m) for i, m in enumerate(messages)])

    with open(FileProtectedAgent.STATE_FILE) as f:
        state = json.load(f)
    print(f"Total requests logged: {state['total_requests']}")
    print(f"Agents: {list(state['sessions'].keys())}")
```

## Solution 4: asyncio.Condition for State-Dependent Waiting

Use Condition variables when one coroutine must wait for another to reach a certain state before proceeding.

```python
import asyncio
from dataclasses import dataclass, field
from enum import Enum
from anthropic import AsyncAnthropic


class AgentPhase(Enum):
    IDLE = "idle"
    LOADING = "loading"
    READY = "ready"
    PROCESSING = "processing"
    ERROR = "error"


@dataclass
class PhaseController:
    phase: AgentPhase = AgentPhase.IDLE
    _condition: asyncio.Condition = field(default_factory=asyncio.Condition, init=False)

    async def transition(self, new_phase: AgentPhase):
        async with self._condition:
            self.phase = new_phase
            self._condition.notify_all()

    async def wait_for_phase(self, target: AgentPhase, timeout: float = 30.0):
        async with self._condition:
            try:
                await asyncio.wait_for(
                    self._condition.wait_for(lambda: self.phase == target),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                raise TimeoutError(
                    f"Timed out waiting for phase {target.value}; currently {self.phase.value}"
                )

    async def wait_for_any(self, targets: set[AgentPhase], timeout: float = 30.0):
        async with self._condition:
            try:
                await asyncio.wait_for(
                    self._condition.wait_for(lambda: self.phase in targets),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                raise TimeoutError(f"Timed out waiting for phases {targets}")


class PhasedAgent:
    def __init__(self):
        self.client = AsyncAnthropic()
        self.controller = PhaseController()
        self._context: str = ""
        self._init_lock = asyncio.Lock()
        self._initialized = False

    async def initialize(self, context: str):
        async with self._init_lock:
            if self._initialized:
                return
            await self.controller.transition(AgentPhase.LOADING)
            # Simulate loading (e.g., fetching docs, warming cache)
            await asyncio.sleep(0.5)
            self._context = context
            self._initialized = True
            await self.controller.transition(AgentPhase.READY)

    async def chat(self, message: str) -> str:
        # Wait until agent is ready (initialized)
        await self.controller.wait_for_phase(AgentPhase.READY)
        await self.controller.transition(AgentPhase.PROCESSING)
        try:
            response = await self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                system=f"Context: {self._context}" if self._context else None,
                messages=[{"role": "user", "content": message}],
            )
            return response.content[0].text
        finally:
            await self.controller.transition(AgentPhase.READY)


async def demo_condition():
    agent = PhasedAgent()

    # Start initialization concurrently with queued requests
    async def deferred_request(msg: str) -> str:
        # Will wait until READY phase is reached
        return await agent.chat(msg)

    init_task = asyncio.create_task(agent.initialize("You are an expert assistant."))
    request_tasks = [
        asyncio.create_task(deferred_request(f"Question {i}"))
        for i in range(3)
    ]

    await init_task
    results = await asyncio.gather(*request_tasks)
    print(f"Phase-gated results: {len(results)}")
    print(f"Final phase: {agent.controller.phase.value}")
```

## Solution 5: Per-Key Locking for Fine-Grained Cache Control

Avoid locking the whole cache for every operation — use per-key locks so unrelated keys don't block each other.

```python
import asyncio
import time
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic


class KeyedLockManager:
    """Maintains a lock per cache key; creates/destroys lazily."""

    def __init__(self):
        self._locks: dict[str, asyncio.Lock] = {}
        self._ref_counts: dict[str, int] = {}
        self._meta_lock = asyncio.Lock()

    async def acquire(self, key: str) -> asyncio.Lock:
        async with self._meta_lock:
            if key not in self._locks:
                self._locks[key] = asyncio.Lock()
                self._ref_counts[key] = 0
            self._ref_counts[key] += 1
            lock = self._locks[key]
        await lock.acquire()
        return lock

    async def release(self, key: str, lock: asyncio.Lock):
        lock.release()
        async with self._meta_lock:
            self._ref_counts[key] -= 1
            if self._ref_counts[key] == 0:
                del self._locks[key]
                del self._ref_counts[key]

    class _KeyCtx:
        def __init__(self, manager: "KeyedLockManager", key: str):
            self._manager = manager
            self._key = key
            self._lock: asyncio.Lock | None = None

        async def __aenter__(self):
            self._lock = await self._manager.acquire(self._key)
            return self

        async def __aexit__(self, *args):
            if self._lock:
                await self._manager.release(self._key, self._lock)

    def lock(self, key: str) -> "_KeyCtx":
        return self._KeyCtx(self, key)


@dataclass
class PerKeyCache:
    _store: dict[str, tuple[str, float]] = field(default_factory=dict)  # key -> (value, expires_at)
    _lock_manager: KeyedLockManager = field(default_factory=KeyedLockManager, init=False)
    ttl_seconds: float = 300.0

    async def get_or_compute(self, key: str, compute_fn) -> str:
        """Cache-aside with per-key locking: only one task computes per key."""
        # Fast path: no lock needed for reads (check-then-recheck pattern)
        cached = self._store.get(key)
        if cached and time.time() < cached[1]:
            return cached[0]

        async with self._lock_manager.lock(key):
            # Re-check after acquiring lock (another task may have computed it)
            cached = self._store.get(key)
            if cached and time.time() < cached[1]:
                return cached[0]

            # Compute the value
            value = await compute_fn(key)
            self._store[key] = (value, time.time() + self.ttl_seconds)
            return value

    def invalidate(self, key: str):
        self._store.pop(key, None)

    @property
    def size(self) -> int:
        return len(self._store)


class PerKeyCachedAgent:
    def __init__(self):
        self.client = AsyncAnthropic()
        self.cache = PerKeyCache(ttl_seconds=60.0)
        self._compute_count = 0

    async def _compute(self, question: str) -> str:
        self._compute_count += 1
        response = await self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": question}],
        )
        return response.content[0].text

    async def ask(self, question: str) -> str:
        return await self.cache.get_or_compute(
            question,
            self._compute,
        )


async def demo_keyed_lock():
    agent = PerKeyCachedAgent()
    # Ask the same question many times concurrently — only one LLM call per unique question
    questions = ["What is Python?"] * 5 + ["What is Rust?"] * 5 + ["What is Go?"] * 3
    results = await asyncio.gather(*[agent.ask(q) for q in questions])
    print(f"Total questions: {len(questions)}, LLM calls made: {agent._compute_count}")
    print(f"Cache size: {agent.cache.size}")
    assert agent._compute_count == 3, f"Expected 3 unique computations, got {agent._compute_count}"
```

## Solution 6: Distributed Lock with Redis for Multi-Process Agents

When agents run in separate processes or containers, use Redis-based distributed locking.

```python
import asyncio
import time
import uuid
from dataclasses import dataclass
from anthropic import AsyncAnthropic

# Requires: redis[asyncio] (pip install redis)
# Falls back to in-memory lock if Redis unavailable


class RedisDistributedLock:
    """
    Redis-based distributed lock (Redlock-lite).
    One process holds the lock; others wait or fail-fast.
    """

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379",
        lock_prefix: str = "agent_lock:",
        default_ttl_ms: int = 5000,
    ):
        try:
            import redis.asyncio as aioredis
            self._redis = aioredis.from_url(redis_url, decode_responses=True)
            self._available = True
        except ImportError:
            self._redis = None
            self._available = False
        self._prefix = lock_prefix
        self._ttl_ms = default_ttl_ms
        self._local_fallback: dict[str, asyncio.Lock] = {}

    async def acquire(self, resource: str, ttl_ms: int | None = None, timeout: float = 10.0) -> str | None:
        """Returns lock token if acquired, None if failed."""
        if not self._available or self._redis is None:
            # Fallback to in-process lock
            lock = self._local_fallback.setdefault(resource, asyncio.Lock())
            try:
                await asyncio.wait_for(lock.acquire(), timeout=timeout)
                return "local_lock"
            except asyncio.TimeoutError:
                return None

        token = str(uuid.uuid4())
        key = f"{self._prefix}{resource}"
        ttl = ttl_ms or self._ttl_ms
        deadline = time.time() + timeout

        while time.time() < deadline:
            try:
                acquired = await self._redis.set(
                    key, token, px=ttl, nx=True  # NX = only set if not exists
                )
                if acquired:
                    return token
            except Exception:
                pass
            await asyncio.sleep(0.05)
        return None

    async def release(self, resource: str, token: str):
        if not self._available or self._redis is None:
            lock = self._local_fallback.get(resource)
            if lock and lock.locked():
                lock.release()
            return

        key = f"{self._prefix}{resource}"
        try:
            # Lua script: only delete if token matches (prevents releasing another's lock)
            lua_script = """
            if redis.call("get", KEYS[1]) == ARGV[1] then
                return redis.call("del", KEYS[1])
            else
                return 0
            end
            """
            await self._redis.eval(lua_script, 1, key, token)
        except Exception:
            pass

    class _LockCtx:
        def __init__(self, dist_lock: "RedisDistributedLock", resource: str, ttl_ms: int, timeout: float):
            self._dist = dist_lock
            self._resource = resource
            self._ttl_ms = ttl_ms
            self._timeout = timeout
            self._token: str | None = None

        async def __aenter__(self):
            self._token = await self._dist.acquire(self._resource, self._ttl_ms, self._timeout)
            if self._token is None:
                raise TimeoutError(f"Could not acquire distributed lock for '{self._resource}'")
            return self

        async def __aexit__(self, *args):
            if self._token:
                await self._dist.release(self._resource, self._token)

    def lock(self, resource: str, ttl_ms: int | None = None, timeout: float = 10.0) -> "_LockCtx":
        return self._LockCtx(self, resource, ttl_ms or self._ttl_ms, timeout)


_DIST_LOCK = RedisDistributedLock()


class DistributedAgent:
    """Agent that uses distributed locking for cross-process shared resource access."""

    def __init__(self, agent_id: str, dist_lock: RedisDistributedLock = _DIST_LOCK):
        self.client = AsyncAnthropic()
        self.agent_id = agent_id
        self.dist_lock = dist_lock

    async def exclusive_operation(self, resource_name: str, operation_fn) -> str:
        """Perform an operation exclusively — only one agent across all processes at a time."""
        async with self.dist_lock.lock(resource_name, ttl_ms=10_000, timeout=15.0):
            print(f"[{self.agent_id}] Acquired lock for '{resource_name}'")
            result = await operation_fn()
            print(f"[{self.agent_id}] Released lock for '{resource_name}'")
            return result

    async def update_shared_counter(self, counter_name: str) -> int:
        """Increment a shared counter atomically across processes."""
        import json
        counter_file = f"/tmp/counter_{counter_name}.json"

        async def do_increment():
            try:
                with open(counter_file) as f:
                    data = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                data = {"count": 0}
            data["count"] += 1
            data["last_updated_by"] = self.agent_id
            with open(counter_file, "w") as f:
                json.dump(data, f)
            return data["count"]

        return await self.exclusive_operation(f"counter:{counter_name}", do_increment)


async def demo_distributed_lock():
    agents = [DistributedAgent(f"agent_{i}") for i in range(4)]
    # All agents try to update the same counter concurrently
    tasks = [agent.update_shared_counter("pageviews") for agent in agents]
    results = await asyncio.gather(*tasks)
    print(f"Counter values after concurrent updates: {results}")
    assert len(set(results)) == len(results) or max(results) == len(agents), \
        "Counter should reflect all increments"
```

## Comparison Table

| Solution | Scope | Granularity | Distributed | Reader Concurrency | Best For |
|---|---|---|---|---|---|
| asyncio.Lock | In-process | Whole resource | No | No | Simple shared dict/counter |
| RW Lock | In-process | Whole resource | No | Yes (multiple readers) | Read-heavy shared config |
| File Lock + Atomic Rename | In-process | Per file | No (same process) | No | Shared log/state files |
| asyncio.Condition | In-process | Phase/state | No | N/A | Phase-gated initialization |
| Per-Key Lock | In-process | Per cache key | No | Yes (different keys) | Fine-grained cache population |
| Redis Distributed Lock | Multi-process | Per resource | Yes | No | Multi-container/multi-host |

**Recommended**: Use **asyncio.Lock** (Solution 1) for any shared in-process state — it's the simplest correct solution. Add **RW Lock** (Solution 2) if reads vastly outnumber writes. Use **Per-Key Lock** (Solution 5) for caches to maximize cache population throughput. Switch to **Redis Distributed Lock** (Solution 6) the moment agents run in more than one process.
