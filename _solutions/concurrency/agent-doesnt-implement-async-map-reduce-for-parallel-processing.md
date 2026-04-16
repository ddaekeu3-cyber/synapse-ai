---
layout: solution
title: "Agent Doesn't Implement Async Map-Reduce for Parallel Processing"
category: concurrency
description: "Agents that process large datasets serially are bottlenecked by sequential API calls. Async map-reduce splits work across parallel workers then combines results, reducing wall-clock time proportional to the number of concurrent workers."
tags: [concurrency, asyncio, map-reduce, parallel, performance, python]
---

## Problem

Processing 100 documents one-by-one at 1 second each takes 100 seconds. An async map-reduce that fans out to 10 concurrent workers completes the same work in ~10 seconds. Without parallelism, agents waste the majority of their wall-clock time waiting for network I/O — time that could be used to process other items.

## Solutions

### Option 1: Simple AsyncIO Gather Map-Reduce

```python
import anthropic
import asyncio
from dataclasses import dataclass
from typing import TypeVar, Callable, Awaitable

T = TypeVar("T")
R = TypeVar("R")

@dataclass
class MapReduceResult:
    items_processed: int
    results: list
    errors: list[str]

async def async_map(
    items: list[T],
    map_fn: Callable[[T], Awaitable[R]],
    concurrency: int = 5,
) -> list[R | Exception]:
    semaphore = asyncio.Semaphore(concurrency)

    async def bounded(item: T) -> R | Exception:
        async with semaphore:
            try:
                return await map_fn(item)
            except Exception as e:
                return e

    return await asyncio.gather(*[bounded(item) for item in items])

def reduce_summaries(results: list[str | Exception]) -> str:
    valid = [r for r in results if isinstance(r, str)]
    errors = [str(e) for e in results if isinstance(e, Exception)]
    combined = "\n\n".join(f"• {r}" for r in valid)
    return f"Combined summary ({len(valid)} of {len(results)} succeeded):\n{combined}"

async def summarize_document(client: anthropic.AsyncAnthropic, doc: str) -> str:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=80,
        messages=[{
            "role": "user",
            "content": f"Summarize in one sentence: {doc[:300]}"
        }],
    )
    return response.content[0].text.strip()

async def main():
    client = anthropic.AsyncAnthropic()
    documents = [
        "Photosynthesis is the process by which plants convert sunlight, water, and CO2 into glucose and oxygen.",
        "The water cycle describes the continuous movement of water within Earth and its atmosphere.",
        "DNA (deoxyribonucleic acid) carries genetic information and is found in the nucleus of cells.",
        "Gravity is a fundamental force that attracts objects with mass toward one another.",
        "The Internet is a global network of interconnected computers that share data via standardized protocols.",
    ]

    import time
    t0 = time.monotonic()
    raw_results = await async_map(
        documents,
        lambda doc: summarize_document(client, doc),
        concurrency=3,
    )
    elapsed = time.monotonic() - t0

    result = reduce_summaries(raw_results)
    print(result)
    print(f"\nProcessed {len(documents)} docs in {elapsed:.2f}s "
          f"({len(documents)/elapsed:.1f} docs/sec)")

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: N/A — wall-clock time reduced by ~concurrency factor
# Environment: pip install anthropic
```

### Option 2: Chunked Map-Reduce with Hierarchical Reduce

