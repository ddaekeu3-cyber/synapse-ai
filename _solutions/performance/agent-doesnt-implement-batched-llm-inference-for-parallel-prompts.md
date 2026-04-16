---
title: "Agent Doesn't Implement Batched LLM Inference for Parallel Prompts"
description: "AI agents that run multiple independent LLM calls sequentially—document classification, entity extraction across chunks, sentiment scoring—incur N × round-trip latency instead of processing all prompts in parallel. Batched inference collects independent prompts, submits them concurrently using asyncio.gather(), and applies concurrency limits to stay within API rate limits, reducing total latency from O(N) to O(1) for independent calls."
date: 2025-02-23
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-batched-llm-inference-for-parallel-prompts
tags:
  - batching
  - parallel-inference
  - asyncio
  - rate-limiting
  - llm-performance
  - concurrency
  - throughput
symptoms:
  - "Processing 20 document chunks for classification takes 60 seconds when they could run in parallel"
  - "Agent scores sentiment of 50 user messages sequentially instead of concurrently"
  - "Batch entity extraction runs one chunk at a time despite having no inter-chunk dependencies"
  - "Rate limit errors because all parallel prompts are submitted simultaneously without throttling"
  - "Tool that fans out to 10 sub-agents serializes their LLM calls instead of running in parallel"
---

## Problem

When an agent needs to process N independent items—classify 20 documents, extract entities from 50 text chunks, score relevance of 30 search results—sequential LLM calls take N × latency_per_call. For a 1-second P50 call, that is 20-50 seconds of wall-clock time for work that could complete in 1-3 seconds with parallelism. The challenge is rate limiting: submitting all N calls simultaneously often triggers 429 errors. The solution is a bounded concurrency pattern: collect all N prompts, then execute them in batches of K concurrent calls, throttling to stay within the API's requests-per-minute limit while maximizing throughput.

---

## Solution 1: ConcurrentBatchInferencer — asyncio.gather with Semaphore

```python
import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class InferenceResult:
    index: int
    result: Any
    latency_ms: float
    error: Optional[str] = None


class ConcurrentBatchInferencer:
    """
    Runs N independent LLM calls concurrently with a bounded semaphore
    to control maximum parallelism. Preserves input order in results.
    Each call is retried once on transient error before recording failure.

    Usage:
        inferencer = ConcurrentBatchInferencer(
            infer_fn=anthropic_client.messages.create,
            max_concurrency=10,
            retry_on_429=True,
        )
        prompts = [{"messages": [{"role": "user", "content": doc}]} for doc in docs]
        results = await inferencer.run(prompts)
        for r in results:
            print(r.index, r.result, r.latency_ms)
    """

    def __init__(
        self,
        infer_fn: Callable,
        max_concurrency: int = 10,
        retry_on_429: bool = True,
        retry_delay: float = 2.0,
        timeout_per_call: float = 30.0,
    ):
        self._infer = infer_fn
        self._sem = asyncio.Semaphore(max_concurrency)
        self._retry_429 = retry_on_429
        self._retry_delay = retry_delay
        self._timeout = timeout_per_call

    async def _call_one(self, index: int, kwargs: Dict) -> InferenceResult:
        async with self._sem:
            t0 = time.monotonic()
            for attempt in range(2):
                try:
                    result = await asyncio.wait_for(
                        self._infer(**kwargs) if asyncio.iscoroutinefunction(self._infer)
                        else asyncio.get_event_loop().run_in_executor(
                            None, lambda: self._infer(**kwargs)
                        ),
                        timeout=self._timeout,
                    )
                    elapsed = round((time.monotonic() - t0) * 1000, 1)
                    return InferenceResult(index=index, result=result, latency_ms=elapsed)
                except Exception as exc:
                    err_str = str(exc)
                    is_429 = "429" in err_str or "rate_limit" in err_str.lower()
                    if attempt == 0 and self._retry_429 and is_429:
                        logger.warning("batch_429_retry index=%d delay=%.1f", index, self._retry_delay)
                        await asyncio.sleep(self._retry_delay)
                        continue
                    elapsed = round((time.monotonic() - t0) * 1000, 1)
                    logger.error("batch_infer_failed index=%d error=%s", index, exc)
                    return InferenceResult(index=index, result=None,
                                           latency_ms=elapsed, error=err_str)

    async def run(self, calls: List[Dict]) -> List[InferenceResult]:
        """Execute all calls concurrently, return results in original order."""
        t0 = time.monotonic()
        tasks = [self._call_one(i, kwargs) for i, kwargs in enumerate(calls)]
        results = await asyncio.gather(*tasks)
        results.sort(key=lambda r: r.index)
        total_ms = round((time.monotonic() - t0) * 1000, 1)
        errors = sum(1 for r in results if r.error)
        logger.info("batch_complete count=%d total_ms=%.0f errors=%d", len(calls), total_ms, errors)
        return results
```

