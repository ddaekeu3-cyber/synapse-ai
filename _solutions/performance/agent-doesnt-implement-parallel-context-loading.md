---
title: "Agent Doesn't Implement Parallel Context Loading"
description: "AI agents build their context sequentially — loading system config, then user history, then relevant documents one after another — paying full latency for each step when most are independent."
category: performance
difficulty: intermediate
tags: [context-loading, parallel, asyncio, latency, rag, session, startup]
---

# Agent Doesn't Implement Parallel Context Loading

## Problem

Before making an LLM call, agents typically load several independent pieces of context: user profile, conversation history, relevant documents from a vector store, tool schemas, and system configuration. Loaded sequentially, each fetch must complete before the next starts. If each takes 100ms, loading 5 items takes 500ms. Since these sources are independent, loading them in parallel reduces total time to ~max(individual_latencies) — often a 3–5× improvement.

## Solution 1: asyncio.gather for Independent Context Sources

The simplest form: gather all independent fetches concurrently.

```python
import asyncio
from dataclasses import dataclass
from typing import Any

@dataclass
class AgentContext:
    user_profile: dict
    conversation_history: list[dict]
    relevant_docs: list[dict]
    tool_schemas: list[dict]
    system_config: dict

async def fetch_user_profile(user_id: str) -> dict:
    await asyncio.sleep(0.08)   # simulate DB query
    return {"id": user_id, "name": "Alice", "plan": "pro", "language": "en"}

async def fetch_conversation_history(session_id: str, limit: int = 10) -> list[dict]:
    await asyncio.sleep(0.12)   # simulate Redis fetch
    return [{"role": "user", "content": f"Turn {i}"} for i in range(limit)]

async def fetch_relevant_docs(query: str, top_k: int = 3) -> list[dict]:
    await asyncio.sleep(0.15)   # simulate vector DB search
    return [{"id": f"doc_{i}", "content": f"Doc {i} about {query}"} for i in range(top_k)]

async def fetch_tool_schemas(tool_names: list[str]) -> list[dict]:
    await asyncio.sleep(0.05)   # simulate schema registry fetch
    return [{"name": t, "description": f"{t} tool"} for t in tool_names]

async def fetch_system_config() -> dict:
    await asyncio.sleep(0.03)   # simulate config fetch
    return {"model": "claude-sonnet-4-6", "max_tokens": 1024, "temperature": 0.0}

async def load_context_parallel(
    user_id: str,
    session_id: str,
    query: str,
    tool_names: list[str],
) -> AgentContext:
    """All sources fetched concurrently — total time = max(individual times)."""
    (
        user_profile,
        history,
        docs,
        tools,
        config,
    ) = await asyncio.gather(
        fetch_user_profile(user_id),
        fetch_conversation_history(session_id),
        fetch_relevant_docs(query),
        fetch_tool_schemas(tool_names),
        fetch_system_config(),
    )
    return AgentContext(
        user_profile=user_profile,
        conversation_history=history,
        relevant_docs=docs,
        tool_schemas=tools,
        system_config=config,
    )

# Sequential: 0.08 + 0.12 + 0.15 + 0.05 + 0.03 = 0.43s
# Parallel:   max(0.08, 0.12, 0.15, 0.05, 0.03) = 0.15s  (2.9× faster)
```

**When to use**: Any agent with 2+ independent context sources. The improvement is always ≥ the second-longest fetch time.

---

## Solution 2: Dependency-Aware Parallel Loading with Task Graph

Some context sources depend on others (e.g., relevant docs need the user query from profile). Model the dependency graph explicitly.

