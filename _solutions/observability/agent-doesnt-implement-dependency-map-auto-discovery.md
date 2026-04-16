---
title: "Agent Doesn't Implement Dependency Map Auto-Discovery"
description: "AI agents that don't track their runtime dependencies have no map of what they rely on, making incident diagnosis, capacity planning, and blast-radius analysis impossible. Learn six patterns for automatically discovering and visualizing agent dependency graphs at runtime."
date: 2026-04-16
difficulty: advanced
category: observability
slug: agent-doesnt-implement-dependency-map-auto-discovery
tags: [dependency-map, service-mesh, topology, observability, tracing, graph]
symptoms:
  - "No one knows what services the agent calls until an outage reveals them"
  - "Blast radius of a downstream failure is unknown until it cascades"
  - "New team members have no way to understand what the agent depends on"
  - "Capacity planning is guesswork because dependency traffic volumes are unknown"
  - "Circular dependencies between agents go undetected until they cause deadlocks"
---

## The Problem

AI agents are not isolated processes — they call LLM APIs, tool endpoints, databases, vector stores, message queues, and other agents. Without an explicit dependency map, teams have no visibility into what the agent relies on, which dependencies are critical vs. optional, what the expected call volume is, or what the blast radius would be if a dependency failed.

Dependency map auto-discovery instruments outgoing calls to build a live topology of what the agent depends on, annotated with call rates, latency distributions, and error rates. This map feeds incident response, capacity planning, and architectural review.

```python
# ❌ No dependency tracking — invisible topology
response = await anthropic_client.messages.create(...)
result = await db.query("SELECT ...")
data = await requests.get("https://api.example.com/search")

# ✓ Auto-discovered dependency map
async with DependencyTracker() as tracker:
    response = await tracker.wrap_llm(anthropic_client).messages.create(...)
    result = await tracker.wrap_db(db).query("SELECT ...")
    # tracker.map() → {"anthropic_api": {...}, "postgres": {...}}
```

---

## Solution 1: Outgoing Call Interceptor

Instrument all outgoing HTTP/database calls automatically using a proxy wrapper that records the target, call count, latency, and error rate into a dependency registry.

```python
import asyncio
import time
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse


@dataclass
class DependencyStats:
    name: str
    dependency_type: str     # "http", "database", "llm", "agent", "queue"
    call_count: int = 0
    error_count: int = 0
    total_latency_ms: float = 0.0
    latencies: list[float] = field(default_factory=list)
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)

    @property
    def error_rate(self) -> float:
        return self.error_count / max(self.call_count, 1)

    @property
    def avg_latency_ms(self) -> float:
        return self.total_latency_ms / max(self.call_count, 1)

    @property
    def p95_latency_ms(self) -> float:
        if not self.latencies:
            return 0.0
        return sorted(self.latencies)[int(len(self.latencies) * 0.95)]

    def record(self, latency_ms: float, error: bool):
        self.call_count += 1
        self.total_latency_ms += latency_ms
        self.latencies.append(latency_ms)
        if len(self.latencies) > 1000:
            self.latencies = self.latencies[-1000:]  # Keep last 1k for percentiles
        if error:
            self.error_count += 1
        self.last_seen = time.time()


class DependencyRegistry:
    """Central registry of all discovered dependencies."""

    def __init__(self):
        self._deps: dict[str, DependencyStats] = {}

    def record(self, name: str, dep_type: str, latency_ms: float, error: bool):
        if name not in self._deps:
            self._deps[name] = DependencyStats(name=name, dependency_type=dep_type)
        self._deps[name].record(latency_ms, error)

    def get_map(self) -> dict[str, dict]:
        return {
            name: {
                "type": dep.dependency_type,
                "call_count": dep.call_count,
                "error_rate": dep.error_rate,
                "avg_latency_ms": dep.avg_latency_ms,
                "p95_latency_ms": dep.p95_latency_ms,
                "first_seen_ago": time.time() - dep.first_seen,
                "last_seen_ago": time.time() - dep.last_seen,
                "status": "degraded" if dep.error_rate > 0.05 else "healthy",
            }
            for name, dep in self._deps.items()
        }

    def critical_dependencies(self) -> list[str]:
        """Dependencies called frequently enough to be critical."""
        if not self._deps:
            return []
        max_calls = max(d.call_count for d in self._deps.values())
        return [
            name for name, dep in self._deps.items()
            if dep.call_count >= max_calls * 0.1  # Top 90th percentile by call volume
        ]


class HTTPDependencyInterceptor:
    """Wraps aiohttp sessions to auto-discover HTTP dependencies."""

    def __init__(self, registry: DependencyRegistry, service_name: str = "agent"):
        self.registry = registry
        self.service_name = service_name

    def _extract_dep_name(self, url: str) -> str:
        try:
            parsed = urlparse(url)
            host = parsed.netloc.split(":")[0]  # Remove port
            # Normalize: strip common prefixes
            host = re.sub(r'^(api\.|www\.)', '', host)
            return host
        except Exception:
            return "unknown_http"

    async def get(self, url: str, **kwargs) -> Any:
        return await self._call("GET", url, **kwargs)

    async def post(self, url: str, **kwargs) -> Any:
        return await self._call("POST", url, **kwargs)

    async def _call(self, method: str, url: str, **kwargs) -> Any:
        import aiohttp
        dep_name = self._extract_dep_name(url)
        start = time.monotonic()
        error = False
        try:
            async with aiohttp.ClientSession() as session:
                async with session.request(method, url, **kwargs) as resp:
                    result = await resp.json()
                    if resp.status >= 500:
                        error = True
                    return result
        except Exception as e:
            error = True
            raise
        finally:
            latency = (time.monotonic() - start) * 1000
            self.registry.record(dep_name, "http", latency, error)
```

