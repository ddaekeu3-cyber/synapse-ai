---
layout: solution
title: "Agent Doesn't Implement Adaptive Worker Count Based on Queue Depth"
category: concurrency
description: "Automatically scale the number of concurrent worker coroutines up or down based on current queue depth and throughput metrics, preventing both resource waste during quiet periods and bottlenecks during bursts."
tags: [concurrency, workers, auto-scaling, queue, throughput, asyncio, adaptive]
---

# Agent Doesn't Implement Adaptive Worker Count Based on Queue Depth

## Problem

A fixed worker pool is either too small (tasks queue up, latency spikes during bursts) or too large (idle workers consume memory and connection pool slots during quiet periods). Without adaptive scaling, the agent wastes resources when idle and falls behind when load spikes. Adaptive worker count ties capacity directly to demand.

## Solutions

### Option 1: Simple Queue-Depth Threshold Scaler

Spawn additional workers when queue depth exceeds a threshold; remove idle workers when queue empties.

```python
import anthropic
import asyncio
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

MIN_WORKERS = 1
MAX_WORKERS = 8
SCALE_UP_THRESHOLD   = 3   # spawn worker if queue > this
SCALE_DOWN_THRESHOLD = 0   # remove worker if queue <= this
IDLE_TIMEOUT         = 5.0 # worker self-terminates after idle for this long


@dataclass
class AdaptivePool:
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    workers: set = field(default_factory=set)
    results: list = field(default_factory=list)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _running: bool = True

    def worker_count(self) -> int:
        return len(self.workers)

    async def worker(self, worker_id: int) -> None:
        idle_since = None
        while self._running:
            try:
                task = self.queue.get_nowait()
                idle_since = None
            except asyncio.QueueEmpty:
                if idle_since is None:
                    idle_since = asyncio.get_event_loop().time()
                elif asyncio.get_event_loop().time() - idle_since > IDLE_TIMEOUT:
                    if self.worker_count() > MIN_WORKERS:
                        print(f"  [worker-{worker_id}] idle timeout — exiting")
                        break
                await asyncio.sleep(0.1)
                continue

            prompt, task_id = task
            try:
                resp = await client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=64,
                    messages=[{"role": "user", "content": prompt}],
                )
                self.results.append({"task_id": task_id, "result": resp.content[0].text[:60]})
            except Exception as e:
                self.results.append({"task_id": task_id, "error": str(e)})
            finally:
                self.queue.task_done()

        async with self._lock:
            self.workers.discard(asyncio.current_task())

    async def scale_check(self) -> None:
        """Periodically check queue depth and adjust worker count."""
        while self._running:
            depth = self.queue.qsize()
            count = self.worker_count()

            if depth > SCALE_UP_THRESHOLD and count < MAX_WORKERS:
                needed = min(MAX_WORKERS - count, depth // SCALE_UP_THRESHOLD)
                for i in range(needed):
                    w_id = count + i
                    task = asyncio.create_task(self.worker(w_id))
                    async with self._lock:
                        self.workers.add(task)
                    print(f"  [scaler] queue={depth} → spawned worker-{w_id} (total={count + i + 1})")

            await asyncio.sleep(0.5)

    async def submit(self, prompt: str, task_id: int) -> None:
        await self.queue.put((prompt, task_id))

    async def run(self, prompts: list[str]) -> list[dict]:
        # start with MIN_WORKERS
        for i in range(MIN_WORKERS):
            t = asyncio.create_task(self.worker(i))
            self.workers.add(t)

        scaler = asyncio.create_task(self.scale_check())

        for i, p in enumerate(prompts):
            await self.submit(p, i)
            print(f"  [queue] submitted task {i:02d} | depth={self.queue.qsize()} workers={self.worker_count()}")

        await self.queue.join()
        self._running = False
        scaler.cancel()
        return self.results


async def main() -> None:
    pool = AdaptivePool()
    prompts = [f"What is {i} squared? Answer in one number." for i in range(16)]
    results = await pool.run(prompts)
    print(f"\nCompleted {len(results)} tasks")


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Prevents idle workers from consuming API connection slots
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 2: PID Controller–Based Worker Scaler

Use a proportional-integral-derivative (PID) controller to smoothly adjust worker count, avoiding oscillation.

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

MIN_WORKERS = 1
MAX_WORKERS = 10
TARGET_QUEUE_DEPTH = 2.0   # desired queue depth per worker

# PID gains
KP, KI, KD = 0.5, 0.1, 0.05


@dataclass
class PIDController:
    target: float
    kp: float
    ki: float
    kd: float
    _integral: float = 0.0
    _prev_error: float = 0.0
    _last_time: float = field(default_factory=time.monotonic)

    def update(self, measured: float) -> float:
        now = time.monotonic()
        dt = max(now - self._last_time, 0.001)
        self._last_time = now

        error = self.target - measured
        self._integral += error * dt
        derivative = (error - self._prev_error) / dt
        self._prev_error = error

        return self.kp * error + self.ki * self._integral + self.kd * derivative


@dataclass
class PIDWorkerPool:
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    worker_tasks: list = field(default_factory=list)
    results: list = field(default_factory=list)
    _running: bool = True
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def worker(self, w_id: int) -> None:
        while self._running:
            try:
                prompt, task_id = await asyncio.wait_for(self.queue.get(), timeout=2.0)
                resp = await client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=64,
                    messages=[{"role": "user", "content": prompt}],
                )
                self.results.append({"task_id": task_id, "result": resp.content[0].text[:60]})
                self.queue.task_done()
            except asyncio.TimeoutError:
                break
            except Exception as e:
                self.results.append({"task_id": task_id, "error": str(e)})
                self.queue.task_done()

    def active_workers(self) -> int:
        return sum(1 for t in self.worker_tasks if not t.done())

    async def pid_scaler(self) -> None:
        pid = PIDController(target=TARGET_QUEUE_DEPTH, kp=KP, ki=KI, kd=KD)
        while self._running:
            depth = self.queue.qsize()
            count = self.active_workers()
            if count == 0:
                await asyncio.sleep(0.5)
                continue

            ratio = depth / count   # queue depth per worker
            adjustment = pid.update(ratio)
            new_count = int(count + adjustment)
            new_count = max(MIN_WORKERS, min(MAX_WORKERS, new_count))

            delta = new_count - count
            if delta > 0:
                for i in range(delta):
                    t = asyncio.create_task(self.worker(count + i))
                    self.worker_tasks.append(t)
                print(f"  [PID] ratio={ratio:.1f} adj={adjustment:+.1f} → +{delta} workers (total={new_count})")
            elif delta < 0:
                print(f"  [PID] ratio={ratio:.1f} adj={adjustment:+.1f} → {delta} workers (total={new_count})")
                # workers self-exit on queue empty via timeout

            await asyncio.sleep(1.0)

    async def run(self, prompts: list[str]) -> list[dict]:
        # seed with MIN_WORKERS
        for i in range(MIN_WORKERS):
            self.worker_tasks.append(asyncio.create_task(self.worker(i)))

        scaler = asyncio.create_task(self.pid_scaler())

        for i, p in enumerate(prompts):
            await self.queue.put((p, i))

        await self.queue.join()
        self._running = False
        scaler.cancel()
        return self.results


async def main() -> None:
    pool = PIDWorkerPool()
    prompts = [f"Define the word '{word}' in one sentence." for word in
               ["serendipity", "ephemeral", "resilient", "quantum", "heuristic",
                "entropy", "latency", "throughput", "concurrency", "idempotent",
                "stochastic", "deterministic"]]
    results = await pool.run(prompts)
    print(f"\nCompleted {len(results)} tasks")


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Smooth scaling prevents over-provisioning while maintaining throughput
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 3: Throughput-Based Scaler with Moving Average

Measure actual throughput (tasks/sec) and compare to target; scale workers to close the gap.

```python
import anthropic
import asyncio
import time
from collections import deque
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

