---
layout: solution
title: "Agent Doesn't Implement Scatter-Gather Pattern for Multi-Source Queries"
category: concurrency
description: "Fan out a query to multiple sources simultaneously, gather all responses within a time budget, and merge results — replacing serial sequential calls with a single parallel round-trip."
tags: [concurrency, parallelism, scatter-gather, async, performance, fan-out]
---

Agents that query multiple data sources — search engines, databases, APIs, other models — often call them one at a time. The total wait time is the sum of all call durations. With scatter-gather, all queries fire simultaneously. The total wait is the maximum single call duration (or the deadline, whichever comes first). For 5 sources each taking 500ms, sequential takes 2.5s; scatter-gather takes 0.5s.

## Option 1: asyncio.gather with Partial Results

Fire all source queries at once with `asyncio.gather(return_exceptions=True)`. Collect whichever results succeed within the timeout. Failed or timed-out sources are noted but don't block the successful ones.

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass

@dataclass
class SourceResult:
    source: str
    content: str | None
    error: str | None
    latency_ms: float

async def query_source(
    client: anthropic.AsyncAnthropic,
    source_name: str,
    prompt: str,
    timeout_s: float = 5.0,
) -> SourceResult:
    start = time.monotonic()
    try:
        response = await asyncio.wait_for(
            client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[{"role": "user", "content": f"[{source_name}] {prompt}"}],
            ),
            timeout=timeout_s,
        )
        latency_ms = (time.monotonic() - start) * 1000
        return SourceResult(source_name, response.content[0].text, None, latency_ms)
    except asyncio.TimeoutError:
        latency_ms = (time.monotonic() - start) * 1000
        return SourceResult(source_name, None, "timeout", latency_ms)
    except Exception as e:
        latency_ms = (time.monotonic() - start) * 1000
        return SourceResult(source_name, None, str(e)[:80], latency_ms)

async def scatter_gather(
    query: str,
    sources: list[str],
    gather_timeout_s: float = 6.0,
) -> list[SourceResult]:
    client = anthropic.AsyncAnthropic()
    start = time.monotonic()

    results = await asyncio.gather(
        *[query_source(client, src, query) for src in sources],
        return_exceptions=False,
    )

    total_ms = (time.monotonic() - start) * 1000
    successful = [r for r in results if r.error is None]
    print(f"[ScatterGather] {len(successful)}/{len(sources)} sources OK in {total_ms:.0f}ms total")
    return list(results)

async def answer_from_sources(query: str, sources: list[str]) -> str:
    client = anthropic.AsyncAnthropic()
    results = await scatter_gather(query, sources)

    context = "\n\n".join(
        f"[{r.source}] ({r.latency_ms:.0f}ms): {r.content}"
        if r.content else
        f"[{r.source}] ERROR: {r.error}"
        for r in results
    )

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": f"Multi-source results for: {query}\n\n{context}\n\nSynthesize a comprehensive answer.",
        }],
    )
    return response.content[0].text