---

## Solution 2: LLM Call Dependency Tracker

Specifically tracks Anthropic API calls as dependencies: model used, token volumes, cost, and response quality signals.

```python
import asyncio
import time
import anthropic
from dataclasses import dataclass, field
from functools import wraps


@dataclass
class LLMDependencyStats:
    model: str
    call_count: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0
    error_count: int = 0
    latencies_ms: list[float] = field(default_factory=list)
    cache_reads: int = 0
    cache_writes: int = 0

    MODEL_PRICES = {
        "claude-haiku-4-5-20251001": (0.00025, 0.00125),
        "claude-3-5-sonnet-20241022": (0.003, 0.015),
        "claude-opus-4-6": (0.015, 0.075),
    }

    def record_call(self, usage, latency_ms: float, error: bool):
        self.call_count += 1
        self.total_input_tokens += usage.input_tokens
        self.total_output_tokens += usage.output_tokens
        self.latencies_ms.append(latency_ms)
        if len(self.latencies_ms) > 500:
            self.latencies_ms = self.latencies_ms[-500:]
        if error:
            self.error_count += 1

        cache_read = getattr(usage, "cache_read_input_tokens", 0)
        cache_write = getattr(usage, "cache_creation_input_tokens", 0)
        self.cache_reads += cache_read
        self.cache_writes += cache_write

        inp_price, out_price = self.MODEL_PRICES.get(self.model, (0.003, 0.015))
        self.total_cost_usd += (
            (usage.input_tokens - cache_read) * inp_price / 1000 +
            cache_write * inp_price * 0.25 / 1000 +
            cache_read * inp_price * 0.10 / 1000 +
            usage.output_tokens * out_price / 1000
        )

    def summary(self) -> dict:
        latencies = sorted(self.latencies_ms)
        n = len(latencies)
        return {
            "model": self.model,
            "call_count": self.call_count,
            "error_rate": self.error_count / max(self.call_count, 1),
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_cost_usd": self.total_cost_usd,
            "avg_cost_per_call": self.total_cost_usd / max(self.call_count, 1),
            "p50_latency_ms": latencies[int(n * 0.5)] if latencies else 0,
            "p95_latency_ms": latencies[int(n * 0.95)] if latencies else 0,
            "cache_hit_rate": self.cache_reads / max(self.total_input_tokens, 1),
        }


class InstrumentedAnthropicClient:
    """Wraps AsyncAnthropic to track LLM dependency stats."""

    def __init__(self, registry: DependencyRegistry):
        self._client = anthropic.AsyncAnthropic()
        self._registry = registry
        self._llm_stats: dict[str, LLMDependencyStats] = {}

    async def create_message(self, model: str, **kwargs) -> anthropic.types.Message:
        if model not in self._llm_stats:
            self._llm_stats[model] = LLMDependencyStats(model=model)

        start = time.monotonic()
        error = False
        try:
            resp = await self._client.messages.create(model=model, **kwargs)
            return resp
        except Exception:
            error = True
            raise
        finally:
            latency_ms = (time.monotonic() - start) * 1000
            if not error:
                self._llm_stats[model].record_call(resp.usage, latency_ms, error)
            self._registry.record(f"anthropic/{model}", "llm", latency_ms, error)

    def llm_summary(self) -> dict:
        return {model: stats.summary() for model, stats in self._llm_stats.items()}
```

