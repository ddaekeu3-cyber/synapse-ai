---
layout: solution
title: "Agent Doesn't Use Worker Pool for CPU-Bound Tasks"
category: concurrency
description: "Agent runs CPU-intensive post-processing (JSON parsing, text chunking, embedding computation, regex scanning) in the main asyncio event loop, blocking all other coroutines while one request monopolises the thread."
tags: [concurrency, worker-pool, cpu-bound, asyncio, multiprocessing, performance]
---

## Symptom

The agent handles 50 concurrent LLM calls fine, but every time a response arrives it parses a large JSON blob, runs regex extraction over 10,000 tokens, or computes a vector embedding on-device. During that computation, all other coroutines stall: the event loop is blocked. Latency for unrelated requests spikes from 200ms to 3,000ms whenever a heavy post-processing job runs. `asyncio.get_event_loop().is_running()` returns True but new tasks queue up with no progress.

## Root Cause

Python's `asyncio` event loop is single-threaded. I/O-bound work (network, disk) yields control via `await`, allowing other coroutines to run. CPU-bound work (parsing, computing, regex) never yields — it holds the thread until it finishes. Calling a synchronous CPU-intensive function directly inside an `async def` function blocks the entire event loop for the duration. The fix is to offload CPU work to a thread pool (`asyncio.to_thread` / `ThreadPoolExecutor`) or a process pool (`ProcessPoolExecutor`) depending on whether the GIL is the bottleneck.

## Fix

### Option 1 — `asyncio.to_thread` to offload blocking CPU work

```python
import asyncio
import json
import re
import time
import anthropic

client = anthropic.AsyncAnthropic()

# CPU-bound post-processing — runs in a thread pool
def extract_structured_data(raw_text: str) -> dict:
    """Simulate heavy regex + JSON extraction."""
    time.sleep(0.05)   # simulate 50ms of CPU work
    emails  = re.findall(r"[\w.+-]+@[\w-]+\.[a-z]{2,}", raw_text)
    urls    = re.findall(r"https?://\S+", raw_text)
    numbers = re.findall(r"\b\d{4,}\b", raw_text)
    return {"emails": emails, "urls": urls, "numbers": numbers, "length": len(raw_text)}

async def ask_and_extract(question: str) -> dict:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": question}],
    )
    raw = response.content[0].text
    # WRONG: extract_structured_data(raw)  ← blocks the event loop
    # CORRECT: offload to thread pool
    result = await asyncio.to_thread(extract_structured_data, raw)
    return result

async def main() -> None:
    questions = [
        "List some example email addresses and URLs.",
        "Give some phone numbers and zip codes.",
        "Write a paragraph mentioning https://example.com and test@example.org.",
    ]

    print("Sequential (blocks event loop on CPU work):")
    t0 = time.perf_counter()
    for q in questions:
        await ask_and_extract(q)
    seq_ms = (time.perf_counter() - t0) * 1000
    print(f"  {seq_ms:.0f}ms total")

    print("\nParallel with asyncio.to_thread (non-blocking):")
    t0 = time.perf_counter()
    results = await asyncio.gather(*[ask_and_extract(q) for q in questions])
    par_ms = (time.perf_counter() - t0) * 1000
    print(f"  {par_ms:.0f}ms total")
    for r in results:
        print(f"  → emails={r['emails'][:1]} urls={r['urls'][:1]} nums={r['numbers'][:2]}")

asyncio.run(main())
```

**Expected Token Savings:** No token reduction; `asyncio.to_thread` offloads CPU work so the event loop remains responsive — prevents 2-5s stalls on other coroutines during heavy post-processing; equivalent to adding concurrency without any infrastructure change.
**Environment:** Async agents with per-response post-processing (regex, JSON parsing, text chunking); `asyncio.to_thread` is the simplest and most idiomatic fix requiring no third-party dependency.

---

### Option 2 — `ProcessPoolExecutor` to bypass the GIL for true parallelism

