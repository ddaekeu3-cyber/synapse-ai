---
title: "Agent Doesn't Implement Barrier Synchronization for Parallel Subagents"
description: "AI agents that fan out to multiple subagents in parallel often need to wait for all of them to reach a checkpoint before proceeding. Without barrier synchronization, agents race ahead with partial results, produce inconsistent outputs, or silently discard slow subagent work."
date: 2025-02-02
difficulty: intermediate
category: concurrency
slug: agent-doesnt-implement-barrier-synchronization-for-parallel-subagents
tags:
  - barrier
  - synchronization
  - parallel-agents
  - fan-out
  - gather
  - concurrency
  - coordination
symptoms:
  - "Orchestrator proceeds with partial results when one of three subagents is slow"
  - "Merging subagent outputs fails because the fastest subagent overwrites the slowest"
  - "Parallel tool calls start a second batch before the first batch has fully completed"
  - "Timeouts in one subagent cause the orchestrator to drop its result silently"
  - "No way to know which subagents are still running vs finished at any point in time"
---

## Problem

When an orchestrator agent fans out work to N subagents, it must synchronise at collection points: "wait for ALL subagents to finish step 1 before any of them starts step 2." Without explicit barriers, the orchestrator either polls with `asyncio.wait` (complex bookkeeping) or calls `asyncio.gather` which has no partial-result or checkpoint semantics.

Barriers add precision: an agent can proceed past the barrier only when all participants have arrived. This maps naturally to multi-step pipelines where each step depends on the global output of the previous one.

---

## Solution 1: Asyncio Barrier (Python 3.11+)

Use the standard library `asyncio.Barrier` introduced in Python 3.11. All coroutines call `await barrier.wait()` and block until N participants have called it.

```python
import asyncio
from typing import Any, Callable, Coroutine, List


async def parallel_with_barrier(
    tasks: List[Callable[[], Coroutine]],
    checkpoint_fn: Callable[[int], Coroutine] = None,
) -> List[Any]:
    """
    Run N tasks, wait at a barrier after each group, then run a shared
    checkpoint before continuing.

    Usage:
        results = await parallel_with_barrier(
            [
                lambda: subagent_a.run(data),
                lambda: subagent_b.run(data),
                lambda: subagent_c.run(data),
            ],
            checkpoint_fn=lambda passed: log(f"{passed} agents reached checkpoint"),
        )
    """
    n = len(tasks)
    barrier = asyncio.Barrier(n)
    results = [None] * n

    async def run_with_barrier(i: int, task_fn: Callable):
        results[i] = await task_fn()
        party_index = await barrier.wait()
        if checkpoint_fn:
            await checkpoint_fn(party_index)

    coros = [run_with_barrier(i, fn) for i, fn in enumerate(tasks)]
    await asyncio.gather(*coros)
    return results


# Example: two-phase pipeline using barriers
class TwoPhaseParallelPipeline:
    """
    Phase 1: all subagents collect data independently.
    Phase 2: all subagents refine results using the merged Phase 1 output.
    A barrier separates the phases.

    Usage:
        pipeline = TwoPhaseParallelPipeline()
        final = await pipeline.run(["topic A", "topic B", "topic C"])
    """

    async def _phase1(self, agent_id: int, topic: str, shared: dict,
                      barrier: asyncio.Barrier):
        # Simulate data collection
        await asyncio.sleep(0.1 * (agent_id + 1))
        shared[agent_id] = f"raw:{topic}"
        await barrier.wait()   # ← all agents must reach here before Phase 2

    async def _phase2(self, agent_id: int, shared: dict) -> str:
        # All Phase 1 results are now available
        merged = " | ".join(shared.values())
        return f"agent-{agent_id} refined: {merged}"

    async def run(self, topics: List[str]) -> List[str]:
        n = len(topics)
        barrier = asyncio.Barrier(n)
        shared: dict = {}

        phase1_tasks = [
            self._phase1(i, t, shared, barrier)
            for i, t in enumerate(topics)
        ]
        await asyncio.gather(*phase1_tasks)

        phase2_tasks = [self._phase2(i, shared) for i in range(n)]
        return await asyncio.gather(*phase2_tasks)
```

---

## Solution 2: Custom Reusable Barrier (Python < 3.11)

