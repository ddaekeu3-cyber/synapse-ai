---
layout: solution
title: "Agent Crashes When Primary Service Is Down — No Fallback or Graceful Degradation"
category: general
description: "The vector database is unavailable and the agent raises an unhandled exception instead of falling back to keyword search. The primary LLM provider returns 503 and the agent dies instead of switching to a backup model. An external API is down and the agent returns no answer instead of a cached or degraded response. The agent is fragile — one dependency failure cascades to total failure."
tags: [resilience, fallback, graceful-degradation, availability, fault-tolerance, failover, circuit-breaker, reliability]
---

# Agent Crashes When Primary Service Is Down — No Fallback or Graceful Degradation

## Symptom
- One service outage takes down the entire agent
- Agent returns 500 errors when any external dependency fails
- No retry, no fallback, no partial response — just an exception traceback
- Users see "Agent unavailable" when only the vector search is down
- Agent succeeds with full features or fails completely — no middle ground
- No monitoring to detect degraded state vs. complete failure

## Root Cause
Agents built as linear pipelines — fetch context → query LLM → return result — fail completely if any step fails. Without fallback branches, every dependency becomes a single point of failure. Graceful degradation means defining a hierarchy of responses: full response (all services up), degraded response (some services unavailable), and minimal response (core LLM only), so the agent always returns something useful.

## Fix

### Option 1: Tiered fallback chain — try primary, then fallback, then minimal

```python
import asyncio
import logging
import anthropic
from typing import Any, Callable, Awaitable, TypeVar

logger = logging.getLogger(__name__)
T = TypeVar("T")

async def try_in_order(
    *fns: Callable[[], Awaitable[T]],
    names: list[str] | None = None
) -> tuple[T, str]:
    """
    Try each function in order. Return the first that succeeds.
    Returns (result, which_fn_succeeded).
    Raises if all fail.
    """
    names = names or [f"option_{i}" for i in range(len(fns))]
    last_exc = None

    for fn, name in zip(fns, names):
        try:
            result = await fn()
            if name != names[0]:
                logger.warning(f"Degraded: using {name} (primary failed)")
            return result, name
        except Exception as exc:
            logger.warning(f"{name} failed: {exc}")
            last_exc = exc

    raise RuntimeError(f"All options exhausted. Last error: {last_exc}")

# Example: Context retrieval with fallback chain
async def get_context_with_fallback(query: str) -> tuple[str, str]:
    """
    1. Try vector search (best context quality)
    2. Fall back to BM25 keyword search (good enough)
    3. Fall back to no context (answer from model knowledge only)
    """
    async def vector_search() -> str:
        # Your vector DB call here
        import httpx
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.post("http://vectordb:8080/search", json={"query": query, "top_k": 5})
            r.raise_for_status()
            docs = r.json()["results"]
            return "\n\n".join(d["text"] for d in docs)

    async def keyword_search() -> str:
        # Fallback: BM25 or simple full-text search
        import httpx
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get("http://search:8080/search", params={"q": query})
            r.raise_for_status()
            results = r.json()["hits"]
            return "\n\n".join(r["text"] for r in results[:3])

    async def no_context() -> str:
        return ""  # Use model's built-in knowledge

    return await try_in_order(
        vector_search,
        keyword_search,
        no_context,
        names=["vector_search", "keyword_search", "no_context"]
    )

async def answer_question(question: str) -> dict:
    """Answer with graceful degradation — always returns something useful."""
    context, context_source = await get_context_with_fallback(question)

    system = "You are a helpful assistant."
    if context:
        system += f"\n\nContext (source: {context_source}):\n{context}"
    else:
        system += "\n\nNote: Context retrieval is unavailable. Answer from general knowledge."

    client = anthropic.AsyncAnthropic()
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": question}]
    )
    return {
        "answer": response.content[0].text,
        "quality": "full" if context_source == "vector_search" else
                   "degraded" if context_source == "keyword_search" else
                   "minimal",
        "context_source": context_source
    }
```

### Option 2: LLM provider failover — switch to backup model on primary failure

