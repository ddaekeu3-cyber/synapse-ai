---
layout: solution
title: "Agent Doesn't Implement Tool Health Monitoring"
category: tool-failure
description: "How to proactively check tool availability before invoking them, track tool health over time, and route around degraded tools to avoid cascading failures."
tags: [tool-failure, health-check, monitoring, circuit-breaker, availability, resilience]
---

# Agent Doesn't Implement Tool Health Monitoring

Agents that invoke tools without checking their health fail loudly in production. A database tool that's unreachable, a search API returning 503s, or a code executor timing out will cause the agent to stall, retry infinitely, or return garbage. Proactive health monitoring catches degraded tools before the agent calls them, routes around failures, and restores service automatically when tools recover.

## Option 1: Pre-Call Health Check with Availability Cache

Run a lightweight probe before each tool call. Cache health status with a TTL to avoid checking on every request.

```python
import anthropic
import time
import urllib.request
import json
from dataclasses import dataclass, field
from typing import Callable, Optional

@dataclass
class HealthStatus:
    tool_name: str
    is_healthy: bool
    last_checked: float
    last_error: Optional[str] = None
    check_count: int = 0
    failure_count: int = 0

@dataclass
class ToolHealthCache:
    ttl_seconds: float = 30.0
    statuses: dict = field(default_factory=dict)

    def get(self, tool_name: str) -> Optional[HealthStatus]:
        status = self.statuses.get(tool_name)
        if status and (time.monotonic() - status.last_checked) < self.ttl_seconds:
            return status
        return None

    def set(self, status: HealthStatus):
        self.statuses[tool_name := status.tool_name] = status


# Tool health probes — lightweight checks, not full invocations
TOOL_HEALTH_PROBES: dict[str, Callable[[], tuple[bool, Optional[str]]]] = {
    "web_search": lambda: (True, None),       # Replace with actual HTTP probe
    "database_query": lambda: (True, None),   # Replace with SELECT 1
    "code_executor": lambda: (True, None),    # Replace with echo test
    "file_system": lambda: (True, None),      # Replace with stat check
}


def probe_tool(tool_name: str) -> HealthStatus:
    probe = TOOL_HEALTH_PROBES.get(tool_name)
    now = time.monotonic()

    if not probe:
        return HealthStatus(tool_name=tool_name, is_healthy=True, last_checked=now)

    try:
        is_healthy, error = probe()
        return HealthStatus(
            tool_name=tool_name,
            is_healthy=is_healthy,
            last_checked=now,
            last_error=error,
        )
    except Exception as e:
        return HealthStatus(
            tool_name=tool_name,
            is_healthy=False,
            last_checked=now,
            last_error=str(e),
        )


health_cache = ToolHealthCache(ttl_seconds=30.0)


def check_tool_health(tool_name: str) -> HealthStatus:
    cached = health_cache.get(tool_name)
    if cached:
        return cached
    status = probe_tool(tool_name)
    health_cache.set(status)
    return status


def agent_with_health_checks(user_query: str) -> str:
    client = anthropic.Anthropic()

    tools = [
        {
            "name": "web_search",
            "description": "Search the web for current information",
            "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        },
        {
            "name": "database_query",
            "description": "Query internal database for user data",
            "input_schema": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]},
        },
    ]

    # Pre-check health of all tools
    healthy_tools = []
    unavailable_tools = []

    for tool in tools:
        status = check_tool_health(tool["name"])
        if status.is_healthy:
            healthy_tools.append(tool)
        else:
            unavailable_tools.append(f"{tool['name']} ({status.last_error})")
            print(f"[HEALTH] {tool['name']} is UNHEALTHY: {status.last_error}")

    system = "You are a helpful assistant."
    if unavailable_tools:
        system += f"\n\nNote: The following tools are currently unavailable: {', '.join(unavailable_tools)}. Do not attempt to use them."

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        system=system,
        tools=healthy_tools if healthy_tools else [],
        messages=[{"role": "user", "content": user_query}],
    )

    return response.content[0].text if response.content else "[No response]"


if __name__ == "__main__":
    result = agent_with_health_checks("What's the current weather in Tokyo?")
    print(result)

# Expected Token Savings: Prevents wasted tool call round-trips to degraded services; eliminates retry storms
# Environment: Production agents with external tool dependencies (APIs, databases, external services)
```