```python
import asyncio
import time
import hashlib
from concurrent.futures import ProcessPoolExecutor

import anthropic

client = anthropic.AsyncAnthropic()

# CPU-bound function that benefits from true parallelism (bypasses GIL)
def compute_content_hash_and_stats(text: str) -> dict:
    """Simulate GIL-bound CPU work: hashing + character frequency."""
    sha256 = hashlib.sha256(text.encode()).hexdigest()
    freq   = {}
    for ch in text.lower():
        if ch.isalpha():
            freq[ch] = freq.get(ch, 0) + 1
    top5 = sorted(freq.items(), key=lambda x: -x[1])[:5]
    time.sleep(0.03)   # simulate heavy computation
    return {"hash": sha256[:16], "top_chars": top5, "word_count": len(text.split())}

# Shared process pool — create once, reuse across all requests
_PROCESS_POOL = ProcessPoolExecutor(max_workers=4)

async def ask_and_analyse(question: str) -> dict:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": question}],
    )
    raw  = response.content[0].text
    loop = asyncio.get_running_loop()
    # Submit to process pool — runs in a separate OS process, bypassing GIL
    result = await loop.run_in_executor(_PROCESS_POOL, compute_content_hash_and_stats, raw)
    return result

async def main() -> None:
    questions = [f"Write a sentence about topic {i}." for i in range(6)]

    t0 = time.perf_counter()
    results = await asyncio.gather(*[ask_and_analyse(q) for q in questions])
    elapsed = (time.perf_counter() - t0) * 1000
    print(f"{len(questions)} requests with process pool in {elapsed:.0f}ms")
    for r in results:
        print(f"  hash={r['hash']} words={r['word_count']} top={r['top_chars'][:2]}")

    _PROCESS_POOL.shutdown(wait=False)

asyncio.run(main())
```

**Expected Token Savings:** `ProcessPoolExecutor` runs CPU work in separate OS processes — true parallelism unaffected by the GIL; for compute-heavy tasks (embedding, image processing, regex on large corpora), process pool delivers 2-4× speedup on multi-core machines.
**Environment:** Agents performing CPU-intensive work that is genuinely GIL-bound (numerical computation, cryptography, heavy regex); process pool adds ~10ms spawn overhead per task so is best for work taking >50ms.

---

### Option 3 — Bounded `ThreadPoolExecutor` with semaphore to prevent overload

```python
import asyncio
import time
import json
from concurrent.futures import ThreadPoolExecutor

import anthropic

client = anthropic.AsyncAnthropic()

# Shared thread pool — sized to CPU count, not request count
_THREAD_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="cpu-worker")
_CPU_SEM     = asyncio.Semaphore(4)   # match pool size

def parse_and_validate(raw: str) -> dict:
    """CPU-bound: attempt JSON parse, extract schema, validate fields."""
    time.sleep(0.02)   # simulate 20ms parsing
    try:
        data     = json.loads(raw)
        is_valid = isinstance(data, dict) and len(data) > 0
    except json.JSONDecodeError:
        data, is_valid = {}, False
    return {"valid": is_valid, "keys": list(data.keys())[:5], "raw_len": len(raw)}

async def ask_and_parse(question: str) -> dict:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        system='Return a JSON object with 2-3 keys relevant to the question. Raw JSON only.',
        messages=[{"role": "user", "content": question}],
    )
    raw  = response.content[0].text.strip()
    loop = asyncio.get_running_loop()

    async with _CPU_SEM:   # prevents spawning more work than the pool can handle
        result = await loop.run_in_executor(_THREAD_POOL, parse_and_validate, raw)

    result["raw"] = raw[:60]
    return result

async def main() -> None:
    questions = [
        "Python language features",
        "HTTP status codes",
        "Database types",
        "Cloud providers",
        "Testing frameworks",
        "CI/CD tools",
    ]
    t0 = time.perf_counter()
    results = await asyncio.gather(*[ask_and_parse(q) for q in questions])
    elapsed = (time.perf_counter() - t0) * 1000
    print(f"{len(questions)} requests in {elapsed:.0f}ms")
    for r in results:
        status = "✓" if r["valid"] else "✗"
        print(f"  [{status}] keys={r['keys']} raw={r['raw'][:40]!r}")

    _THREAD_POOL.shutdown(wait=False)

asyncio.run(main())
```

**Expected Token Savings:** Bounded pool prevents thread explosion under burst load — without a semaphore, 100 concurrent requests would each enqueue CPU work and the thread pool would queue them internally, causing unbounded memory growth; semaphore caps queued work to pool size.
**Environment:** High-concurrency agents where burst traffic can arrive faster than CPU workers can drain the queue; semaphore-bounded dispatch is the production-safe pattern.

---

### Option 4 — Background worker queue: decouple response generation from post-processing