---

## Solution 2: TokenBucketBatchScheduler — Rate-Limit-Aware Submission

```python
import asyncio
import logging
import time
from typing import Any, Callable, Dict, List

logger = logging.getLogger(__name__)


class TokenBucketBatchScheduler:
    """
    Schedules LLM calls using a token bucket algorithm to respect
    requests-per-minute (RPM) limits. Each call consumes one token;
    tokens refill at rate = rpm / 60 per second. Calls that arrive
    when the bucket is empty wait the minimum time needed before
    submitting.

    Usage:
        scheduler = TokenBucketBatchScheduler(
            infer_fn=client.complete,
            rpm=60,          # 60 requests per minute limit
            burst=10,        # allow bursting up to 10 at once
        )
        prompts = [{"prompt": p} for p in prompt_list]
        results = await scheduler.run_batch(prompts)
    """

    def __init__(
        self,
        infer_fn: Callable,
        rpm: float = 60.0,
        burst: int = 10,
        timeout_per_call: float = 30.0,
    ):
        self._infer = infer_fn
        self._rate = rpm / 60.0  # tokens per second
        self._burst = float(burst)
        self._tokens = float(burst)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()
        self._timeout = timeout_per_call

    async def _acquire(self):
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
            self._last_refill = now

            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return

            # Wait for one token
            wait = (1.0 - self._tokens) / self._rate
            self._tokens = 0.0
        await asyncio.sleep(wait)

    async def _call_one(self, kwargs: Dict) -> Any:
        await self._acquire()
        t0 = time.monotonic()
        try:
            result = await asyncio.wait_for(
                self._infer(**kwargs) if asyncio.iscoroutinefunction(self._infer)
                else asyncio.get_event_loop().run_in_executor(None, lambda: self._infer(**kwargs)),
                timeout=self._timeout,
            )
            logger.debug("scheduled_call_ok latency_ms=%.0f", (time.monotonic() - t0) * 1000)
            return result
        except Exception as exc:
            logger.error("scheduled_call_failed error=%s", exc)
            raise

    async def run_batch(self, calls: List[Dict]) -> List[Any]:
        return await asyncio.gather(*[self._call_one(c) for c in calls],
                                     return_exceptions=True)
```

---

## Solution 3: DocumentBatchClassifier — High-Level Batch Classification Tool

```python
import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ClassificationResult:
    text: str
    label: str
    confidence: float
    raw: Any = None
    error: Optional[str] = None


class DocumentBatchClassifier:
    """
    Classifies a list of text documents concurrently using a provided
    LLM client. Formats classification prompts, runs them in parallel
    with bounded concurrency, and parses structured label/confidence output.

    Usage:
        classifier = DocumentBatchClassifier(
            infer_fn=anthropic_client.messages.create,
            labels=["technical", "business", "legal", "other"],
            model="claude-haiku-4-5-20251001",   # use haiku for classification
            max_concurrency=15,
        )
        docs = ["We need to file a 10-K by March 15...", "Deploy nginx with TLS..."]
        results = await classifier.classify(docs)
    """

    SYSTEM_PROMPT = (
        "You are a document classifier. Respond with JSON only: "
        '{"label": "<label>", "confidence": <0.0-1.0>}. '
        "Label must be one of: {labels}."
    )

    def __init__(
        self,
        infer_fn: Callable,
        labels: List[str],
        model: str = "claude-haiku-4-5-20251001",
        max_concurrency: int = 15,
        max_tokens: int = 50,
    ):
        self._infer = infer_fn
        self._labels = labels
        self._model = model
        self._max_concurrency = max_concurrency
        self._max_tokens = max_tokens
        self._inferencer = ConcurrentBatchInferencer(
            infer_fn=infer_fn, max_concurrency=max_concurrency
        )

    def _build_call(self, text: str) -> Dict:
        return {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "system": self.SYSTEM_PROMPT.format(labels=", ".join(self._labels)),
            "messages": [{"role": "user", "content": text[:2000]}],
        }

    def _parse_result(self, text: str, raw: Any) -> ClassificationResult:
        import json, re
        try:
            m = re.search(r'\{"label":\s*"([^"]+)",\s*"confidence":\s*([0-9.]+)\}', str(raw))
            if m:
                label = m.group(1)
                conf = float(m.group(2))
                return ClassificationResult(text=text, label=label,
                                             confidence=conf, raw=raw)
        except Exception:
            pass
        return ClassificationResult(text=text, label="unknown",
                                     confidence=0.0, raw=raw, error="parse_failed")

    async def classify(self, texts: List[str]) -> List[ClassificationResult]:
        calls = [self._build_call(t) for t in texts]
        inference_results = await self._inferencer.run(calls)
        output = []
        for text, ir in zip(texts, inference_results):
            if ir.error:
                output.append(ClassificationResult(text=text, label="error",
                                                    confidence=0.0, error=ir.error))
            else:
                output.append(self._parse_result(text, ir.result))
        return output
```