```python
import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

@dataclass
class ContextTask:
    name: str
    fn: Callable[..., Awaitable[Any]]
    dependencies: list[str] = field(default_factory=list)

class ContextTaskGraph:
    """Execute context loading tasks in dependency order, maximizing parallelism."""

    def __init__(self):
        self._tasks: dict[str, ContextTask] = {}

    def add(self, task: ContextTask):
        self._tasks[task.name] = task

    async def execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
        results: dict[str, Any] = dict(inputs)
        running: dict[str, asyncio.Task] = {}
        completed: set[str] = set(inputs.keys())
        pending: set[str] = set(self._tasks.keys())

        async def run_task(name: str, task: ContextTask):
            # Pass results of dependencies as kwargs
            dep_results = {dep: results[dep] for dep in task.dependencies}
            result = await task.fn(**dep_results)
            results[name] = result
            completed.add(name)

        while pending:
            # Find tasks whose dependencies are all completed
            ready = [
                name for name in pending
                if all(dep in completed for dep in self._tasks[name].dependencies)
                and name not in running
            ]

            for name in ready:
                running[name] = asyncio.create_task(run_task(name, self._tasks[name]))
                pending.discard(name)

            if not running:
                raise RuntimeError(f"Deadlock: pending={pending}, completed={completed}")

            # Wait for at least one task to finish
            done, _ = await asyncio.wait(
                running.values(), return_when=asyncio.FIRST_COMPLETED
            )
            for task_obj in done:
                finished_name = next(n for n, t in running.items() if t is task_obj)
                del running[finished_name]

        return results

# Setup context loading graph
graph = ContextTaskGraph()

graph.add(ContextTask("user_profile", lambda: fetch_user_profile("user_123"), []))
graph.add(ContextTask("system_config", lambda: fetch_system_config(), []))
# These depend on user_profile being loaded first
graph.add(ContextTask(
    "conversation_history",
    lambda user_profile: fetch_conversation_history(user_profile["id"]),
    dependencies=["user_profile"],
))
graph.add(ContextTask(
    "relevant_docs",
    lambda user_profile: fetch_relevant_docs(user_profile.get("last_query", "")),
    dependencies=["user_profile"],
))
# Tool schemas depend on system config
graph.add(ContextTask(
    "tool_schemas",
    lambda system_config: fetch_tool_schemas(system_config.get("enabled_tools", [])),
    dependencies=["system_config"],
))

async def demo():
    results = await graph.execute({})
    # Execution order: {user_profile, system_config} → {history, docs, tool_schemas}
    print(f"Loaded: {list(results.keys())}")
```

**When to use**: Complex context loading where some sources depend on results from others.

---

## Solution 3: Timeout-Bounded Parallel Loading with Partial Context

Load all sources in parallel but don't let a slow source block the LLM call — use whatever is ready by the deadline.

```python
import asyncio
import time
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

@dataclass
class PartialContext:
    loaded: dict[str, Any] = field(default_factory=dict)
    timed_out: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    load_time_ms: float = 0.0

    def get(self, key: str, default=None):
        return self.loaded.get(key, default)

    @property
    def is_complete(self) -> bool:
        return not self.timed_out and not self.errors

async def load_with_deadline(
    sources: dict[str, tuple[Callable, float]],  # name → (fetch_fn, individual_timeout)
    global_deadline_ms: float = 300.0,
) -> PartialContext:
    """
    Load all sources concurrently; use whatever is ready by the global deadline.
    Sources that time out produce empty results (agent continues with partial context).
    """
    ctx = PartialContext()
    t0 = time.monotonic()
    deadline = t0 + global_deadline_ms / 1000

    async def timed_fetch(name: str, fn: Callable, timeout: float):
        try:
            result = await asyncio.wait_for(fn(), timeout=timeout)
            ctx.loaded[name] = result
        except asyncio.TimeoutError:
            ctx.timed_out.append(name)
            logger.warning("context_load_timeout", extra={"source": name, "timeout_s": timeout})
        except Exception as e:
            ctx.errors[name] = str(e)[:100]
            logger.error("context_load_error", extra={"source": name, "error": str(e)[:100]})

    tasks = [
        asyncio.create_task(timed_fetch(name, fn, timeout))
        for name, (fn, timeout) in sources.items()
    ]

    # Wait until global deadline
    remaining = deadline - time.monotonic()
    if remaining > 0:
        await asyncio.wait(tasks, timeout=remaining)

    # Cancel any still-running tasks
    for t in tasks:
        if not t.done():
            t.cancel()
            await asyncio.gather(t, return_exceptions=True)

    ctx.load_time_ms = (time.monotonic() - t0) * 1000
    return ctx

# Usage
async def load_agent_context(user_id: str, query: str) -> PartialContext:
    sources = {
        "user_profile":   (lambda: fetch_user_profile(user_id), 1.0),
        "history":        (lambda: fetch_conversation_history(user_id), 0.5),
        "relevant_docs":  (lambda: fetch_relevant_docs(query), 0.8),
        "tool_schemas":   (lambda: fetch_tool_schemas(["search"]), 0.3),
    }
    ctx = await load_with_deadline(sources, global_deadline_ms=400)
    if ctx.timed_out:
        logger.warning("partial_context", extra={"missing": ctx.timed_out})
    return ctx
```

**When to use**: Agents with latency SLOs. Never let a slow context source cause a timeout cascade.

---

## Solution 4: Warm Context Cache — Pre-Load Before User Sends Message

Pre-load context for recently active users during idle time so it's ready when the message arrives.

