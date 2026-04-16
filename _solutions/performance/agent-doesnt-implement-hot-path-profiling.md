---
layout: solution
title: "Agent Doesn't Implement Hot-Path Profiling"
category: performance
description: "Instrument agent execution to identify which tool calls, model calls, and processing steps consume the most time — so optimization effort targets actual bottlenecks rather than guesses."
tags: [performance, profiling, hot-path, observability, sqlite, python]
---

# Agent Doesn't Implement Hot-Path Profiling

Optimizing agent performance without profiling data is guesswork. Hot-path profiling measures where time actually goes — which model calls take longest, which tools are slowest, which processing steps are unexpectedly expensive — so every optimization has measurable ROI.

## Option 1: cProfile Integration for Agent Runs

```python
import anthropic
import cProfile
import pstats
import io
import time

client = anthropic.Anthropic()

def tool_preprocess(data: str) -> str:
    """Simulated preprocessing step."""
    time.sleep(0.01)
    return data.upper().strip()

def tool_postprocess(result: str) -> str:
    """Simulated postprocessing step."""
    time.sleep(0.005)
    return result[:200]

def call_model(prompt: str) -> str:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text

def agent_pipeline(user_input: str) -> str:
    processed = tool_preprocess(user_input)
    result = call_model(processed)
    return tool_postprocess(result)

def profile_agent(user_input: str, top_n: int = 10) -> str:
    profiler = cProfile.Profile()
    profiler.enable()
    result = agent_pipeline(user_input)
    profiler.disable()

    # Print top functions by cumulative time
    stream = io.StringIO()
    stats = pstats.Stats(profiler, stream=stream)
    stats.sort_stats("cumulative")
    stats.print_stats(top_n)
    print(stream.getvalue())
    return result

result = profile_agent("Explain Python asyncio in one sentence.")
print(f"Result: {result[:80]}")

# Expected Token Savings: Identify if preprocessing or postprocessing — not the model — is the bottleneck
# Environment: stdlib only; wrap any agent function with cProfile.Profile()
```

## Option 2: Manual Timer Instrumentation with SQLite

```python
import anthropic
import sqlite3
import time
import uuid
from contextlib import contextmanager

client = anthropic.Anthropic()
DB = "profile_data.db"

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS spans (
            run_id TEXT, name TEXT,
            start_ts REAL, end_ts REAL,
            duration_ms REAL, metadata TEXT
        )
    """)
    con.commit(); con.close()

class Profiler:
    def __init__(self):
        self.run_id = uuid.uuid4().hex[:8]
        self._spans: list[dict] = []

    @contextmanager
    def measure(self, name: str, **meta):
        start = time.perf_counter()
        try:
            yield
        finally:
            end = time.perf_counter()
            dur_ms = (end - start) * 1000
            self._spans.append({
                "name": name, "start": start, "end": end,
                "dur_ms": dur_ms, "meta": meta,
            })
            con = sqlite3.connect(DB)
            con.execute("INSERT INTO spans VALUES (?,?,?,?,?,?)",
                        (self.run_id, name, start, end, dur_ms, str(meta)))
            con.commit(); con.close()

    def report(self) -> str:
        total = sum(s["dur_ms"] for s in self._spans)
        lines = [f"Run {self.run_id} — total={total:.1f}ms"]
        for s in sorted(self._spans, key=lambda x: -x["dur_ms"]):
            pct = s["dur_ms"] / total * 100 if total else 0
            lines.append(f"  {s['name']:30s} {s['dur_ms']:7.1f}ms ({pct:4.1f}%)")
        return "\n".join(lines)

def hot_path_report(top_n: int = 5) -> list[dict]:
    """Aggregate across all runs to find consistently slow steps."""
    con = sqlite3.connect(DB)
    rows = con.execute("""
        SELECT name,
               ROUND(AVG(duration_ms),1) avg_ms,
               ROUND(MAX(duration_ms),1) max_ms,
               COUNT(*) calls
        FROM spans GROUP BY name ORDER BY avg_ms DESC LIMIT ?
    """, (top_n,)).fetchall()
    con.close()
    return [{"name": r[0], "avg_ms": r[1], "max_ms": r[2], "calls": r[3]} for r in rows]

init_db()

def agent_run(prompt: str) -> str:
    p = Profiler()
    with p.measure("total"):
        with p.measure("input_validation"):
            time.sleep(0.002)
            validated = prompt.strip()
        with p.measure("model_call", model="haiku"):
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=128,
                messages=[{"role": "user", "content": validated}],
            )
            result = resp.content[0].text
        with p.measure("response_format"):
            time.sleep(0.001)
            formatted = result.strip()
    print(p.report())
    return formatted

for q in ["What is TCP?", "Explain DNS", "What is HTTP?"]:
    agent_run(q)

print("\nHot path (aggregated):")
for row in hot_path_report():
    print(f"  {row['name']:30s} avg={row['avg_ms']}ms max={row['max_ms']}ms calls={row['calls']}")

# Expected Token Savings: Pinpoint if preprocessing or formatting adds unexpected latency vs model call
# Environment: SQLite persists across runs; aggregate hot_path_report() in CI performance reports
```