```python
import asyncio
import time
import anthropic

client = anthropic.AsyncAnthropic()

# CPU-bound work item
async def cpu_worker(queue: asyncio.Queue, results: dict) -> None:
    while True:
        item = await queue.get()
        if item is None:
            queue.task_done()
            break
        request_id, raw = item
        # Offload actual CPU work to thread so worker coroutine stays non-blocking
        processed = await asyncio.to_thread(heavy_process, raw)
        results[request_id] = processed
        queue.task_done()

def heavy_process(text: str) -> dict:
    time.sleep(0.03)   # simulate CPU work
    sentences = [s.strip() for s in text.split(".") if s.strip()]
    return {"sentences": len(sentences), "chars": len(text), "preview": text[:80]}

async def run_pipeline(questions: list[str]) -> list[dict]:
    queue   = asyncio.Queue(maxsize=20)   # bounded — back-pressure if workers fall behind
    results = {}
    # Start 3 CPU workers
    workers = [asyncio.create_task(cpu_worker(queue, results)) for _ in range(3)]

    # Step 1: fire all LLM calls concurrently
    async def ask_and_enqueue(idx: int, question: str) -> None:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{"role": "user", "content": question}],
        )
        await queue.put((idx, response.content[0].text))

    await asyncio.gather(*[ask_and_enqueue(i, q) for i, q in enumerate(questions)])

    # Step 2: signal workers to stop and wait for queue to drain
    for _ in workers:
        await queue.put(None)
    await queue.join()
    for w in workers:
        w.cancel()

    return [results[i] for i in range(len(questions))]

async def main() -> None:
    questions = [f"Describe concept #{i} in one sentence." for i in range(8)]
    t0 = time.perf_counter()
    results = await run_pipeline(questions)
    elapsed = (time.perf_counter() - t0) * 1000
    print(f"Pipeline: {len(questions)} LLM calls + CPU processing in {elapsed:.0f}ms")
    for r in results:
        print(f"  sentences={r['sentences']} chars={r['chars']} preview={r['preview'][:50]!r}")

asyncio.run(main())
```

**Expected Token Savings:** Queue-based decoupling allows LLM responses to be collected at network speed while CPU workers process them at compute speed — the slower stage determines throughput; prevents idle LLM call capacity when post-processing is the bottleneck.
**Environment:** Pipeline agents where LLM response rate and CPU processing rate differ significantly; queue provides natural flow control with the `maxsize` parameter preventing memory unbounded growth.

---

### Option 5 — Chunked CPU work with `asyncio.sleep(0)` yield points

```python
import asyncio
import time
import anthropic

client = anthropic.AsyncAnthropic()

async def chunked_text_analysis(text: str, chunk_size: int = 500) -> dict:
    """
    Break CPU-bound text analysis into chunks, yielding between each chunk
    so the event loop can process other coroutines between chunks.
    """
    chunks      = [text[i:i+chunk_size] for i in range(0, len(text), chunk_size)]
    word_counts = []
    char_counts = []

    for i, chunk in enumerate(chunks):
        # CPU work for this chunk
        words = len(chunk.split())
        chars = sum(1 for c in chunk if c.isalpha())
        word_counts.append(words)
        char_counts.append(chars)

        # Yield control every chunk so other coroutines can run
        if i % 5 == 0:
            await asyncio.sleep(0)   # yield point — event loop processes other tasks

    return {
        "total_words": sum(word_counts),
        "total_chars": sum(char_counts),
        "chunks":      len(chunks),
    }

async def ask_and_analyse(question: str) -> dict:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": question}],
    )
    raw    = response.content[0].text
    result = await chunked_text_analysis(raw)
    return result

async def ticker(label: str, stop: asyncio.Event) -> None:
    """Demonstrates that the event loop stays responsive during analysis."""
    while not stop.is_set():
        print(f"  [{label}] event loop alive")
        await asyncio.sleep(0.1)

async def main() -> None:
    stop  = asyncio.Event()
    tick  = asyncio.create_task(ticker("ticker", stop))

    questions = [
        "Write a detailed paragraph about distributed systems.",
        "Explain microservices architecture in depth.",
    ]
    t0 = time.perf_counter()
    results = await asyncio.gather(*[ask_and_analyse(q) for q in questions])
    elapsed = (time.perf_counter() - t0) * 1000

    stop.set()
    await tick
    print(f"\n{len(questions)} analyses in {elapsed:.0f}ms")
    for r in results:
        print(f"  words={r['total_words']} alpha_chars={r['total_chars']} chunks={r['chunks']}")

asyncio.run(main())
```