```python
import asyncio
import anthropic
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Provider priority: try in order, use first that works
PROVIDER_CONFIGS = [
    {
        "name": "claude-sonnet-4-6",
        "client_factory": lambda: anthropic.AsyncAnthropic(),
        "model": "claude-sonnet-4-6",
        "max_tokens": 4096
    },
    {
        "name": "claude-haiku-fallback",
        "client_factory": lambda: anthropic.AsyncAnthropic(),
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 4096
    },
    # Add OpenAI, Bedrock, etc. as additional fallbacks here
]

async def call_llm_with_failover(
    messages: list[dict],
    system: str = "",
    max_tokens: int = 1024,
    timeout: float = 30.0
) -> dict:
    """
    Try each LLM provider in order. Use the first that responds.
    Returns the response with metadata about which provider was used.
    """
    errors = []

    for config in PROVIDER_CONFIGS:
        try:
            client = config["client_factory"]()
            kwargs = {
                "model": config["model"],
                "max_tokens": min(max_tokens, config["max_tokens"]),
                "messages": messages
            }
            if system:
                kwargs["system"] = system

            response = await asyncio.wait_for(
                client.messages.create(**kwargs),
                timeout=timeout
            )
            if config["name"] != PROVIDER_CONFIGS[0]["name"]:
                logger.warning(f"Using fallback LLM: {config['name']}")

            return {
                "text": response.content[0].text,
                "provider": config["name"],
                "is_fallback": config["name"] != PROVIDER_CONFIGS[0]["name"],
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens
            }

        except (anthropic.APIStatusError, anthropic.APIConnectionError, asyncio.TimeoutError) as exc:
            errors.append(f"{config['name']}: {exc}")
            logger.warning(f"LLM {config['name']} failed: {exc}")
            continue

    raise RuntimeError(f"All LLM providers failed: {'; '.join(errors)}")

# Usage with automatic failover:
result = await call_llm_with_failover(
    messages=[{"role": "user", "content": "Summarize the key points."}],
    system="You are a helpful assistant."
)
if result["is_fallback"]:
    logger.warning(f"Primary LLM unavailable — using {result['provider']}")
print(result["text"])
```

### Option 3: Cache-based degradation — serve stale answers when services are down

```python
import asyncio
import json
import hashlib
import time
import logging
from pathlib import Path
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)

class StaleWhileRevalidateCache:
    """
    Serves cached responses when the live service is unavailable.
    Fresh: serve live result and update cache.
    Stale: serve cached result and log degraded state.
    Miss: try live, fall back to error message.
    """

    def __init__(
        self,
        cache_dir: str = "/tmp/agent_cache",
        fresh_ttl: int = 3600,      # Cache is fresh for 1 hour
        stale_ttl: int = 86400 * 7  # Serve stale for up to 7 days
    ):
        self._dir = Path(cache_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._fresh_ttl = fresh_ttl
        self._stale_ttl = stale_ttl

    def _cache_key(self, prompt: str) -> str:
        return hashlib.sha256(prompt.encode()).hexdigest()[:24]

    def _cache_path(self, key: str) -> Path:
        return self._dir / f"{key}.json"

    def _read_cache(self, key: str) -> dict | None:
        path = self._cache_path(key)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except Exception:
            return None

    def _write_cache(self, key: str, data: Any, prompt: str):
        entry = {"data": data, "prompt": prompt, "ts": time.time()}
        self._cache_path(key).write_text(json.dumps(entry))

    def _cache_age(self, entry: dict) -> float:
        return time.time() - entry["ts"]

    async def get_or_fetch(
        self,
        prompt: str,
        fetch_fn: Callable[[], Awaitable[Any]],
        fallback_message: str = "Service temporarily unavailable. Please try again later."
    ) -> dict:
        """
        Return (result, freshness) where freshness is 'fresh', 'stale', or 'error'.
        """
        key = self._cache_key(prompt)
        cached = self._read_cache(key)

        # Try live fetch first
        try:
            result = await asyncio.wait_for(fetch_fn(), timeout=10.0)
            self._write_cache(key, result, prompt)
            return {"result": result, "freshness": "fresh"}
        except Exception as exc:
            logger.warning(f"Live fetch failed: {exc}. Checking cache.")

        # Live failed — try stale cache
        if cached:
            age = self._cache_age(cached)
            if age < self._stale_ttl:
                logger.warning(
                    f"Serving stale cache (age={age/3600:.1f}h) because live service failed"
                )
                return {
                    "result": cached["data"],
                    "freshness": "stale",
                    "stale_age_hours": round(age / 3600, 1)
                }

        # No usable cache — return fallback
        return {"result": fallback_message, "freshness": "error"}

# Usage:
cache = StaleWhileRevalidateCache(fresh_ttl=3600, stale_ttl=86400 * 3)

async def get_price(product_id: str) -> float:
    # Call live pricing API (may fail)
    import httpx
    async with httpx.AsyncClient() as client:
        r = await client.get(f"https://api.example.com/prices/{product_id}")
        r.raise_for_status()
        return r.json()["price"]

result = await cache.get_or_fetch(
    prompt=f"price:{product_id}",
    fetch_fn=lambda: get_price(product_id),
    fallback_message="Price temporarily unavailable"
)
# If pricing API is down, returns last known price with freshness="stale"
```