## Option 3: Async Profiler with Concurrent Span Tracking

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

@dataclass
class AsyncSpan:
    name: str
    start: float = field(default_factory=time.monotonic)
    end: float | None = None
    children: list["AsyncSpan"] = field(default_factory=list)
    task_id: int = 0

    @property
    def duration_ms(self) -> float:
        return ((self.end or time.monotonic()) - self.start) * 1000

    def finish(self):
        self.end = time.monotonic()

class AsyncProfiler:
    def __init__(self):
        self._roots: list[AsyncSpan] = []
        self._current: dict[int, list[AsyncSpan]] = {}  # task_id -> span stack

    def _task_id(self) -> int:
        try:
            return id(asyncio.current_task())
        except RuntimeError:
            return 0

    def start_span(self, name: str) -> AsyncSpan:
        tid = self._task_id()
        span = AsyncSpan(name=name, task_id=tid)
        stack = self._current.setdefault(tid, [])
        if stack:
            stack[-1].children.append(span)
        else:
            self._roots.append(span)
        stack.append(span)
        return span

    def end_span(self, span: AsyncSpan):
        span.finish()
        tid = self._task_id()
        stack = self._current.get(tid, [])
        if stack and stack[-1] is span:
            stack.pop()

    def print_tree(self, span: AsyncSpan | None = None, indent: int = 0):
        spans = [span] if span else self._roots
        for s in spans:
            print(f"{'  ' * indent}{s.name}: {s.duration_ms:.1f}ms")
            for child in s.children:
                self.print_tree(child, indent + 1)

PROFILER = AsyncProfiler()

async def measured(name: str, coro):
    span = PROFILER.start_span(name)
    try:
        return await coro
    finally:
        PROFILER.end_span(span)

async def fetch_context(query: str) -> str:
    await asyncio.sleep(0.05)
    return f"Context: {query}"

async def call_model(prompt: str) -> str:
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text

async def agent_turn(query: str) -> str:
    span = PROFILER.start_span(f"agent_turn:{query[:20]}")
    context = await measured("fetch_context", fetch_context(query))
    full_prompt = f"{context}\n{query}"
    result = await measured("model_call", call_model(full_prompt))
    PROFILER.end_span(span)
    return result

async def main():
    queries = ["What is asyncio?", "Explain coroutines", "What is the GIL?"]
    results = await asyncio.gather(*[agent_turn(q) for q in queries])
    print("\nAsync profile tree:")
    PROFILER.print_tree()
    print("\nResults:")
    for r in results:
        print(f"  {r[:60]}")

asyncio.run(main())

# Expected Token Savings: Concurrent span tracking reveals which parallel branches are slowest
# Environment: asyncio; task_id isolates spans across concurrent coroutines correctly
```

## Option 4: Statistical Percentile Profiler

```python
import anthropic
import time
import statistics
import sqlite3
from collections import defaultdict

client = anthropic.Anthropic()
DB = "perf_stats.db"

def init_db():
    con = sqlite3.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS samples (step TEXT, duration_ms REAL, ts REAL)")
    con.commit(); con.close()

_samples: dict[str, list[float]] = defaultdict(list)

def record(step: str, duration_ms: float):
    _samples[step].append(duration_ms)
    con = sqlite3.connect(DB)
    con.execute("INSERT INTO samples VALUES (?,?,?)", (step, duration_ms, time.time()))
    con.commit(); con.close()

