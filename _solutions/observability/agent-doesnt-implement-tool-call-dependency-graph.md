---
title: "Agent Doesn't Implement Tool Call Dependency Graph"
description: "AI agents execute tool calls without recording which calls depend on others; engineers cannot identify sequential bottlenecks, unnecessary serialization, or critical-path latency without a dependency graph."
category: observability
difficulty: intermediate
tags: [dependency-graph, tool-calls, dag, critical-path, observability, latency, parallelism]
---

# Agent Doesn't Implement Tool Call Dependency Graph

## Problem

An agent that makes 10 tool calls may complete them in 2 seconds or 20 seconds depending on whether they run in parallel or sequentially. Without a dependency graph, engineers cannot answer: "Why did this request take 15 seconds? Which tool was on the critical path? Could these three calls have run in parallel?" Instrumenting each tool call with dependency metadata enables automated critical-path analysis, parallelism recommendations, and bottleneck detection — the same visibility that distributed tracing gives to microservices.

## Solution 1: DAG Node Recording — Track Dependencies at Invocation Time

Record each tool call as a DAG node with its declared dependencies. Build the graph from execution metadata.

```python
import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

@dataclass
class ToolNode:
    node_id: str
    tool_name: str
    depends_on: list[str]   # node_ids of dependencies
    started_at: float | None = None
    completed_at: float | None = None
    result: any = None
    error: str | None = None

    @property
    def duration_ms(self) -> float | None:
        if self.started_at and self.completed_at:
            return round((self.completed_at - self.started_at) * 1000, 1)
        return None

    @property
    def success(self) -> bool:
        return self.error is None and self.result is not None

class ToolDependencyGraph:
    """
    Records tool calls and their dependencies as a DAG.
    Supports critical-path analysis and parallelism detection.
    """

    def __init__(self, request_id: str):
        self.request_id = request_id
        self._nodes: dict[str, ToolNode] = {}

    def add_node(self, tool_name: str, depends_on: list[str] | None = None) -> str:
        """Register a tool call. Returns node_id for use as dependency."""
        node_id = f"{tool_name}:{uuid.uuid4().hex[:8]}"
        self._nodes[node_id] = ToolNode(
            node_id=node_id,
            tool_name=tool_name,
            depends_on=depends_on or [],
        )
        return node_id

    def record_start(self, node_id: str) -> None:
        self._nodes[node_id].started_at = time.monotonic()

    def record_complete(self, node_id: str, result: any = None, error: str | None = None) -> None:
        node = self._nodes[node_id]
        node.completed_at = time.monotonic()
        node.result = result
        node.error = error

    def critical_path(self) -> list[str]:
        """
        Find the longest-duration path through the DAG (the critical path).
        Returns a list of node_ids in critical-path order.
        """
        if not self._nodes:
            return []

        # Build reverse index: node → children
        children: dict[str, list[str]] = {nid: [] for nid in self._nodes}
        for nid, node in self._nodes.items():
            for dep in node.depends_on:
                if dep in children:
                    children[dep].append(nid)

        # DP: compute earliest completion time for each node
        earliest_end: dict[str, float] = {}
        path_to: dict[str, str | None] = {}

        def compute(nid: str) -> float:
            if nid in earliest_end:
                return earliest_end[nid]
            node = self._nodes[nid]
            dur = (node.duration_ms or 0) / 1000

            if not node.depends_on:
                earliest_end[nid] = dur
                path_to[nid] = None
            else:
                max_dep_end = 0.0
                critical_dep = None
                for dep in node.depends_on:
                    dep_end = compute(dep)
                    if dep_end > max_dep_end:
                        max_dep_end = dep_end
                        critical_dep = dep
                earliest_end[nid] = max_dep_end + dur
                path_to[nid] = critical_dep
            return earliest_end[nid]

        for nid in self._nodes:
            compute(nid)

        # Find terminal node with max earliest_end
        terminal = max(self._nodes.keys(), key=lambda n: earliest_end.get(n, 0))

        # Trace back critical path
        path = []
        current: str | None = terminal
        while current is not None:
            path.append(current)
            current = path_to.get(current)
        path.reverse()
        return path

    def parallelizable_groups(self) -> list[list[str]]:
        """
        Group nodes by dependency level (nodes at the same level can run in parallel).
        """
        levels: dict[str, int] = {}

        def level(nid: str) -> int:
            if nid in levels:
                return levels[nid]
            node = self._nodes[nid]
            if not node.depends_on:
                levels[nid] = 0
            else:
                levels[nid] = 1 + max(level(dep) for dep in node.depends_on if dep in self._nodes)
            return levels[nid]

        for nid in self._nodes:
            level(nid)

        max_level = max(levels.values(), default=0)
        groups = [[] for _ in range(max_level + 1)]
        for nid, lvl in levels.items():
            groups[lvl].append(nid)
        return groups

    def summary(self) -> dict:
        nodes = list(self._nodes.values())
        total_sequential_ms = sum(n.duration_ms or 0 for n in nodes)
        cp = self.critical_path()
        cp_ms = sum((self._nodes[nid].duration_ms or 0) for nid in cp)
        parallelism_savings = total_sequential_ms - cp_ms

        return {
            "request_id": self.request_id,
            "total_nodes": len(nodes),
            "total_sequential_ms": round(total_sequential_ms, 1),
            "critical_path_ms": round(cp_ms, 1),
            "critical_path": [self._nodes[n].tool_name for n in cp],
            "parallelism_savings_ms": round(parallelism_savings, 1),
            "success_rate": round(sum(1 for n in nodes if n.success) / len(nodes), 2) if nodes else 0,
        }

async def agent_with_dependency_graph(user_query: str) -> dict:
    dag = ToolDependencyGraph(request_id=str(uuid.uuid4())[:8])

    async def run_node(node_id: str, coro, dep_futures: list[asyncio.Task]) -> any:
        # Wait for all dependencies to complete
        if dep_futures:
            await asyncio.gather(*dep_futures)
        dag.record_start(node_id)
        try:
            result = await coro
            dag.record_complete(node_id, result=result)
            return result
        except Exception as exc:
            dag.record_complete(node_id, error=str(exc))
            raise

    # Define the dependency graph
    search_id = dag.add_node("web_search", depends_on=[])
    user_id = dag.add_node("user_profile", depends_on=[])
    product_id = dag.add_node("product_catalog", depends_on=[])
    # Personalize depends on both user_profile and product_catalog
    personal_id = dag.add_node("personalize", depends_on=[user_id, product_id])
    # Summarize depends on web_search and personalize
    summary_id = dag.add_node("summarize", depends_on=[search_id, personal_id])

    async def fake_tool(name: str, delay: float):
        await asyncio.sleep(delay)
        return {"tool": name, "status": "ok"}

    # Execute with declared dependencies
    search_task = asyncio.create_task(run_node(search_id, fake_tool("web_search", 0.3), []))
    user_task = asyncio.create_task(run_node(user_id, fake_tool("user_profile", 0.1), []))
    product_task = asyncio.create_task(run_node(product_id, fake_tool("product_catalog", 0.2), []))
    personal_task = asyncio.create_task(run_node(personal_id, fake_tool("personalize", 0.15), [user_task, product_task]))
    summary_task = asyncio.create_task(run_node(summary_id, fake_tool("summarize", 0.1), [search_task, personal_task]))

    await summary_task
    return dag.summary()
```

