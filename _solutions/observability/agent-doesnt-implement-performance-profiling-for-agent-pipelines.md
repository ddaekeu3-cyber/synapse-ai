---
layout: solution
title: "Agent Doesn't Implement Performance Profiling for Agent Pipelines"
category: observability
description: "Instrument agent pipelines with per-step timing, bottleneck detection, percentile tracking, and distributed profiling to identify and eliminate latency hot spots."
tags: [observability, profiling, latency, performance, tracing, bottleneck, async]
---

# Agent Doesn't Implement Performance Profiling for Agent Pipelines

## Problem

Without instrumentation, slow agent pipelines are black boxes. You know the total wall-clock time but not which step — retrieval, LLM call, post-processing, or tool execution — consumed it. Developers optimize randomly rather than targeting the true bottleneck. P99 latency regressions go undetected until users complain.

## Solution Options

### Option 1: Per-Step Timer with Bottleneck Report

```python
import anthropic
import time
from contextlib import contextmanager
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Generator


@dataclass
class StepProfile:
    name: str
    duration_ms: float
    tokens_in: int = 0
    tokens_out: int = 0


@dataclass
class PipelineProfile:
    steps: list[StepProfile] = field(default_factory=list)

    def add(self, step: StepProfile) -> None:
        self.steps.append(step)

    def total_ms(self) -> float:
        return sum(s.duration_ms for s in self.steps)

    def bottleneck(self) -> StepProfile:
        return max(self.steps, key=lambda s: s.duration_ms)

    def report(self) -> str:
        total = self.total_ms()
        lines = ["=== Pipeline Profile ==="]
        for s in self.steps:
            pct = s.duration_ms / total * 100 if total else 0
            lines.append(f"  {s.name:<30} {s.duration_ms:>8.1f} ms  ({pct:.1f}%)")
        lines.append(f"  {'TOTAL':<30} {total:>8.1f} ms")
        b = self.bottleneck()
        lines.append(f"  Bottleneck: {b.name} ({b.duration_ms:.1f} ms)")
        return "\n".join(lines)


@contextmanager
def timed_step(profile: PipelineProfile, name: str) -> Generator[dict, None, None]:
    ctx: dict = {}
    start = time.perf_counter()
    try:
        yield ctx
    finally:
        elapsed_ms = (time.perf_counter() - start) * 1000
        profile.add(StepProfile(
            name=name,
            duration_ms=elapsed_ms,
            tokens_in=ctx.get("tokens_in", 0),
            tokens_out=ctx.get("tokens_out", 0),
        ))


def run_agent_pipeline(user_query: str) -> str:
    client = anthropic.Anthropic()
    profile = PipelineProfile()

    # Step 1: Query understanding
    with timed_step(profile, "query_understanding") as ctx:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=[{"role": "user", "content": f"Classify this query in one word: {user_query}"}],
        )
        ctx["tokens_in"] = resp.usage.input_tokens
        ctx["tokens_out"] = resp.usage.output_tokens
        query_type = resp.content[0].text.strip()

    # Step 2: Simulated retrieval
    with timed_step(profile, "vector_retrieval"):
        time.sleep(0.05)  # simulate DB lookup
        retrieved = f"[doc] Context about {query_type}"

    # Step 3: Main LLM call
    with timed_step(profile, "llm_generation") as ctx:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[
                {"role": "user", "content": f"{retrieved}\n\nAnswer: {user_query}"},
            ],
        )
        ctx["tokens_in"] = resp.usage.input_tokens
        ctx["tokens_out"] = resp.usage.output_tokens
        answer = resp.content[0].text

    # Step 4: Post-processing
    with timed_step(profile, "post_processing"):
        time.sleep(0.01)
        answer = answer.strip()

    print(profile.report())
    return answer


if __name__ == "__main__":
    result = run_agent_pipeline("Explain gradient descent simply")
    print(f"\nAnswer: {result[:120]}...")

# Expected Token Savings: Zero extra tokens; profiling overhead <1 ms per step
# Environment: Any synchronous pipeline needing per-step attribution before optimization
```

---

### Option 2: Async Profiler with Percentile Tracking

