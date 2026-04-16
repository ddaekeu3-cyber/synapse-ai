---
layout: solution
title: "Agent Doesn't Implement Subagent Execution Log Aggregation"
category: observability
description: "Aggregate logs from all spawned subagents into a unified, structured stream with correlation IDs so operators can reconstruct the full execution timeline across parallel workers."
tags: [observability, logging, subagents, aggregation, correlation, tracing, multi-agent]
---

# Agent Doesn't Implement Subagent Execution Log Aggregation

## Problem

A coordinator spawns ten subagents to process tasks in parallel. Each subagent writes its own logs to stdout — or worse, nothing at all. When something fails, operators face a jumble of interleaved, unattributed log lines with no way to reconstruct which subagent produced which output, in which order, or how long each step took. Debugging requires guesswork instead of evidence.

## Solution Options

### Option 1: Centralized Log Queue with Subagent IDs

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field


@dataclass
class LogEntry:
    timestamp: float
    agent_id: str
    level: str
    message: str
    task_id: int | None = None

    def format(self) -> str:
        ts = time.strftime("%H:%M:%S", time.localtime(self.timestamp))
        task = f"[task={self.task_id}]" if self.task_id is not None else ""
        return f"{ts} [{self.level:<5}] [{self.agent_id}]{task} {self.message}"


class LogAggregator:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[LogEntry] = asyncio.Queue()
        self._entries: list[LogEntry] = []
        self._running = True

    async def log(self, agent_id: str, level: str, message: str, task_id: int | None = None) -> None:
        entry = LogEntry(
            timestamp=time.time(),
            agent_id=agent_id,
            level=level,
            message=message,
            task_id=task_id,
        )
        await self._queue.put(entry)

    async def drain(self) -> None:
        """Process all queued log entries."""
        while not self._queue.empty():
            entry = await self._queue.get()
            self._entries.append(entry)
            print(entry.format())

    async def run_drain_loop(self) -> None:
        while self._running:
            await self.drain()
            await asyncio.sleep(0.05)

    def stop(self) -> None:
        self._running = False

    def entries_for(self, agent_id: str) -> list[LogEntry]:
        return [e for e in self._entries if e.agent_id == agent_id]


async def subagent(
    agent_id: str,
    task_id: int,
    prompt: str,
    client: anthropic.AsyncAnthropic,
    log: LogAggregator,
) -> str:
    await log.log(agent_id, "INFO", f"Starting task: {prompt[:40]}", task_id)
    try:
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=[{"role": "user", "content": prompt}],
        )
        result = resp.content[0].text.strip()
        await log.log(agent_id, "INFO", f"Completed: {result[:40]}", task_id)
        return result
    except Exception as e:
        await log.log(agent_id, "ERROR", f"Failed: {e}", task_id)
        raise


async def coordinator(tasks: list[str]) -> None:
    client = anthropic.AsyncAnthropic()
    log = LogAggregator()
    drain_task = asyncio.create_task(log.run_drain_loop())

    await log.log("coordinator", "INFO", f"Spawning {len(tasks)} subagents")
    coros = [
        subagent(f"agent-{i:02d}", i, task, client, log)
        for i, task in enumerate(tasks)
    ]
    results = await asyncio.gather(*coros, return_exceptions=True)

    await log.log("coordinator", "INFO", f"All done. Results: {len(results)}")
    log.stop()
    await asyncio.sleep(0.1)
    drain_task.cancel()
    await client.close()

    print(f"\nAgent-00 logs: {len(log.entries_for('agent-00'))} entries")


if __name__ == "__main__":
    tasks = [f"Name one famous {topic}" for topic in ["scientist", "painter", "musician", "author"]]
    asyncio.run(coordinator(tasks))

# Expected Token Savings: No extra tokens; all subagent output visible in one unified stream
# Environment: Any async multi-agent coordinator with parallel subagent execution
```

---

### Option 2: Structured JSON Log Aggregation with Trace Context

```python
import anthropic
import asyncio
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from io import StringIO


@dataclass
class TraceLog:
    trace_id: str
    span_id: str
    parent_span_id: str | None
    agent_id: str
    event: str
    level: str = "INFO"
    timestamp: float = field(default_factory=time.time)
    duration_ms: float | None = None
    metadata: dict = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self))