## Option 2: Rolling Health Score with Exponential Moving Average

Track tool health as a continuous score (0.0–1.0) updated with each call outcome. Disable tools when score drops below a threshold.

```python
import anthropic
import time
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class ToolHealthScore:
    tool_name: str
    score: float = 1.0          # 1.0 = fully healthy, 0.0 = completely degraded
    ema_alpha: float = 0.3      # Weight of new observations
    min_score: float = 0.4      # Disable tool below this threshold
    call_count: int = 0
    failure_count: int = 0
    last_failure_time: Optional[float] = None

    def record_success(self):
        self.call_count += 1
        self.score = self.score * (1 - self.ema_alpha) + 1.0 * self.ema_alpha

    def record_failure(self, error: str = ""):
        self.call_count += 1
        self.failure_count += 1
        self.last_failure_time = time.monotonic()
        self.score = self.score * (1 - self.ema_alpha) + 0.0 * self.ema_alpha
        print(f"[HEALTH] {self.tool_name} failure recorded. Score: {self.score:.2f} | Error: {error[:60]}")

    @property
    def is_available(self) -> bool:
        return self.score >= self.min_score

    @property
    def status_label(self) -> str:
        if self.score >= 0.8:
            return "healthy"
        elif self.score >= 0.5:
            return "degraded"
        elif self.score >= 0.4:
            return "critical"
        return "unavailable"


class HealthAwareToolRouter:
    def __init__(self):
        self.health_scores: dict[str, ToolHealthScore] = {}
        self.client = anthropic.Anthropic()

    def register_tool(self, tool_name: str, alpha: float = 0.3, min_score: float = 0.4):
        self.health_scores[tool_name] = ToolHealthScore(
            tool_name=tool_name, ema_alpha=alpha, min_score=min_score
        )

    def on_tool_success(self, tool_name: str):
        if tool_name in self.health_scores:
            self.health_scores[tool_name].record_success()

    def on_tool_failure(self, tool_name: str, error: str = ""):
        if tool_name in self.health_scores:
            self.health_scores[tool_name].record_failure(error)

    def available_tools(self, all_tools: list[dict]) -> list[dict]:
        result = []
        for tool in all_tools:
            name = tool["name"]
            score = self.health_scores.get(name)
            if score is None or score.is_available:
                result.append(tool)
            else:
                print(f"[ROUTER] Skipping {name}: {score.status_label} (score={score.score:.2f})")
        return result

    def health_summary(self) -> str:
        lines = []
        for name, score in self.health_scores.items():
            lines.append(f"  {name}: {score.status_label} ({score.score:.2f}) — {score.failure_count}/{score.call_count} failures")
        return "\n".join(lines) if lines else "No tools registered"


router = HealthAwareToolRouter()

# Register tools with custom sensitivity
router.register_tool("web_search", alpha=0.2, min_score=0.3)
router.register_tool("database_query", alpha=0.4, min_score=0.5)
router.register_tool("email_sender", alpha=0.5, min_score=0.6)


def simulate_tool_degradation():
    """Simulate a tool becoming degraded over time."""
    for _ in range(5):
        router.on_tool_failure("database_query", "Connection timeout after 30s")

    # Should now be unavailable
    print("\nHealth summary after failures:")
    print(router.health_summary())


def query_with_health_routing(user_prompt: str) -> str:
    all_tools = [
        {
            "name": "web_search",
            "description": "Search the web",
            "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
        },
        {
            "name": "database_query",
            "description": "Query internal database",
            "input_schema": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]},
        },
    ]

    available = router.available_tools(all_tools)

    response = router.client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        tools=available,
        messages=[{"role": "user", "content": user_prompt}],
    )
    return response.content[0].text or "[tool call initiated]"


if __name__ == "__main__":
    simulate_tool_degradation()
    result = query_with_health_routing("Look up the user's account history.")
    print(f"\nResponse: {result}")

# Expected Token Savings: Routes around degraded tools automatically, avoiding failed tool call loops
# Environment: Multi-tool agents in production where individual services have different reliability profiles
```

## Option 3: Async Parallel Health Checks at Startup and on Schedule