```python
import anthropic
import asyncio
import time
import statistics
from contextlib import asynccontextmanager
from collections import defaultdict
from dataclasses import dataclass, field
from typing import AsyncGenerator


@dataclass
class StepStats:
    name: str
    samples: list[float] = field(default_factory=list)

    def record(self, ms: float) -> None:
        self.samples.append(ms)

    def p50(self) -> float:
        return statistics.median(self.samples) if self.samples else 0.0

    def p95(self) -> float:
        if not self.samples:
            return 0.0
        sorted_s = sorted(self.samples)
        idx = int(len(sorted_s) * 0.95)
        return sorted_s[min(idx, len(sorted_s) - 1)]

    def p99(self) -> float:
        if not self.samples:
            return 0.0
        sorted_s = sorted(self.samples)
        idx = int(len(sorted_s) * 0.99)
        return sorted_s[min(idx, len(sorted_s) - 1)]

    def mean(self) -> float:
        return statistics.mean(self.samples) if self.samples else 0.0


class AsyncProfiler:
    """Collects per-step latency across many concurrent requests and reports percentiles."""

    def __init__(self) -> None:
        self._stats: dict[str, StepStats] = defaultdict(lambda: StepStats(name=""))
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def step(self, name: str) -> AsyncGenerator[None, None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000
            async with self._lock:
                if name not in self._stats:
                    self._stats[name] = StepStats(name=name)
                self._stats[name].record(elapsed_ms)

    def report(self) -> str:
        lines = [f"{'Step':<30} {'p50':>8} {'p95':>8} {'p99':>8} {'mean':>8} {'n':>5}"]
        lines.append("-" * 65)
        for name, stats in sorted(self._stats.items()):
            lines.append(
                f"{name:<30} {stats.p50():>7.1f}ms {stats.p95():>7.1f}ms "
                f"{stats.p99():>7.1f}ms {stats.mean():>7.1f}ms {len(stats.samples):>5}"
            )
        return "\n".join(lines)


profiler = AsyncProfiler()


async def handle_request(client: anthropic.AsyncAnthropic, query: str) -> str:
    async with profiler.step("llm_call"):
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{"role": "user", "content": query}],
        )
        result = resp.content[0].text

    async with profiler.step("post_process"):
        await asyncio.sleep(0.005)
        return result.strip()


async def main() -> None:
    client = anthropic.AsyncAnthropic()
    queries = [f"Define term {i} in one sentence" for i in range(20)]

    # Simulate 20 concurrent requests
    tasks = [handle_request(client, q) for q in queries]
    await asyncio.gather(*tasks)

    print(profiler.report())
    await client.close()


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: No extra tokens; percentile report identifies tail latency regressions
# Environment: Async API servers processing many concurrent agent requests
```

---

### Option 3: Flame-Graph Style Span Tree

```python
import anthropic
import asyncio
import time
import uuid
from dataclasses import dataclass, field


@dataclass
class Span:
    name: str
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    parent_id: str | None = None
    start_ms: float = field(default_factory=lambda: time.perf_counter() * 1000)
    end_ms: float | None = None
    tags: dict = field(default_factory=dict)
    children: list["Span"] = field(default_factory=list)

    def finish(self) -> None:
        self.end_ms = time.perf_counter() * 1000

    @property
    def duration_ms(self) -> float:
        if self.end_ms is None:
            return 0.0
        return self.end_ms - self.start_ms

    def flame(self, depth: int = 0, total_ms: float | None = None) -> str:
        if total_ms is None:
            total_ms = self.duration_ms or 1
        pct = self.duration_ms / total_ms * 100
        bar_width = int(pct / 2)
        bar = "█" * bar_width + "░" * (50 - bar_width)
        indent = "  " * depth
        tag_str = " ".join(f"{k}={v}" for k, v in self.tags.items())
        lines = [f"{indent}{self.name:<25} |{bar}| {self.duration_ms:>7.1f}ms ({pct:.1f}%) {tag_str}"]
        for child in self.children:
            lines.append(child.flame(depth + 1, total_ms))
        return "\n".join(lines)


class SpanTracer:
    def __init__(self) -> None:
        self._stack: list[Span] = []
        self._roots: list[Span] = []

    def start(self, name: str, **tags) -> Span:
        parent_id = self._stack[-1].span_id if self._stack else None
        span = Span(name=name, parent_id=parent_id, tags=tags)
        if self._stack:
            self._stack[-1].children.append(span)
        else:
            self._roots.append(span)
        self._stack.append(span)
        return span

    def finish(self, span: Span) -> None:
        span.finish()
        if self._stack and self._stack[-1] is span:
            self._stack.pop()

    def print_flames(self) -> None:
        for root in self._roots:
            print(root.flame())


tracer = SpanTracer()


def rag_pipeline(query: str) -> str:
    client = anthropic.Anthropic()
    root = tracer.start("rag_pipeline", query=query[:20])

    # Retrieval
    ret_span = tracer.start("retrieval")
    time.sleep(0.04)
    docs = ["Doc A content", "Doc B content"]
    ret_span.tags["docs_found"] = len(docs)
    tracer.finish(ret_span)

    # Reranking
    rerank_span = tracer.start("reranking")
    time.sleep(0.015)
    tracer.finish(rerank_span)

    # LLM generation
    llm_span = tracer.start("llm_generation")
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": f"Context: {' '.join(docs)}\n\nQ: {query}"}],
    )
    llm_span.tags["tokens"] = resp.usage.input_tokens + resp.usage.output_tokens
    tracer.finish(llm_span)

    # Formatting
    fmt_span = tracer.start("formatting")
    result = resp.content[0].text.strip()
    tracer.finish(fmt_span)

    tracer.finish(root)
    return result


if __name__ == "__main__":
    answer = rag_pipeline("What is retrieval-augmented generation?")
    print(f"\nAnswer: {answer[:100]}...\n")
    tracer.print_flames()

# Expected Token Savings: Zero extra tokens; flame tree pinpoints nested step costs visually
# Environment: Multi-step RAG or tool-use pipelines with nested sub-operations
```