**When to use**: Any agent that makes multiple tool calls per request. The dependency graph reveals whether tool calls are correctly parallelized and which is on the critical path.

---

## Solution 2: Automatic Dependency Detection — Infer Dependencies from Data Flow

Automatically detect which tool results are used as inputs to subsequent tool calls by tracking data flow between nodes.

```python
import asyncio
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

@dataclass
class DataFlowNode:
    node_id: str
    tool_name: str
    inputs_from: list[str] = field(default_factory=list)   # node_ids whose output fed this input
    output_hash: str | None = None
    duration_ms: float | None = None

class DataFlowTracker:
    """
    Tracks which tool outputs are consumed as inputs by other tool calls.
    Dependencies are auto-detected from data flow, not manually declared.
    """

    def __init__(self):
        self._nodes: dict[str, DataFlowNode] = {}
        self._output_registry: dict[str, str] = {}  # hash → node_id

    def _hash_value(self, value: any) -> str:
        raw = json.dumps(value, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode()).hexdigest()[:12]

    def record_call(self, tool_name: str, inputs: list[any], output: any, duration_ms: float) -> str:
        node_id = f"{tool_name}:{uuid.uuid4().hex[:6]}"

        # Find which nodes produced these inputs
        inputs_from = []
        for inp in inputs:
            inp_hash = self._hash_value(inp)
            producer = self._output_registry.get(inp_hash)
            if producer:
                inputs_from.append(producer)

        # Register this node's output
        if output is not None:
            out_hash = self._hash_value(output)
            self._output_registry[out_hash] = node_id

        self._nodes[node_id] = DataFlowNode(
            node_id=node_id,
            tool_name=tool_name,
            inputs_from=inputs_from,
            output_hash=self._hash_value(output),
            duration_ms=round(duration_ms, 1),
        )
        return node_id

    def serialized_pairs(self) -> list[tuple[str, str]]:
        """Return pairs of (producer, consumer) that ran sequentially but could be parallelized."""
        pairs = []
        for node in self._nodes.values():
            for dep_id in node.inputs_from:
                pairs.append((self._nodes[dep_id].tool_name, node.tool_name))
        return pairs

    def report(self) -> dict:
        return {
            "nodes": len(self._nodes),
            "data_dependencies": self.serialized_pairs(),
            "total_ms": sum(n.duration_ms or 0 for n in self._nodes.values()),
        }

tracker = DataFlowTracker()

async def tracked_tool_call(tool_name: str, fn, inputs: list) -> any:
    import time
    start = time.monotonic()
    result = await fn()
    elapsed = (time.monotonic() - start) * 1000
    tracker.record_call(tool_name, inputs=inputs, output=result, duration_ms=elapsed)
    return result

async def demo_data_flow():
    user_id_result = await tracked_tool_call(
        "get_user_id",
        lambda: asyncio.sleep(0.05) or asyncio.coroutine(lambda: {"user_id": "u123"})(),
        inputs=[],
    )
    # Simplified — in practice use real async calls
    user_id_result = {"user_id": "u123"}
    tracker.record_call("get_user_id", inputs=[], output=user_id_result, duration_ms=50)

    orders_result = {"orders": [1, 2, 3]}
    tracker.record_call("get_orders", inputs=[user_id_result], output=orders_result, duration_ms=80)

    summary_result = {"summary": "3 orders"}
    tracker.record_call("summarize_orders", inputs=[orders_result], output=summary_result, duration_ms=30)

    return tracker.report()
```