async def main():
    query = "What are the best practices for Python async error handling?"
    sources = ["internal_docs", "web_search", "stackoverflow", "github_examples", "official_python_docs"]
    result = await answer_from_sources(query, sources)
    print(result[:400])

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Zero (same total tokens); wall-clock time reduced by 4-5x vs sequential
# Environment: pip install anthropic
```

## Option 2: Deadline-Bounded Gather with asyncio.wait

Use `asyncio.wait` with a `FIRST_COMPLETED` strategy or deadline loop. Process results as they arrive. After the deadline, cancel remaining tasks and synthesize from whatever arrived — guaranteeing bounded response time even if some sources are slow.

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass

@dataclass
class GatherResult:
    source: str
    content: str
    latency_ms: float

async def timed_source_call(
    client: anthropic.AsyncAnthropic,
    source: str,
    query: str,
) -> GatherResult:
    start = time.monotonic()
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{"role": "user", "content": f"Provide information about '{query}' from {source} perspective."}],
    )
    return GatherResult(source, response.content[0].text, (time.monotonic() - start) * 1000)

async def deadline_gather(
    query: str,
    sources: list[str],
    deadline_ms: float = 3000,
    min_sources: int = 2,
) -> list[GatherResult]:
    client = anthropic.AsyncAnthropic()
    deadline = time.monotonic() + deadline_ms / 1000
    results: list[GatherResult] = []

    pending = {
        asyncio.create_task(timed_source_call(client, src, query), name=src)
        for src in sources
    }

    while pending and time.monotonic() < deadline:
        remaining_s = deadline - time.monotonic()
        if remaining_s <= 0:
            break

        done, pending = await asyncio.wait(
            pending,
            timeout=remaining_s,
            return_when=asyncio.FIRST_COMPLETED,
        )

        for task in done:
            if task.exception() is None:
                results.append(task.result())
                print(f"[DeadlineGather] +{task.get_name()} ({results[-1].latency_ms:.0f}ms) | {len(results)}/{len(sources)} done")

        if len(results) >= len(sources):
            break  # All done before deadline

    # Cancel remaining after deadline
    for task in pending:
        task.cancel()
        print(f"[DeadlineGather] Cancelled {task.get_name()} (deadline)")

    elapsed = (deadline_ms - (deadline - time.monotonic()) * 1000)
    print(f"[DeadlineGather] {len(results)}/{len(sources)} results in {elapsed:.0f}ms")
    return results

async def answer_with_deadline(query: str, sources: list[str]) -> str:
    client = anthropic.AsyncAnthropic()
    results = await deadline_gather(query, sources, deadline_ms=4000, min_sources=2)

    if not results:
        return "No sources responded within deadline."

    context = "\n\n".join(f"[{r.source}]: {r.content}" for r in results)
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": f"Synthesize from {len(results)} sources:\n\n{context}\n\nQuery: {query}"}],
    )
    return response.content[0].text

async def main():
    sources = ["database", "cache", "external_api", "search_engine", "knowledge_base"]
    result = await answer_with_deadline(
        "What are the trade-offs between microservices and monolithic architectures?",
        sources,
    )
    print(result[:400])

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Guaranteed bounded latency; faster total response than any sequential approach
# Environment: pip install anthropic
```

## Option 3: Weighted Scatter with Priority Lanes

Assign priority tiers to sources. High-priority sources (fast cache, primary DB) run immediately. Low-priority sources (slow external APIs) run only if high-priority sources don't return enough results. This limits unnecessary calls to slow sources when fast ones are sufficient.

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass
from enum import IntEnum

class Priority(IntEnum):
    HIGH = 1    # fast, authoritative (cache, primary DB)
    MEDIUM = 2  # moderate speed (secondary DB, internal API)
    LOW = 3     # slow, supplemental (external APIs, search)

@dataclass
class PrioritizedSource:
    name: str
    priority: Priority
    expected_ms: float
    weight: float = 1.0  # contribution weight in synthesis

async def call_source(
    client: anthropic.AsyncAnthropic,
    source: PrioritizedSource,
    query: str,
) -> tuple[PrioritizedSource, str, float]:
    start = time.monotonic()
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        messages=[{"role": "user", "content": f"Answer from {source.name}: {query}"}],
    )
    return source, response.content[0].text, (time.monotonic() - start) * 1000