**Expected Token Savings:** Cooperative yielding via `asyncio.sleep(0)` keeps the event loop responsive without spawning threads or processes — a zero-dependency solution for moderate CPU loads; reduces worst-case latency spikes by distributing CPU work across multiple event loop ticks.
**Environment:** Agents where CPU work can be naturally decomposed into chunks (text scanning, token counting, incremental parsing); best when work is moderate (<100ms total) and process/thread overhead isn't justified.

---

### Option 6 — CPU budget monitor: detect and warn on event loop blocking

```python
import asyncio
import time
import threading
import anthropic

client = anthropic.AsyncAnthropic()

class EventLoopMonitor:
    """
    Background thread that pings the event loop periodically.
    If a ping takes longer than the threshold, it logs the blocking call.
    """

    def __init__(self, threshold_ms: float = 50.0, interval_ms: float = 20.0):
        self._threshold  = threshold_ms / 1000
        self._interval   = interval_ms / 1000
        self._loop:  asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._blocked_count = 0

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop    = loop
        self._running = True
        self._thread  = threading.Thread(target=self._monitor, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def _monitor(self) -> None:
        while self._running:
            start = time.monotonic()
            done  = threading.Event()
            self._loop.call_soon_threadsafe(done.set)
            done.wait(timeout=self._threshold * 3)
            latency = time.monotonic() - start - self._interval
            if latency > self._threshold:
                self._blocked_count += 1
                print(f"  [MONITOR] event loop blocked {latency*1000:.0f}ms — #{self._blocked_count}")
            time.sleep(self._interval)

    @property
    def blocked_count(self) -> int:
        return self._blocked_count

MONITOR = EventLoopMonitor(threshold_ms=30.0)

def slow_cpu_work(text: str) -> dict:
    """Intentionally blocking CPU work — should be offloaded."""
    time.sleep(0.08)   # 80ms blocking
    return {"len": len(text)}

async def ask_bad(question: str) -> dict:
    """BAD: runs CPU work directly in the event loop."""
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": question}],
    )
    return slow_cpu_work(response.content[0].text)   # BLOCKS event loop

async def ask_good(question: str) -> dict:
    """GOOD: offloads CPU work to thread pool."""
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": question}],
    )
    return await asyncio.to_thread(slow_cpu_work, response.content[0].text)

async def main() -> None:
    loop = asyncio.get_running_loop()
    MONITOR.start(loop)

    questions = ["What is Python?", "What is asyncio?"]

    print("BAD: blocking CPU work in event loop:")
    for q in questions:
        await ask_bad(q)
    await asyncio.sleep(0.1)
    print(f"  Blocked events detected: {MONITOR.blocked_count}")

    before = MONITOR.blocked_count
    print("\nGOOD: CPU work offloaded to thread:")
    for q in questions:
        await ask_good(q)
    await asyncio.sleep(0.1)
    after = MONITOR.blocked_count
    print(f"  New blocked events: {after - before}")

    MONITOR.stop()

asyncio.run(main())
```

**Expected Token Savings:** Event loop monitor detects blocking calls in production before they become user-visible latency spikes; running in staging with the monitor catches every accidental `time.sleep()` or synchronous file read left in async handlers.
**Environment:** Teams instrumenting new async agents in staging; the monitor is a diagnostic tool that identifies which functions need to be offloaded, then removed from production once all blocking calls are fixed.

---

## Comparison

| Option | GIL Bypass | Overhead | Cooperative | Best For |
|---|---|---|---|---|
| 1. `asyncio.to_thread` | No (thread) | Low | Yes | General blocking CPU work, most common case |
| 2. `ProcessPoolExecutor` | Yes | Medium (spawn) | Yes | True CPU parallelism, GIL-bound tasks >50ms |
| 3. Bounded thread pool + semaphore | No | Low | Yes | High-concurrency with back-pressure control |
| 4. Worker queue pipeline | No | Low | Yes | LLM + post-processing pipelines, decoupled stages |
| 5. Chunked with yield points | No | None | Yes | Moderate CPU work, zero extra dependencies |
| 6. Loop monitor | N/A | Minimal | N/A | Diagnostics — detect blocking calls in staging |