---

### Option 4: Token-Rate and Throughput Profiler

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field


@dataclass
class TokenRateProfile:
    step: str
    input_tokens: int
    output_tokens: int
    duration_ms: float

    @property
    def tokens_per_second(self) -> float:
        return (self.output_tokens / (self.duration_ms / 1000)) if self.duration_ms > 0 else 0

    @property
    def cost_per_1k_output(self) -> float:
        # Haiku pricing: $0.0004 per 1K output tokens
        return 0.0004

    @property
    def estimated_cost_usd(self) -> float:
        return (self.input_tokens * 0.00025 + self.output_tokens * 0.0004) / 1000


class ThroughputProfiler:
    """Tracks token generation rate and cost efficiency across pipeline steps."""

    def __init__(self) -> None:
        self.profiles: list[TokenRateProfile] = []

    async def profile_call(
        self,
        client: anthropic.AsyncAnthropic,
        step: str,
        model: str,
        messages: list[dict],
        max_tokens: int = 512,
    ) -> str:
        start = time.perf_counter()
        resp = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=messages,
        )
        elapsed_ms = (time.perf_counter() - start) * 1000

        self.profiles.append(TokenRateProfile(
            step=step,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            duration_ms=elapsed_ms,
        ))
        return resp.content[0].text

    def report(self) -> str:
        lines = [
            f"{'Step':<28} {'In':>6} {'Out':>6} {'ms':>8} {'tok/s':>8} {'$cost':>8}"
        ]
        lines.append("-" * 68)
        total_cost = 0.0
        for p in self.profiles:
            total_cost += p.estimated_cost_usd
            lines.append(
                f"{p.step:<28} {p.input_tokens:>6} {p.output_tokens:>6} "
                f"{p.duration_ms:>7.0f}ms {p.tokens_per_second:>7.0f}/s "
                f"${p.estimated_cost_usd:>6.5f}"
            )
        lines.append(f"\nTotal estimated cost: ${total_cost:.5f}")
        return "\n".join(lines)


async def multi_step_pipeline(query: str) -> str:
    client = anthropic.AsyncAnthropic()
    profiler = ThroughputProfiler()

    # Step 1: Intent extraction (cheap/fast)
    intent = await profiler.profile_call(
        client, "intent_extraction", "claude-haiku-4-5-20251001",
        [{"role": "user", "content": f"Extract intent (one word): {query}"}],
        max_tokens=10,
    )

    # Step 2: Main answer
    answer = await profiler.profile_call(
        client, "main_generation", "claude-haiku-4-5-20251001",
        [{"role": "user", "content": f"Intent: {intent}\n\nAnswer: {query}"}],
        max_tokens=512,
    )

    # Step 3: Summary
    summary = await profiler.profile_call(
        client, "summarization", "claude-haiku-4-5-20251001",
        [{"role": "user", "content": f"Summarize in 10 words: {answer}"}],
        max_tokens=20,
    )

    print(profiler.report())
    await client.close()
    return f"{answer}\n\nTL;DR: {summary}"