**When to use**: Agents where data flow between tool calls isn't manually declared. Auto-detection from data hashes catches implicit dependencies that developers forgot to model.

---

## Solution 3: Critical-Path Alerting — Notify When a Tool Dominates Latency

Alert when a single tool call accounts for more than a configurable fraction of total request latency.

```python
import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic

client = AsyncAnthropic()
logger = logging.getLogger("critical_path")

@dataclass
class LatencyNode:
    tool_name: str
    node_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    start_ms: float = 0.0
    end_ms: float = 0.0

    @property
    def duration_ms(self) -> float:
        return self.end_ms - self.start_ms

class CriticalPathAlerter:
    """
    Detects when a single tool call dominates request latency.
    """

    def __init__(self, dominance_threshold: float = 0.6, alert_min_ms: float = 500.0):
        self._threshold = dominance_threshold  # alert if any tool > 60% of total
        self._alert_min_ms = alert_min_ms     # only alert if total > 500ms
        self._nodes: list[LatencyNode] = []
        self._request_start = time.monotonic() * 1000

    def start_tool(self, tool_name: str) -> LatencyNode:
        node = LatencyNode(tool_name=tool_name, start_ms=time.monotonic() * 1000)
        self._nodes.append(node)
        return node

    def end_tool(self, node: LatencyNode) -> None:
        node.end_ms = time.monotonic() * 1000

    def analyze(self) -> dict:
        if not self._nodes:
            return {}

        total_ms = sum(n.duration_ms for n in self._nodes)
        request_ms = time.monotonic() * 1000 - self._request_start
        slowest = max(self._nodes, key=lambda n: n.duration_ms)
        dominance = slowest.duration_ms / total_ms if total_ms > 0 else 0

        alert = None
        if total_ms >= self._alert_min_ms and dominance >= self._threshold:
            alert = {
                "type": "critical_path_dominance",
                "tool": slowest.tool_name,
                "tool_ms": round(slowest.duration_ms, 1),
                "total_ms": round(total_ms, 1),
                "dominance_pct": round(dominance * 100, 1),
                "recommendation": f"Optimize or parallelize '{slowest.tool_name}'",
            }
            logger.warning("critical_path_alert", extra=alert)

        return {
            "request_ms": round(request_ms, 1),
            "total_tool_ms": round(total_ms, 1),
            "slowest_tool": slowest.tool_name,
            "slowest_ms": round(slowest.duration_ms, 1),
            "dominance_pct": round(dominance * 100, 1),
            "alert": alert,
            "tools": [{"name": n.tool_name, "ms": round(n.duration_ms, 1)} for n in self._nodes],
        }

async def agent_with_alerting(user_query: str) -> dict:
    alerter = CriticalPathAlerter(dominance_threshold=0.6, alert_min_ms=200)

    async def slow_db():
        await asyncio.sleep(0.8)  # 800ms — will dominate
        return {"rows": 5}

    async def fast_cache():
        await asyncio.sleep(0.05)
        return {"cached": True}

    db_node = alerter.start_tool("database")
    db_result = await slow_db()
    alerter.end_tool(db_node)

    cache_node = alerter.start_tool("cache")
    cache_result = await fast_cache()
    alerter.end_tool(cache_node)

    analysis = alerter.analyze()

    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": user_query}],
    )

    return {"response": resp.content[0].text, "latency_analysis": analysis}
```