---

## Solution 3: Agent-to-Agent Call Graph

When agents call other agents, track the call graph: which agent calls which, with what frequency and what outcomes. Detects circular dependencies and fan-out patterns.

```python
import asyncio
import time
import uuid
from dataclasses import dataclass, field
from collections import defaultdict


@dataclass
class AgentCallEdge:
    caller: str
    callee: str
    call_count: int = 0
    error_count: int = 0
    total_latency_ms: float = 0.0
    last_call: float = field(default_factory=time.time)

    def record(self, latency_ms: float, error: bool):
        self.call_count += 1
        self.total_latency_ms += latency_ms
        if error:
            self.error_count += 1
        self.last_call = time.time()


class AgentCallGraphTracker:
    """
    Tracks agent-to-agent calls to build a runtime call graph.
    Detects circular dependencies and measures fan-out depth.
    """

    def __init__(self, agent_id: str):
        self.agent_id = agent_id
        self._edges: dict[tuple[str, str], AgentCallEdge] = {}
        self._active_calls: dict[str, list[str]] = defaultdict(list)  # trace_id → call_stack

    def _edge_key(self, caller: str, callee: str) -> tuple[str, str]:
        return (caller, callee)

    def record_call(self, callee_id: str, latency_ms: float,
                    error: bool, trace_id: str | None = None):
        key = self._edge_key(self.agent_id, callee_id)
        if key not in self._edges:
            self._edges[key] = AgentCallEdge(caller=self.agent_id, callee=callee_id)
        self._edges[key].record(latency_ms, error)

        # Check for cycles in trace
        if trace_id and self.agent_id in self._active_calls[trace_id]:
            print(
                f"[dep_map] CIRCULAR DEPENDENCY detected: "
                f"{' → '.join(self._active_calls[trace_id])} → {self.agent_id}"
            )

    def enter_call(self, trace_id: str):
        """Call when this agent starts handling a request."""
        self._active_calls[trace_id].append(self.agent_id)

    def exit_call(self, trace_id: str):
        """Call when this agent finishes handling a request."""
        if trace_id in self._active_calls:
            stack = self._active_calls[trace_id]
            if self.agent_id in stack:
                stack.remove(self.agent_id)

    def detect_cycles(self) -> list[list[str]]:
        """Detect cycles in the recorded call graph using DFS."""
        # Build adjacency list
        graph: dict[str, set[str]] = defaultdict(set)
        for (caller, callee) in self._edges.keys():
            graph[caller].add(callee)

        cycles = []
        visited = set()
        rec_stack = []

        def dfs(node: str) -> bool:
            visited.add(node)
            rec_stack.append(node)
            for neighbor in graph.get(node, []):
                if neighbor not in visited:
                    if dfs(neighbor):
                        return True
                elif neighbor in rec_stack:
                    cycle_start = rec_stack.index(neighbor)
                    cycles.append(list(rec_stack[cycle_start:]))
            rec_stack.pop()
            return False

        for node in graph:
            if node not in visited:
                dfs(node)

        return cycles

    def to_mermaid(self) -> str:
        """Generate a Mermaid.js graph diagram of the call graph."""
        lines = ["graph LR"]
        for (caller, callee), edge in self._edges.items():
            rate = edge.call_count
            err_pct = int(edge.error_count / max(edge.call_count, 1) * 100)
            label = f"{rate} calls"
            if err_pct > 0:
                label += f" {err_pct}% err"
            # Sanitize node names for Mermaid
            c1 = caller.replace("-", "_").replace(".", "_")
            c2 = callee.replace("-", "_").replace(".", "_")
            style = "-->|" + label + "|"
            lines.append(f"  {c1} {style} {c2}")
        return "\n".join(lines)

    def call_graph(self) -> dict:
        return {
            f"{e.caller}→{e.callee}": {
                "call_count": e.call_count,
                "error_rate": e.error_count / max(e.call_count, 1),
                "avg_latency_ms": e.total_latency_ms / max(e.call_count, 1),
            }
            for e in self._edges.values()
        }
```

---

## Solution 4: Database Dependency Discovery

Track database queries as dependencies: which tables are accessed, query types (read/write), and latency patterns. Groups by table to identify hot tables and slow queries.

