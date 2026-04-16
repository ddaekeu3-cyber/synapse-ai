---
title: "Agent Doesn't Implement Message Queue-Based Tool Call Offloading"
description: "Agents that execute long-running tool calls synchronously within the request lifecycle block the response thread for minutes: a data export tool, a PDF generation job, or a large database query holds the HTTP connection open until completion and fails on timeout. Implement message queue-based tool call offloading that enqueues long-running tool calls, returns a job handle immediately, and allows the agent to poll or receive a callback when the result is ready."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-message-queue-based-tool-call-offloading
tags: [message-queue, async-offloading, long-running-tools, job-handle, callback, timeout-prevention]
symptoms:
  - "HTTP timeout on requests that trigger a 3-minute data export tool"
  - "Agent thread pool exhausted because 20 concurrent long-running jobs hold connections"
  - "No way to check status of a tool call that was started but not yet finished"
  - "Users see spinning indicators for 5+ minutes with no progress feedback"
  - "Retry logic re-starts already-running jobs because there is no job ID to check"
---

## Why This Happens

Tool calls that take seconds are fine to run synchronously. Tool calls that take minutes are not: they exhaust HTTP timeouts, hold thread pool slots, prevent retries from detecting in-progress jobs, and give users no feedback. The fix is to classify tool calls by expected duration, offload long-running ones to a job queue, return a job handle immediately, and provide a status-check mechanism. The agent can poll the handle, subscribe to a completion event, or inject the handle into the LLM context and let the user follow up.

## Solution 1: Tool Call Job

```python
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class ToolCallJob:
    job_id: str
    tool_name: str
    args: Dict[str, Any]
    status: JobStatus = JobStatus.PENDING
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    result: Any = None
    error: Optional[str] = None
    session_id: str = ""
    priority: int = 5            # 1 (highest) to 10 (lowest)

    @property
    def duration_ms(self) -> Optional[float]:
        if self.started_at and self.completed_at:
            return round((self.completed_at - self.started_at) * 1000, 2)
        return None

    @property
    def age_seconds(self) -> float:
        return time.time() - self.created_at
```

## Solution 2: In-Process Job Queue

```python
import asyncio
import time
from typing import Callable, Dict, List, Optional


class InProcessJobQueue:
    """
    Simple in-process async job queue for tool call offloading.
    Replace with Redis/SQS/RabbitMQ for multi-instance deployments.
    """

    def __init__(self, max_workers: int = 10):
        self._max_workers = max_workers
        self._queue: asyncio.Queue = asyncio.Queue()
        self._jobs: Dict[str, ToolCallJob] = {}
        self._workers: List[asyncio.Task] = []
        self._tool_registry: Dict[str, Callable] = {}
        self._running = False

    def register_tool(self, name: str, fn: Callable) -> None:
        self._tool_registry[name] = fn

    async def start(self) -> None:
        self._running = True
        self._workers = [
            asyncio.create_task(self._worker())
            for _ in range(self._max_workers)
        ]

    async def stop(self) -> None:
        self._running = False
        for w in self._workers:
            w.cancel()

    def enqueue(self, job: ToolCallJob) -> str:
        self._jobs[job.job_id] = job
        self._queue.put_nowait(job)
        return job.job_id

    def get_job(self, job_id: str) -> Optional[ToolCallJob]:
        return self._jobs.get(job_id)

    async def _worker(self) -> None:
        while self._running:
            try:
                job = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            job.status = JobStatus.RUNNING
            job.started_at = time.time()

            tool_fn = self._tool_registry.get(job.tool_name)
            if not tool_fn:
                job.status = JobStatus.FAILED
                job.error = f"Tool '{job.tool_name}' not registered"
                job.completed_at = time.time()
                continue

            try:
                job.result = await tool_fn(**job.args)
                job.status = JobStatus.COMPLETED
            except Exception as exc:
                job.status = JobStatus.FAILED
                job.error = str(exc)
            finally:
                job.completed_at = time.time()
```

## Solution 3: Job Handle

```python
import asyncio
import time
from typing import Any, Optional


class JobHandle:
    """
    Returned immediately after enqueuing a long-running tool call.
    Provides polling and awaiting interfaces for the job result.
    """

    def __init__(self, job_id: str, queue: InProcessJobQueue):
        self._job_id = job_id
        self._queue = queue

    @property
    def job_id(self) -> str:
        return self._job_id

    def status(self) -> JobStatus:
        job = self._queue.get_job(self._job_id)
        return job.status if job else JobStatus.FAILED

    def is_done(self) -> bool:
        return self.status() in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED)

    def result(self) -> Optional[Any]:
        job = self._queue.get_job(self._job_id)
        return job.result if job else None

    def error(self) -> Optional[str]:
        job = self._queue.get_job(self._job_id)
        return job.error if job else "job not found"

    async def wait(self, timeout_seconds: float = 300.0, poll_interval: float = 0.5) -> Any:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            if self.is_done():
                if self.status() == JobStatus.COMPLETED:
                    return self.result()
                raise RuntimeError(f"Job {self._job_id} failed: {self.error()}")
            await asyncio.sleep(poll_interval)
        raise asyncio.TimeoutError(f"Job {self._job_id} did not complete within {timeout_seconds}s")

    def to_context_note(self) -> str:
        """Injects job status into LLM context for asynchronous workflows."""
        return (
            f"[async_job: job_id={self._job_id}, status={self.status().value}. "
            f"Call check_job_status(job_id='{self._job_id}') to retrieve results when ready.]"
        )
```