MIN_WORKERS    = 1
MAX_WORKERS    = 12
TARGET_TPS     = 3.0   # target tasks per second
MEASURE_WINDOW = 10.0  # seconds for moving average


@dataclass
class ThroughputScaler:
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    _completion_times: deque = field(default_factory=lambda: deque())
    _worker_tasks: list = field(default_factory=list)
    results: list = field(default_factory=list)
    _running: bool = True
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def record_completion(self) -> None:
        self._completion_times.append(time.monotonic())

    def current_tps(self) -> float:
        now = time.monotonic()
        cutoff = now - MEASURE_WINDOW
        while self._completion_times and self._completion_times[0] < cutoff:
            self._completion_times.popleft()
        return len(self._completion_times) / MEASURE_WINDOW

    def active_workers(self) -> int:
        return sum(1 for t in self._worker_tasks if not t.done())

    async def worker(self) -> None:
        while self._running:
            try:
                prompt, task_id = await asyncio.wait_for(self.queue.get(), timeout=3.0)
                resp = await client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=64,
                    messages=[{"role": "user", "content": prompt}],
                )
                self.record_completion()
                self.results.append({"task_id": task_id, "reply": resp.content[0].text[:50]})
                self.queue.task_done()
            except asyncio.TimeoutError:
                if self.active_workers() > MIN_WORKERS:
                    break
            except Exception as e:
                self.results.append({"task_id": task_id, "error": str(e)})
                self.queue.task_done()

    async def scaler_loop(self) -> None:
        await asyncio.sleep(MEASURE_WINDOW * 0.5)  # warm-up period
        while self._running:
            tps = self.current_tps()
            active = self.active_workers()
            depth = self.queue.qsize()

            if tps < TARGET_TPS * 0.8 and depth > 0 and active < MAX_WORKERS:
                # under target — add worker
                t = asyncio.create_task(self.worker())
                self._worker_tasks.append(t)
                print(f"  [scaler] tps={tps:.1f}/{TARGET_TPS} depth={depth} → added worker (total={active+1})")
            elif tps > TARGET_TPS * 1.2 and active > MIN_WORKERS:
                print(f"  [scaler] tps={tps:.1f}/{TARGET_TPS} → at capacity (total={active})")

            await asyncio.sleep(2.0)

    async def run(self, prompts: list[str]) -> list[dict]:
        for _ in range(MIN_WORKERS):
            self._worker_tasks.append(asyncio.create_task(self.worker()))

        scaler = asyncio.create_task(self.scaler_loop())

        for i, p in enumerate(prompts):
            await self.queue.put((p, i))

        await self.queue.join()
        self._running = False
        scaler.cancel()
        return self.results