---

## Solution 4: ChunkedParallelExtractor — Entity Extraction Across Document Chunks

```python
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ExtractionResult:
    chunk_index: int
    entities: List[Dict[str, str]] = field(default_factory=list)
    error: Optional[str] = None


class ChunkedParallelExtractor:
    """
    Splits a large document into overlapping chunks and runs entity
    extraction on all chunks in parallel. Deduplicates entities across
    chunks by value + type before returning the final merged result.

    Usage:
        extractor = ChunkedParallelExtractor(
            infer_fn=client.messages.create,
            chunk_size=1000,
            overlap=100,
            max_concurrency=8,
        )
        entities = await extractor.extract(long_document, entity_types=["PERSON", "ORG", "DATE"])
    """

    EXTRACT_PROMPT = (
        "Extract all {entity_types} from the following text. "
        'Return JSON: {{"entities": [{{"text": "<text>", "type": "<type>"}}]}}.\n\n'
        "Text:\n{chunk}"
    )

    def __init__(
        self,
        infer_fn: Callable,
        chunk_size: int = 1000,
        overlap: int = 100,
        max_concurrency: int = 8,
        model: str = "claude-haiku-4-5-20251001",
    ):
        self._infer = infer_fn
        self._chunk_size = chunk_size
        self._overlap = overlap
        self._inferencer = ConcurrentBatchInferencer(infer_fn, max_concurrency)
        self._model = model

    def _split(self, text: str) -> List[str]:
        chunks = []
        step = self._chunk_size - self._overlap
        for i in range(0, len(text), step):
            chunk = text[i: i + self._chunk_size]
            if chunk:
                chunks.append(chunk)
        return chunks

    def _build_call(self, chunk: str, entity_types: List[str]) -> Dict:
        prompt = self.EXTRACT_PROMPT.format(
            entity_types=", ".join(entity_types), chunk=chunk
        )
        return {
            "model": self._model,
            "max_tokens": 500,
            "messages": [{"role": "user", "content": prompt}],
        }

    def _parse_entities(self, raw: Any) -> List[Dict[str, str]]:
        import json, re
        try:
            text = str(raw)
            m = re.search(r'\{.*"entities".*\}', text, re.DOTALL)
            if m:
                data = json.loads(m.group())
                return data.get("entities", [])
        except Exception:
            pass
        return []

    def _deduplicate(self, entities: List[Dict]) -> List[Dict]:
        seen = set()
        deduped = []
        for e in entities:
            key = (e.get("text", "").lower(), e.get("type", ""))
            if key not in seen:
                seen.add(key)
                deduped.append(e)
        return deduped

    async def extract(self, text: str, entity_types: Optional[List[str]] = None) -> List[Dict]:
        entity_types = entity_types or ["PERSON", "ORG", "LOCATION", "DATE"]
        chunks = self._split(text)
        calls = [self._build_call(c, entity_types) for c in chunks]
        results = await self._inferencer.run(calls)

        all_entities = []
        for ir in results:
            if not ir.error:
                all_entities.extend(self._parse_entities(ir.result))

        deduped = self._deduplicate(all_entities)
        logger.info("extraction_complete chunks=%d entities=%d", len(chunks), len(deduped))
        return deduped
```

---

## Solution 5: ParallelScoringPipeline — Batch Relevance Scoring for RAG

