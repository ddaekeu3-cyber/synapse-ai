---
title: "Agent Doesn't Implement Partial Tool Result Handling"
description: "When an agent fires multiple tools in parallel and some fail, it either aborts the entire request or silently drops failed results; neither strategy is correct for tools with independent outputs."
category: reliability
difficulty: intermediate
tags: [tools, partial-failure, parallel, reliability, fallback, error-handling, asyncio]
---

# Agent Doesn't Implement Partial Tool Result Handling

## Problem

Agents that call multiple tools in parallel (search + database + API) treat the result set as all-or-nothing: if any tool fails, the whole request fails. In reality, many tool results are independently useful — a failed stock-price lookup shouldn't prevent the agent from returning valid weather and news data. Conversely, silently dropping failed results causes the model to reason over incomplete data without knowing it. The solution is explicit partial-failure handling: collect what succeeded, surface what failed, and let the model reason over both.

## Solution 1: Structured Partial Result Container

Collect tool results into a typed container that distinguishes successes from failures, then pass both to the model.

```python
import asyncio
from dataclasses import dataclass, field
from typing import Any
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

@dataclass
class ToolResult:
    name: str
    success: bool
    data: Any = None
    error: str | None = None

    def to_prompt_text(self) -> str:
        if self.success:
            return f"[{self.name}]: {self.data}"
        return f"[{self.name}]: UNAVAILABLE ({self.error})"

@dataclass
class PartialResults:
    results: list[ToolResult] = field(default_factory=list)

    @property
    def successes(self) -> list[ToolResult]:
        return [r for r in self.results if r.success]

    @property
    def failures(self) -> list[ToolResult]:
        return [r for r in self.results if not r.success]

    @property
    def all_failed(self) -> bool:
        return len(self.successes) == 0

    def to_context_block(self) -> str:
        lines = ["Tool results:"]
        for r in self.results:
            lines.append(f"  {r.to_prompt_text()}")
        if self.failures:
            lines.append(f"\nNote: {len(self.failures)} tool(s) failed and returned no data.")
        return "\n".join(lines)

async def run_tool_safe(name: str, coro) -> ToolResult:
    """Run a single tool coroutine; catch all exceptions."""
    try:
        data = await asyncio.wait_for(coro, timeout=10.0)
        return ToolResult(name=name, success=True, data=data)
    except asyncio.TimeoutError:
        return ToolResult(name=name, success=False, error="timeout")
    except Exception as exc:
        return ToolResult(name=name, success=False, error=str(exc))

async def fetch_weather(city: str) -> dict:
    await asyncio.sleep(0.1)
    return {"city": city, "temp": 22, "condition": "sunny"}

async def fetch_stock(ticker: str) -> dict:
    await asyncio.sleep(0.05)
    raise ConnectionError("Market data API unreachable")  # simulated failure

async def fetch_news(topic: str) -> list:
    await asyncio.sleep(0.15)
    return [{"headline": f"Latest on {topic}", "source": "Reuters"}]

async def agent_with_partial_results(city: str, ticker: str, topic: str) -> str:
    # Run all tools in parallel; collect partial results
    raw_results = await asyncio.gather(
        run_tool_safe("weather", fetch_weather(city)),
        run_tool_safe("stock", fetch_stock(ticker)),
        run_tool_safe("news", fetch_news(topic)),
    )
    partial = PartialResults(results=list(raw_results))

    if partial.all_failed:
        return "All data sources are currently unavailable. Please try again."

    context = partial.to_context_block()
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"{context}\n\nSummarize available information. Acknowledge any unavailable data sources.",
        }],
    )
    return resp.content[0].text
```

**When to use**: Any agent that calls multiple independent tools in parallel. This is the baseline: never let one tool failure abort the entire request.

---

## Solution 2: Criticality-Weighted Partial Failure

Mark each tool as required, preferred, or optional. Abort only when required tools fail; degrade gracefully for the rest.