```python
import re
import time
from dataclasses import dataclass, field
from collections import defaultdict


@dataclass
class TableAccessStats:
    table_name: str
    reads: int = 0
    writes: int = 0
    errors: int = 0
    total_latency_ms: float = 0.0
    slow_queries: list[float] = field(default_factory=list)  # Latencies > threshold
    slow_threshold_ms: float = 100.0

    def record(self, is_write: bool, latency_ms: float, error: bool):
        if is_write:
            self.writes += 1
        else:
            self.reads += 1
        self.total_latency_ms += latency_ms
        if error:
            self.errors += 1
        if latency_ms > self.slow_threshold_ms:
            self.slow_queries.append(latency_ms)
            if len(self.slow_queries) > 100:
                self.slow_queries = self.slow_queries[-100:]

    @property
    def total_calls(self) -> int:
        return self.reads + self.writes

    @property
    def avg_latency_ms(self) -> float:
        return self.total_latency_ms / max(self.total_calls, 1)


class DatabaseDependencyTracker:
    """
    Discovers database dependencies by parsing SQL queries.
    Tracks per-table access patterns, identifies hot tables and slow queries.
    """

    # Simple SQL patterns to extract table names
    TABLE_PATTERNS = [
        r'\bFROM\s+(\w+)',
        r'\bJOIN\s+(\w+)',
        r'\bINTO\s+(\w+)',
        r'\bUPDATE\s+(\w+)',
        r'\bDELETE\s+FROM\s+(\w+)',
    ]

    WRITE_KEYWORDS = {"INSERT", "UPDATE", "DELETE", "UPSERT", "MERGE", "CREATE", "DROP", "ALTER"}

    def __init__(self, db_name: str = "postgres"):
        self.db_name = db_name
        self._tables: dict[str, TableAccessStats] = {}
        self._registry: DependencyRegistry | None = None
        self._query_count = 0
        self._total_latency_ms = 0.0

    def set_registry(self, registry: DependencyRegistry):
        self._registry = registry

    def _extract_tables(self, query: str) -> list[str]:
        tables = []
        upper = query.upper()
        for pattern in self.TABLE_PATTERNS:
            matches = re.findall(pattern, upper)
            tables.extend(m.lower() for m in matches)
        return list(set(tables))

    def _is_write(self, query: str) -> bool:
        first_word = query.strip().split()[0].upper() if query.strip() else ""
        return first_word in self.WRITE_KEYWORDS

    def record_query(self, query: str, latency_ms: float, error: bool = False):
        self._query_count += 1
        self._total_latency_ms += latency_ms

        tables = self._extract_tables(query)
        is_write = self._is_write(query)

        for table in tables:
            if table not in self._tables:
                self._tables[table] = TableAccessStats(table_name=table)
            self._tables[table].record(is_write, latency_ms, error)

        if self._registry:
            dep_name = f"{self.db_name}/{','.join(tables[:2])}" if tables else self.db_name
            self._registry.record(dep_name, "database", latency_ms, error)

    def hot_tables(self, top_n: int = 5) -> list[dict]:
        return sorted([
            {
                "table": stats.table_name,
                "total_calls": stats.total_calls,
                "read_ratio": stats.reads / max(stats.total_calls, 1),
                "avg_latency_ms": stats.avg_latency_ms,
                "slow_query_count": len(stats.slow_queries),
                "error_rate": stats.errors / max(stats.total_calls, 1),
            }
            for stats in self._tables.values()
        ], key=lambda x: -x["total_calls"])[:top_n]

    def slow_query_tables(self) -> list[str]:
        return [
            stats.table_name for stats in self._tables.values()
            if stats.slow_queries
        ]
```

---

## Solution 5: Dependency Health Dashboard Generator

Aggregates all discovered dependencies and generates a structured health dashboard: dependency status, SLA compliance, change detection, and Markdown/JSON export.