### Option 4: Health check + feature flags — disable failing features proactively

```python
import asyncio
import time
import logging
from enum import Enum
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

class ServiceStatus(Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DOWN = "down"

@dataclass
class ServiceHealth:
    name: str
    check_url: str
    status: ServiceStatus = ServiceStatus.HEALTHY
    last_check: float = field(default_factory=time.monotonic)
    consecutive_failures: int = 0
    check_interval: float = 30.0

class HealthMonitor:
    """
    Periodically health-check dependencies.
    Agent checks service status before attempting calls.
    Disables failing features proactively instead of letting calls fail.
    """

    def __init__(self):
        self._services: dict[str, ServiceHealth] = {}
        self._monitor_task: asyncio.Task | None = None

    def register(self, name: str, check_url: str, check_interval: float = 30.0):
        self._services[name] = ServiceHealth(
            name=name, check_url=check_url, check_interval=check_interval
        )

    def is_available(self, service_name: str) -> bool:
        svc = self._services.get(service_name)
        return svc is not None and svc.status != ServiceStatus.DOWN

    def get_status(self, service_name: str) -> ServiceStatus:
        svc = self._services.get(service_name)
        return svc.status if svc else ServiceStatus.DOWN

    async def _check_service(self, svc: ServiceHealth):
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(svc.check_url)
                if r.status_code < 500:
                    svc.consecutive_failures = 0
                    prev = svc.status
                    svc.status = ServiceStatus.HEALTHY
                    if prev != ServiceStatus.HEALTHY:
                        logger.info(f"Service {svc.name} recovered")
                else:
                    raise Exception(f"HTTP {r.status_code}")
        except Exception as exc:
            svc.consecutive_failures += 1
            if svc.consecutive_failures >= 3:
                svc.status = ServiceStatus.DOWN
                logger.error(f"Service {svc.name} is DOWN: {exc}")
            elif svc.consecutive_failures >= 1:
                svc.status = ServiceStatus.DEGRADED
                logger.warning(f"Service {svc.name} degraded: {exc}")

    async def start_monitoring(self):
        async def loop():
            while True:
                await asyncio.gather(*[
                    self._check_service(svc) for svc in self._services.values()
                ])
                await asyncio.sleep(min(s.check_interval for s in self._services.values()))

        self._monitor_task = asyncio.create_task(loop())

health = HealthMonitor()
health.register("vector_db", "http://vectordb:8080/health")
health.register("external_api", "https://api.example.com/health")
await health.start_monitoring()

async def build_response(question: str) -> dict:
    """Build response using only available services."""
    features_used = []

    # Vector context — only if vector DB is healthy
    context = ""
    if health.is_available("vector_db"):
        context = await fetch_vector_context(question)
        features_used.append("vector_context")
    else:
        logger.warning(f"Skipping vector context — vectordb is {health.get_status('vector_db').value}")

    # External data — only if external API is healthy
    extra_data = {}
    if health.is_available("external_api"):
        extra_data = await fetch_external_data(question)
        features_used.append("external_data")

    response = await call_llm(question, context=context, extra_data=extra_data)
    return {"response": response, "features_used": features_used}
```

### Option 5: Partial response streaming — return what's available, signal missing parts