**When to use**: Production agents with latency SLAs. Critical-path dominance alerts tell you which tool to optimize first — no guesswork needed.

---

## Solution 4: Parallelism Detector — Find Sequential Calls That Could Run in Parallel

Automatically identify tool calls that have no data dependencies between them but ran sequentially.

```python
import asyncio
import time
import uuid
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

@dataclass
class SequenceRecord:
    tool_name: str
    node_id: str
    start_ms: float
    end_ms: float
    inputs_used: set[str] = field(default_factory=set)   # node_ids whose output was used
    outputs_produced: set[str] = field(default_factory=set)  # symbolic output names

    @property
    def duration_ms(self) -> float:
        return self.end_ms - self.start_ms

class ParallelismDetector:
    """
    Detects tool calls that ran sequentially but could have run in parallel
    (no data dependency between them).
    """

    def __init__(self):
        self._sequence: list[SequenceRecord] = []

    def record(self, tool_name: str, start_ms: float, end_ms: float,
               inputs_used: set[str] | None = None,
               outputs_produced: set[str] | None = None) -> str:
        node_id = f"{tool_name}:{uuid.uuid4().hex[:6]}"
        self._sequence.append(SequenceRecord(
            tool_name=tool_name,
            node_id=node_id,
            start_ms=start_ms,
            end_ms=end_ms,
            inputs_used=inputs_used or set(),
            outputs_produced=outputs_produced or set(),
        ))
        return node_id

    def missed_parallelism(self) -> list[dict]:
        """
        Find pairs of sequentially-run nodes that had no data dependency.
        These are candidates for parallelization.
        """
        opportunities = []
        n = len(self._sequence)
        for i in range(n):
            for j in range(i + 1, n):
                a, b = self._sequence[i], self._sequence[j]

                # Check if they ran sequentially (b started after a ended)
                ran_sequentially = b.start_ms >= a.end_ms

                if not ran_sequentially:
                    continue

                # Check for data dependency (b used output that a produced)
                has_dependency = bool(a.outputs_produced & b.inputs_used)

                if not has_dependency:
                    savings_ms = b.duration_ms  # if parallel: b's duration is hidden
                    opportunities.append({
                        "tool_a": a.tool_name,
                        "tool_b": b.tool_name,
                        "potential_savings_ms": round(savings_ms, 1),
                        "recommendation": f"Run '{a.tool_name}' and '{b.tool_name}' in parallel with asyncio.gather()",
                    })

        return sorted(opportunities, key=lambda x: -x["potential_savings_ms"])

    def report(self) -> dict:
        total_ms = sum(r.duration_ms for r in self._sequence)
        opportunities = self.missed_parallelism()
        max_savings = sum(o["potential_savings_ms"] for o in opportunities)
        return {
            "total_sequential_ms": round(total_ms, 1),
            "parallelism_opportunities": opportunities,
            "max_savings_ms": round(max_savings, 1),
            "max_speedup_pct": round(100 * max_savings / total_ms, 1) if total_ms else 0,
        }

async def demo_detector():
    detector = ParallelismDetector()
    t0 = time.monotonic() * 1000

    await asyncio.sleep(0.1)
    detector.record("search",  t0,        t0 + 100, inputs_used=set(), outputs_produced={"search_results"})
    await asyncio.sleep(0.2)
    detector.record("weather", t0 + 100,  t0 + 300, inputs_used=set(), outputs_produced={"weather_data"})
    await asyncio.sleep(0.05)
    detector.record("summary", t0 + 300,  t0 + 350, inputs_used={"search_results", "weather_data"}, outputs_produced={"summary"})

    report = detector.report()
    # search and weather have no dependency → should have run in parallel
    # summary depends on both → must run after
    return report
```