def timed(step: str):
    def decorator(fn):
        def wrapper(*args, **kwargs):
            start = time.perf_counter()
            try:
                return fn(*args, **kwargs)
            finally:
                record(step, (time.perf_counter() - start) * 1000)
        return wrapper
    return decorator

def percentile(data: list[float], p: float) -> float:
    if not data: return 0.0
    sorted_data = sorted(data)
    idx = int(len(sorted_data) * p)
    return sorted_data[min(idx, len(sorted_data) - 1)]

def stats_report() -> str:
    lines = ["Performance Profile (all runs):"]
    for step, durations in sorted(_samples.items()):
        if not durations: continue
        lines.append(
            f"  {step:35s} "
            f"p50={percentile(durations, 0.50):6.1f}ms "
            f"p95={percentile(durations, 0.95):6.1f}ms "
            f"p99={percentile(durations, 0.99):6.1f}ms "
            f"max={max(durations):6.1f}ms "
            f"n={len(durations)}"
        )
    return "\n".join(lines)

@timed("prompt_build")
def build_prompt(user_input: str, context: str) -> str:
    time.sleep(0.002)
    return f"Context: {context}\n\nUser: {user_input}"

@timed("token_count_estimate")
def estimate_tokens(text: str) -> int:
    time.sleep(0.001)
    return len(text) // 4

@timed("model_call")
def call_model(prompt: str) -> str:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text

@timed("response_parse")
def parse_response(text: str) -> dict:
    time.sleep(0.001)
    return {"text": text.strip(), "word_count": len(text.split())}

def run_agent(user_input: str) -> dict:
    prompt = build_prompt(user_input, "Python programming context")
    estimate_tokens(prompt)
    result = call_model(prompt)
    return parse_response(result)

init_db()
queries = [
    "What is asyncio?", "Explain decorators", "What is a generator?",
    "What is list comprehension?", "Explain f-strings",
]
for q in queries:
    result = run_agent(q)
    print(f"OK: {result['text'][:50]}")

print(f"\n{stats_report()}")

# Expected Token Savings: p95/p99 reveals tail latency; target model_call first if it dominates
# Environment: SQLite persists samples; @timed decorator wraps any function non-invasively
```

## Option 5: Flame Graph Data Collector

```python
import anthropic
import time
import json
from dataclasses import dataclass, field
from pathlib import Path

client = anthropic.Anthropic()

@dataclass
class FlameNode:
    name: str
    start_us: int
    end_us: int = 0
    children: list["FlameNode"] = field(default_factory=list)

    @property
    def duration_us(self) -> int:
        return self.end_us - self.start_us

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "start": self.start_us,
            "end": self.end_us,
            "duration_ms": self.duration_us / 1000,
            "children": [c.to_dict() for c in self.children],
        }

class FlameProfiler:
    def __init__(self):
        self._roots: list[FlameNode] = []
        self._stack: list[FlameNode] = []
        self._t0 = time.perf_counter()

    def _us(self) -> int:
        return int((time.perf_counter() - self._t0) * 1_000_000)

    def enter(self, name: str) -> FlameNode:
        node = FlameNode(name=name, start_us=self._us())
        if self._stack:
            self._stack[-1].children.append(node)
        else:
            self._roots.append(node)
        self._stack.append(node)
        return node

    def exit(self, node: FlameNode):
        node.end_us = self._us()
        if self._stack and self._stack[-1] is node:
            self._stack.pop()

    def save(self, path: str = "flame.json"):
        data = {"roots": [r.to_dict() for r in self._roots]}
        Path(path).write_text(json.dumps(data, indent=2))
        print(f"Flame data saved to {path}")

    def print_flame(self, node: FlameNode | None = None, depth: int = 0):
        nodes = [node] if node else self._roots
        for n in nodes:
            bar = "█" * min(int(n.duration_us / 1000), 40)
            print(f"{'  ' * depth}{n.name}: {n.duration_us/1000:.1f}ms {bar}")
            for c in n.children:
                self.print_flame(c, depth + 1)

flame = FlameProfiler()

def agent_pipeline(user_input: str) -> str:
    root = flame.enter("pipeline")

    n = flame.enter("validate_input")
    time.sleep(0.003)
    flame.exit(n)

    n = flame.enter("build_context")
    time.sleep(0.002)
    flame.exit(n)

    n = flame.enter("model_call")
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": user_input}],
    )
    result = resp.content[0].text
    flame.exit(n)

    n = flame.enter("format_output")
    time.sleep(0.001)
    flame.exit(n)

    flame.exit(root)
    return result