A reusable barrier implementation compatible with Python 3.9+. Supports multiple phases: after N participants arrive, the barrier auto-resets for the next phase.

```python
import asyncio
from dataclasses import dataclass, field
from typing import Any, Optional


class ReusableBarrier:
    """
    Multi-phase barrier that auto-resets after N participants pass.
    Each phase is identified by a monotonically increasing generation counter.

    Usage:
        barrier = ReusableBarrier(parties=3)
        # In each of 3 coroutines:
        party_id = await barrier.wait()   # blocks until all 3 arrive
        # All 3 continue together
        party_id = await barrier.wait()   # barrier resets; wait for next phase
    """

    def __init__(self, parties: int):
        if parties < 1:
            raise ValueError("parties must be >= 1")
        self._parties = parties
        self._count = 0
        self._generation = 0
        self._lock = asyncio.Lock()
        self._event = asyncio.Event()

    async def wait(self, timeout: Optional[float] = None) -> int:
        """
        Block until `parties` coroutines have called wait().
        Returns this coroutine's party index (0 to parties-1).
        """
        async with self._lock:
            generation = self._generation
            index = self._count
            self._count += 1
            if self._count == self._parties:
                # Last arrival: release all and reset
                self._count = 0
                self._generation += 1
                self._event.set()
                self._event = asyncio.Event()
                return index

        # Wait for the last party to arrive
        try:
            if timeout is not None:
                await asyncio.wait_for(
                    self._wait_for_generation(generation), timeout=timeout
                )
            else:
                await self._wait_for_generation(generation)
        except asyncio.TimeoutError:
            raise TimeoutError(f"Barrier timeout after {timeout}s")
        return index

    async def _wait_for_generation(self, generation: int):
        while self._generation == generation:
            await asyncio.sleep(0)
            if self._generation != generation:
                break
        # After generation advances the event has been set

    @property
    def parties(self) -> int:
        return self._parties

    @property
    def n_waiting(self) -> int:
        return self._count
```

---

## Solution 3: Phased Subagent Coordinator

Manages a pool of subagents through multiple sequential phases. Each phase fans out work, waits at a barrier, then aggregates before starting the next phase.

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class PhaseResult:
    phase: int
    agent_id: int
    data: Any
    duration_ms: float
    error: Optional[Exception] = None


class PhasedSubagentCoordinator:
    """
    Orchestrates N subagents through M sequential phases with barrier sync.

    Usage:
        async def phase_fn(agent_id: int, phase: int, shared: dict) -> Any:
            if phase == 0:
                return await collect_data(agent_id)
            else:
                return await refine(agent_id, shared)

        coordinator = PhasedSubagentCoordinator(n_agents=4, n_phases=2)
        all_results = await coordinator.run(phase_fn)
        # all_results[phase][agent_id] = PhaseResult
    """

    def __init__(self, n_agents: int, n_phases: int,
                 phase_timeout: Optional[float] = None):
        self._n = n_agents
        self._phases = n_phases
        self._timeout = phase_timeout
        self._results: List[List[Optional[PhaseResult]]] = [
            [None] * n_agents for _ in range(n_phases)
        ]

    async def run(
        self,
        phase_fn: Callable[[int, int, dict], Any],
    ) -> List[List[Optional[PhaseResult]]]:
        shared: Dict[str, Any] = {}

        for phase in range(self._phases):
            barrier = asyncio.Barrier(self._n)

            async def run_agent(agent_id: int, p: int = phase):
                t0 = time.monotonic()
                try:
                    data = await phase_fn(agent_id, p, shared)
                    result = PhaseResult(
                        phase=p, agent_id=agent_id, data=data,
                        duration_ms=(time.monotonic() - t0) * 1000,
                    )
                except Exception as exc:
                    result = PhaseResult(
                        phase=p, agent_id=agent_id, data=None,
                        duration_ms=(time.monotonic() - t0) * 1000,
                        error=exc,
                    )
                self._results[p][agent_id] = result
                # Wait for all agents to finish this phase
                if self._timeout:
                    try:
                        await asyncio.wait_for(barrier.wait(), self._timeout)
                    except asyncio.TimeoutError:
                        pass  # proceed with partial results
                else:
                    await barrier.wait()

            await asyncio.gather(*(run_agent(i) for i in range(self._n)))

            # Aggregate phase results into shared dict
            shared[f"phase_{phase}"] = [r for r in self._results[phase] if r]

        return self._results

    def success_rate(self, phase: int) -> float:
        results = self._results[phase]
        if not results:
            return 0.0
        ok = sum(1 for r in results if r and r.error is None)
        return ok / len(results)