```python
import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

class ToolCriticality(Enum):
    REQUIRED  = "required"   # failure aborts the request
    PREFERRED = "preferred"  # failure degrades quality but request continues
    OPTIONAL  = "optional"   # failure is silently tolerated

@dataclass
class CriticalTool:
    name: str
    coro_fn: Callable[[], Awaitable[Any]]
    criticality: ToolCriticality
    timeout: float = 10.0

@dataclass
class WeightedResult:
    name: str
    criticality: ToolCriticality
    success: bool
    data: Any = None
    error: str | None = None

async def run_weighted_tools(tools: list[CriticalTool]) -> tuple[list[WeightedResult], bool]:
    """
    Run tools in parallel. Returns (results, should_abort).
    should_abort is True if any REQUIRED tool failed.
    """
    async def _run(tool: CriticalTool) -> WeightedResult:
        try:
            data = await asyncio.wait_for(tool.coro_fn(), timeout=tool.timeout)
            return WeightedResult(name=tool.name, criticality=tool.criticality, success=True, data=data)
        except Exception as exc:
            return WeightedResult(name=tool.name, criticality=tool.criticality, success=False, error=str(exc))

    results = await asyncio.gather(*[_run(t) for t in tools])
    should_abort = any(
        r.criticality == ToolCriticality.REQUIRED and not r.success
        for r in results
    )
    return list(results), should_abort

async def agent_with_criticality(user_query: str) -> dict:
    tools = [
        CriticalTool(
            name="user_profile",
            coro_fn=lambda: asyncio.sleep(0.05) or {"user_id": "u1", "name": "Alice"},
            criticality=ToolCriticality.REQUIRED,   # can't personalize without this
        ),
        CriticalTool(
            name="product_catalog",
            coro_fn=lambda: asyncio.sleep(0.1) or [{"id": 1, "name": "Widget"}],
            criticality=ToolCriticality.PREFERRED,  # degrade without it
        ),
        CriticalTool(
            name="promotions",
            coro_fn=lambda: (_ for _ in ()).throw(ConnectionError("promo service down")),
            criticality=ToolCriticality.OPTIONAL,   # fine to skip
        ),
    ]

    results, abort = await run_weighted_tools(tools)

    if abort:
        failed_required = [r.name for r in results if r.criticality == ToolCriticality.REQUIRED and not r.success]
        return {"error": "required_tools_failed", "tools": failed_required}

    # Build context from successful results
    context_parts = []
    warnings = []
    for r in results:
        if r.success:
            context_parts.append(f"[{r.name}]: {r.data}")
        elif r.criticality == ToolCriticality.PREFERRED:
            warnings.append(f"{r.name} data unavailable")

    context = "\n".join(context_parts)
    if warnings:
        context += f"\n\nNote: {', '.join(warnings)} — respond with available information only."

    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": f"{context}\n\n{user_query}"}],
    )
    return {"response": resp.content[0].text, "degraded": bool(warnings)}
```

**When to use**: Agents with mixed-criticality tool pipelines (identity lookup required; recommendations optional). Criticality tagging makes the abort logic explicit and auditable.

---

## Solution 3: Retry Failed Tools While Serving Partial Results

Return partial results immediately; retry failed tools in the background and stream updates.