class StructuredLogAggregator:
    """
    Collects structured JSON logs from all subagents.
    Each log entry carries trace_id + span hierarchy for distributed tracing.
    """

    def __init__(self, output: StringIO | None = None) -> None:
        self._queue: asyncio.Queue[TraceLog] = asyncio.Queue()
        self._store: list[TraceLog] = []
        self._output = output or StringIO()

    def make_span(
        self,
        agent_id: str,
        trace_id: str,
        parent_span_id: str | None = None,
    ) -> str:
        return uuid.uuid4().hex[:12]

    async def emit(self, log: TraceLog) -> None:
        await self._queue.put(log)

    async def flush(self) -> None:
        while not self._queue.empty():
            entry = await self._queue.get()
            self._store.append(entry)
            line = entry.to_json()
            print(line)
            self._output.write(line + "\n")

    def query(self, trace_id: str) -> list[TraceLog]:
        return sorted(
            [e for e in self._store if e.trace_id == trace_id],
            key=lambda e: e.timestamp,
        )

    def agent_timeline(self, agent_id: str) -> list[TraceLog]:
        return sorted(
            [e for e in self._store if e.agent_id == agent_id],
            key=lambda e: e.timestamp,
        )


async def traced_subagent(
    agent_id: str,
    prompt: str,
    trace_id: str,
    parent_span_id: str,
    log: StructuredLogAggregator,
    client: anthropic.AsyncAnthropic,
) -> str:
    span_id = uuid.uuid4().hex[:12]
    start = time.perf_counter()

    await log.emit(TraceLog(
        trace_id=trace_id, span_id=span_id, parent_span_id=parent_span_id,
        agent_id=agent_id, event="task.start", metadata={"prompt": prompt[:40]},
    ))

    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": prompt}],
    )
    result = resp.content[0].text.strip()
    elapsed = (time.perf_counter() - start) * 1000

    await log.emit(TraceLog(
        trace_id=trace_id, span_id=span_id, parent_span_id=parent_span_id,
        agent_id=agent_id, event="task.complete", duration_ms=round(elapsed, 1),
        metadata={"result": result[:40], "tokens": resp.usage.output_tokens},
    ))
    return result


async def main() -> None:
    client = anthropic.AsyncAnthropic()
    log = StructuredLogAggregator()
    trace_id = uuid.uuid4().hex
    root_span = uuid.uuid4().hex[:12]

    tasks = ["Explain AI in 5 words", "Name a planet", "Define recursion briefly"]
    coros = [
        traced_subagent(f"agent-{i}", task, trace_id, root_span, log, client)
        for i, task in enumerate(tasks)
    ]
    results = await asyncio.gather(*coros)

    await log.flush()
    timeline = log.query(trace_id)
    print(f"\nTrace {trace_id[:8]}: {len(timeline)} events across {len(tasks)} agents")
    await client.close()


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: No extra tokens; JSON logs feed directly into log aggregators (Loki, CloudWatch)
# Environment: Production multi-agent systems requiring distributed tracing integration
```

---

### Option 3: Log Aggregator with Per-Agent Buffering and Flush on Error

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field
from collections import defaultdict


@dataclass
class BufferedEntry:
    level: str
    message: str
    timestamp: float = field(default_factory=time.time)


class BufferedLogAggregator:
    """
    Each subagent buffers its own logs locally.
    On success: flush as a single compressed summary.
    On error: flush full verbose log for forensics.
    This reduces noise from successful agents while preserving debug info on failures.
    """

    def __init__(self) -> None:
        self._buffers: dict[str, list[BufferedEntry]] = defaultdict(list)
        self._flushed: list[dict] = []

    def log(self, agent_id: str, level: str, message: str) -> None:
        self._buffers[agent_id].append(BufferedEntry(level=level, message=message))

    def flush_success(self, agent_id: str, summary: str) -> None:
        count = len(self._buffers[agent_id])
        self._flushed.append({"agent_id": agent_id, "status": "ok", "summary": summary, "log_count": count})
        print(f"[{agent_id}] OK ({count} log lines suppressed) → {summary[:50]}")
        del self._buffers[agent_id]

    def flush_error(self, agent_id: str, error: str) -> None:
        entries = self._buffers.pop(agent_id, [])
        print(f"\n[{agent_id}] FAILED — full log dump:")
        for e in entries:
            ts = time.strftime("%H:%M:%S", time.localtime(e.timestamp))
            print(f"  {ts} [{e.level}] {e.message}")
        print(f"  ERROR: {error}\n")
        self._flushed.append({"agent_id": agent_id, "status": "error", "error": error})

    def stats(self) -> dict:
        ok = sum(1 for f in self._flushed if f["status"] == "ok")
        err = sum(1 for f in self._flushed if f["status"] == "error")
        return {"ok": ok, "error": err, "total": len(self._flushed)}


async def subagent_with_buffer(
    agent_id: str,
    prompt: str,
    agg: BufferedLogAggregator,
    client: anthropic.AsyncAnthropic,
    should_fail: bool = False,
) -> str | None:
    agg.log(agent_id, "DEBUG", f"Received task: {prompt[:40]}")
    agg.log(agent_id, "DEBUG", "Calling Anthropic API")

    if should_fail:
        agg.log(agent_id, "ERROR", "Simulated failure before API call")
        agg.flush_error(agent_id, "Intentional test failure")
        return None

    try:
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=32,
            messages=[{"role": "user", "content": prompt}],
        )
        result = resp.content[0].text.strip()
        agg.log(agent_id, "DEBUG", f"Got response: {result[:30]}")
        agg.log(agent_id, "INFO", "Task complete")
        agg.flush_success(agent_id, result)
        return result
    except Exception as e:
        agg.flush_error(agent_id, str(e))
        return None


async def main() -> None:
    client = anthropic.AsyncAnthropic()
    agg = BufferedLogAggregator()

    tasks = [
        ("agent-A", "Define 'entropy' in one sentence", False),
        ("agent-B", "Name the fastest animal", False),
        ("agent-C", "This will fail", True),
        ("agent-D", "What color is the sky?", False),
    ]

    await asyncio.gather(
        *[subagent_with_buffer(aid, prompt, agg, client, fail) for aid, prompt, fail in tasks]
    )
    print("\nStats:", agg.stats())
    await client.close()


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: No extra tokens; verbose logs only emitted on failure — low noise in production
# Environment: Large fan-out pipelines where success is common and verbose logs cause alert fatigue
```

