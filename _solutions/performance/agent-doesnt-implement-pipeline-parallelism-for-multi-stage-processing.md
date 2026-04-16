---
title: "Agent Doesn't Implement Pipeline Parallelism for Multi-Stage Processing"
description: "Agents that process batches of items through sequential stages — retrieve, enrich, validate, format — wait for every item to complete each stage before starting the next. Implement pipeline parallelism to overlap stage execution: while stage N processes item K, stage N+1 processes item K-1 — eliminating inter-stage wait time and increasing throughput proportionally to the number of pipeline stages."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-pipeline-parallelism-for-multi-stage-processing
tags: [pipeline-parallelism, throughput, async-pipeline, producer-consumer, staged-processing, performance]
symptoms:
  - "Processing 100 items through 4 stages takes 4× as long as it should — stages run sequentially"
  - "Stage 2 is idle while stage 1 processes the full batch, then stage 1 is idle while stage 2 works"
  - "High throughput batch processing is bottlenecked by the slowest single stage"
  - "No overlap between retrieval, embedding, validation, and formatting stages"
  - "Adding more stages to the pipeline linearly increases total processing time"
---

## Why This Happens

Sequential stage execution treats a multi-stage pipeline as a series of sequential loops: complete all of stage 1, then all of stage 2, etc. This wastes the parallelism available between stages — stage 2 can start processing item 1 the moment stage 1 finishes it, without waiting for stage 1 to finish items 2 through N. Pipeline parallelism connects stages with async queues: each stage reads from its input queue and writes to its output queue, running concurrently with all other stages. Total throughput approaches the throughput of the slowest single stage rather than the sum of all stage latencies.

## Solution 1: Pipeline Stage Definition

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Coroutine, Optional

@dataclass
class StageMetrics:
    stage_name: str
    items_processed: int = 0
    items_dropped: int = 0
    total_latency_ms: float = 0.0
    errors: int = 0

    def avg_latency_ms(self) -> float:
        return self.total_latency_ms / max(self.items_processed, 1)

    def throughput_per_sec(self, elapsed_seconds: float) -> float:
        return self.items_processed / max(elapsed_seconds, 0.001)

@dataclass
class PipelineStage:
    name: str
    process_fn: Callable[[Any], Coroutine]   # async item -> item (or None to drop)
    concurrency: int = 4        # max concurrent items in this stage
    queue_maxsize: int = 100    # backpressure: block upstream when queue is full
    timeout_seconds: float = 30.0
    metrics: StageMetrics = field(default_factory=lambda: StageMetrics(""))

    def __post_init__(self):
        self.metrics.stage_name = self.name
```

## Solution 2: Async Pipeline Runner

```python
import asyncio
import time
from typing import Any, AsyncIterator, List, Optional, Tuple

_SENTINEL = object()

class AsyncPipelineRunner:
    """
    Connects pipeline stages with asyncio queues and runs them concurrently.
    Items flow from source -> stage[0] -> stage[1] -> ... -> stage[N] -> sink.
    Each stage runs with configurable concurrency; queues provide backpressure.
    Dropped items (process_fn returns None) are counted but not forwarded.
    """

    def __init__(self, stages: List[PipelineStage]):
        self._stages = stages
        self._start_time: Optional[float] = None

    async def run(
        self,
        source: AsyncIterator,
        sink: Optional[asyncio.Queue] = None,
    ) -> List[Any]:
        """
        Runs all stages concurrently.
        Returns collected output items if sink is None.
        """
        self._start_time = time.monotonic()

        # Create inter-stage queues
        queues = [
            asyncio.Queue(maxsize=stage.queue_maxsize)
            for stage in self._stages
        ]
        output_queue: asyncio.Queue = sink or asyncio.Queue()

        # Start producer (source -> queue[0])
        producer = asyncio.ensure_future(
            self._produce(source, queues[0])
        )

        # Start stage workers
        stage_tasks = []
        for i, stage in enumerate(self._stages):
            in_q = queues[i]
            out_q = queues[i + 1] if i + 1 < len(queues) else output_queue
            for _ in range(stage.concurrency):
                task = asyncio.ensure_future(
                    self._stage_worker(stage, in_q, out_q)
                )
                stage_tasks.append(task)

        # Collect output
        results = []
        if sink is None:
            collector = asyncio.ensure_future(
                self._collect(output_queue, results)
            )
            await asyncio.gather(producer, *stage_tasks)
            await output_queue.put(_SENTINEL)
            await collector
        else:
            await asyncio.gather(producer, *stage_tasks)

        return results

    async def _produce(self, source: AsyncIterator, queue: asyncio.Queue) -> None:
        async for item in source:
            await queue.put(item)
        # Signal end of stream to all stage workers
        for _ in range(sum(s.concurrency for s in self._stages[:1])):
            await queue.put(_SENTINEL)

    async def _stage_worker(
        self,
        stage: PipelineStage,
        in_q: asyncio.Queue,
        out_q: asyncio.Queue,
    ) -> None:
        while True:
            item = await in_q.get()
            if item is _SENTINEL:
                # Propagate sentinel to next stage
                await out_q.put(_SENTINEL)
                in_q.task_done()
                return

            t0 = time.monotonic()
            try:
                result = await asyncio.wait_for(
                    stage.process_fn(item),
                    timeout=stage.timeout_seconds,
                )
                latency_ms = (time.monotonic() - t0) * 1000
                stage.metrics.total_latency_ms += latency_ms
                stage.metrics.items_processed += 1

                if result is None:
                    stage.metrics.items_dropped += 1
                else:
                    await out_q.put(result)
            except asyncio.TimeoutError:
                stage.metrics.errors += 1
                stage.metrics.items_dropped += 1
            except Exception:
                stage.metrics.errors += 1
                stage.metrics.items_dropped += 1
            finally:
                in_q.task_done()

    async def _collect(self, queue: asyncio.Queue, results: List) -> None:
        sentinels_seen = 0
        expected_sentinels = self._stages[-1].concurrency
        while sentinels_seen < expected_sentinels:
            item = await queue.get()
            if item is _SENTINEL:
                sentinels_seen += 1
            else:
                results.append(item)

    def stage_metrics(self) -> List[dict]:
        elapsed = time.monotonic() - (self._start_time or time.monotonic())
        return [
            {
                "stage": s.metrics.stage_name,
                "processed": s.metrics.items_processed,
                "dropped": s.metrics.items_dropped,
                "errors": s.metrics.errors,
                "avg_latency_ms": round(s.metrics.avg_latency_ms(), 2),
                "throughput_per_sec": round(s.metrics.throughput_per_sec(elapsed), 2),
            }
            for s in self._stages
        ]