```python
import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class ParallelScoringPipeline:
    """
    Scores N candidate documents for relevance to a query in parallel,
    using a lightweight LLM call per document. Returns documents sorted
    by score in descending order. Designed for re-ranking retrieved
    chunks in RAG pipelines where cross-encoder latency dominates.

    Usage:
        scorer = ParallelScoringPipeline(
            infer_fn=client.messages.create,
            max_concurrency=20,
            model="claude-haiku-4-5-20251001",
        )
        ranked = await scorer.score_and_rank(
            query="What is backpressure?",
            documents=retrieved_chunks,
            top_k=5,
        )
    """

    SCORE_PROMPT = (
        "Rate how relevant the following document is to the query on a scale of 0-10.\n"
        "Query: {query}\nDocument: {doc}\n"
        "Respond with only a number between 0 and 10."
    )

    def __init__(
        self,
        infer_fn: Callable,
        max_concurrency: int = 20,
        model: str = "claude-haiku-4-5-20251001",
        max_tokens: int = 5,
    ):
        self._inferencer = ConcurrentBatchInferencer(infer_fn, max_concurrency)
        self._model = model
        self._max_tokens = max_tokens

    def _build_call(self, query: str, doc: str) -> Dict:
        return {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": [{"role": "user", "content": self.SCORE_PROMPT.format(
                query=query, doc=doc[:500]
            )}],
        }

    def _parse_score(self, raw: Any) -> float:
        import re
        try:
            text = str(raw)
            m = re.search(r'\b([0-9]|10)\b', text)
            if m:
                return float(m.group(1)) / 10.0
        except Exception:
            pass
        return 0.0

    async def score_and_rank(
        self, query: str, documents: List[str], top_k: Optional[int] = None
    ) -> List[Tuple[float, str]]:
        calls = [self._build_call(query, doc) for doc in documents]
        results = await self._inferencer.run(calls)

        scored = []
        for doc, ir in zip(documents, results):
            score = 0.0 if ir.error else self._parse_score(ir.result)
            scored.append((score, doc))

        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[:top_k] if top_k else scored
```

---

## Solution 6: BatchInferenceMetrics — Throughput and Parallelism Tracking

```python
import logging
import time
from typing import Any, Callable, Dict, List

logger = logging.getLogger(__name__)


class BatchInferenceMetrics:
    """
    Wraps ConcurrentBatchInferencer and records throughput, parallelism
    efficiency, and error rates across batch runs. Use to tune
    max_concurrency: if P99 latency is not improving but errors are
    increasing, reduce concurrency; if latency stays flat with more
    concurrency, raise it.

    Usage:
        metrics = BatchInferenceMetrics(
            inferencer=ConcurrentBatchInferencer(client.complete, max_concurrency=10)
        )
        results = await metrics.run(calls)
        print(metrics.report())
    """

    def __init__(self, inferencer: ConcurrentBatchInferencer):
        self._inferencer = inferencer
        self._runs: List[Dict] = []

    async def run(self, calls: List[Dict]) -> List[InferenceResult]:
        t0 = time.monotonic()
        results = await self._inferencer.run(calls)
        wall_ms = round((time.monotonic() - t0) * 1000, 1)

        latencies = sorted(r.latency_ms for r in results)
        errors = [r for r in results if r.error]
        n = len(results)

        run_stats = {
            "n": n,
            "wall_ms": wall_ms,
            "serial_ms": sum(latencies),
            "speedup_x": round(sum(latencies) / max(wall_ms, 1), 2),
            "p50_ms": latencies[n // 2] if latencies else 0,
            "p99_ms": latencies[int(n * 0.99)] if latencies else 0,
            "error_count": len(errors),
            "error_rate_pct": round(len(errors) / max(n, 1) * 100, 1),
            "throughput_rps": round(n / max(wall_ms / 1000, 0.001), 1),
        }
        self._runs.append(run_stats)
        logger.info("batch_metrics %s", run_stats)
        return results

    def report(self) -> Dict[str, Any]:
        if not self._runs:
            return {"runs": 0}
        total_calls = sum(r["n"] for r in self._runs)
        avg_speedup = sum(r["speedup_x"] for r in self._runs) / len(self._runs)
        return {
            "total_runs": len(self._runs),
            "total_calls": total_calls,
            "mean_speedup_x": round(avg_speedup, 2),
            "last_run": self._runs[-1],
        }
```

---

## Comparison

| Approach | Bounded Concurrency | Rate-Limit Aware | Order Preserved | Retry 429 | High-Level API | Metrics |
|---|---|---|---|---|---|---|
| **ConcurrentBatchInferencer** | Yes | No | Yes | Yes | No | No |
| **TokenBucketBatchScheduler** | No | Yes | No | No | No | No |
| **DocumentBatchClassifier** | Via inferencer | No | Yes | No | Yes | No |
| **ChunkedParallelExtractor** | Via inferencer | No | No (merged) | No | Yes | No |
| **ParallelScoringPipeline** | Via inferencer | No | Yes | No | Yes | No |
| **BatchInferenceMetrics** | Via inferencer | No | Yes | No | No | Yes |

**Key insight**: the immediate fix is replacing sequential `for doc in docs: result = await llm.complete(doc)` with `ConcurrentBatchInferencer(llm.complete, max_concurrency=10).run(calls)`. For 20 documents with 1-second P50 latency, sequential takes 20s; concurrent takes 2-3s (10× speedup). Start with `max_concurrency=10` and tune up until you hit 429 errors, then add `TokenBucketBatchScheduler` to respect the API's RPM limit. Use `BatchInferenceMetrics` to measure actual speedup: if `speedup_x` plateaus below concurrency level, the bottleneck is the provider's rate limit, not your concurrency setting.