```python
import anthropic
import asyncio
import math
from typing import TypeVar, Callable, Awaitable

T = TypeVar("T")

async def map_chunk(
    client: anthropic.AsyncAnthropic,
    chunk: list[str],
    chunk_id: int,
) -> str:
    """Map phase: summarize a chunk of documents into one summary."""
    combined = "\n---\n".join(chunk)
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        messages=[{
            "role": "user",
            "content": (f"You are given {len(chunk)} text excerpts. "
                        f"Write one cohesive summary paragraph:\n\n{combined[:1200]}")
        }],
    )
    summary = response.content[0].text.strip()
    print(f"[Chunk {chunk_id}] Summarized {len(chunk)} docs → {len(summary)} chars")
    return summary

async def reduce_phase(
    client: anthropic.AsyncAnthropic,
    summaries: list[str],
) -> str:
    """Reduce phase: combine chunk summaries into final answer."""
    all_summaries = "\n\n".join(f"Summary {i+1}: {s}" for i, s in enumerate(summaries))
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{
            "role": "user",
            "content": (f"Combine these {len(summaries)} summaries into one "
                        f"unified final summary:\n\n{all_summaries}")
        }],
    )
    return response.content[0].text.strip()

async def chunked_map_reduce(
    client: anthropic.AsyncAnthropic,
    documents: list[str],
    chunk_size: int = 3,
    max_concurrency: int = 4,
) -> str:
    # Split into chunks
    chunks = [documents[i:i+chunk_size] for i in range(0, len(documents), chunk_size)]
    print(f"Map: {len(documents)} docs → {len(chunks)} chunks (size={chunk_size})")

    # Map phase (parallel)
    semaphore = asyncio.Semaphore(max_concurrency)
    async def bounded_map(chunk: list[str], idx: int) -> str:
        async with semaphore:
            return await map_chunk(client, chunk, idx)

    import time
    t0 = time.monotonic()
    chunk_summaries = await asyncio.gather(*[
        bounded_map(chunk, i) for i, chunk in enumerate(chunks)
    ])
    map_elapsed = time.monotonic() - t0

    # Reduce phase
    t1 = time.monotonic()
    if len(chunk_summaries) == 1:
        final = chunk_summaries[0]
    else:
        final = await reduce_phase(client, list(chunk_summaries))
    reduce_elapsed = time.monotonic() - t1

    print(f"\nMap: {map_elapsed:.2f}s | Reduce: {reduce_elapsed:.2f}s | "
          f"Total: {map_elapsed+reduce_elapsed:.2f}s")
    return final

async def main():
    client = anthropic.AsyncAnthropic()
    documents = [
        "Machine learning is a branch of AI that enables systems to learn from data.",
        "Deep learning uses neural networks with many layers to model complex patterns.",
        "Natural language processing helps computers understand human language.",
        "Computer vision enables machines to interpret visual information.",
        "Reinforcement learning trains agents through reward and punishment signals.",
        "Transfer learning reuses a model trained on one task for a different task.",
    ]

    result = await chunked_map_reduce(client, documents, chunk_size=2, max_concurrency=3)
    print(f"\nFinal result:\n{result}")

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Hierarchical reduce uses fewer tokens than single large context
# Environment: pip install anthropic
```

### Option 3: TaskGroup Map-Reduce with Progress Tracking

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

@dataclass
class MapReduceProgress:
    total: int
    completed: int = 0
    failed: int = 0
    start_time: float = field(default_factory=time.monotonic)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.start_time

    @property
    def rate(self) -> float:
        return self.completed / max(self.elapsed, 0.001)

    def update(self, success: bool) -> None:
        if success:
            self.completed += 1
        else:
            self.failed += 1
        pct = (self.completed + self.failed) / self.total * 100
        print(f"\r[Progress] {self.completed}/{self.total} done "
              f"({pct:.0f}%) | {self.rate:.1f} items/s | "
              f"{self.failed} failed", end="", flush=True)

async def process_item(
    client: anthropic.AsyncAnthropic,
    item: dict,
    semaphore: asyncio.Semaphore,
    progress: MapReduceProgress,
) -> dict:
    async with semaphore:
        try:
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=60,
                messages=[{
                    "role": "user",
                    "content": (f"Classify this text as POSITIVE, NEGATIVE, or NEUTRAL. "
                                f"Reply with just the label.\n\nText: {item['text']}")
                }],
            )
            label = response.content[0].text.strip().upper()
            result = {**item, "label": label, "success": True}
            progress.update(success=True)
            return result
        except Exception as e:
            progress.update(success=False)
            return {**item, "label": "ERROR", "error": str(e), "success": False}