```python
import asyncio
from dataclasses import dataclass, field
from typing import Any, AsyncIterator
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

@dataclass
class RetryableResult:
    name: str
    data: Any = None
    error: str | None = None
    attempts: int = 0
    final: bool = False

    @property
    def available(self) -> bool:
        return self.data is not None

async def fetch_with_retry(
    name: str,
    coro_fn,
    max_retries: int = 2,
    delay: float = 0.5,
) -> RetryableResult:
    result = RetryableResult(name=name)
    for attempt in range(max_retries + 1):
        result.attempts = attempt + 1
        try:
            result.data = await asyncio.wait_for(coro_fn(), timeout=5.0)
            result.final = True
            return result
        except Exception as exc:
            result.error = str(exc)
            if attempt < max_retries:
                await asyncio.sleep(delay * (2 ** attempt))

    result.final = True
    return result

async def progressive_tool_results(
    tool_fns: dict[str, Any],
) -> AsyncIterator[dict[str, RetryableResult]]:
    """
    Yield partial result snapshots as tools complete.
    First yields whatever finishes quickly; yields updates as retries complete.
    """
    pending = {
        name: asyncio.create_task(fetch_with_retry(name, fn))
        for name, fn in tool_fns.items()
    }
    results: dict[str, RetryableResult] = {}

    while pending:
        done, _ = await asyncio.wait(pending.values(), return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            result = task.result()
            results[result.name] = result
            # Remove from pending
            pending = {k: v for k, v in pending.items() if not v.done()}
        yield dict(results)

async def streaming_partial_agent(query: str) -> str:
    tools = {
        "database": lambda: asyncio.sleep(0.05) or {"records": 42},
        "external_api": lambda: (_ for _ in ()).throw(TimeoutError()),  # will fail
        "cache": lambda: asyncio.sleep(0.01) or {"cached": True, "value": "cached_response"},
    }

    final_context = {}
    async for snapshot in progressive_tool_results(tools):
        final_context = snapshot  # keep taking updates

    # Build prompt from final snapshot
    parts = []
    unavailable = []
    for name, result in final_context.items():
        if result.available:
            parts.append(f"[{name}]: {result.data}")
        else:
            unavailable.append(f"{name} (failed after {result.attempts} attempts: {result.error})")

    context = "\n".join(parts)
    if unavailable:
        context += f"\n\nUnavailable: {', '.join(unavailable)}"

    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": f"{context}\n\n{query}"}],
    )
    return resp.content[0].text
```

**When to use**: Agents with slow tools where partial results are immediately useful. Progressive delivery reduces perceived latency even when some tools are slow or failing.

---

## Solution 4: Tool Result Substitution — Fill Gaps with Fallback Data

When a tool fails, substitute a cached value, default, or synthesized fallback so the model always sees a complete context.

```python
import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

@dataclass
class ToolWithFallback:
    name: str
    primary_fn: Callable[[], Awaitable[Any]]
    fallback_fn: Callable[[], Any] | None = None  # sync fallback
    cache: dict | None = None  # simple last-known-good cache
    timeout: float = 5.0

_last_good: dict[str, tuple[Any, float]] = {}

async def run_with_fallback(tool: ToolWithFallback) -> tuple[Any, str]:
    """
    Returns (value, source) where source is 'primary'|'cache'|'fallback'|'none'.
    """
    try:
        result = await asyncio.wait_for(tool.primary_fn(), timeout=tool.timeout)
        _last_good[tool.name] = (result, time.monotonic())
        return result, "primary"
    except Exception:
        pass

    # Try cache (last known good)
    if tool.name in _last_good:
        cached_value, cached_at = _last_good[tool.name]
        age = time.monotonic() - cached_at
        if age < 300:  # up to 5 minutes stale
            return cached_value, f"cache:{int(age)}s_old"

    # Try static fallback
    if tool.fallback_fn is not None:
        return tool.fallback_fn(), "fallback"

    return None, "none"

async def agent_with_substitution(user_query: str) -> dict:
    tools = [
        ToolWithFallback(
            name="exchange_rate",
            primary_fn=lambda: asyncio.sleep(0.1) or {"USD/EUR": 0.92},
            fallback_fn=lambda: {"USD/EUR": 0.90, "note": "stale fallback"},
        ),
        ToolWithFallback(
            name="inventory",
            primary_fn=lambda: (_ for _ in ()).throw(ConnectionError()),
            fallback_fn=lambda: {"status": "unknown", "note": "inventory system down"},
        ),
        ToolWithFallback(
            name="user_prefs",
            primary_fn=lambda: asyncio.sleep(0.05) or {"language": "en", "currency": "USD"},
            fallback_fn=None,  # no fallback — will return None
        ),
    ]

    pairs = await asyncio.gather(*[run_with_fallback(t) for t in tools])

    context_parts = []
    source_notes = []
    for tool, (value, source) in zip(tools, pairs):
        if value is not None:
            context_parts.append(f"[{tool.name}] ({source}): {value}")
            if source != "primary":
                source_notes.append(f"{tool.name} data is from {source}")
        else:
            context_parts.append(f"[{tool.name}]: UNAVAILABLE")

    context = "\n".join(context_parts)
    if source_notes:
        context += f"\n\nData quality note: {'; '.join(source_notes)}"

    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": f"{context}\n\n{user_query}"}],
    )
    return {
        "response": resp.content[0].text,
        "sources": {tool.name: src for tool, (_, src) in zip(tools, pairs)},
    }
```