async def priority_scatter_gather(
    query: str,
    sources: list[PrioritizedSource],
    high_priority_timeout_ms: float = 1000,
    total_timeout_ms: float = 4000,
    min_weight_threshold: float = 1.5,
) -> list[tuple[PrioritizedSource, str, float]]:
    client = anthropic.AsyncAnthropic()
    results: list[tuple[PrioritizedSource, str, float]] = []
    total_weight = 0.0

    by_priority = {p: [s for s in sources if s.priority == p] for p in Priority}

    # Fire high-priority immediately
    high_tasks = {
        asyncio.create_task(call_source(client, src, query)): src
        for src in by_priority.get(Priority.HIGH, [])
    }
    medium_tasks: dict = {}
    low_tasks: dict = {}

    start = time.monotonic()

    def elapsed_ms() -> float:
        return (time.monotonic() - start) * 1000

    # Wait for high-priority with short timeout
    if high_tasks:
        done, pending = await asyncio.wait(
            high_tasks, timeout=high_priority_timeout_ms / 1000
        )
        for t in done:
            if not t.exception():
                src, content, ms = t.result()
                results.append((src, content, ms))
                total_weight += src.weight
                print(f"[PriorityGather] HIGH {src.name} done in {ms:.0f}ms (weight={src.weight})")
        for t in pending:
            t.cancel()

    # Check if high-priority is sufficient
    if total_weight >= min_weight_threshold:
        print(f"[PriorityGather] High-priority sufficient (weight={total_weight}), skipping lower tiers")
        return results

    # Fire medium-priority
    remaining_ms = total_timeout_ms - elapsed_ms()
    medium_tasks = {
        asyncio.create_task(call_source(client, src, query)): src
        for src in by_priority.get(Priority.MEDIUM, [])
    }
    if medium_tasks:
        done, pending = await asyncio.wait(medium_tasks, timeout=remaining_ms / 1000)
        for t in done:
            if not t.exception():
                src, content, ms = t.result()
                results.append((src, content, ms))
                total_weight += src.weight
                print(f"[PriorityGather] MEDIUM {src.name} done in {ms:.0f}ms")
        for t in pending:
            t.cancel()

    # Fire low-priority only if still not enough
    if total_weight < min_weight_threshold:
        remaining_ms = total_timeout_ms - elapsed_ms()
        low_tasks = {
            asyncio.create_task(call_source(client, src, query)): src
            for src in by_priority.get(Priority.LOW, [])
        }
        if low_tasks:
            done, _ = await asyncio.wait(low_tasks, timeout=remaining_ms / 1000)
            for t in done:
                if not t.exception():
                    src, content, ms = t.result()
                    results.append((src, content, ms))
                    total_weight += src.weight
            for t in low_tasks:
                if t not in done:
                    t.cancel()

    print(f"[PriorityGather] Total: {len(results)} results, weight={total_weight:.1f}, {elapsed_ms():.0f}ms")
    return results

async def main():
    sources = [
        PrioritizedSource("in_memory_cache", Priority.HIGH, 50, weight=1.0),
        PrioritizedSource("primary_database", Priority.HIGH, 200, weight=1.2),
        PrioritizedSource("internal_api", Priority.MEDIUM, 500, weight=0.8),
        PrioritizedSource("analytics_db", Priority.MEDIUM, 800, weight=0.7),
        PrioritizedSource("external_search", Priority.LOW, 1500, weight=0.5),
        PrioritizedSource("third_party_api", Priority.LOW, 2000, weight=0.4),
    ]
    client = anthropic.AsyncAnthropic()
    results = await priority_scatter_gather(
        "What is the current system status and recent error rates?",
        sources,
        min_weight_threshold=2.0,
    )
    context = "\n".join(f"[{s.name}]: {c[:100]}" for s, c, _ in results)
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": f"Synthesize system status:\n{context}"}],
    )
    print(response.content[0].text[:300])

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: 30-50% by skipping low-priority sources when high-priority is sufficient
# Environment: pip install anthropic
```

## Option 4: Scatter with Result Deduplication and Merge

When multiple sources return overlapping information, deduplicate similar facts before synthesis. Instead of sending all raw results (with repetition) to the model, send a deduplicated merged view. Reduces synthesis prompt size and improves coherence.

```python
import anthropic
import asyncio
import re
from dataclasses import dataclass

@dataclass
class SourceResponse:
    source: str
    sentences: list[str]
    latency_ms: float

def extract_sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text.strip()) if len(s.strip()) > 20]

def jaccard_similarity(a: str, b: str) -> float:
    wa = set(a.lower().split())
    wb = set(b.lower().split())
    return len(wa & wb) / len(wa | wb) if (wa | wb) else 0.0

def deduplicate_across_sources(responses: list[SourceResponse], threshold: float = 0.6) -> list[tuple[str, str]]:
    """Return list of (sentence, sources) with near-duplicates merged."""
    unique: list[tuple[str, list[str]]] = []  # (sentence, [sources])

    for resp in responses:
        for sentence in resp.sentences:
            matched = False
            for i, (existing, sources) in enumerate(unique):
                if jaccard_similarity(sentence, existing) >= threshold:
                    unique[i] = (existing, sources + [resp.source])
                    matched = True
                    break
            if not matched:
                unique.append((sentence, [resp.source]))

    return [(sent, ", ".join(srcs)) for sent, srcs in unique]