```

---

## Solution 4: Fan-Out / Fan-In with Partial-Result Barrier

A softer barrier that proceeds after either all agents finish OR a timeout expires, collecting whatever partial results are available. Useful when some subagents are optional.

```python
import asyncio
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class PartialResult:
    agent_id: str
    data: Optional[Any]
    status: str   # "ok" | "timeout" | "error"
    duration_ms: float


class PartialResultBarrier:
    """
    Waits for N subagents, but proceeds after `min_results` arrive
    OR after `deadline` seconds, whichever comes first.

    Usage:
        barrier = PartialResultBarrier(
            agent_count=5, min_results=3, deadline=2.0
        )
        results = await barrier.gather(agent_coroutines)
        # Returns as soon as 3 of 5 finish OR after 2.0 s
    """

    def __init__(self, agent_count: int, min_results: int = None,
                 deadline: float = None):
        self._count = agent_count
        self._min = min_results or agent_count
        self._deadline = deadline

    async def gather(
        self,
        coros: List[Tuple[str, Any]],   # List of (agent_id, coroutine)
    ) -> List[PartialResult]:
        results: Dict[str, PartialResult] = {}
        done_event = asyncio.Event()
        lock = asyncio.Lock()

        async def run(agent_id: str, coro):
            t0 = time.monotonic()
            try:
                data = await coro
                status = "ok"
            except asyncio.CancelledError:
                data, status = None, "cancelled"
            except Exception as exc:
                data, status = None, "error"
            ms = (time.monotonic() - t0) * 1000
            async with lock:
                results[agent_id] = PartialResult(agent_id, data, status, ms)
                if len(results) >= self._min:
                    done_event.set()

        tasks = [asyncio.create_task(run(aid, coro)) for aid, coro in coros]

        # Wait for min_results or deadline
        try:
            if self._deadline:
                await asyncio.wait_for(done_event.wait(), self._deadline)
            else:
                await done_event.wait()
        except asyncio.TimeoutError:
            pass

        # Cancel remaining tasks; collect timeout results
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        # Fill in timeout results for agents that didn't finish
        for agent_id, _ in coros:
            if agent_id not in results:
                results[agent_id] = PartialResult(
                    agent_id, None, "timeout",
                    self._deadline * 1000 if self._deadline else 0.0,
                )

        return list(results.values())
```

---

## Solution 5: Countdown Latch for One-Shot Fan-Out

A countdown latch decrements on each subagent completion and triggers the orchestrator when it reaches zero. Unlike a barrier, the orchestrator itself is NOT one of the participants — it simply waits for all workers.

```python
import asyncio
from typing import Any, Callable, Coroutine, List, Optional


class CountdownLatch:
    """
    Orchestrator waits on the latch; workers count it down.
    Unlike Barrier, the orchestrator is not a participant.

    Usage:
        latch = CountdownLatch(3)

        async def worker(i):
            result = await do_work(i)
            latch.count_down()
            return result

        tasks = [asyncio.create_task(worker(i)) for i in range(3)]
        await latch.wait()   # orchestrator waits here
        results = [await t for t in tasks]
    """

    def __init__(self, count: int):
        if count < 0:
            raise ValueError("count must be >= 0")
        self._count = count
        self._event = asyncio.Event()
        if count == 0:
            self._event.set()

    def count_down(self):
        if self._count > 0:
            self._count -= 1
            if self._count == 0:
                self._event.set()

    async def wait(self, timeout: Optional[float] = None):
        if timeout:
            await asyncio.wait_for(self._event.wait(), timeout)
        else:
            await self._event.wait()

    @property
    def remaining(self) -> int:
        return self._count

    @property
    def is_done(self) -> bool:
        return self._count == 0