**When to use**: Agents where a complete context produces better model output than an incomplete one. Fallback substitution keeps the model grounded even when primary data sources are down.

---

## Solution 5: Model-Aware Partial Context — Tell the Model What It Doesn't Know

Instead of hiding failures, explicitly tell the model which tools failed and instruct it to acknowledge uncertainty in its response.

```python
import asyncio
from dataclasses import dataclass
from typing import Any
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

PARTIAL_RESULT_SYSTEM = """You are a helpful assistant with access to real-time data tools.
When tool data is marked UNAVAILABLE, you must:
1. Acknowledge the missing data explicitly in your response.
2. State what you would have said if the data were available.
3. Never fabricate values for unavailable data.
4. Suggest the user retry if the missing data is important."""

@dataclass
class NamedResult:
    name: str
    description: str  # human-readable description of what this tool provides
    data: Any | None
    error: str | None

    def to_xml_block(self) -> str:
        if self.data is not None:
            return f"<tool name='{self.name}' description='{self.description}'>{self.data}</tool>"
        return f"<tool name='{self.name}' description='{self.description}' status='UNAVAILABLE' reason='{self.error}'/>"

async def safe_call(name: str, description: str, coro) -> NamedResult:
    try:
        data = await asyncio.wait_for(coro, timeout=8.0)
        return NamedResult(name=name, description=description, data=data, error=None)
    except asyncio.TimeoutError:
        return NamedResult(name=name, description=description, data=None, error="timeout")
    except Exception as exc:
        return NamedResult(name=name, description=description, data=None, error=str(exc))

async def model_aware_partial_agent(user_query: str) -> str:
    results = await asyncio.gather(
        safe_call("weather", "current weather conditions",
                  asyncio.sleep(0.1) and asyncio.coroutine(lambda: {"temp": 22})()),
        safe_call("flights", "available flight options",
                  asyncio.coroutine(lambda: (_ for _ in ()).throw(ConnectionError("flight API down")))()),
        safe_call("hotels", "hotel availability and prices",
                  asyncio.sleep(0.15) and asyncio.coroutine(lambda: [{"name": "Grand Hotel", "price": 150}])()),
    )

    tool_context = "\n".join(r.to_xml_block() for r in results)
    available = sum(1 for r in results if r.data is not None)
    unavailable = len(results) - available

    prompt = f"""<tool_results>
{tool_context}
</tool_results>

{available} of {len(results)} data sources available ({unavailable} unavailable).

User question: {user_query}"""

    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=PARTIAL_RESULT_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text

# Fix the coroutine calls for the demo
async def agent_demo(user_query: str) -> str:
    async def get_weather():
        await asyncio.sleep(0.1)
        return {"temp": 22, "condition": "sunny"}

    async def get_flights():
        raise ConnectionError("flight API down")

    async def get_hotels():
        await asyncio.sleep(0.15)
        return [{"name": "Grand Hotel", "price": 150}]

    results = await asyncio.gather(
        safe_call("weather", "current weather conditions", get_weather()),
        safe_call("flights", "available flight options", get_flights()),
        safe_call("hotels", "hotel availability and prices", get_hotels()),
    )

    tool_context = "\n".join(r.to_xml_block() for r in results)
    available = sum(1 for r in results if r.data is not None)
    unavailable = len(results) - available

    prompt = f"<tool_results>\n{tool_context}\n</tool_results>\n\n{available}/{len(results)} sources available.\n\n{user_query}"

    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=PARTIAL_RESULT_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text
```

**When to use**: Agents in consumer-facing products where honesty about data gaps is important. A model that knows it's missing data will hedge appropriately; one that doesn't know will confabulate.

---

## Solution 6: Partial Result Audit Log — Track Tool Failure Patterns Over Time

Log every tool success/failure with context so you can identify which tools fail most often and under what conditions.