**When to use**: Agent optimization. Run this detector in staging to find serial bottlenecks before they reach production. It directly tells you which `asyncio.gather()` calls to add.

---

## Solution 5: Dependency Graph Visualization — Export as DOT for Graphviz

Export the tool dependency graph in Graphviz DOT format for visual inspection by engineers.

```python
import asyncio
import time
import uuid
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

@dataclass
class VizNode:
    node_id: str
    tool_name: str
    depends_on: list[str]
    duration_ms: float = 0.0
    success: bool = True

class GraphVizExporter:
    """
    Builds a tool dependency graph and exports it as Graphviz DOT format.
    Paste output into https://dreampuf.github.io/GraphvizOnline/ to visualize.
    """

    def __init__(self, request_id: str):
        self.request_id = request_id
        self._nodes: dict[str, VizNode] = {}

    def add(self, tool_name: str, depends_on: list[str], duration_ms: float, success: bool = True) -> str:
        node_id = f"{tool_name}_{uuid.uuid4().hex[:4]}"
        self._nodes[node_id] = VizNode(
            node_id=node_id,
            tool_name=tool_name,
            depends_on=depends_on,
            duration_ms=duration_ms,
            success=success,
        )
        return node_id

    def to_dot(self) -> str:
        """Generate Graphviz DOT notation."""
        lines = [
            f'digraph "request_{self.request_id}" {{',
            '  rankdir=LR;',
            '  node [shape=box, style=filled];',
            '',
        ]

        # Nodes
        for nid, node in self._nodes.items():
            color = "lightgreen" if node.success else "salmon"
            label = f"{node.tool_name}\\n{node.duration_ms:.0f}ms"
            lines.append(f'  "{nid}" [label="{label}", fillcolor="{color}"];')

        lines.append("")

        # Edges
        for nid, node in self._nodes.items():
            for dep in node.depends_on:
                lines.append(f'  "{dep}" -> "{nid}";')

        lines.append("}")
        return "\n".join(lines)

    def to_mermaid(self) -> str:
        """Generate Mermaid flowchart notation (renders in GitHub/Notion)."""
        lines = ["graph LR"]
        for nid, node in self._nodes.items():
            label = f"{node.tool_name} ({node.duration_ms:.0f}ms)"
            lines.append(f'  {nid}["{label}"]')
        for nid, node in self._nodes.items():
            for dep in node.depends_on:
                lines.append(f"  {dep} --> {nid}")
        return "\n".join(lines)

async def demo_viz():
    exporter = GraphVizExporter(request_id="abc123")

    search_id = exporter.add("web_search",     depends_on=[],                  duration_ms=320)
    user_id   = exporter.add("user_profile",   depends_on=[],                  duration_ms=80)
    prods_id  = exporter.add("products",       depends_on=[],                  duration_ms=150)
    rank_id   = exporter.add("rank_results",   depends_on=[search_id, prods_id], duration_ms=45)
    reply_id  = exporter.add("generate_reply", depends_on=[rank_id, user_id],  duration_ms=380)

    print("=== DOT (paste into Graphviz) ===")
    print(exporter.to_dot())
    print("\n=== Mermaid (paste into GitHub) ===")
    print(exporter.to_mermaid())
    return {"dot": exporter.to_dot(), "mermaid": exporter.to_mermaid()}
```

**When to use**: Engineering reviews and post-mortems. A visual dependency graph makes it immediately obvious which calls are on the critical path and which could be parallelized — no spreadsheet analysis required.

---

## Solution 6: Dependency Graph Metrics — Export to Prometheus for Dashboards

Emit dependency graph metrics to Prometheus so latency and parallelism statistics appear in Grafana dashboards alongside other agent metrics.