async def scatter_with_dedup(
    query: str,
    source_names: list[str],
) -> str:
    client = anthropic.AsyncAnthropic()

    async def fetch(source: str) -> SourceResponse:
        import time
        start = time.monotonic()
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": f"Answer from {source} perspective (2-3 sentences): {query}"}],
        )
        return SourceResponse(
            source=source,
            sentences=extract_sentences(response.content[0].text),
            latency_ms=(time.monotonic() - start) * 1000,
        )

    responses = await asyncio.gather(*[fetch(src) for src in source_names])
    total_sentences = sum(len(r.sentences) for r in responses)
    deduped = deduplicate_across_sources(list(responses))

    print(f"[DeduGather] {total_sentences} sentences → {len(deduped)} unique ({(1-len(deduped)/total_sentences)*100:.1f}% reduction)")

    context = "\n".join(
        f"[{sources}] {sent}" for sent, sources in deduped
    )

    synthesis = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{
            "role": "user",
            "content": f"Synthesize these unique facts for: {query}\n\n{context}",
        }],
    )
    return synthesis.content[0].text

async def main():
    sources = ["official_docs", "stack_overflow", "blog_posts", "github_examples"]
    result = await scatter_with_dedup(
        "How does Python's garbage collection work?",
        sources,
    )
    print(result[:400])

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: 20-40% reduction in synthesis prompt from deduplication
# Environment: pip install anthropic
```

## Option 5: Streaming Scatter with Progressive Synthesis

Stream responses from all sources simultaneously. As each token arrives from any source, feed it into a progressive synthesis. The user sees partial results building up in real time instead of waiting for all sources to finish before seeing anything.

```python
import anthropic
import asyncio
from dataclasses import dataclass, field
from typing import AsyncIterator

@dataclass
class StreamChunk:
    source: str
    text: str
    done: bool = False

async def stream_source(
    client: anthropic.AsyncAnthropic,
    source: str,
    query: str,
    queue: asyncio.Queue,
) -> None:
    try:
        async with client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{"role": "user", "content": f"[{source}] Answer briefly: {query}"}],
        ) as stream:
            async for text in stream.text_stream:
                await queue.put(StreamChunk(source, text))
        await queue.put(StreamChunk(source, "", done=True))
    except Exception as e:
        await queue.put(StreamChunk(source, f"[ERROR: {e}]", done=True))

async def progressive_scatter_stream(
    query: str,
    sources: list[str],
    timeout_s: float = 5.0,
) -> AsyncIterator[str]:
    client = anthropic.AsyncAnthropic()
    queue: asyncio.Queue[StreamChunk] = asyncio.Queue()
    finished = set()
    buffers: dict[str, str] = {src: "" for src in sources}

    # Launch all streams concurrently
    tasks = [
        asyncio.create_task(stream_source(client, src, query, queue))
        for src in sources
    ]

    deadline = asyncio.get_event_loop().time() + timeout_s

    while len(finished) < len(sources):
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            for t in tasks:
                t.cancel()
            break
        try:
            chunk = await asyncio.wait_for(queue.get(), timeout=remaining)
            if chunk.done:
                finished.add(chunk.source)
                if buffers[chunk.source]:
                    yield f"\n[{chunk.source} complete]: {buffers[chunk.source][:100]}..."
            else:
                buffers[chunk.source] += chunk.text
                yield chunk.text  # stream each chunk to caller
        except asyncio.TimeoutError:
            break

    # Final synthesis from complete buffers
    complete_sources = {src: buf for src, buf in buffers.items() if src in finished and buf}
    if complete_sources:
        yield "\n\n[Synthesizing...]\n"
        context = "\n".join(f"[{src}]: {buf}" for src, buf in complete_sources.items())
        synthesis = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": f"Synthesize: {context}\n\nFinal answer for: {query}"}],
        )
        yield synthesis.content[0].text