def reduce_classifications(results: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for r in results:
        label = r.get("label", "ERROR")
        counts[label] = counts.get(label, 0) + 1
    return {
        "total": len(results),
        "counts": counts,
        "success_rate": sum(1 for r in results if r["success"]) / len(results),
    }

async def main():
    client = anthropic.AsyncAnthropic()
    items = [
        {"id": i, "text": text} for i, text in enumerate([
            "I love this product, it works perfectly!",
            "Terrible experience, would not recommend.",
            "The package arrived on time.",
            "Absolutely fantastic quality!",
            "It's okay, nothing special.",
            "Broken on arrival, very disappointed.",
            "Great value for money.",
            "Average product, meets expectations.",
        ])
    ]

    semaphore = asyncio.Semaphore(4)
    progress = MapReduceProgress(total=len(items))

    # Map phase using TaskGroup (Python 3.11+)
    results = []
    try:
        async with asyncio.TaskGroup() as tg:
            tasks = [
                tg.create_task(process_item(client, item, semaphore, progress))
                for item in items
            ]
        results = [t.result() for t in tasks]
    except* Exception as eg:
        print(f"\nErrors: {eg.exceptions}")
        results = [t.result() for t in tasks if not t.cancelled() and t.done()]

    print()  # newline after progress bar

    # Reduce phase
    summary = reduce_classifications(results)
    print(f"\nClassification summary: {summary}")
    for r in results:
        print(f"  [{r['label']:8}] {r['text'][:50]}")

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: N/A — parallel execution reduces wall-clock time
# Environment: pip install anthropic; Python 3.11+ for TaskGroup
```

### Option 4: Multi-Stage Pipeline Map-Reduce

```python
import anthropic
import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

@dataclass
class PipelineStage:
    name: str
    fn: Callable[[anthropic.AsyncAnthropic, Any], Awaitable[Any]]
    concurrency: int = 5

@dataclass
class PipelineItem:
    id: str
    data: Any
    stage_results: dict = field(default_factory=dict)
    failed_at: str | None = None
    error: str | None = None

async def run_stage(
    client: anthropic.AsyncAnthropic,
    stage: PipelineStage,
    items: list[PipelineItem],
) -> list[PipelineItem]:
    semaphore = asyncio.Semaphore(stage.concurrency)
    results = []

    async def process_one(item: PipelineItem) -> PipelineItem:
        if item.failed_at:
            return item  # Skip failed items
        async with semaphore:
            try:
                result = await stage.fn(client, item.data)
                item.stage_results[stage.name] = result
                item.data = result  # Pass output to next stage
            except Exception as e:
                item.failed_at = stage.name
                item.error = str(e)
        return item

    processed = await asyncio.gather(*[process_one(item) for item in items])
    ok = sum(1 for p in processed if not p.failed_at)
    failed = len(processed) - ok
    print(f"[Stage:{stage.name}] {ok} ok, {failed} failed")
    return list(processed)

# Stage functions
async def extract_keywords(client: anthropic.AsyncAnthropic, text: str) -> str:
    r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=50,
        messages=[{"role": "user",
                   "content": f"List 3 keywords from: {text[:200]}. Format: word1, word2, word3"}],
    )
    return r.content[0].text.strip()

async def classify_domain(client: anthropic.AsyncAnthropic, keywords: str) -> str:
    r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=20,
        messages=[{"role": "user",
                   "content": f"What domain? (Science/Tech/Arts/Business/Other): {keywords}"}],
    )
    return r.content[0].text.strip()

async def generate_tag(client: anthropic.AsyncAnthropic, domain: str) -> str:
    r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=15,
        messages=[{"role": "user",
                   "content": f"Short hashtag for {domain} domain (no spaces):"}],
    )
    return r.content[0].text.strip().lstrip("#")

def reduce_by_domain(items: list[PipelineItem]) -> dict:
    domain_groups: dict[str, list[str]] = {}
    for item in items:
        if not item.failed_at:
            domain = item.stage_results.get("classify_domain", "Unknown")
            domain_groups.setdefault(domain, []).append(item.id)
    return domain_groups

async def main():
    client = anthropic.AsyncAnthropic()
    documents = {
        "doc-1": "Quantum entanglement is a physical phenomenon in quantum mechanics.",
        "doc-2": "The stock market reached new highs driven by tech sector growth.",
        "doc-3": "Machine learning models improve with more training data.",
        "doc-4": "The Renaissance was a period of European cultural and artistic rebirth.",
        "doc-5": "CRISPR technology enables precise editing of DNA sequences.",
    }

    items = [PipelineItem(id=k, data=v) for k, v in documents.items()]
    stages = [
        PipelineStage("extract_keywords", extract_keywords, concurrency=3),
        PipelineStage("classify_domain", classify_domain, concurrency=3),
        PipelineStage("generate_tag", generate_tag, concurrency=3),
    ]

    for stage in stages:
        items = await run_stage(client, stage, items)

    # Reduce
    groups = reduce_by_domain(items)
    print("\nDocuments by domain:")
    for domain, ids in groups.items():
        print(f"  {domain}: {ids}")

    print("\nPer-document results:")
    for item in items:
        if not item.failed_at:
            print(f"  {item.id}: keywords={item.stage_results.get('extract_keywords', '')[:40]} "
                  f"domain={item.stage_results.get('classify_domain', '')} "
                  f"tag=#{item.stage_results.get('generate_tag', '')}")

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Pipelining 3 stages in parallel vs serial cuts time by ~3x
# Environment: pip install anthropic
```

### Option 5: Distributed Map-Reduce with Worker Pools and Load Balancing

```python
import anthropic
import asyncio
import time
import uuid
from dataclasses import dataclass, field
from collections import defaultdict
from typing import Any