```python
import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass
from typing import Any, Awaitable, Callable
from anthropic import AsyncAnthropic

client = AsyncAnthropic()
logger = logging.getLogger("tool_audit")

@dataclass
class ToolAuditRecord:
    request_id: str
    tool_name: str
    success: bool
    latency_ms: float
    error_type: str | None = None
    error_message: str | None = None
    timestamp: float = 0.0

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = time.time()

class AuditedToolRunner:
    def __init__(self):
        self._records: list[ToolAuditRecord] = []

    async def run(
        self,
        request_id: str,
        name: str,
        coro_fn: Callable[[], Awaitable[Any]],
        timeout: float = 10.0,
    ) -> tuple[Any, ToolAuditRecord]:
        start = time.monotonic()
        try:
            result = await asyncio.wait_for(coro_fn(), timeout=timeout)
            latency = (time.monotonic() - start) * 1000
            record = ToolAuditRecord(
                request_id=request_id,
                tool_name=name,
                success=True,
                latency_ms=round(latency, 2),
            )
            self._records.append(record)
            logger.info("tool_success", extra=asdict(record))
            return result, record

        except Exception as exc:
            latency = (time.monotonic() - start) * 1000
            record = ToolAuditRecord(
                request_id=request_id,
                tool_name=name,
                success=False,
                latency_ms=round(latency, 2),
                error_type=type(exc).__name__,
                error_message=str(exc)[:200],
            )
            self._records.append(record)
            logger.warning("tool_failure", extra=asdict(record))
            return None, record

    def failure_rate(self, tool_name: str | None = None) -> dict:
        records = self._records if tool_name is None else [
            r for r in self._records if r.tool_name == tool_name
        ]
        if not records:
            return {}
        failures = [r for r in records if not r.success]
        return {
            "total": len(records),
            "failures": len(failures),
            "failure_rate": round(len(failures) / len(records), 3),
            "top_errors": _count_errors(failures),
        }

def _count_errors(records: list[ToolAuditRecord]) -> dict:
    counts: dict[str, int] = {}
    for r in records:
        key = r.error_type or "unknown"
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda x: -x[1]))

runner = AuditedToolRunner()

async def audited_agent(request_id: str, user_query: str) -> dict:
    import secrets
    rid = request_id or secrets.token_hex(8)

    async def slow_db():
        await asyncio.sleep(0.2)
        return {"rows": 5}

    async def flaky_api():
        import random
        if random.random() < 0.5:
            raise ConnectionError("intermittent failure")
        return {"status": "ok"}

    db_result, db_record = await runner.run(rid, "database", slow_db)
    api_result, api_record = await runner.run(rid, "external_api", flaky_api)

    available = [x for x in [db_result, api_result] if x is not None]
    context = f"DB: {db_result}\nAPI: {api_result}"

    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": f"{context}\n\n{user_query}"}],
    )

    return {
        "response": resp.content[0].text,
        "tool_stats": {
            "database": runner.failure_rate("database"),
            "external_api": runner.failure_rate("external_api"),
        },
    }
```

**When to use**: Production agents. The audit log identifies chronic partial failures (e.g., "external_api fails 40% of the time between 2–4 AM") so you can fix root causes rather than just tolerating them.

---

## Comparison

| Solution | Granularity | Model Awareness | Retry | Fallback | Audit | Best For |
|---|---|---|---|---|---|---|
| Structured container | Type-safe | No | No | No | No | Getting started |
| Criticality weighting | Per-tool | No | No | No | No | Mixed-importance tools |
| Retry with progressive delivery | Per-tool | No | Yes | No | No | Slow tools with transient failures |
| Fallback substitution | Per-tool | Partial | No | Yes | No | Cache/default available |
| Model-aware partial context | Per-tool | Full | No | No | No | Consumer-facing agents |
| Audit log | Per-tool | No | No | No | Yes | Production reliability tracking |

**Rule of thumb**: Always use a structured result container (Solution 1) for parallel tool calls. Add criticality weighting (Solution 2) to define which failures are fatal. Add model-aware context (Solution 5) for consumer products where honesty matters.