```python
import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass, field

@dataclass
class CachedContext:
    data: dict
    loaded_at: float = field(default_factory=time.monotonic)
    ttl: float = 60.0

    @property
    def is_fresh(self) -> bool:
        return time.monotonic() - self.loaded_at < self.ttl

class WarmContextCache:
    def __init__(self, max_entries: int = 100, ttl: float = 60.0):
        self._cache: OrderedDict[str, CachedContext] = OrderedDict()
        self._loading: dict[str, asyncio.Task] = {}
        self._max = max_entries
        self._ttl = ttl

    def _evict_if_full(self):
        while len(self._cache) >= self._max:
            self._cache.popitem(last=False)  # remove oldest

    async def get(self, key: str, load_fn) -> dict:
        """Return cached context or load it now."""
        cached = self._cache.get(key)
        if cached and cached.is_fresh:
            self._cache.move_to_end(key)  # LRU refresh
            return cached.data

        # Check if already loading
        if key in self._loading:
            return await self._loading[key]

        # Load now
        task = asyncio.create_task(load_fn())
        self._loading[key] = task
        try:
            data = await task
            self._evict_if_full()
            self._cache[key] = CachedContext(data=data, ttl=self._ttl)
            return data
        finally:
            self._loading.pop(key, None)

    def warm(self, key: str, load_fn) -> asyncio.Task | None:
        """Start background loading (fire-and-forget)."""
        cached = self._cache.get(key)
        if cached and cached.is_fresh:
            return None
        if key in self._loading:
            return self._loading[key]

        async def _load():
            try:
                data = await load_fn()
                self._evict_if_full()
                self._cache[key] = CachedContext(data=data, ttl=self._ttl)
            except Exception:
                pass  # background failure is non-fatal

        task = asyncio.create_task(_load())
        self._loading[key] = task
        return task

context_cache = WarmContextCache(max_entries=200, ttl=120)

# Called when a user starts typing (before sending the message)
async def on_user_typing(user_id: str, session_id: str):
    """Pre-warm context so it's ready when the message arrives."""
    context_cache.warm(
        f"user:{user_id}",
        lambda: fetch_user_profile(user_id),
    )
    context_cache.warm(
        f"history:{session_id}",
        lambda: fetch_conversation_history(session_id),
    )

# Called when message is received — context is usually already cached
async def on_message(user_id: str, session_id: str, query: str) -> dict:
    profile, history = await asyncio.gather(
        context_cache.get(f"user:{user_id}", lambda: fetch_user_profile(user_id)),
        context_cache.get(f"history:{session_id}", lambda: fetch_conversation_history(session_id)),
    )
    docs = await fetch_relevant_docs(query)  # query-specific, can't pre-warm
    return {"profile": profile, "history": history, "docs": docs}
```

**When to use**: Chat agents where users type before sending. Pre-warming eliminates most context load latency.

---

## Solution 5: Streaming Context Injection — Don't Wait for All Context

Inject context pieces as they load, making the LLM call with a progressive context that grows over time.

```python
import asyncio
import time
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

async def progressive_context_call(
    user_message: str,
    user_id: str,
    query: str,
    deadline_ms: float = 500.0,
) -> str:
    """
    Start LLM call as soon as minimal context is available;
    inject additional context only if it loads in time.
    """
    # Core context (must have — small and fast)
    system_config = await asyncio.wait_for(fetch_system_config(), timeout=0.5)

    # Start background context loads
    history_task = asyncio.create_task(fetch_conversation_history(user_id))
    docs_task = asyncio.create_task(fetch_relevant_docs(query))
    profile_task = asyncio.create_task(fetch_user_profile(user_id))

    # Build initial minimal context
    messages = [{"role": "user", "content": user_message}]
    system = "You are a helpful assistant."

    # Wait briefly for fast sources
    deadline = time.monotonic() + 0.15  # 150ms for fast context
    for task, build_fn in [
        (profile_task, lambda r: f" The user's name is {r.get('name', 'User')}."),
        (history_task, None),  # history goes into messages
    ]:
        remaining = deadline - time.monotonic()
        if remaining > 0:
            try:
                result = await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
                if build_fn:
                    system += build_fn(result)
                elif isinstance(result, list) and result:
                    # Prepend last 3 turns of history
                    messages = result[-3:] + messages
            except asyncio.TimeoutError:
                pass  # skip — context wasn't ready in time

    # Check if docs are ready
    remaining = deadline - time.monotonic()
    if remaining > 0:
        try:
            docs = await asyncio.wait_for(asyncio.shield(docs_task), timeout=remaining)
            doc_text = "\n".join(d["content"][:200] for d in docs[:2])
            messages.insert(0, {"role": "user", "content": f"Relevant context:\n{doc_text}"})
            messages.insert(1, {"role": "assistant", "content": "I've noted the relevant context."})
        except asyncio.TimeoutError:
            pass  # proceed without docs

    # Make the LLM call with whatever context we have
    resp = await client.messages.create(
        model=system_config.get("model", "claude-haiku-4-5-20251001"),
        max_tokens=1024,
        system=system,
        messages=messages,
    )

    # Cancel background tasks we didn't use
    for task in [history_task, docs_task, profile_task]:
        if not task.done():
            task.cancel()

    return resp.content[0].text
```