```

## Solution 3: Pipeline Bottleneck Detector

```python
import time
from typing import Dict, List, Optional, Tuple

class PipelineBottleneckDetector:
    """
    Analyzes pipeline stage metrics to identify bottleneck stages.
    The bottleneck is the stage with the lowest throughput (highest avg latency).
    Recommends concurrency adjustments to balance throughput across stages.
    """

    def analyze(self, stage_metrics: List[dict]) -> dict:
        if not stage_metrics:
            return {}

        # Bottleneck = stage with highest average latency
        bottleneck = max(stage_metrics, key=lambda s: s["avg_latency_ms"])
        fastest = min(stage_metrics, key=lambda s: s["avg_latency_ms"])

        recommendations = []
        for stage in stage_metrics:
            ratio = stage["avg_latency_ms"] / max(fastest["avg_latency_ms"], 0.001)
            if ratio > 2.0:
                recommended_concurrency = min(32, int(ratio) + 1)
                recommendations.append({
                    "stage": stage["stage"],
                    "current_avg_latency_ms": stage["avg_latency_ms"],
                    "latency_ratio_vs_fastest": round(ratio, 2),
                    "recommendation": f"increase concurrency to ~{recommended_concurrency}",
                })

        pipeline_efficiency = min(s["throughput_per_sec"] for s in stage_metrics) / max(
            max(s["throughput_per_sec"] for s in stage_metrics), 0.001
        )

        return {
            "bottleneck_stage": bottleneck["stage"],
            "bottleneck_latency_ms": bottleneck["avg_latency_ms"],
            "pipeline_efficiency": round(pipeline_efficiency, 4),
            "recommendations": recommendations,
        }
```

## Solution 4: Adaptive Stage Concurrency

```python
import asyncio
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict

@dataclass
class ConcurrencySample:
    stage_name: str
    queue_depth: int
    throughput_per_sec: float
    avg_latency_ms: float
    timestamp: float

class AdaptiveStageConcurrency:
    """
    Dynamically adjusts per-stage concurrency based on queue depth.
    A growing queue indicates the stage is a bottleneck — increase concurrency.
    A shrinking queue with low utilization — decrease concurrency to reclaim resources.
    """

    def __init__(
        self,
        stages: List[PipelineStage],
        queues: List[asyncio.Queue],
        min_concurrency: int = 1,
        max_concurrency: int = 32,
        sample_interval_seconds: float = 5.0,
    ):
        self._stages = stages
        self._queues = queues
        self._min = min_concurrency
        self._max = max_concurrency
        self._interval = sample_interval_seconds
        self._samples: Dict[str, Deque[ConcurrencySample]] = {
            s.name: deque(maxlen=20) for s in stages
        }
        self._adjustments: list = []

    async def monitor_loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            self._adjust_concurrency()

    def _adjust_concurrency(self) -> None:
        for i, stage in enumerate(self._stages):
            if i >= len(self._queues):
                break
            queue_depth = self._queues[i].qsize()

            if queue_depth > stage.queue_maxsize * 0.8:
                # Queue backing up — stage is bottleneck
                new_concurrency = min(self._max, stage.concurrency + 2)
                if new_concurrency != stage.concurrency:
                    self._adjustments.append({
                        "stage": stage.name,
                        "from": stage.concurrency,
                        "to": new_concurrency,
                        "reason": "queue_backpressure",
                        "timestamp": time.time(),
                    })
                    stage.concurrency = new_concurrency

            elif queue_depth == 0 and stage.concurrency > self._min:
                # Queue empty — stage may be over-provisioned
                if stage.metrics.items_processed > 10:  # enough data
                    new_concurrency = max(self._min, stage.concurrency - 1)
                    stage.concurrency = new_concurrency

    def adjustment_log(self) -> list:
        return list(self._adjustments[-20:])