async def main() -> None:
    pool = ThroughputScaler()
    prompts = [f"What is the capital of country number {i % 50 + 1} alphabetically?" for i in range(20)]
    results = await pool.run(prompts)
    print(f"\nCompleted {len(results)} tasks, final TPS={pool.current_tps():.1f}")


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Workers scale to hit throughput target without idle overcapacity
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 4: Priority-Tier Worker Pool with Adaptive Sizing Per Tier

Maintain separate worker pools for high/normal/low priority, and adaptively size each pool based on its own queue depth.

```python
import anthropic
import asyncio
from dataclasses import dataclass, field
from enum import Enum

client = anthropic.AsyncAnthropic()


class Priority(Enum):
    HIGH   = 0
    NORMAL = 1
    LOW    = 2


TIER_LIMITS = {
    Priority.HIGH:   {"min": 2, "max": 6},
    Priority.NORMAL: {"min": 1, "max": 4},
    Priority.LOW:    {"min": 1, "max": 2},
}

SCALE_UP_RATIO = 2.0   # queue depth per worker to trigger scale-up


@dataclass
class TierPool:
    priority: Priority
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    results: list = field(default_factory=list)
    _tasks: list = field(default_factory=list)
    _running: bool = True

    def active(self) -> int:
        return sum(1 for t in self._tasks if not t.done())

    async def worker(self) -> None:
        limits = TIER_LIMITS[self.priority]
        while self._running:
            try:
                prompt, task_id = await asyncio.wait_for(self.queue.get(), timeout=3.0)
                resp = await client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=64,
                    messages=[{"role": "user", "content": prompt}],
                )
                self.results.append({
                    "task_id":  task_id,
                    "priority": self.priority.name,
                    "reply":    resp.content[0].text[:60],
                })
                self.queue.task_done()
            except asyncio.TimeoutError:
                if self.active() > limits["min"]:
                    break

    async def scale_check(self) -> None:
        limits = TIER_LIMITS[self.priority]
        while self._running:
            depth  = self.queue.qsize()
            active = self.active()
            ratio  = depth / max(active, 1)

            if ratio >= SCALE_UP_RATIO and active < limits["max"]:
                t = asyncio.create_task(self.worker())
                self._tasks.append(t)
                print(f"  [{self.priority.name:6s}] depth={depth} ratio={ratio:.1f} → +1 worker (total={active+1})")

            await asyncio.sleep(0.5)

    def start(self) -> None:
        limits = TIER_LIMITS[self.priority]
        for _ in range(limits["min"]):
            self._tasks.append(asyncio.create_task(self.worker()))
        asyncio.create_task(self.scale_check())

    async def submit(self, prompt: str, task_id: int) -> None:
        await self.queue.put((prompt, task_id))

    async def drain(self) -> None:
        await self.queue.join()
        self._running = False


async def main() -> None:
    pools = {p: TierPool(priority=p) for p in Priority}
    for pool in pools.values():
        pool.start()

    # submit mixed priority tasks
    tasks = (
        [(f"URGENT: define '{w}' in 5 words.", Priority.HIGH) for w in
         ["latency", "throughput", "jitter", "bandwidth", "packet"]]
        + [(f"Define '{w}' briefly.", Priority.NORMAL) for w in
           ["coroutine", "semaphore", "mutex", "deadlock", "livelock"]]
        + [(f"Give a synonym for '{w}'.", Priority.LOW) for w in
           ["fast", "slow", "big", "small", "smart", "dumb"]]
    )

    for i, (prompt, priority) in enumerate(tasks):
        await pools[priority].submit(prompt, i)

    await asyncio.gather(*[p.drain() for p in pools.values()])
    total = sum(len(p.results) for p in pools.values())
    print(f"\nTotal completed: {total}")
    for pri, pool in pools.items():
        print(f"  {pri.name}: {len(pool.results)} tasks")


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: High-priority work gets immediate capacity; low-priority never starves workers
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 5: Latency-Aware Scaler with P95 Target

Scale workers based on P95 latency rather than queue depth — keep adding workers until P95 meets the SLO target.

```python
import anthropic
import asyncio
import time
import statistics
from collections import deque
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