for q in ["What is Python?", "What is asyncio?"]:
    agent_pipeline(q)

print("\nFlame graph:")
flame.print_flame()
flame.save("flame.json")

# Expected Token Savings: Flame data shows model_call vs. processing time share clearly
# Environment: flame.json importable into speedscope.app or similar viewer for visualization
```

## Option 6: Continuous Profiling with Alert on Regression

```python
import anthropic
import sqlite3
import time
import statistics

client = anthropic.Anthropic()
DB = "perf_regression.db"

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS perf_samples (
            step TEXT, duration_ms REAL,
            version TEXT DEFAULT 'current', ts REAL
        )
    """)
    con.commit(); con.close()

def record_sample(step: str, duration_ms: float, version: str = "current"):
    con = sqlite3.connect(DB)
    con.execute("INSERT INTO perf_samples VALUES (?,?,?,?)",
                (step, duration_ms, version, time.time()))
    con.commit(); con.close()

def baseline_p95(step: str, window_days: int = 7) -> float | None:
    cutoff = time.time() - window_days * 86400
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT duration_ms FROM perf_samples WHERE step=? AND ts>? ORDER BY duration_ms",
        (step, cutoff)
    ).fetchall()
    con.close()
    if len(rows) < 10:
        return None
    data = [r[0] for r in rows]
    idx = int(len(data) * 0.95)
    return data[min(idx, len(data) - 1)]

def check_regression(step: str, current_ms: float, regression_threshold: float = 1.5) -> bool:
    """Alert if current measurement exceeds baseline p95 by threshold factor."""
    baseline = baseline_p95(step)
    if baseline is None:
        return False  # Not enough data
    ratio = current_ms / baseline
    if ratio > regression_threshold:
        print(f"  [PERF REGRESSION] {step}: {current_ms:.1f}ms is {ratio:.1f}x baseline p95 ({baseline:.1f}ms)")
        return True
    return False

def timed_step(step: str, fn, *args, **kwargs):
    start = time.perf_counter()
    result = fn(*args, **kwargs)
    dur_ms = (time.perf_counter() - start) * 1000
    record_sample(step, dur_ms)
    check_regression(step, dur_ms)
    return result

def preprocess(text: str) -> str:
    time.sleep(0.002)
    return text.strip().lower()

def call_model(prompt: str) -> str:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text

def postprocess(text: str) -> str:
    time.sleep(0.001)
    return text[:200]

def agent_run(user_input: str) -> str:
    processed = timed_step("preprocess", preprocess, user_input)
    result = timed_step("model_call", call_model, processed)
    return timed_step("postprocess", postprocess, result)

init_db()

# Run multiple times to build baseline
print("Building baseline...")
for q in ["What is TCP?", "Explain HTTP", "What is DNS?",
          "What is TLS?", "What is SSH?", "Explain UDP",
          "What is ICMP?", "What is BGP?", "Explain NAT", "What is a VLAN?"]:
    agent_run(q)

print("\nChecking for regressions on new run...")
agent_run("What is a firewall?")

# Show summary
for step in ["preprocess", "model_call", "postprocess"]:
    p95 = baseline_p95(step)
    print(f"  {step}: p95={p95:.1f}ms" if p95 else f"  {step}: insufficient data")

# Expected Token Savings: Automatic regression detection flags when new code makes model path slower
# Environment: SQLite; run in CI with version tag per deployment; alert on ratio > 1.5x
```

## Comparison

| Option | Instrumentation | Storage | Best For |
|--------|----------------|---------|----------|
| 1 — cProfile | stdlib decorator | In-memory | Quick function-level profiling |
| 2 — Manual Timer + SQLite | Context manager | SQLite | Persistent hot-path tracking |
| 3 — Async Span Tree | Task-aware stack | In-memory | Concurrent async pipelines |
| 4 — Statistical Percentile | @timed decorator | SQLite + dict | p95/p99 tail latency analysis |
| 5 — Flame Graph | Enter/exit API | JSON file | Visual waterfall in speedscope |
| 6 — Continuous + Regression | timed_step wrapper | SQLite | CI performance regression gate |