---

### Option 4: Real-Time Log Streaming with WebSocket-Style Fan-Out

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass


@dataclass
class LogEvent:
    agent_id: str
    level: str
    message: str
    ts: float = 0.0

    def __post_init__(self) -> None:
        if not self.ts:
            self.ts = time.time()


class LogBroadcaster:
    """
    Fan-out pattern: subagents publish log events; multiple consumers can subscribe.
    Useful for: UI dashboards, alert systems, log shippers — all receiving in real time.
    """

    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue[LogEvent | None]] = []

    def subscribe(self) -> asyncio.Queue[LogEvent | None]:
        q: asyncio.Queue[LogEvent | None] = asyncio.Queue()
        self._subscribers.append(q)
        return q

    async def publish(self, event: LogEvent) -> None:
        for q in self._subscribers:
            await q.put(event)

    async def close(self) -> None:
        for q in self._subscribers:
            await q.put(None)  # sentinel


async def log_printer(q: asyncio.Queue[LogEvent | None], label: str) -> None:
    """Consumer: prints all events (simulates a log shipper)."""
    while True:
        event = await q.get()
        if event is None:
            return
        ts = time.strftime("%H:%M:%S", time.localtime(event.ts))
        print(f"[{label}] {ts} [{event.agent_id}] [{event.level}] {event.message}")


async def alert_consumer(q: asyncio.Queue[LogEvent | None]) -> None:
    """Consumer: alerts only on ERROR-level events."""
    while True:
        event = await q.get()
        if event is None:
            return
        if event.level == "ERROR":
            print(f"  !! ALERT from {event.agent_id}: {event.message}")


async def subagent(
    agent_id: str,
    prompt: str,
    broadcaster: LogBroadcaster,
    client: anthropic.AsyncAnthropic,
) -> str:
    await broadcaster.publish(LogEvent(agent_id, "INFO", f"Start: {prompt[:30]}"))
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=32,
        messages=[{"role": "user", "content": prompt}],
    )
    result = resp.content[0].text.strip()
    await broadcaster.publish(LogEvent(agent_id, "INFO", f"Done: {result[:30]}"))
    return result


async def main() -> None:
    client = anthropic.AsyncAnthropic()
    broadcaster = LogBroadcaster()

    printer_q = broadcaster.subscribe()
    alert_q = broadcaster.subscribe()

    consumer_tasks = [
        asyncio.create_task(log_printer(printer_q, "STREAM")),
        asyncio.create_task(alert_consumer(alert_q)),
    ]

    tasks = [
        subagent("agent-1", "Name a star", broadcaster, client),
        subagent("agent-2", "Name a river", broadcaster, client),
        subagent("agent-3", "Name a mountain", broadcaster, client),
    ]
    await asyncio.gather(*tasks)
    await broadcaster.close()
    await asyncio.gather(*consumer_tasks)
    await client.close()


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: No extra tokens; fan-out enables simultaneous UI + alerting + shipping
# Environment: Agent platforms with real-time monitoring dashboards or alert integrations
```

---

### Option 5: SQLite Log Aggregation with Post-Run Analysis

```python
import anthropic
import asyncio
import sqlite3
import time
import uuid
from dataclasses import dataclass