MIN_WORKERS = 1
MAX_WORKERS = 10
P95_TARGET_MS = 3000.0   # target P95 latency in milliseconds
HISTORY_SIZE  = 50       # rolling window of latency samples


@dataclass
class LatencyAwarePool:
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    _latencies: deque = field(default_factory=lambda: deque(maxlen=HISTORY_SIZE))
    _worker_tasks: list = field(default_factory=list)
    results: list = field(default_factory=list)
    _running: bool = True
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def p95_latency(self) -> float | None:
        if len(self._latencies) < 5:
            return None
        s = sorted(self._latencies)
        return s[int(len(s) * 0.95)]

    def active_workers(self) -> int:
        return sum(1 for t in self._worker_tasks if not t.done())

    async def worker(self) -> None:
        while self._running:
            try:
                prompt, task_id = await asyncio.wait_for(self.queue.get(), timeout=3.0)
                start = time.monotonic()
                resp = await client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=64,
                    messages=[{"role": "user", "content": prompt}],
                )
                latency_ms = (time.monotonic() - start) * 1000
                async with self._lock:
                    self._latencies.append(latency_ms)
                self.results.append({"task_id": task_id, "latency_ms": round(latency_ms), "reply": resp.content[0].text[:50]})
                self.queue.task_done()
            except asyncio.TimeoutError:
                if self.active_workers() > MIN_WORKERS:
                    break
            except Exception as e:
                self.results.append({"task_id": task_id, "error": str(e)})
                self.queue.task_done()

    async def latency_scaler(self) -> None:
        await asyncio.sleep(5.0)   # warm-up: collect some samples first
        while self._running:
            p95 = self.p95_latency()
            active = self.active_workers()
            depth = self.queue.qsize()

            if p95 is not None:
                if p95 > P95_TARGET_MS and active < MAX_WORKERS and depth > 0:
                    t = asyncio.create_task(self.worker())
                    self._worker_tasks.append(t)
                    print(f"  [latency-scaler] P95={p95:.0f}ms > {P95_TARGET_MS:.0f}ms → +1 worker (total={active+1})")
                elif p95 < P95_TARGET_MS * 0.5:
                    print(f"  [latency-scaler] P95={p95:.0f}ms well under target — holding at {active} workers")

            await asyncio.sleep(3.0)

    async def run(self, prompts: list[str]) -> list[dict]:
        for _ in range(MIN_WORKERS):
            self._worker_tasks.append(asyncio.create_task(self.worker()))

        asyncio.create_task(self.latency_scaler())

        for i, p in enumerate(prompts):
            await self.queue.put((p, i))

        await self.queue.join()
        self._running = False
        p95 = self.p95_latency()
        print(f"\nFinal P95: {p95:.0f}ms | Workers peaked at: {max(1, len(self._worker_tasks))}")
        return self.results


async def main() -> None:
    pool = LatencyAwarePool()
    prompts = [f"What is {i} × {i+1}?" for i in range(20)]
    results = await pool.run(prompts)
    lats = [r.get("latency_ms", 0) for r in results if "latency_ms" in r]
    if lats:
        print(f"Avg latency: {statistics.mean(lats):.0f}ms | Completed: {len(results)}")


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Scales to meet latency SLO without over-provisioning
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 6: Work-Rate Feedback Loop with Hysteresis