```python
import time
from dataclasses import dataclass


@dataclass
class DependencyHealthReport:
    generated_at: float
    agent_id: str
    total_dependencies: int
    healthy: int
    degraded: int
    failing: int
    dependencies: list[dict]
    recommendations: list[str]


class DependencyHealthDashboard:
    """
    Aggregates dependency data into a health dashboard.
    Generates Markdown reports and detects regressions.
    """

    HEALTHY_THRESHOLDS = {
        "error_rate": 0.01,        # < 1% errors
        "p95_latency_ms": 1000.0,  # < 1s p95
    }

    DEGRADED_THRESHOLDS = {
        "error_rate": 0.05,         # < 5% errors
        "p95_latency_ms": 5000.0,   # < 5s p95
    }

    def __init__(self, registry: DependencyRegistry, agent_id: str):
        self._registry = registry
        self.agent_id = agent_id
        self._snapshots: list[dict] = []

    def _classify_health(self, dep: dict) -> str:
        error_rate = dep.get("error_rate", 0)
        p95 = dep.get("p95_latency_ms", 0)
        if (error_rate > self.DEGRADED_THRESHOLDS["error_rate"] or
                p95 > self.DEGRADED_THRESHOLDS["p95_latency_ms"]):
            return "failing"
        elif (error_rate > self.HEALTHY_THRESHOLDS["error_rate"] or
              p95 > self.HEALTHY_THRESHOLDS["p95_latency_ms"]):
            return "degraded"
        return "healthy"

    def generate_report(self) -> DependencyHealthReport:
        dep_map = self._registry.get_map()
        deps_with_health = []
        counts = {"healthy": 0, "degraded": 0, "failing": 0}

        for name, stats in dep_map.items():
            health = self._classify_health(stats)
            counts[health] += 1
            deps_with_health.append({
                "name": name,
                "health": health,
                **stats,
            })

        # Sort: failing first, then degraded, then healthy
        health_order = {"failing": 0, "degraded": 1, "healthy": 2}
        deps_with_health.sort(key=lambda d: health_order.get(d["health"], 3))

        recommendations = self._generate_recommendations(deps_with_health)

        report = DependencyHealthReport(
            generated_at=time.time(),
            agent_id=self.agent_id,
            total_dependencies=len(deps_with_health),
            healthy=counts["healthy"],
            degraded=counts["degraded"],
            failing=counts["failing"],
            dependencies=deps_with_health,
            recommendations=recommendations,
        )
        # Save snapshot for regression detection
        self._snapshots.append({"at": time.time(), "map": dep_map})
        return report

    def _generate_recommendations(self, deps: list[dict]) -> list[str]:
        recs = []
        for dep in deps:
            if dep["health"] == "failing":
                recs.append(
                    f"CRITICAL: {dep['name']} has {dep['error_rate']:.0%} error rate — "
                    "add circuit breaker and fallback"
                )
            elif dep["health"] == "degraded":
                recs.append(
                    f"WARNING: {dep['name']} p95={dep['p95_latency_ms']:.0f}ms — "
                    "investigate latency source"
                )
            if dep.get("call_count", 0) > 1000 and dep["health"] != "healthy":
                recs.append(
                    f"HIGH IMPACT: {dep['name']} is called {dep['call_count']} times — "
                    "prioritize remediation"
                )
        return recs[:10]  # Top 10 recommendations

    def to_markdown(self, report: DependencyHealthReport) -> str:
        status_icon = {"healthy": "✅", "degraded": "⚠️", "failing": "❌"}
        lines = [
            f"# Dependency Health Report — {self.agent_id}",
            f"Generated: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(report.generated_at))}",
            "",
            f"**Total: {report.total_dependencies}** | "
            f"✅ {report.healthy} healthy | "
            f"⚠️ {report.degraded} degraded | "
            f"❌ {report.failing} failing",
            "",
            "## Dependencies",
            "",
            "| Dependency | Type | Health | Calls | Error Rate | p95 Latency |",
            "|---|---|---|---|---|---|",
        ]
        for dep in report.dependencies:
            icon = status_icon.get(dep["health"], "?")
            lines.append(
                f"| {dep['name']} | {dep.get('type', '?')} | {icon} {dep['health']} | "
                f"{dep.get('call_count', 0)} | {dep.get('error_rate', 0):.1%} | "
                f"{dep.get('p95_latency_ms', 0):.0f}ms |"
            )
        if report.recommendations:
            lines += ["", "## Recommendations", ""]
            for rec in report.recommendations:
                lines.append(f"- {rec}")
        return "\n".join(lines)

    def detect_regressions(self) -> list[dict]:
        """Compare current state to previous snapshot and flag regressions."""
        if len(self._snapshots) < 2:
            return []
        prev = self._snapshots[-2]["map"]
        curr = self._snapshots[-1]["map"]
        regressions = []
        for name, curr_stats in curr.items():
            if name not in prev:
                continue
            prev_stats = prev[name]
            err_delta = curr_stats.get("error_rate", 0) - prev_stats.get("error_rate", 0)
            lat_delta = curr_stats.get("p95_latency_ms", 0) - prev_stats.get("p95_latency_ms", 0)
            if err_delta > 0.02 or lat_delta > 200:
                regressions.append({
                    "dependency": name,
                    "error_rate_delta": err_delta,
                    "p95_latency_delta_ms": lat_delta,
                })
        return regressions
```