@dataclass
class WorkerStats:
    worker_id: str
    tasks_completed: int = 0
    tasks_failed: int = 0
    total_tokens: int = 0
    total_time_ms: float = 0.0

    @property
    def avg_time_ms(self) -> float:
        return self.total_time_ms / max(self.tasks_completed, 1)

class WorkerPool:
    def __init__(self, n_workers: int, client: anthropic.AsyncAnthropic):
        self.n_workers = n_workers
        self.client = client
        self._queue: asyncio.Queue = asyncio.Queue()
        self._results: asyncio.Queue = asyncio.Queue()
        self._stats: dict[str, WorkerStats] = {}
        self._workers: list[asyncio.Task] = []

    async def start(self) -> None:
        for i in range(self.n_workers):
            wid = f"worker-{i}"
            self._stats[wid] = WorkerStats(worker_id=wid)
            self._workers.append(asyncio.create_task(self._worker_loop(wid)))

    async def _worker_loop(self, worker_id: str) -> None:
        stats = self._stats[worker_id]
        while True:
            try:
                task_id, item = await asyncio.wait_for(self._queue.get(), timeout=5.0)
            except asyncio.TimeoutError:
                return  # No more work

            t0 = time.monotonic()
            try:
                response = await self.client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=60,
                    messages=[{
                        "role": "user",
                        "content": f"Summarize in ≤10 words: {item['text'][:300]}"
                    }],
                )
                elapsed_ms = (time.monotonic() - t0) * 1000
                stats.tasks_completed += 1
                stats.total_tokens += response.usage.output_tokens
                stats.total_time_ms += elapsed_ms
                await self._results.put({
                    "task_id": task_id, "worker_id": worker_id,
                    "summary": response.content[0].text.strip(),
                    "success": True, "original": item,
                })
            except Exception as e:
                stats.tasks_failed += 1
                await self._results.put({
                    "task_id": task_id, "worker_id": worker_id,
                    "error": str(e), "success": False, "original": item,
                })
            finally:
                self._queue.task_done()

    async def map(self, items: list[dict]) -> list[dict]:
        for item in items:
            await self._queue.put((str(uuid.uuid4()), item))

        await self._queue.join()
        await asyncio.gather(*self._workers, return_exceptions=True)

        results = []
        while not self._results.empty():
            results.append(self._results.get_nowait())
        return results

    def worker_stats(self) -> list[dict]:
        return [
            {"id": s.worker_id, "done": s.tasks_completed, "failed": s.tasks_failed,
             "tokens": s.total_tokens, "avg_ms": f"{s.avg_time_ms:.0f}"}
            for s in self._stats.values()
        ]

def reduce_by_worker(results: list[dict]) -> dict:
    by_worker = defaultdict(list)
    for r in results:
        by_worker[r["worker_id"]].append(r)
    return {wid: len(items) for wid, items in by_worker.items()}