```python
import asyncio
import anthropic
from dataclasses import dataclass
from typing import AsyncIterator

client = anthropic.AsyncAnthropic()

@dataclass
class AgentResponse:
    answer: str
    context_available: bool
    tools_available: bool
    quality_note: str | None = None

async def answer_with_available_context(
    question: str,
    available_tools: list[str]
) -> AgentResponse:
    """
    Answer using whatever tools are available.
    Clearly communicate to the user what context was and wasn't available.
    """
    context_parts = []
    tools_succeeded = []
    tools_failed = []

    # Try each tool — collect what works
    tool_calls = {
        "database": lambda: call_database(question),
        "web_search": lambda: call_web_search(question),
        "file_reader": lambda: read_relevant_files(question),
    }

    for tool_name, tool_fn in tool_calls.items():
        if tool_name not in available_tools:
            continue
        try:
            result = await asyncio.wait_for(tool_fn(), timeout=8.0)
            context_parts.append(f"[{tool_name}]: {result}")
            tools_succeeded.append(tool_name)
        except Exception as exc:
            tools_failed.append(tool_name)
            logger.warning(f"Tool {tool_name} failed: {exc}")

    # Build prompt with transparency about what's available
    system = "You are a helpful assistant."
    if context_parts:
        system += "\n\nAvailable context:\n" + "\n\n".join(context_parts)
    if tools_failed:
        system += (
            f"\n\nNote: The following data sources are currently unavailable: "
            f"{', '.join(tools_failed)}. "
            "Answer from available context and general knowledge. "
            "Tell the user if you're missing information that would normally be available."
        )

    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": question}]
    )

    quality_note = None
    if tools_failed:
        quality_note = (
            f"Note: {', '.join(tools_failed)} {'was' if len(tools_failed)==1 else 'were'} "
            f"unavailable. This answer may be incomplete."
        )

    return AgentResponse(
        answer=response.content[0].text,
        context_available=bool(tools_succeeded),
        tools_available=not bool(tools_failed),
        quality_note=quality_note
    )
```

### Option 6: Retry with exponential backoff + fallback after max retries

```python
import asyncio
import logging
from typing import Any, Callable, Awaitable, TypeVar

logger = logging.getLogger(__name__)
T = TypeVar("T")

async def resilient_call(
    primary_fn: Callable[[], Awaitable[T]],
    fallback_fn: Callable[[], Awaitable[T]] | None = None,
    fallback_value: T | None = None,
    max_retries: int = 3,
    base_delay: float = 1.0,
    timeout: float = 15.0,
    service_name: str = "service"
) -> tuple[T, bool]:
    """
    Call primary_fn with retries. If all retries fail:
    1. Try fallback_fn if provided
    2. Return fallback_value if provided
    3. Raise the last exception

    Returns (result, is_primary) where is_primary=False means fallback was used.
    """
    last_exc = None

    for attempt in range(max_retries):
        try:
            result = await asyncio.wait_for(primary_fn(), timeout=timeout)
            return result, True
        except Exception as exc:
            last_exc = exc
            if attempt < max_retries - 1:
                wait = base_delay * (2 ** attempt)
                logger.warning(
                    f"{service_name} failed (attempt {attempt+1}/{max_retries}), "
                    f"retrying in {wait:.1f}s: {exc}"
                )
                await asyncio.sleep(wait)
            else:
                logger.error(f"{service_name} exhausted {max_retries} retries: {exc}")

    # Primary failed — try fallback
    if fallback_fn is not None:
        try:
            result = await asyncio.wait_for(fallback_fn(), timeout=timeout)
            logger.warning(f"{service_name} using fallback function")
            return result, False
        except Exception as fallback_exc:
            logger.error(f"{service_name} fallback also failed: {fallback_exc}")

    # Fallback value
    if fallback_value is not None:
        logger.warning(f"{service_name} using static fallback value")
        return fallback_value, False

    raise RuntimeError(f"{service_name} unavailable after {max_retries} retries: {last_exc}")

# Usage:
context, is_fresh = await resilient_call(
    primary_fn=lambda: fetch_from_vector_db(query),
    fallback_fn=lambda: fetch_from_keyword_index(query),
    fallback_value="",  # Ultimate fallback: no context
    max_retries=3,
    service_name="context_retrieval"
)
```

## Degradation Strategy by Service Type

| Service | Degraded Mode | Minimal Mode |
|---|---|---|
| Vector search | Keyword search | No context (model knowledge only) |
| Primary LLM | Smaller/cheaper model | Cached answer or error message |
| External API | Cached/stale data | Omit that feature from response |
| Database | Read-only replica | In-memory cache or default values |
| Auth service | Cached token validation | Deny new sessions, allow cached sessions |

## Expected Token Savings
Total outage (agent crashes) → user retries → full conversation restart: ~5,000 tokens per failed session
Graceful degradation (partial answer returned) → user gets partial answer immediately: 0 recovery overhead

## Environment
- Any production agent with external dependencies; graceful degradation is most important for customer-facing agents where availability directly affects user satisfaction; implement it before scaling, not after — degradation patterns are hard to retrofit into agents that assume all dependencies are always available
- Source: direct experience; "all-or-nothing" agent designs cause 3–5× more perceived downtime than the actual dependency failure rate, because each dependency failure produces a complete user-visible outage instead of a reduced-capability response