---

## Solution 6: Full DependencyTracker Facade

A unified `DependencyTracker` that wraps all discovery mechanisms and provides a single interface for instrumenting the entire agent.

```python
import asyncio
import time
from contextlib import asynccontextmanager


class DependencyTracker:
    """
    Unified dependency tracking facade.
    Instruments HTTP calls, LLM calls, database queries, and agent calls
    through a single interface.
    """

    def __init__(self, agent_id: str):
        self.agent_id = agent_id
        self._registry = DependencyRegistry()
        self._http = HTTPDependencyInterceptor(self._registry, agent_id)
        self._llm = InstrumentedAnthropicClient(self._registry)
        self._call_graph = AgentCallGraphTracker(agent_id)
        self._db_tracker: dict[str, DatabaseDependencyTracker] = {}
        self._dashboard = DependencyHealthDashboard(self._registry, agent_id)

    def http(self) -> HTTPDependencyInterceptor:
        return self._http

    def llm(self) -> InstrumentedAnthropicClient:
        return self._llm

    def db(self, db_name: str = "postgres") -> DatabaseDependencyTracker:
        if db_name not in self._db_tracker:
            tracker = DatabaseDependencyTracker(db_name)
            tracker.set_registry(self._registry)
            self._db_tracker[db_name] = tracker
        return self._db_tracker[db_name]

    def record_agent_call(self, callee_id: str, latency_ms: float, error: bool):
        self._call_graph.record_call(callee_id, latency_ms, error)
        self._registry.record(f"agent/{callee_id}", "agent", latency_ms, error)

    def map(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "dependencies": self._registry.get_map(),
            "call_graph": self._call_graph.call_graph(),
            "critical_dependencies": self._registry.critical_dependencies(),
            "cycles": self._call_graph.detect_cycles(),
            "llm_summary": self._llm.llm_summary(),
        }

    def health_report(self) -> str:
        report = self._dashboard.generate_report()
        return self._dashboard.to_markdown(report)

    def prometheus_metrics(self) -> str:
        lines = []
        for name, stats in self._registry.get_map().items():
            safe_name = name.replace("/", "_").replace(".", "_").replace("-", "_")
            lbl = f'dep="{name}"'
            lines += [
                f'agent_dep_calls_total{{{lbl}}} {stats["call_count"]}',
                f'agent_dep_error_rate{{{lbl}}} {stats["error_rate"]:.4f}',
                f'agent_dep_p95_latency_ms{{{lbl}}} {stats["p95_latency_ms"]:.1f}',
            ]
        return "\n".join(lines)

    @asynccontextmanager
    async def trace_request(self, trace_id: str):
        """Context manager for a single request: sets up call graph tracing."""
        self._call_graph.enter_call(trace_id)
        try:
            yield self
        finally:
            self._call_graph.exit_call(trace_id)
```

---

## Comparison

| Pattern | HTTP Deps | LLM Deps | DB Deps | Agent Deps | Visualization |
|---|---|---|---|---|---|
| HTTP interceptor | Yes | No | No | No | None |
| LLM call tracker | No | Yes (cost+tokens) | No | No | None |
| Agent call graph | No | No | No | Yes (cycles) | Mermaid |
| DB query tracker | No | No | Yes (per table) | No | None |
| Health dashboard | All (via registry) | All | All | All | Markdown |
| DependencyTracker (full) | Yes | Yes | Yes | Yes | All formats |

**Recommendations:**
- Deploy **DependencyTracker facade** (Solution 6) from the start — it's purely additive and answers "what does this agent call?" on day one.
- Use **LLM call tracker** (Solution 2) to get per-model cost attribution, which is often the largest operational cost line item.
- Use **agent call graph** (Solution 3) for multi-agent systems to detect circular dependencies before they cause production deadlocks.
- Add **database dependency tracker** (Solution 4) to identify hot tables and slow queries early, before they become bottlenecks.
- Generate the **health report** (Solution 5) weekly and review regressions — it surfaces silent degradations before they become incidents.
- Export Prometheus metrics from the tracker and add Grafana panels for real-time dependency health visibility.