if __name__ == "__main__":
    result = asyncio.run(multi_step_pipeline("Explain transformer attention mechanisms"))
    print(f"\n--- Result ---\n{result[:200]}...")

# Expected Token Savings: Reports cost per step; reveals where token spend is disproportionate
# Environment: Cost-sensitive pipelines needing ROI per pipeline step
```

---

### Option 5: Streaming Latency Profiler (TTFT + Generation Rate)

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass


@dataclass
class StreamProfile:
    step: str
    time_to_first_token_ms: float
    generation_duration_ms: float
    total_tokens: int

    @property
    def generation_rate(self) -> float:
        return self.total_tokens / (self.generation_duration_ms / 1000) if self.generation_duration_ms > 0 else 0


class StreamingProfiler:
    """
    Profiles streaming responses measuring:
    - TTFT (time to first token) — perceived responsiveness
    - Generation rate (tokens/second) — throughput
    - Total streaming duration
    """

    def __init__(self) -> None:
        self.profiles: list[StreamProfile] = []

    async def profile_stream(
        self,
        client: anthropic.AsyncAnthropic,
        step: str,
        messages: list[dict],
        model: str = "claude-haiku-4-5-20251001",
    ) -> str:
        start = time.perf_counter()
        first_token_time: float | None = None
        chunks: list[str] = []
        token_count = 0

        async with client.messages.stream(
            model=model,
            max_tokens=512,
            messages=messages,
        ) as stream:
            async for text in stream.text_stream:
                if first_token_time is None:
                    first_token_time = time.perf_counter()
                chunks.append(text)
                token_count += 1  # approximate: 1 chunk ≈ 1 token

        end = time.perf_counter()
        ttft_ms = ((first_token_time or end) - start) * 1000
        gen_ms = (end - (first_token_time or end)) * 1000

        self.profiles.append(StreamProfile(
            step=step,
            time_to_first_token_ms=ttft_ms,
            generation_duration_ms=gen_ms,
            total_tokens=token_count,
        ))
        return "".join(chunks)

    def report(self) -> str:
        lines = [f"{'Step':<28} {'TTFT':>10} {'Gen':>10} {'tok':>6} {'tok/s':>8}"]
        lines.append("-" * 65)
        for p in self.profiles:
            lines.append(
                f"{p.step:<28} {p.time_to_first_token_ms:>9.0f}ms "
                f"{p.generation_duration_ms:>9.0f}ms {p.total_tokens:>6} "
                f"{p.generation_rate:>7.0f}/s"
            )
        return "\n".join(lines)


async def main() -> None:
    client = anthropic.AsyncAnthropic()
    profiler = StreamingProfiler()

    steps = [
        ("intent_step", "Classify in one word: explain neural networks"),
        ("main_answer", "Explain neural networks to a 10-year-old"),
        ("followup", "Give three real-world examples of neural networks"),
    ]
    for step_name, prompt in steps:
        await profiler.profile_stream(
            client, step_name,
            [{"role": "user", "content": prompt}],
        )

    print(profiler.report())
    await client.close()


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Zero extra tokens; TTFT measurement isolates network vs generation latency
# Environment: Chat UIs where streaming perceived responsiveness matters more than total duration
```

---

### Option 6: Distributed Pipeline Profiler with SQLite Persistence