@dataclass
class DBLogEntry:
    run_id: str
    agent_id: str
    level: str
    message: str
    timestamp: float
    duration_ms: float | None = None


class SQLiteLogAggregator:
    """
    Persists all subagent logs to SQLite.
    Enables post-run queries: slowest agents, error frequency, task timelines.
    """

    def __init__(self, db_path: str = ":memory:") -> None:
        self.db_path = db_path
        self._con = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = asyncio.Lock()
        self._con.execute("""
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT, agent_id TEXT, level TEXT,
                message TEXT, timestamp REAL, duration_ms REAL
            )
        """)
        self._con.execute("CREATE INDEX IF NOT EXISTS idx_run ON logs(run_id)")
        self._con.execute("CREATE INDEX IF NOT EXISTS idx_agent ON logs(agent_id)")
        self._con.commit()

    async def log(self, entry: DBLogEntry) -> None:
        async with self._lock:
            self._con.execute(
                "INSERT INTO logs (run_id, agent_id, level, message, timestamp, duration_ms) VALUES (?,?,?,?,?,?)",
                (entry.run_id, entry.agent_id, entry.level, entry.message, entry.timestamp, entry.duration_ms),
            )
            self._con.commit()

    def query_run(self, run_id: str) -> list[dict]:
        rows = self._con.execute(
            "SELECT agent_id, level, message, timestamp, duration_ms FROM logs WHERE run_id=? ORDER BY timestamp",
            (run_id,),
        ).fetchall()
        return [{"agent_id": r[0], "level": r[1], "message": r[2], "ts": r[3], "ms": r[4]} for r in rows]

    def slowest_agents(self, run_id: str) -> list[dict]:
        rows = self._con.execute("""
            SELECT agent_id, SUM(duration_ms) as total_ms, COUNT(*) as events
            FROM logs WHERE run_id=? AND duration_ms IS NOT NULL
            GROUP BY agent_id ORDER BY total_ms DESC
        """, (run_id,)).fetchall()
        return [{"agent_id": r[0], "total_ms": r[1], "events": r[2]} for r in rows]

    def error_summary(self, run_id: str) -> list[dict]:
        rows = self._con.execute(
            "SELECT agent_id, message FROM logs WHERE run_id=? AND level='ERROR'",
            (run_id,),
        ).fetchall()
        return [{"agent_id": r[0], "message": r[1]} for r in rows]


async def subagent_db(
    agent_id: str,
    prompt: str,
    run_id: str,
    agg: SQLiteLogAggregator,
    client: anthropic.AsyncAnthropic,
) -> str:
    start = time.perf_counter()
    await agg.log(DBLogEntry(run_id, agent_id, "INFO", f"Start: {prompt[:40]}", time.time()))

    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=32,
        messages=[{"role": "user", "content": prompt}],
    )
    elapsed = (time.perf_counter() - start) * 1000
    result = resp.content[0].text.strip()

    await agg.log(DBLogEntry(run_id, agent_id, "INFO", f"Done: {result[:30]}", time.time(), duration_ms=elapsed))
    return result