**When to use**: Agents with hard latency SLOs. Better to answer with partial context than miss the deadline.

---

## Solution 6: Context Load Profiler — Measure and Optimize Hot Paths

Profile which context sources are slowest; use the data to prioritize optimization.

```python
import asyncio
import time
import logging
from dataclasses import dataclass, field
from collections import defaultdict, deque

logger = logging.getLogger(__name__)

@dataclass
class SourceStats:
    name: str
    latencies_ms: deque = field(default_factory=lambda: deque(maxlen=100))
    errors: int = 0
    calls: int = 0

    @property
    def p50_ms(self) -> float:
        if not self.latencies_ms:
            return 0.0
        s = sorted(self.latencies_ms)
        return s[int(len(s) * 0.5)]

    @property
    def p95_ms(self) -> float:
        if not self.latencies_ms:
            return 0.0
        s = sorted(self.latencies_ms)
        return s[int(len(s) * 0.95)]

    @property
    def error_rate(self) -> float:
        return self.errors / max(self.calls, 1)

class ContextLoadProfiler:
    def __init__(self):
        self._stats: dict[str, SourceStats] = defaultdict(lambda: SourceStats(name=""))

    async def timed_fetch(self, name: str, fn) -> Any:
        stats = self._stats[name]
        stats.name = name
        stats.calls += 1
        t0 = time.monotonic()
        try:
            result = await fn()
            stats.latencies_ms.append((time.monotonic() - t0) * 1000)
            return result
        except Exception as e:
            stats.errors += 1
            stats.latencies_ms.append((time.monotonic() - t0) * 1000)
            raise

    async def load_all(self, sources: dict[str, Callable]) -> dict[str, Any]:
        tasks = {name: asyncio.create_task(self.timed_fetch(name, fn)) for name, fn in sources.items()}
        results = {}
        for name, task in tasks.items():
            try:
                results[name] = await task
            except Exception as e:
                results[name] = None
        return results

    def report(self) -> list[dict]:
        rows = [
            {
                "source": s.name,
                "calls": s.calls,
                "p50_ms": round(s.p50_ms, 1),
                "p95_ms": round(s.p95_ms, 1),
                "error_rate": round(s.error_rate, 3),
                "recommendation": "cache" if s.p95_ms > 200 else "ok",
            }
            for s in sorted(self._stats.values(), key=lambda x: x.p95_ms, reverse=True)
        ]
        logger.info("context_load_profile", extra={"sources": rows})
        return rows

profiler = ContextLoadProfiler()

async def profiled_context_load(user_id: str, query: str) -> dict:
    return await profiler.load_all({
        "user_profile": lambda: fetch_user_profile(user_id),
        "history": lambda: fetch_conversation_history(user_id),
        "docs": lambda: fetch_relevant_docs(query),
        "config": lambda: fetch_system_config(),
    })

# Call profiler.report() periodically to find bottlenecks
```

**When to use**: Optimizing an existing agent. Profile first, then apply parallelism/caching to the slowest sources.

---

## Comparison

| Solution | Parallelism | Handles Dependencies | Partial Results | Cache | Profiling | Best For |
|---|---|---|---|---|---|---|
| asyncio.gather | Full | No | No | No | No | Simple independent sources |
| Task graph | Partial (respects DAG) | Yes | No | No | No | Dependent context sources |
| Timeout-bounded | Full | No | Yes | No | No | Latency SLO enforcement |
| Warm cache | Full | No | No | Yes | No | Chat agents with typing indicator |
| Progressive injection | Partial | No | Yes | No | No | Hard latency budget |
| Load profiler | Full | No | No | No | Yes | Identifying bottlenecks |

**Rule of thumb**: Always use `asyncio.gather` for independent sources (free 2–5× speedup). Add timeout bounds to prevent slow sources from blocking the LLM call. Warm the cache on user activity signals.