async def main():
    sources = ["docs", "examples", "api_reference", "community"]
    print("Streaming scatter-gather:\n")
    async for chunk in progressive_scatter_stream(
        "Explain Python's asyncio event loop",
        sources,
    ):
        print(chunk, end="", flush=True)
    print()

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Time-to-first-token reduced to fastest source response time
# Environment: pip install anthropic
```

## Option 6: Adaptive Scatter with Source Health Tracking

Track per-source latency and error rate. Dynamically adjust which sources to include in each scatter round based on recent health. Sources with high error rates or P95 latency above budget are excluded or deprioritized until they recover.

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field
from collections import deque
from statistics import median

@dataclass
class SourceHealth:
    name: str
    latency_window: deque = field(default_factory=lambda: deque(maxlen=20))
    error_window: deque = field(default_factory=lambda: deque(maxlen=20))
    consecutive_errors: int = 0

    def record(self, latency_ms: float, success: bool) -> None:
        self.latency_window.append(latency_ms)
        self.error_window.append(0 if success else 1)
        if success:
            self.consecutive_errors = 0
        else:
            self.consecutive_errors += 1

    @property
    def p95_latency(self) -> float:
        if not self.latency_window:
            return 0.0
        sorted_l = sorted(self.latency_window)
        return sorted_l[int(len(sorted_l) * 0.95)]

    @property
    def error_rate(self) -> float:
        if not self.error_window:
            return 0.0
        return sum(self.error_window) / len(self.error_window)

    def is_healthy(self, max_p95_ms: float = 3000, max_error_rate: float = 0.3) -> bool:
        if self.consecutive_errors >= 3:
            return False
        if self.error_window and self.error_rate > max_error_rate:
            return False
        if self.latency_window and self.p95_latency > max_p95_ms:
            return False
        return True

class AdaptiveScatterGather:
    def __init__(self, source_names: list[str]):
        self.health: dict[str, SourceHealth] = {n: SourceHealth(n) for n in source_names}
        self._client = anthropic.AsyncAnthropic()

    def select_sources(self, budget_ms: float) -> list[str]:
        healthy = [
            name for name, h in self.health.items()
            if h.is_healthy(max_p95_ms=budget_ms * 0.8)
        ]
        if not healthy:
            # Fall back to all sources if none are healthy
            print("[AdaptiveGather] All sources unhealthy — using all anyway")
            return list(self.health.keys())
        return healthy

    async def query_source(self, source: str, query: str, timeout_s: float) -> tuple[str, str | None]:
        start = time.monotonic()
        try:
            response = await asyncio.wait_for(
                self._client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=150,
                    messages=[{"role": "user", "content": f"[{source}] {query}"}],
                ),
                timeout=timeout_s,
            )
            latency_ms = (time.monotonic() - start) * 1000
            self.health[source].record(latency_ms, True)
            return source, response.content[0].text
        except Exception as e:
            latency_ms = (time.monotonic() - start) * 1000
            self.health[source].record(latency_ms, False)
            return source, None

    async def scatter(self, query: str, budget_ms: float = 3000) -> list[tuple[str, str]]:
        sources = self.select_sources(budget_ms)
        print(f"[AdaptiveGather] Selected {len(sources)}/{len(self.health)} healthy sources: {sources}")

        tasks = [self.query_source(src, query, budget_ms / 1000) for src in sources]
        raw_results = await asyncio.gather(*tasks)
        results = [(src, content) for src, content in raw_results if content]

        for name, h in self.health.items():
            if h.latency_window:
                print(f"  {name}: p95={h.p95_latency:.0f}ms err_rate={h.error_rate:.1%} healthy={h.is_healthy()}")

        return results

    async def answer(self, query: str) -> str:
        results = await self.scatter(query)
        if not results:
            return "No sources available."
        context = "\n".join(f"[{src}]: {content}" for src, content in results)
        response = await self._client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{"role": "user", "content": f"Context:\n{context}\n\nAnswer: {query}"}],
        )
        return response.content[0].text

async def main():
    gatherer = AdaptiveScatterGather(["db_primary", "db_replica", "cache", "search", "external_api"])

    # Simulate multiple query rounds
    queries = [
        "What are the benefits of database indexing?",
        "How do read replicas improve scalability?",
        "What is the CAP theorem?",
    ]
    for q in queries:
        print(f"\nQuery: {q}")
        result = await gatherer.answer(q)
        print(f"Answer: {result[:150]}...")

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Reduces wasted calls to unhealthy sources; focuses budget on responsive sources
# Environment: pip install anthropic
```

## Comparison

| Option | Error Handling | Latency Guarantee | Source Selection | Best For |
|--------|---------------|------------------|-----------------|----------|
| 1. asyncio.gather | Partial results | None | All sources | Simple multi-source fan-out |
| 2. Deadline-Bounded | Cancels late sources | Hard deadline | All within budget | SLA-constrained responses |
| 3. Priority Lanes | Skip low-priority | Tiered | By weight/priority | Mixed-speed source pools |
| 4. Dedup + Merge | Partial results | None | All sources | Overlapping source corpora |
| 5. Streaming | Partial results | First-token fast | All sources | Real-time user-facing |
| 6. Adaptive Health | Skips unhealthy | None | Health-filtered | Long-running production agents |