```

## Solution 5: Ordered Pipeline with Result Resequencing

```python
import asyncio
import heapq
import time
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

@dataclass(order=True)
class OrderedItem:
    sequence: int
    item: Any = field(compare=False)

class OrderedPipelineRunner:
    """
    Pipeline variant that preserves input ordering in the output.
    Uses sequence numbers and a min-heap to reassemble output in order.
    Required when downstream consumers depend on item order.
    """

    def __init__(self, stages: List[PipelineStage]):
        self._stages = stages

    async def run_ordered(
        self,
        items: List[Any],
    ) -> List[Any]:
        """Processes items through the pipeline and returns results in input order."""
        # Tag items with sequence numbers
        tagged_input = [OrderedItem(sequence=i, item=item) for i, item in enumerate(items)]

        async def source():
            for tagged in tagged_input:
                yield tagged

        # Wrap each stage to pass sequence numbers through
        wrapped_stages = []
        for stage in self._stages:
            original_fn = stage.process_fn
            async def make_wrapped(fn):
                async def wrapped(tagged: OrderedItem) -> Optional[OrderedItem]:
                    result = await fn(tagged.item)
                    if result is None:
                        return None
                    return OrderedItem(sequence=tagged.sequence, item=result)
                return wrapped
            wrapped_stage = PipelineStage(
                name=stage.name,
                process_fn=await make_wrapped(original_fn),
                concurrency=stage.concurrency,
                queue_maxsize=stage.queue_maxsize,
                timeout_seconds=stage.timeout_seconds,
            )
            wrapped_stages.append(wrapped_stage)

        runner = AsyncPipelineRunner(wrapped_stages)
        raw_results = await runner.run(source())

        # Reassemble in order using min-heap
        heap = []
        for tagged in raw_results:
            heapq.heappush(heap, tagged)
        return [heapq.heappop(heap).item for _ in heap]
```

## Solution 6: Pipeline Dashboard

```python
import time
from typing import List, Optional

class PipelineDashboard:
    """
    Real-time pipeline throughput dashboard.
    Tracks end-to-end latency, per-stage utilization, and drop rates.
    """

    def __init__(self, runner: AsyncPipelineRunner, detector: PipelineBottleneckDetector):
        self._runner = runner
        self._detector = detector
        self._run_start: Optional[float] = None
        self._run_end: Optional[float] = None
        self._input_count: int = 0
        self._output_count: int = 0

    def start(self, input_count: int) -> None:
        self._run_start = time.time()
        self._input_count = input_count

    def finish(self, output_count: int) -> None:
        self._run_end = time.time()
        self._output_count = output_count

    def render(self) -> dict:
        metrics = self._runner.stage_metrics()
        analysis = self._detector.analyze(metrics)
        elapsed = (self._run_end or time.time()) - (self._run_start or time.time())

        return {
            "elapsed_seconds": round(elapsed, 2),
            "input_count": self._input_count,
            "output_count": self._output_count,
            "end_to_end_throughput_per_sec": round(
                self._output_count / max(elapsed, 0.001), 2
            ),
            "drop_rate": round(
                1.0 - self._output_count / max(self._input_count, 1), 4
            ),
            "stages": metrics,
            "bottleneck_analysis": analysis,
        }
```

## Comparison

| Approach | Overlap Execution | Backpressure | Preserves Order | Dynamic Concurrency |
|---|---|---|---|---|
| AsyncPipelineRunner | Yes | Via queue maxsize | No | No |
| PipelineBottleneckDetector | N/A (analysis) | N/A | N/A | Recommendations only |
| AdaptiveStageConcurrency | Via runner | Yes | N/A | Yes |
| OrderedPipelineRunner | Yes | Via runner | Yes | No |
| PipelineDashboard | N/A | N/A | N/A | N/A |

**Best for production**: Model each processing step (retrieve, embed, validate, format) as a `PipelineStage` with `concurrency` set to roughly `avg_stage_latency / target_inter-item_latency`. Use `AsyncPipelineRunner` for unordered output (faster); `OrderedPipelineRunner` when downstream depends on sequence. Set `queue_maxsize` to 2–4× concurrency to allow burst absorption without unbounded memory growth. Run `PipelineBottleneckDetector.analyze()` after each batch to tune concurrency — a bottleneck stage with latency ratio > 3 needs more workers. Monitor end-to-end throughput via `PipelineDashboard` to verify that pipeline parallelism actually improves throughput versus sequential processing.