## Solution 4: Offloading Tool Dispatcher

```python
import uuid
from typing import Any, Callable, Dict, Optional


OFFLOAD_THRESHOLD_SECONDS = 5.0   # tools expected to take longer than this are offloaded


class OffloadingToolDispatcher:
    """
    Routes tool calls to synchronous execution or the job queue
    based on a per-tool expected duration classification.
    """

    def __init__(
        self,
        job_queue: InProcessJobQueue,
        sync_tool_registry: Dict[str, Callable],
        offload_tools: set = None,       # tools always offloaded
        session_id: str = "",
    ):
        self._queue = job_queue
        self._sync_registry = sync_tool_registry
        self._offload_tools = offload_tools or set()
        self._session_id = session_id

    async def dispatch(
        self,
        tool_name: str,
        args: Dict[str, Any],
        force_async: bool = False,
    ):
        should_offload = force_async or tool_name in self._offload_tools

        if should_offload:
            job = ToolCallJob(
                job_id=str(uuid.uuid4())[:12],
                tool_name=tool_name,
                args=args,
                session_id=self._session_id,
            )
            self._queue.enqueue(job)
            return JobHandle(job.job_id, self._queue)

        tool_fn = self._sync_registry.get(tool_name)
        if not tool_fn:
            raise KeyError(f"Tool '{tool_name}' not registered")
        return await tool_fn(**args)
```

## Solution 5: Job Status Checker Tool

```python
from typing import Any


class JobStatusCheckerTool:
    """
    A meta-tool that the LLM can call to check the status of an offloaded job.
    Registered as a regular tool so the agent can query async job results.
    """

    def __init__(self, queue: InProcessJobQueue):
        self._queue = queue

    async def check_job_status(self, job_id: str) -> dict:
        job = self._queue.get_job(job_id)
        if not job:
            return {"job_id": job_id, "status": "not_found"}
        result = {
            "job_id": job_id,
            "status": job.status.value,
            "tool_name": job.tool_name,
            "age_seconds": round(job.age_seconds, 1),
            "duration_ms": job.duration_ms,
        }
        if job.status == JobStatus.COMPLETED:
            result["result_preview"] = str(job.result)[:200]
        if job.status == JobStatus.FAILED:
            result["error"] = job.error
        return result
```

## Solution 6: Job Queue Dashboard

```python
import time
from typing import Dict, List


class JobQueueDashboard:
    """
    Summarizes job queue state: pending, running, completed,
    failed counts and average completion times.
    """

    def __init__(self, queue: InProcessJobQueue):
        self._queue = queue

    def render(self) -> dict:
        jobs = list(self._queue._jobs.values())
        by_status: Dict[str, List[ToolCallJob]] = {}
        for job in jobs:
            by_status.setdefault(job.status.value, []).append(job)

        completed = by_status.get("completed", [])
        avg_duration = (
            sum(j.duration_ms for j in completed if j.duration_ms) / len(completed)
            if completed else 0
        )

        return {
            "generated_at": time.time(),
            "total_jobs": len(jobs),
            "pending": len(by_status.get("pending", [])),
            "running": len(by_status.get("running", [])),
            "completed": len(completed),
            "failed": len(by_status.get("failed", [])),
            "avg_completion_ms": round(avg_duration, 2),
            "worker_count": self._queue._max_workers,
        }
```

## Comparison

| Approach | Async Offload | Job Handle | Status Polling | LLM Context Note | Dashboard |
|---|---|---|---|---|---|
| InProcessJobQueue | Yes | No | Via get_job() | No | No |
| JobHandle | No | Yes | Yes (poll + await) | Yes | No |
| OffloadingToolDispatcher | Yes (per-tool) | Via handle | Via handle | No | No |
| JobStatusCheckerTool | No | No | Yes (as LLM tool) | No | No |
| JobQueueDashboard | No | No | No | No | Yes |

**Best for production**: Use a durable queue (Redis Streams, SQS, RabbitMQ) instead of `InProcessJobQueue` for multi-instance deployments — in-process queues lose all pending jobs on restart. Classify tools by expected duration at registration time and set `offload_tools` accordingly; a 200ms tool should never be offloaded (queue overhead exceeds benefit). Register `JobStatusCheckerTool.check_job_status` as a first-class tool so the LLM can autonomously query job status in multi-turn conversations. Set job TTL to 1 hour and prune completed jobs to prevent unbounded memory growth.