async def main():
    client = anthropic.AsyncAnthropic()
    pool = WorkerPool(n_workers=3, client=client)
    await pool.start()

    items = [
        {"id": i, "text": text} for i, text in enumerate([
            "Artificial intelligence is transforming healthcare diagnostics.",
            "Climate change threatens biodiversity worldwide.",
            "Electric vehicles reduce carbon emissions significantly.",
            "Blockchain technology enables decentralized finance.",
            "The James Webb telescope reveals distant galaxies.",
            "Gene therapy shows promise for treating inherited diseases.",
        ])
    ]

    t0 = time.monotonic()
    results = await pool.map(items)
    elapsed = time.monotonic() - t0

    print(f"Processed {len(results)} items in {elapsed:.2f}s")
    print(f"\nWorker load distribution: {reduce_by_worker(results)}")
    print("\nWorker stats:")
    for s in pool.worker_stats():
        print(f"  {s}")
    print("\nSummaries:")
    for r in sorted(results, key=lambda x: x["original"]["id"]):
        if r["success"]:
            print(f"  [{r['worker_id']}] {r['summary'][:60]}")

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: N/A — worker pool maximizes throughput
# Environment: pip install anthropic
```

### Option 6: Adaptive Map-Reduce with Dynamic Concurrency Control

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

@dataclass
class AdaptiveController:
    """Dynamically adjusts concurrency based on error rate and latency."""
    min_concurrency: int = 1
    max_concurrency: int = 10
    current: int = 3
    _error_window: list[bool] = field(default_factory=list)
    _latency_window: list[float] = field(default_factory=list)
    _window_size: int = 10

    def record(self, success: bool, latency_ms: float) -> None:
        self._error_window.append(not success)
        self._latency_window.append(latency_ms)
        if len(self._error_window) > self._window_size:
            self._error_window.pop(0)
            self._latency_window.pop(0)

    def adjust(self) -> int:
        if len(self._error_window) < 3:
            return self.current
        error_rate = sum(self._error_window) / len(self._error_window)
        avg_latency = sum(self._latency_window) / len(self._latency_window)

        old = self.current
        if error_rate > 0.3 or avg_latency > 5000:
            self.current = max(self.min_concurrency, self.current - 1)
        elif error_rate < 0.05 and avg_latency < 2000:
            self.current = min(self.max_concurrency, self.current + 1)

        if self.current != old:
            print(f"[AdaptiveCtrl] concurrency {old} → {self.current} "
                  f"(err={error_rate:.0%} lat={avg_latency:.0f}ms)")
        return self.current

async def adaptive_map_reduce(
    client: anthropic.AsyncAnthropic,
    items: list[Any],
    map_fn: Callable[[anthropic.AsyncAnthropic, Any], Awaitable[Any]],
    reduce_fn: Callable[[list[Any]], Any],
    initial_concurrency: int = 3,
) -> Any:
    controller = AdaptiveController(current=initial_concurrency)
    semaphore = asyncio.Semaphore(controller.current)
    results: list[Any] = [None] * len(items)
    lock = asyncio.Lock()

    async def process(idx: int, item: Any) -> None:
        async with semaphore:
            t0 = time.monotonic()
            success = True
            try:
                result = await map_fn(client, item)
                results[idx] = result
            except Exception as e:
                results[idx] = e
                success = False
            finally:
                latency_ms = (time.monotonic() - t0) * 1000
                async with lock:
                    controller.record(success, latency_ms)
                    new_limit = controller.adjust()
                    semaphore._value = new_limit  # Dynamically update

    await asyncio.gather(*[process(i, item) for i, item in enumerate(items)])
    return reduce_fn([r for r in results if not isinstance(r, Exception)])

async def extract_sentiment_score(client: anthropic.AsyncAnthropic, text: str) -> float:
    r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=10,
        messages=[{"role": "user",
                   "content": f"Rate sentiment 0.0 (negative) to 1.0 (positive). "
                               f"Reply with number only: {text[:200]}"}],
    )
    try:
        return float(r.content[0].text.strip())
    except ValueError:
        return 0.5

def average_sentiment(scores: list[float]) -> dict:
    if not scores:
        return {"avg": 0.0, "count": 0}
    avg = sum(scores) / len(scores)
    return {
        "avg_sentiment": round(avg, 3),
        "count": len(scores),
        "label": "positive" if avg > 0.6 else "negative" if avg < 0.4 else "neutral",
    }

async def main():
    client = anthropic.AsyncAnthropic()
    texts = [
        "The new feature works flawlessly and I'm very happy with it.",
        "Support response time was slow but eventually resolved my issue.",
        "Complete waste of money. Nothing works as advertised.",
        "Decent product with room for improvement.",
        "Exceeded my expectations in every way!",
        "Neutral experience, nothing remarkable.",
    ]

    t0 = time.monotonic()
    result = await adaptive_map_reduce(
        client, texts,
        extract_sentiment_score,
        average_sentiment,
        initial_concurrency=3,
    )
    elapsed = time.monotonic() - t0

    print(f"\nSentiment analysis of {len(texts)} reviews:")
    print(f"  Result: {result}")
    print(f"  Time: {elapsed:.2f}s ({len(texts)/elapsed:.1f} texts/sec)")

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Adaptive control prevents rate-limit errors that waste retried tokens
# Environment: pip install anthropic
```

## Comparison

| Option | Pattern | Concurrency Control | Fault Tolerance | Best For |
|--------|---------|---------------------|-----------------|----------|
| 1. Simple Gather | Fan-out + combine | Semaphore | Error capture | Quick parallel tasks |
| 2. Chunked Hierarchical | Map → reduce in stages | Semaphore | None | Large document sets |
| 3. TaskGroup + Progress | Structured concurrency | Semaphore | TaskGroup errors | Python 3.11+ |
| 4. Multi-Stage Pipeline | Sequential stages in parallel | Per-stage | Stage-level skip | ETL pipelines |
| 5. Worker Pool | Dedicated workers | Queue-based | Per-worker retry | High-volume streams |
| 6. Adaptive Concurrency | Dynamic rate control | Auto-scaling | Rate limit aware | Unpredictable loads |