Run health probes in parallel at agent startup and periodically refresh them in the background.

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ToolProbeConfig:
    tool_name: str
    probe_url: Optional[str] = None    # HTTP endpoint to probe
    probe_timeout: float = 5.0
    check_interval: float = 60.0       # Seconds between background checks


@dataclass
class LiveHealthState:
    tool_name: str
    is_healthy: bool = True
    last_check: float = field(default_factory=time.monotonic)
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    recovery_threshold: int = 2  # Successes needed to re-enable


async def probe_tool_async(config: ToolProbeConfig) -> tuple[bool, Optional[str]]:
    """Async probe — replace with real connectivity checks."""
    try:
        await asyncio.sleep(0.01)  # Simulate probe latency
        # In real code: aiohttp.get(config.probe_url, timeout=config.probe_timeout)
        return True, None
    except asyncio.TimeoutError:
        return False, f"Probe timed out after {config.probe_timeout}s"
    except Exception as e:
        return False, str(e)


class AsyncHealthMonitor:
    def __init__(self):
        self.configs: dict[str, ToolProbeConfig] = {}
        self.states: dict[str, LiveHealthState] = {}
        self._bg_task: Optional[asyncio.Task] = None

    def register(self, config: ToolProbeConfig):
        self.configs[config.tool_name] = config
        self.states[config.tool_name] = LiveHealthState(tool_name=config.tool_name)

    async def check_all(self):
        """Run all probes concurrently."""
        tasks = {
            name: asyncio.create_task(probe_tool_async(cfg))
            for name, cfg in self.configs.items()
        }
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)

        for (name, _), result in zip(tasks.items(), results):
            state = self.states[name]
            if isinstance(result, Exception):
                is_healthy, error = False, str(result)
            else:
                is_healthy, error = result

            state.last_check = time.monotonic()

            if is_healthy:
                state.consecutive_failures = 0
                state.consecutive_successes += 1
                if not state.is_healthy and state.consecutive_successes >= state.recovery_threshold:
                    state.is_healthy = True
                    print(f"[HEALTH] {name} RECOVERED after {state.consecutive_successes} successes")
            else:
                state.consecutive_successes = 0
                state.consecutive_failures += 1
                if state.is_healthy:
                    state.is_healthy = False
                    print(f"[HEALTH] {name} DEGRADED: {error} (failure #{state.consecutive_failures})")

    async def start_background_monitor(self):
        """Run health checks in background."""
        async def _loop():
            while True:
                await self.check_all()
                await asyncio.sleep(min(cfg.check_interval for cfg in self.configs.values()))

        self._bg_task = asyncio.create_task(_loop())
        print("[HEALTH] Background monitor started")

    def stop(self):
        if self._bg_task:
            self._bg_task.cancel()

    def available_tool_names(self) -> set[str]:
        return {name for name, state in self.states.items() if state.is_healthy}

    def health_report(self) -> dict:
        return {
            name: {"healthy": s.is_healthy, "failures": s.consecutive_failures}
            for name, s in self.states.items()
        }


monitor = AsyncHealthMonitor()
monitor.register(ToolProbeConfig("web_search", check_interval=30.0))
monitor.register(ToolProbeConfig("database_query", check_interval=15.0))
monitor.register(ToolProbeConfig("file_reader", check_interval=60.0))


async def async_agent(prompt: str) -> str:
    client = anthropic.AsyncAnthropic()

    # Use currently healthy tools
    all_tools = {
        "web_search": {
            "name": "web_search",
            "description": "Search the web for information",
            "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        },
        "database_query": {
            "name": "database_query",
            "description": "Query internal database",
            "input_schema": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]},
        },
    }

    available = monitor.available_tool_names()
    active_tools = [v for k, v in all_tools.items() if k in available]

    print(f"[HEALTH] Active tools: {list(available)}")

    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        tools=active_tools,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text or "[tool response]"