```python
import asyncio
import time
import uuid
from dataclasses import dataclass, field

try:
    from prometheus_client import Histogram, Counter, Gauge
    PROMETHEUS_AVAILABLE = True
    tool_duration_hist = Histogram(
        "agent_tool_duration_ms",
        "Tool call duration in milliseconds",
        ["tool_name", "critical_path"],
        buckets=[10, 50, 100, 250, 500, 1000, 2500, 5000],
    )
    request_parallelism_gauge = Gauge(
        "agent_request_parallelism_ratio",
        "Ratio of parallel execution vs sequential: actual/sequential",
    )
    critical_path_counter = Counter(
        "agent_critical_path_tool_total",
        "Number of times each tool was on the critical path",
        ["tool_name"],
    )
except ImportError:
    PROMETHEUS_AVAILABLE = False

@dataclass
class MetricNode:
    tool_name: str
    node_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    duration_ms: float = 0.0
    depends_on: list[str] = field(default_factory=list)
    on_critical_path: bool = False

class DependencyGraphMetricsEmitter:
    def __init__(self):
        self._nodes: dict[str, MetricNode] = {}

    def record(self, tool_name: str, duration_ms: float, depends_on: list[str] | None = None) -> str:
        node_id = f"{tool_name}:{uuid.uuid4().hex[:6]}"
        self._nodes[node_id] = MetricNode(
            tool_name=tool_name, node_id=node_id,
            duration_ms=duration_ms, depends_on=depends_on or [],
        )
        return node_id

    def _compute_critical_path(self) -> list[str]:
        memo: dict[str, float] = {}
        prev: dict[str, str | None] = {}

        def dp(nid: str) -> float:
            if nid in memo:
                return memo[nid]
            node = self._nodes[nid]
            if not node.depends_on:
                memo[nid] = node.duration_ms
                prev[nid] = None
            else:
                max_dep, max_dep_id = 0.0, None
                for dep in node.depends_on:
                    if dep in self._nodes:
                        val = dp(dep)
                        if val > max_dep:
                            max_dep, max_dep_id = val, dep
                memo[nid] = max_dep + node.duration_ms
                prev[nid] = max_dep_id
            return memo[nid]

        for nid in self._nodes:
            dp(nid)

        if not memo:
            return []
        terminal = max(memo, key=memo.get)
        path = []
        cur: str | None = terminal
        while cur is not None:
            path.append(cur)
            cur = prev.get(cur)
        return path

    def emit_metrics(self) -> dict:
        if not self._nodes:
            return {}

        cp = set(self._compute_critical_path())
        for nid in cp:
            self._nodes[nid].on_critical_path = True

        total_seq_ms = sum(n.duration_ms for n in self._nodes.values())
        cp_ms = sum(self._nodes[nid].duration_ms for nid in cp if nid in self._nodes)

        if PROMETHEUS_AVAILABLE:
            for nid, node in self._nodes.items():
                tool_duration_hist.labels(
                    tool_name=node.tool_name,
                    critical_path="yes" if node.on_critical_path else "no",
                ).observe(node.duration_ms)
            if total_seq_ms > 0:
                request_parallelism_gauge.set(round(cp_ms / total_seq_ms, 3))
            for nid in cp:
                if nid in self._nodes:
                    critical_path_counter.labels(tool_name=self._nodes[nid].tool_name).inc()

        return {
            "total_sequential_ms": round(total_seq_ms, 1),
            "critical_path_ms": round(cp_ms, 1),
            "parallelism_ratio": round(cp_ms / total_seq_ms, 3) if total_seq_ms else 1.0,
            "critical_path_tools": [self._nodes[n].tool_name for n in cp if n in self._nodes],
        }

emitter = DependencyGraphMetricsEmitter()
```

**When to use**: Production agents with Prometheus/Grafana. The `agent_tool_duration_ms{critical_path="yes"}` metric shows the distribution of critical-path tool latencies over time, immediately highlighting regressions.

---

## Comparison

| Solution | Insight | Online | Setup | Visualization | Alerting | Best For |
|---|---|---|---|---|---|---|
| DAG node recording | Full graph + critical path | Yes | Medium | No | No | Request-level analysis |
| Auto dependency detection | Data flow graph | Yes | Medium | No | No | Unknown dependencies |
| Critical-path alerting | Dominance detection | Yes | Low | No | Yes | SLA enforcement |
| Parallelism detector | Missing gather() calls | Yes | Low | No | No | Optimization discovery |
| DOT/Mermaid export | Visual graph | No | Low | Yes | No | Engineering reviews |
| Prometheus metrics | Trending over time | Yes | High | Grafana | Via alerts | Production dashboards |

**Rule of thumb**: Start with the parallelism detector (Solution 4) — it finds the quickest wins (missing `asyncio.gather()` calls). Add critical-path alerting (Solution 3) in production. Add Prometheus metrics (Solution 6) when you have Grafana to visualize them.