async def main() -> None:
    client = anthropic.AsyncAnthropic()
    agg = SQLiteLogAggregator()
    run_id = uuid.uuid4().hex

    tasks = [f"Name a famous {topic}" for topic in ["physicist", "novelist", "composer", "architect", "athlete"]]
    coros = [subagent_db(f"agent-{i:02d}", task, run_id, agg, client) for i, task in enumerate(tasks)]
    results = await asyncio.gather(*coros)

    print(f"\nRun {run_id[:8]} complete. {len(results)} results.")
    print("\nSlowest agents:")
    for row in agg.slowest_agents(run_id):
        print(f"  {row['agent_id']}: {row['total_ms']:.0f} ms total")

    timeline = agg.query_run(run_id)
    print(f"\nTotal log entries: {len(timeline)}")
    await client.close()


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: No extra tokens; SQL queries enable post-mortem SLO analysis
# Environment: Production pipelines requiring durable log persistence and analytical queries
```

---

### Option 6: Hierarchical Log Aggregator with Parent-Child Attribution

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field
from collections import defaultdict


@dataclass
class AgentNode:
    agent_id: str
    parent_id: str | None
    depth: int
    logs: list[dict] = field(default_factory=list)
    children: list["AgentNode"] = field(default_factory=list)
    start_time: float = field(default_factory=time.monotonic)
    end_time: float | None = None

    @property
    def duration_ms(self) -> float:
        if self.end_time is None:
            return 0.0
        return (self.end_time - self.start_time) * 1000


class HierarchicalLogAggregator:
    """
    Tracks parent-child agent relationships.
    Renders the full execution tree with indentation and duration per node.
    """

    def __init__(self) -> None:
        self._nodes: dict[str, AgentNode] = {}
        self._lock = asyncio.Lock()

    async def register(self, agent_id: str, parent_id: str | None = None) -> None:
        async with self._lock:
            depth = 0
            if parent_id and parent_id in self._nodes:
                depth = self._nodes[parent_id].depth + 1
                self._nodes[parent_id].children.append(
                    AgentNode(agent_id=agent_id, parent_id=parent_id, depth=depth)
                )
            node = AgentNode(agent_id=agent_id, parent_id=parent_id, depth=depth)
            self._nodes[agent_id] = node

    async def log(self, agent_id: str, level: str, message: str) -> None:
        async with self._lock:
            if agent_id in self._nodes:
                self._nodes[agent_id].logs.append({
                    "ts": time.time(), "level": level, "message": message
                })

    async def finish(self, agent_id: str) -> None:
        async with self._lock:
            if agent_id in self._nodes:
                self._nodes[agent_id].end_time = time.monotonic()

    def _render_node(self, node: AgentNode, indent: str = "") -> list[str]:
        dur = f"{node.duration_ms:.0f}ms" if node.end_time else "running"
        lines = [f"{indent}[{node.agent_id}] ({dur})"]
        for log in node.logs:
            ts = time.strftime("%H:%M:%S", time.localtime(log["ts"]))
            lines.append(f"{indent}  {ts} [{log['level']}] {log['message']}")
        for child in node.children:
            lines.extend(self._render_node(child, indent + "  "))
        return lines

    def render_tree(self) -> str:
        roots = [n for n in self._nodes.values() if n.parent_id is None]
        lines = ["=== Execution Tree ==="]
        for root in roots:
            lines.extend(self._render_node(root))
        return "\n".join(lines)


async def leaf_agent(
    agent_id: str,
    parent_id: str,
    prompt: str,
    agg: HierarchicalLogAggregator,
    client: anthropic.AsyncAnthropic,
) -> str:
    await agg.register(agent_id, parent_id)
    await agg.log(agent_id, "INFO", f"Processing: {prompt[:30]}")
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=32,
        messages=[{"role": "user", "content": prompt}],
    )
    result = resp.content[0].text.strip()
    await agg.log(agent_id, "INFO", f"Result: {result[:30]}")
    await agg.finish(agent_id)
    return result


async def coordinator_agent(
    agent_id: str,
    tasks: list[str],
    agg: HierarchicalLogAggregator,
    client: anthropic.AsyncAnthropic,
) -> list[str]:
    await agg.register(agent_id)
    await agg.log(agent_id, "INFO", f"Spawning {len(tasks)} leaf agents")

    coros = [
        leaf_agent(f"{agent_id}/leaf-{i}", agent_id, task, agg, client)
        for i, task in enumerate(tasks)
    ]
    results = await asyncio.gather(*coros)
    await agg.log(agent_id, "INFO", "All leaves complete")
    await agg.finish(agent_id)
    return list(results)


async def main() -> None:
    client = anthropic.AsyncAnthropic()
    agg = HierarchicalLogAggregator()

    tasks = ["What is gravity?", "Name a star", "Define entropy", "What is DNA?"]
    results = await coordinator_agent("coordinator", tasks, agg, client)

    print(agg.render_tree())
    print(f"\nTotal results: {len(results)}")
    await client.close()


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: No extra tokens; tree rendering maps execution hierarchy for root-cause analysis
# Environment: Hierarchical multi-agent systems (coordinator + sub-coordinators + leaf agents)
```

---

## Comparison

| Option | Approach | Best For | Log Format | Persistence |
|--------|----------|----------|------------|-------------|
| 1 | Centralized async queue with agent IDs | Simple parallel fan-out debugging | Human-readable | None |
| 2 | Structured JSON with trace context | Distributed tracing integration | JSON (Loki/CloudWatch) | None |
| 3 | Per-agent buffer, flush-on-error only | High-volume pipelines with low failure rate | Human-readable | None |
| 4 | Real-time broadcaster to multiple consumers | Dashboard + alerting simultaneously | Configurable | None |
| 5 | SQLite persistence with analytical queries | Post-run SLO and bottleneck analysis | SQL-queryable | SQLite |
| 6 | Hierarchical tree with parent-child tracking | Nested coordinator + leaf agent systems | Tree-rendered | None |