async def main():
    # Initial health check before serving
    await monitor.check_all()
    print(f"Startup health: {monitor.health_report()}")

    # Start background monitoring
    await monitor.start_background_monitor()

    result = await async_agent("What's trending in AI research today?")
    print(f"Response: {result}")

    monitor.stop()


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Zero-latency tool routing using cached health state; no blocking probes in hot path
# Environment: High-throughput async agents, API servers with multiple tool backends
```

## Option 4: Tool Health Dashboard with SQLite History

Persist health check history to SQLite. Detect patterns like time-of-day degradation, intermittent failures, and SLA violations.

```python
import anthropic
import sqlite3
import time
import json
from dataclasses import dataclass
from typing import Optional


DB_PATH = "tool_health.db"


def init_health_db(db_path: str = DB_PATH) -> sqlite3.Connection:
    db = sqlite3.connect(db_path)
    db.execute("""
        CREATE TABLE IF NOT EXISTS tool_health_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tool_name TEXT NOT NULL,
            is_healthy INTEGER NOT NULL,
            latency_ms REAL,
            error TEXT,
            checked_at REAL NOT NULL
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_tool_time ON tool_health_log(tool_name, checked_at)")
    db.commit()
    return db


def record_health_check(
    db: sqlite3.Connection,
    tool_name: str,
    is_healthy: bool,
    latency_ms: Optional[float] = None,
    error: Optional[str] = None,
):
    db.execute("""
        INSERT INTO tool_health_log (tool_name, is_healthy, latency_ms, error, checked_at)
        VALUES (?, ?, ?, ?, ?)
    """, (tool_name, int(is_healthy), latency_ms, error, time.time()))
    db.commit()


def get_health_stats(
    db: sqlite3.Connection,
    tool_name: str,
    window_seconds: float = 3600.0,
) -> dict:
    since = time.time() - window_seconds
    rows = db.execute("""
        SELECT is_healthy, latency_ms FROM tool_health_log
        WHERE tool_name = ? AND checked_at >= ?
    """, (tool_name, since)).fetchall()

    if not rows:
        return {"tool": tool_name, "checks": 0, "uptime": 1.0, "avg_latency_ms": None}

    total = len(rows)
    healthy = sum(1 for r in rows if r[0])
    latencies = [r[1] for r in rows if r[1] is not None]

    return {
        "tool": tool_name,
        "checks": total,
        "uptime": healthy / total,
        "avg_latency_ms": sum(latencies) / len(latencies) if latencies else None,
        "p95_latency_ms": sorted(latencies)[int(len(latencies) * 0.95)] if latencies else None,
    }


def sla_compliant(stats: dict, min_uptime: float = 0.95) -> bool:
    return stats["uptime"] >= min_uptime


def probe_tool_with_timing(tool_name: str) -> tuple[bool, float, Optional[str]]:
    """Probe a tool and measure latency. Replace with real probes."""
    start = time.monotonic()
    try:
        time.sleep(0.005)  # Simulate probe
        latency = (time.monotonic() - start) * 1000
        return True, latency, None
    except Exception as e:
        latency = (time.monotonic() - start) * 1000
        return False, latency, str(e)


def health_aware_agent(prompt: str, db: sqlite3.Connection) -> str:
    client = anthropic.Anthropic()
    tool_names = ["web_search", "database_query", "code_executor"]

    # Run probes and record results
    tool_health = {}
    for tool_name in tool_names:
        is_healthy, latency, error = probe_tool_with_timing(tool_name)
        record_health_check(db, tool_name, is_healthy, latency, error)
        stats = get_health_stats(db, tool_name)
        tool_health[tool_name] = {
            "available": is_healthy and sla_compliant(stats),
            "stats": stats,
        }

    # Build tool list with only SLA-compliant tools
    available_tools = []
    skipped = []

    all_tool_defs = {
        "web_search": {
            "name": "web_search",
            "description": "Search the web",
            "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        },
        "database_query": {
            "name": "database_query",
            "description": "Query database",
            "input_schema": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]},
        },
    }

    for name, health in tool_health.items():
        if health["available"] and name in all_tool_defs:
            available_tools.append(all_tool_defs[name])
            uptime = health["stats"]["uptime"]
            print(f"[HEALTH] {name}: available (uptime={uptime:.1%})")
        else:
            skipped.append(name)
            uptime = tool_health[name]["stats"]["uptime"]
            print(f"[HEALTH] {name}: SKIPPED (uptime={uptime:.1%})")

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        system=f"Available tools: {[t['name'] for t in available_tools]}. Unavailable: {skipped}.",
        tools=available_tools,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text or "[tool call]"


if __name__ == "__main__":
    db = init_health_db(":memory:")

    # Seed some historical failures for database_query
    for _ in range(8):
        record_health_check(db, "database_query", False, 5000.0, "timeout")
    for _ in range(2):
        record_health_check(db, "database_query", True, 120.0)

    result = health_aware_agent("Get the latest news and query user stats.", db)
    print(f"\nResult: {result}")

# Expected Token Savings: SLA-based filtering prevents calling tools with chronic reliability issues
# Environment: SRE-monitored production agents, services with formal uptime requirements
```

## Option 5: Graceful Degradation with Tool Fallback Chains

Define fallback chains per tool — when a primary tool is unhealthy, automatically substitute an alternative.

```python
import anthropic
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ToolFallbackChain:
    primary: str
    fallbacks: list[str]
    current_index: int = 0
    failure_counts: dict = field(default_factory=dict)

    def active_tool(self) -> str:
        return self.primary if self.current_index == 0 else self.fallbacks[self.current_index - 1]

    def advance(self) -> Optional[str]:
        """Move to next fallback. Returns new tool name or None if exhausted."""
        if self.current_index < len(self.fallbacks):
            self.current_index += 1
            return self.active_tool()
        return None

    def reset(self):
        self.current_index = 0


# Tool health registry
_tool_health: dict[str, bool] = {
    "vector_search": True,
    "keyword_search": True,
    "llm_synthesis": True,
    "cached_search": True,
}


def is_tool_healthy(tool_name: str) -> bool:
    return _tool_health.get(tool_name, True)


def degrade_tool(tool_name: str):
    """Simulate tool becoming unhealthy."""
    _tool_health[tool_name] = False
    print(f"[HEALTH] {tool_name} marked unhealthy")


def resolve_active_tool(chain: ToolFallbackChain) -> Optional[str]:
    """Walk the chain until a healthy tool is found."""
    # Check primary
    if is_tool_healthy(chain.primary):
        chain.reset()
        return chain.primary

    # Walk fallbacks
    for fallback in chain.fallbacks:
        if is_tool_healthy(fallback):
            print(f"[FALLBACK] {chain.primary} unhealthy → using {fallback}")
            return fallback

    print(f"[FALLBACK] All tools in chain exhausted: {chain.primary} + {chain.fallbacks}")
    return None


def agent_with_fallback_chains(user_query: str) -> str:
    client = anthropic.Anthropic()

    # Define fallback chains
    search_chain = ToolFallbackChain(
        primary="vector_search",
        fallbacks=["keyword_search", "cached_search"],
    )

    active_search = resolve_active_tool(search_chain)

    available_tools = []
    if active_search:
        available_tools.append({
            "name": active_search,
            "description": f"Search for information (active: {active_search})",
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        })

    system = "You are a helpful research assistant."
    if not active_search:
        system += " Note: Search tools are currently unavailable. Answer from training knowledge only."

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        system=system,
        tools=available_tools,
        messages=[{"role": "user", "content": user_query}],
    )
    return response.content[0].text or f"[{active_search or 'no-tool'} response]"


if __name__ == "__main__":
    print("=== Test 1: All healthy ===")
    result = agent_with_fallback_chains("What are the latest AI papers?")
    print(f"Result: {result[:100]}\n")

    print("=== Test 2: Primary degraded ===")
    degrade_tool("vector_search")
    result = agent_with_fallback_chains("What are the latest AI papers?")
    print(f"Result: {result[:100]}\n")

    print("=== Test 3: Primary + first fallback degraded ===")
    degrade_tool("keyword_search")
    result = agent_with_fallback_chains("What are the latest AI papers?")
    print(f"Result: {result[:100]}")

# Expected Token Savings: Eliminates failed tool call retries by routing to healthy alternatives instantly
# Environment: Agents with redundant tool backends (multiple search providers, database replicas)
```

## Option 6: Health-Gated Tool Schema Injection

Only inject tool definitions into the system prompt for tools that are currently healthy. Unhealthy tools are invisible to the model.

```python
import anthropic
import time
import random
from dataclasses import dataclass
from typing import Optional


@dataclass
class ManagedTool:
    definition: dict
    last_healthy_at: float = 0.0
    last_probe_at: float = 0.0
    probe_interval: float = 20.0
    is_healthy: bool = True
    error_message: Optional[str] = None

    @property
    def name(self) -> str:
        return self.definition["name"]

    def needs_probe(self) -> bool:
        return (time.monotonic() - self.last_probe_at) >= self.probe_interval

    def mark_healthy(self):
        self.is_healthy = True
        self.last_healthy_at = time.monotonic()
        self.last_probe_at = time.monotonic()
        self.error_message = None

    def mark_unhealthy(self, error: str):
        self.is_healthy = False
        self.last_probe_at = time.monotonic()
        self.error_message = error


class HealthGatedToolRegistry:
    def __init__(self):
        self._tools: dict[str, ManagedTool] = {}

    def register(self, tool_def: dict, probe_interval: float = 20.0):
        self._tools[tool_def["name"]] = ManagedTool(
            definition=tool_def,
            probe_interval=probe_interval,
        )

    def probe_tool(self, name: str) -> bool:
        """Override in production with real probe logic."""
        # Simulate random failures for demo
        success = random.random() > 0.2
        tool = self._tools[name]
        if success:
            tool.mark_healthy()
        else:
            tool.mark_unhealthy("Simulated probe failure")
        return success

    def refresh_if_needed(self):
        for name, tool in self._tools.items():
            if tool.needs_probe():
                healthy = self.probe_tool(name)
                status = "healthy" if healthy else f"unhealthy ({tool.error_message})"
                print(f"[HEALTH PROBE] {name}: {status}")

    def healthy_tool_definitions(self) -> list[dict]:
        self.refresh_if_needed()
        return [t.definition for t in self._tools.values() if t.is_healthy]

    def health_context(self) -> str:
        lines = []
        for name, tool in self._tools.items():
            if not tool.is_healthy:
                lines.append(f"{name}: unavailable ({tool.error_message})")
        return "Currently unavailable tools: " + ", ".join(lines) if lines else ""


registry = HealthGatedToolRegistry()

registry.register({
    "name": "web_search",
    "description": "Search the web for real-time information",
    "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
}, probe_interval=15.0)

registry.register({
    "name": "code_runner",
    "description": "Execute Python code and return output",
    "input_schema": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]},
}, probe_interval=30.0)

registry.register({
    "name": "knowledge_base",
    "description": "Query internal knowledge base",
    "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
}, probe_interval=45.0)


def health_gated_agent(prompt: str) -> str:
    client = anthropic.Anthropic()

    healthy_tools = registry.healthy_tool_definitions()
    context_note = registry.health_context()

    print(f"[HEALTH] Active tools: {[t['name'] for t in healthy_tools]}")

    system = "You are a helpful assistant."
    if context_note:
        system += f"\n\n{context_note}"

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        system=system,
        tools=healthy_tools,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text or "[tool invoked]"


if __name__ == "__main__":
    random.seed(42)

    for i in range(3):
        print(f"\n=== Request {i+1} ===")
        result = health_gated_agent("Find recent research on transformer efficiency.")
        print(f"Response: {result[:120]}")

# Expected Token Savings: Reduces tool schema tokens by 30-70% when multiple tools are degraded; prevents model attempting unavailable tools
# Environment: Agents with many optional tools where partial availability is expected
```

## Comparison

| Option | Probe Timing | Persistence | Fallback | Best For |
|--------|-------------|-------------|----------|----------|
| 1 Pre-Call with TTL Cache | Before each call | In-memory TTL | None | Simple agents with few tools |
| 2 Rolling EMA Score | Per call outcome | In-memory | Score-based disable | Tools with variable reliability |
| 3 Async Background Monitor | Parallel + scheduled | In-memory | Availability set | High-throughput async servers |
| 4 SQLite History + SLA | Per call + startup | SQLite | SLA compliance | Production agents with uptime SLAs |
| 5 Fallback Chains | Per resolution | In-memory | Ordered fallbacks | Redundant tool backends |
| 6 Health-Gated Schema Injection | TTL probe | In-memory | Schema exclusion | Agents with many optional tools |