Use hysteresis bands (scale up aggressively, scale down slowly) to prevent rapid oscillation in worker count.

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

MIN_WORKERS   = 1
MAX_WORKERS   = 10
SCALE_UP_AT   = 5    # queue depth to add a worker
SCALE_DOWN_AT = 1    # queue depth to consider removing a worker
COOLDOWN_UP   = 3.0  # seconds before scaling up again
COOLDOWN_DOWN = 10.0 # seconds before scaling down again


@dataclass
class HysteresisPool:
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    _tasks: list = field(default_factory=list)
    results: list = field(default_factory=list)
    _running: bool = True
    _last_scale_up:   float = 0.0
    _last_scale_down: float = 0.0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def active(self) -> int:
        return sum(1 for t in self._tasks if not t.done())

    async def worker(self, w_id: int) -> None:
        while self._running:
            try:
                prompt, task_id = await asyncio.wait_for(self.queue.get(), timeout=2.0)
                resp = await client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=64,
                    messages=[{"role": "user", "content": prompt}],
                )
                self.results.append({"task_id": task_id, "worker": w_id, "reply": resp.content[0].text[:50]})
                self.queue.task_done()
            except asyncio.TimeoutError:
                # only exit if we're above min AND cooldown has passed
                now = time.monotonic()
                if self.active() > MIN_WORKERS and now - self._last_scale_down >= COOLDOWN_DOWN:
                    async with self._lock:
                        self._last_scale_down = now
                    print(f"  [worker-{w_id}] scaling down (cooldown elapsed)")
                    break

    async def hysteresis_scaler(self) -> None:
        worker_counter = MIN_WORKERS
        while self._running:
            now   = time.monotonic()
            depth = self.queue.qsize()
            active = self.active()

            can_up   = (now - self._last_scale_up)   >= COOLDOWN_UP
            can_down = (now - self._last_scale_down) >= COOLDOWN_DOWN

            if depth >= SCALE_UP_AT and active < MAX_WORKERS and can_up:
                t = asyncio.create_task(self.worker(worker_counter))
                self._tasks.append(t)
                worker_counter += 1
                self._last_scale_up = now
                print(f"  [hysteresis] depth={depth} ≥ {SCALE_UP_AT} → scale UP to {active+1} workers")

            elif depth <= SCALE_DOWN_AT and active > MIN_WORKERS and can_down:
                print(f"  [hysteresis] depth={depth} ≤ {SCALE_DOWN_AT} → scale DOWN (signal workers to exit)")
                self._last_scale_down = now

            await asyncio.sleep(0.5)

    async def run(self, prompts: list[str]) -> list[dict]:
        for i in range(MIN_WORKERS):
            self._tasks.append(asyncio.create_task(self.worker(i)))

        asyncio.create_task(self.hysteresis_scaler())

        # submit prompts in bursts to demonstrate scaling
        for i in range(0, len(prompts), 5):
            batch = prompts[i:i+5]
            for j, p in enumerate(batch):
                await self.queue.put((p, i + j))
            print(f"  [submit] queued batch {i//5} (depth={self.queue.qsize()})")
            await asyncio.sleep(1.0)

        await self.queue.join()
        self._running = False
        return self.results


async def main() -> None:
    pool = HysteresisPool()
    prompts = [f"Translate 'hello' to language #{i % 20 + 1}." for i in range(20)]
    results = await pool.run(prompts)
    print(f"\nCompleted {len(results)} tasks")


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Hysteresis prevents thrashing; stable worker count reduces connection overhead
# Environment: ANTHROPIC_API_KEY must be set
```

---

## Comparison

| Option | Scaling Signal | Oscillation Risk | Priority Support | Complexity | Best For |
|--------|---------------|-----------------|-----------------|-----------|----------|
| 1 | Queue depth threshold | Medium | No | Low | Simple burst handling |
| 2 | PID controller | Low | No | Medium | Smooth, stable scaling |
| 3 | Throughput (TPS) | Medium | No | Medium | Throughput SLO targets |
| 4 | Per-tier queue depth | Low | Yes | Medium | Multi-priority workloads |
| 5 | P95 latency | Low | No | Medium | Latency SLO targets |
| 6 | Queue depth + hysteresis | Very Low | No | Low | Stable environments with bursty load |