async def fan_out_with_latch(
    work_items: List[Any],
    worker_fn: Callable[[Any], Coroutine],
    timeout: Optional[float] = None,
) -> List[Any]:
    """
    Fan out work_items to worker_fn coroutines; collect results after
    all workers signal the countdown latch.
    """
    n = len(work_items)
    latch = CountdownLatch(n)
    results = [None] * n

    async def wrapped_worker(i: int, item: Any):
        try:
            results[i] = await worker_fn(item)
        finally:
            latch.count_down()

    tasks = [
        asyncio.create_task(wrapped_worker(i, item))
        for i, item in enumerate(work_items)
    ]
    await latch.wait(timeout)
    return results
```

---

## Solution 6: Barrier-Synchronized Multi-Agent Workflow Engine

Full workflow engine supporting named stages, per-stage barriers, error accumulation, and a final merge step. Agents register handlers per stage; the engine drives execution.

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class StageOutput:
    agent_id: str
    stage: str
    data: Any
    error: Optional[Exception] = None
    duration_ms: float = 0.0


class BarrierSyncedWorkflowEngine:
    """
    Runs N agents through sequential named stages.
    All agents must complete each stage before any proceeds to the next.

    Usage:
        engine = BarrierSyncedWorkflowEngine(agent_ids=["a", "b", "c"])
        engine.add_stage("collect", handler_fn)
        engine.add_stage("analyse", handler_fn)
        engine.add_stage("synthesise", handler_fn)
        final_outputs = await engine.run(initial_context)
    """

    def __init__(self, agent_ids: List[str],
                 stage_timeout: Optional[float] = 30.0):
        self._agents = agent_ids
        self._timeout = stage_timeout
        self._stages: List[tuple] = []   # [(stage_name, handler_fn)]
        self._stage_outputs: Dict[str, List[StageOutput]] = {}

    def add_stage(self, name: str,
                  handler: Callable[[str, str, dict], Any]):
        """
        handler(agent_id, stage_name, shared_context) -> Any
        """
        self._stages.append((name, handler))

    async def run(self, initial_context: dict = None) -> Dict[str, List[StageOutput]]:
        shared = dict(initial_context or {})

        for stage_name, handler in self._stages:
            barrier = asyncio.Barrier(len(self._agents))
            stage_results: List[Optional[StageOutput]] = [None] * len(self._agents)

            async def run_agent(idx: int, agent_id: str):
                t0 = time.monotonic()
                try:
                    data = await handler(agent_id, stage_name, shared)
                    out = StageOutput(agent_id, stage_name, data,
                                      duration_ms=(time.monotonic() - t0) * 1000)
                except Exception as exc:
                    out = StageOutput(agent_id, stage_name, None, error=exc,
                                      duration_ms=(time.monotonic() - t0) * 1000)
                stage_results[idx] = out
                if self._timeout:
                    try:
                        await asyncio.wait_for(barrier.wait(), self._timeout)
                    except asyncio.TimeoutError:
                        pass
                else:
                    await barrier.wait()

            await asyncio.gather(*(
                run_agent(i, aid) for i, aid in enumerate(self._agents)
            ))

            completed = [r for r in stage_results if r is not None]
            self._stage_outputs[stage_name] = completed
            # Share successful outputs for the next stage
            shared[stage_name] = {
                r.agent_id: r.data for r in completed if r.error is None
            }

        return self._stage_outputs

    def summary(self) -> dict:
        result = {}
        for stage, outputs in self._stage_outputs.items():
            result[stage] = {
                "total": len(outputs),
                "succeeded": sum(1 for o in outputs if o.error is None),
                "avg_ms": round(
                    sum(o.duration_ms for o in outputs) / max(1, len(outputs)), 1
                ),
            }
        return result
```

---

## Comparison

| Approach | Requires Python 3.11+ | Partial Results | Orchestrator as Participant |
|---|---|---|---|
| **asyncio.Barrier (stdlib)** | Yes | No | Yes |
| **ReusableBarrier** | No (3.9+) | No | Yes |
| **Phased Coordinator** | No | No (full wait) | No |
| **Partial-Result Barrier** | No | Yes (min_results) | No |
| **Countdown Latch** | No | No | No |
| **Barrier Workflow Engine** | No | Per-stage errors | No |

**Recommendation**: use `asyncio.Barrier` on Python 3.11+; use `ReusableBarrier` for compatibility. Reach for the `PartialResultBarrier` when some subagents are optional or when latency SLOs are tight — you need the fastest N-of-M results, not all-or-nothing.