```python
import anthropic
import asyncio
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncGenerator


@dataclass
class TraceSpan:
    trace_id: str
    span_id: str
    parent_span_id: str | None
    step: str
    start_epoch: float
    end_epoch: float | None = None
    tags: dict = field(default_factory=dict)

    @property
    def duration_ms(self) -> float:
        if self.end_epoch is None:
            return 0.0
        return (self.end_epoch - self.start_epoch) * 1000


class DistributedProfiler:
    """
    Persists spans to SQLite. Enables cross-request aggregation and
    post-hoc analysis — e.g., P99 per step over the last 1000 traces.
    """

    def __init__(self, db_path: str = "profiler.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        con = sqlite3.connect(self.db_path)
        con.execute("""
            CREATE TABLE IF NOT EXISTS spans (
                trace_id TEXT,
                span_id TEXT PRIMARY KEY,
                parent_span_id TEXT,
                step TEXT,
                start_epoch REAL,
                end_epoch REAL,
                duration_ms REAL,
                tags TEXT
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_trace ON spans(trace_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_step ON spans(step)")
        con.commit()
        con.close()

    def _save_span(self, span: TraceSpan) -> None:
        import json
        con = sqlite3.connect(self.db_path)
        con.execute(
            "INSERT OR REPLACE INTO spans VALUES (?,?,?,?,?,?,?,?)",
            (
                span.trace_id, span.span_id, span.parent_span_id,
                span.step, span.start_epoch, span.end_epoch,
                span.duration_ms, json.dumps(span.tags),
            ),
        )
        con.commit()
        con.close()

    @asynccontextmanager
    async def trace_step(
        self, trace_id: str, step: str, parent_span_id: str | None = None, **tags
    ) -> AsyncGenerator[TraceSpan, None]:
        span = TraceSpan(
            trace_id=trace_id,
            span_id=uuid.uuid4().hex,
            parent_span_id=parent_span_id,
            step=step,
            start_epoch=time.time(),
            tags=tags,
        )
        try:
            yield span
        finally:
            span.end_epoch = time.time()
            self._save_span(span)

    def p99_by_step(self, step: str, last_n: int = 1000) -> float:
        con = sqlite3.connect(self.db_path)
        rows = con.execute(
            "SELECT duration_ms FROM spans WHERE step=? ORDER BY start_epoch DESC LIMIT ?",
            (step, last_n),
        ).fetchall()
        con.close()
        if not rows:
            return 0.0
        samples = sorted(r[0] for r in rows)
        idx = int(len(samples) * 0.99)
        return samples[min(idx, len(samples) - 1)]

    def step_summary(self) -> list[dict]:
        con = sqlite3.connect(self.db_path)
        rows = con.execute("""
            SELECT step,
                   COUNT(*) as n,
                   AVG(duration_ms) as avg_ms,
                   MAX(duration_ms) as max_ms
            FROM spans
            GROUP BY step
            ORDER BY avg_ms DESC
        """).fetchall()
        con.close()
        return [{"step": r[0], "n": r[1], "avg_ms": r[2], "max_ms": r[3]} for r in rows]


async def run_traced_pipeline(profiler: DistributedProfiler, query: str) -> str:
    client = anthropic.AsyncAnthropic()
    trace_id = uuid.uuid4().hex

    async with profiler.trace_step(trace_id, "full_pipeline") as root_span:

        async with profiler.trace_step(
            trace_id, "llm_call", parent_span_id=root_span.span_id, model="haiku"
        ) as llm_span:
            resp = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[{"role": "user", "content": query}],
            )
            llm_span.tags["tokens"] = resp.usage.input_tokens + resp.usage.output_tokens
            answer = resp.content[0].text

        async with profiler.trace_step(
            trace_id, "post_process", parent_span_id=root_span.span_id
        ):
            await asyncio.sleep(0.005)
            answer = answer.strip()

    await client.close()
    return answer


async def main() -> None:
    profiler = DistributedProfiler(db_path=":memory:")  # use file path in production

    queries = [
        "Define photosynthesis",
        "Explain Newton's first law",
        "What is a hash table?",
        "Describe recursion",
        "What is an API?",
    ]
    for q in queries:
        result = await run_traced_pipeline(profiler, q)
        print(f"Q: {q[:30]:<30} A: {result[:50]}...")

    print("\n=== Step Summary ===")
    for row in profiler.step_summary():
        p99 = profiler.p99_by_step(row["step"])
        print(f"  {row['step']:<20} n={row['n']:>4}  avg={row['avg_ms']:>7.1f}ms  p99={p99:>7.1f}ms")


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Zero extra tokens; SQLite enables long-term trend analysis across deploys
# Environment: Production agents needing persistent profiling data for SLO dashboards
```

---

## Comparison

| Option | Approach | Best For | Overhead | Persistence |
|--------|----------|----------|----------|-------------|
| 1 | Per-step timer with bottleneck report | Single-request debugging | Negligible | None (in-memory) |
| 2 | Async percentile tracker (p50/p95/p99) | High-concurrency latency analysis | <0.1 ms/step | None (in-memory) |
| 3 | Flame-graph span tree | Nested sub-operation visualization | Negligible | None (in-memory) |
| 4 | Token-rate and cost profiler | Cost-per-step ROI analysis | Negligible | None (in-memory) |
| 5 | Streaming TTFT + generation rate | Chat UI perceived responsiveness | Negligible | None (in-memory) |
| 6 | Distributed SQLite trace store | Production SLO monitoring & trends | Low | SQLite (durable) |
