---
layout: solution
title: "Agent Doesn't Implement Streaming Progress Indicators for Long Tasks"
category: streaming
description: "Agent runs long multi-step tasks silently with no progress feedback — users see nothing for 30+ seconds, assume it's broken, and cancel — because there's no mechanism to stream intermediate status updates."
tags: [streaming, sse, progress, user-experience, long-running-tasks, asyncio]
---

# Agent Doesn't Implement Streaming Progress Indicators for Long Tasks

## Problem

When an agent runs a multi-step task (research, analysis, batch processing), the user sees silence until the final response arrives. For tasks taking more than a few seconds, this creates:

- **Perceived failure**: users think the agent is stuck or crashed
- **Premature cancellation**: users abort tasks that were actually progressing
- **Trust erosion**: no visibility into what the agent is doing
- **No partial recovery**: if the task fails at step 8 of 10, there's nothing to show for it

**Root cause:** The agent loop accumulates results internally and flushes them only when `stop_reason == "end_turn"`. No streaming hooks send intermediate status to the client.

---

## Option 1: Text-Based Progress Prefix Streaming

Stream a progress prefix token before the final answer so users see activity immediately.

```python
import anthropic
import time

client = anthropic.Anthropic()

STEPS = [
    "Analyzing the request",
    "Retrieving relevant context",
    "Generating structured response",
    "Reviewing for accuracy",
]

def run_with_progress_prefix(query: str):
    """Stream progress tokens before the final answer."""
    print("Agent: ", end="", flush=True)

    # Show progress steps before API call
    for i, step in enumerate(STEPS, 1):
        print(f"[{i}/{len(STEPS)}] {step}... ", end="", flush=True)
        time.sleep(0.3)  # Simulate work

    print()  # Newline after progress line

    # Now stream the actual response
    with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": query}]
    ) as stream:
        for text in stream.text_stream:
            print(text, end="", flush=True)

    print()  # Final newline

run_with_progress_prefix("Explain the trade-offs between SQL and NoSQL databases for a startup.")

# Expected Token Savings: ~0% (progress is local; no extra API tokens consumed)
# Environment: CLI tools, terminal-based agents, developer-facing tools
```

---

## Option 2: SSE Progress Stream for Web Clients

Emit Server-Sent Events with step-level progress so a browser client can render a live progress bar.

```python
import anthropic
import json
import time
from typing import Iterator

client = anthropic.Anthropic()

def sse_event(event_type: str, data: dict) -> str:
    """Format a Server-Sent Event message."""
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"

def run_agent_with_sse_progress(query: str, steps: list[str]) -> Iterator[str]:
    """Generator yielding SSE events. Pipe to an HTTP response in production."""

    yield sse_event("task_start", {"total_steps": len(steps), "query": query[:80]})

    # Simulate multi-step execution
    results = []
    for i, step in enumerate(steps, 1):
        yield sse_event("step_start", {"step": i, "name": step, "total": len(steps)})

        # Simulate step work (in production: real API calls, tool execution, etc.)
        time.sleep(0.2)

        step_result = f"Result of '{step}'"
        results.append(step_result)

        yield sse_event("step_complete", {
            "step": i,
            "name": step,
            "result_preview": step_result[:60],
            "progress_pct": round(i / len(steps) * 100)
        })

    yield sse_event("llm_start", {"message": "Generating final response..."})

    # Stream the actual LLM response token by token
    full_text = ""
    with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=f"Context from completed steps:\n" + "\n".join(f"- {r}" for r in results),
        messages=[{"role": "user", "content": query}]
    ) as stream:
        for chunk in stream.text_stream:
            full_text += chunk
            yield sse_event("token", {"text": chunk})

    yield sse_event("task_complete", {
        "steps_completed": len(steps),
        "total_chars": len(full_text)
    })

# Simulate consuming the SSE stream (in production, this streams over HTTP)
RESEARCH_STEPS = [
    "Searching knowledge base",
    "Retrieving SQL documentation",
    "Retrieving NoSQL documentation",
    "Comparing performance characteristics",
    "Analyzing startup-specific trade-offs",
]

print("=== SSE Event Stream ===")
for event in run_agent_with_sse_progress(
    "Compare SQL vs NoSQL for a startup with unpredictable scale",
    RESEARCH_STEPS
):
    # Parse and display
    lines = event.strip().split("\n")
    event_type = lines[0].replace("event: ", "")
    data = json.loads(lines[1].replace("data: ", ""))
    if event_type == "token":
        print(data["text"], end="", flush=True)
    elif event_type not in ("task_complete",):
        print(f"\n[{event_type}] {data}")

print("\n=== Done ===")

# Expected Token Savings: ~0% (SSE overhead is local; LLM tokens unchanged)
# Environment: Web applications, Slack bots, any frontend that supports SSE or WebSocket
```

---

## Option 3: Async Progress Queue with Concurrent Step Execution

Use asyncio to run steps concurrently while streaming progress updates through a queue.

```python
import anthropic
import asyncio
import json
import time
from dataclasses import dataclass
from typing import AsyncIterator

client = anthropic.AsyncAnthropic()

@dataclass
class ProgressEvent:
    event_type: str  # "start", "step", "complete", "error"
    step_name: str = ""
    step_index: int = 0
    total_steps: int = 0
    message: str = ""
    data: dict = None

async def execute_step(name: str, duration: float = 0.3) -> str:
    """Simulate an async step (in production: real tool call or API request)."""
    await asyncio.sleep(duration)
    return f"Completed: {name}"

async def run_with_progress_queue(
    query: str,
    steps: list[dict]
) -> AsyncIterator[ProgressEvent]:
    """Async generator yielding progress events as steps complete."""
    queue: asyncio.Queue[ProgressEvent | None] = asyncio.Queue()
    total = len(steps)

    async def worker():
        try:
            results = []
            for i, step in enumerate(steps, 1):
                await queue.put(ProgressEvent(
                    event_type="step_start",
                    step_name=step["name"],
                    step_index=i,
                    total_steps=total
                ))

                result = await execute_step(step["name"], step.get("duration", 0.2))
                results.append(result)

                await queue.put(ProgressEvent(
                    event_type="step_complete",
                    step_name=step["name"],
                    step_index=i,
                    total_steps=total,
                    message=result
                ))

            # Final LLM call
            await queue.put(ProgressEvent(event_type="llm_generating", message="Generating answer..."))

            context = "\n".join(results)
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                system=f"Steps completed:\n{context}",
                messages=[{"role": "user", "content": query}]
            )
            answer = response.content[0].text

            await queue.put(ProgressEvent(
                event_type="complete",
                message=answer,
                data={"token_count": response.usage.output_tokens}
            ))
        except Exception as e:
            await queue.put(ProgressEvent(event_type="error", message=str(e)))
        finally:
            await queue.put(None)  # Sentinel

    asyncio.create_task(worker())

    while True:
        event = await queue.get()
        if event is None:
            break
        yield event

async def main():
    steps = [
        {"name": "Fetch market data", "duration": 0.15},
        {"name": "Analyze trends", "duration": 0.2},
        {"name": "Identify competitors", "duration": 0.15},
        {"name": "Assess risks", "duration": 0.1},
        {"name": "Synthesize findings", "duration": 0.2},
    ]

    print("Starting analysis...")
    async for event in run_with_progress_queue(
        "Provide a market analysis for an AI productivity tool startup",
        steps
    ):
        if event.event_type == "step_start":
            print(f"  [{event.step_index}/{event.total_steps}] Starting: {event.step_name}")
        elif event.event_type == "step_complete":
            print(f"  [{event.step_index}/{event.total_steps}] ✓ {event.step_name}")
        elif event.event_type == "llm_generating":
            print(f"  Generating final answer...")
        elif event.event_type == "complete":
            print(f"\nFinal Answer:\n{event.message}")
        elif event.event_type == "error":
            print(f"Error: {event.message}")

asyncio.run(main())

# Expected Token Savings: ~5% (async concurrency reduces wall time; fewer timeout-induced retries)
# Environment: High-concurrency agents; research tools with multiple parallel data fetches
```

---

## Option 4: Structured Log-Based Progress for Agent Pipelines

Emit structured progress logs to stdout/a log sink; a sidecar process renders them as a live dashboard.

```python
import anthropic
import json
import time
import sys
from datetime import datetime
from dataclasses import dataclass, asdict

client = anthropic.Anthropic()

@dataclass
class ProgressLog:
    timestamp: str
    task_id: str
    level: str  # INFO, PROGRESS, WARN, ERROR
    step: str
    message: str
    pct_complete: float = 0.0
    metadata: dict = None

    def emit(self, sink=sys.stdout):
        log = asdict(self)
        if self.metadata is None:
            log.pop("metadata")
        print(json.dumps(log), file=sink, flush=True)

class ProgressTracker:
    def __init__(self, task_id: str, total_steps: int):
        self.task_id = task_id
        self.total_steps = total_steps
        self.current_step = 0
        self.start_time = time.time()

    def advance(self, step_name: str, message: str = ""):
        self.current_step += 1
        pct = round(self.current_step / self.total_steps * 100, 1)
        elapsed = round(time.time() - self.start_time, 2)
        ProgressLog(
            timestamp=datetime.utcnow().isoformat(),
            task_id=self.task_id,
            level="PROGRESS",
            step=step_name,
            message=message or f"Completed step {self.current_step}/{self.total_steps}",
            pct_complete=pct,
            metadata={"elapsed_s": elapsed, "step_num": self.current_step}
        ).emit()

    def warn(self, step: str, message: str):
        ProgressLog(
            timestamp=datetime.utcnow().isoformat(),
            task_id=self.task_id,
            level="WARN",
            step=step,
            message=message,
            pct_complete=round(self.current_step / self.total_steps * 100, 1)
        ).emit()

    def complete(self, final_message: str):
        elapsed = round(time.time() - self.start_time, 2)
        ProgressLog(
            timestamp=datetime.utcnow().isoformat(),
            task_id=self.task_id,
            level="INFO",
            step="done",
            message=final_message,
            pct_complete=100.0,
            metadata={"total_elapsed_s": elapsed}
        ).emit()

def run_pipeline_with_structured_logs(task_id: str, documents: list[str]) -> str:
    tracker = ProgressTracker(task_id, total_steps=len(documents) + 2)

    summaries = []
    for i, doc in enumerate(documents):
        tracker.advance(f"summarize_doc_{i}", f"Summarizing document {i+1}/{len(documents)}")
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=[{"role": "user", "content": f"One sentence summary: {doc[:150]}"}]
        )
        summaries.append(response.content[0].text)
        time.sleep(0.1)

    tracker.advance("synthesize", "Synthesizing summaries into final report")
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"Write a brief report from these summaries:\n" + "\n".join(f"- {s}" for s in summaries)
        }]
    )
    final = response.content[0].text

    tracker.advance("finalize", "Report complete")
    tracker.complete(f"Processed {len(documents)} documents successfully")
    return final

DOCS = [
    "AI agents are becoming the primary interface for enterprise software automation.",
    "Vector databases enable semantic search at scale for RAG applications.",
    "Prompt engineering is evolving from art to engineering discipline.",
]

result = run_pipeline_with_structured_logs("pipeline-abc-001", DOCS)
print(f"\nFinal Report:\n{result}")

# Expected Token Savings: ~0% (logs are to stdout; no API overhead)
# Environment: Backend pipelines with log aggregation (Datadog, CloudWatch, Loki); ops-monitored agents
```

---

## Option 5: Streaming with Estimated Time Remaining

Stream progress with ETA calculation based on elapsed time per step.

```python
import anthropic
import time
from collections import deque

client = anthropic.Anthropic()

class ETATracker:
    def __init__(self, total_items: int, window: int = 5):
        self.total = total_items
        self.done = 0
        self.start = time.time()
        self.step_times: deque[float] = deque(maxlen=window)
        self._last_step_start = time.time()

    def record_step_complete(self):
        now = time.time()
        self.step_times.append(now - self._last_step_start)
        self._last_step_start = now
        self.done += 1

    @property
    def eta_seconds(self) -> float | None:
        if not self.step_times or self.done >= self.total:
            return None
        avg = sum(self.step_times) / len(self.step_times)
        remaining = self.total - self.done
        return avg * remaining

    @property
    def progress_bar(self) -> str:
        pct = self.done / self.total
        filled = int(pct * 20)
        bar = "█" * filled + "░" * (20 - filled)
        eta = self.eta_seconds
        eta_str = f"ETA: {eta:.1f}s" if eta is not None else "ETA: calculating..."
        return f"[{bar}] {self.done}/{self.total} ({pct*100:.0f}%) | {eta_str}"

    @property
    def elapsed(self) -> float:
        return time.time() - self.start

def run_with_eta_progress(items: list[str], task_name: str) -> list[str]:
    tracker = ETATracker(total_items=len(items))
    results = []

    print(f"Task: {task_name} ({len(items)} items)")
    print(tracker.progress_bar, end="\r", flush=True)

    for item in items:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=48,
            messages=[{"role": "user", "content": f"One word category for: {item}"}]
        )
        results.append(response.content[0].text.strip())
        tracker.record_step_complete()
        print(tracker.progress_bar + "  ", end="\r", flush=True)

    print()  # Move to new line after progress bar
    print(f"Completed in {tracker.elapsed:.1f}s")
    return results

ITEMS = [
    "kubernetes pod scheduling",
    "React hooks useState",
    "SQL window functions",
    "transformer self-attention",
    "gradient descent optimization",
]

results = run_with_eta_progress(ITEMS, "Classify technical topics")
for item, cat in zip(ITEMS, results):
    print(f"  {item}: {cat}")

# Expected Token Savings: ~0% (ETA is local computation; no extra API calls)
# Environment: CLI batch tools, developer utilities, data labeling agents
```

---

## Option 6: Hierarchical Task Progress with Sub-Step Tracking

Track progress at multiple levels (task → subtask → step) and stream nested progress events.

```python
import anthropic
import json
import time
from dataclasses import dataclass, field
from typing import Callable

client = anthropic.Anthropic()

@dataclass
class SubTask:
    name: str
    steps: list[str]
    completed_steps: list[str] = field(default_factory=list)

    @property
    def progress(self) -> float:
        return len(self.completed_steps) / len(self.steps) if self.steps else 1.0

@dataclass
class HierarchicalTask:
    name: str
    subtasks: list[SubTask]
    on_progress: Callable[[dict], None] = field(default=lambda x: None)

    @property
    def overall_progress(self) -> float:
        if not self.subtasks:
            return 1.0
        return sum(s.progress for s in self.subtasks) / len(self.subtasks)

    def emit(self, event: str, extra: dict = None):
        payload = {
            "event": event,
            "task": self.name,
            "overall_pct": round(self.overall_progress * 100, 1),
            "subtasks": [
                {"name": s.name, "pct": round(s.progress * 100, 1)}
                for s in self.subtasks
            ]
        }
        if extra:
            payload.update(extra)
        self.on_progress(payload)

def render_progress(event: dict):
    """Console renderer for hierarchical progress events."""
    overall = event["overall_pct"]
    bar_len = 30
    filled = int(overall / 100 * bar_len)
    bar = "█" * filled + "░" * (bar_len - filled)
    subtask_line = " | ".join(f"{s['name']}:{s['pct']:.0f}%" for s in event["subtasks"])
    print(f"\r[{bar}] {overall:.0f}% | {subtask_line}   ", end="", flush=True)

def run_hierarchical_task(query: str) -> str:
    subtasks = [
        SubTask("Research", ["Search topic", "Find examples", "Identify gaps"]),
        SubTask("Outline", ["Draft structure", "Order sections"]),
        SubTask("Write", ["Introduction", "Body", "Conclusion"]),
    ]

    task = HierarchicalTask(
        name="Article Generation",
        subtasks=subtasks,
        on_progress=render_progress
    )

    task.emit("start")
    results: dict[str, list[str]] = {s.name: [] for s in subtasks}

    for subtask in subtasks:
        for step in subtask.steps:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=48,
                messages=[{"role": "user", "content": f"One sentence for '{step}' about: {query}"}]
            )
            result = response.content[0].text
            results[subtask.name].append(result)
            subtask.completed_steps.append(step)
            task.emit("step_complete", {"subtask": subtask.name, "step": step})
            time.sleep(0.05)

    print()  # Newline after progress bar

    # Final synthesis
    all_points = [r for rs in results.values() for r in rs]
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": f"Write a short article using these points:\n" + "\n".join(f"- {p}" for p in all_points)
        }]
    )

    task.emit("complete")
    print()
    return response.content[0].text

result = run_hierarchical_task("The future of AI agent architectures")
print(f"\nResult:\n{result[:300]}...")

# Expected Token Savings: ~5% (granular tracking enables early termination of stuck subtasks)
# Environment: Multi-phase content generation, research pipelines, report automation tools
```

---

## Comparison

| Option | Output Channel | Granularity | ETA | Multi-Level | Best For |
|--------|---------------|-------------|-----|-------------|----------|
| 1. Text Progress Prefix | stdout | Task-level | No | No | CLI tools, simple scripts |
| 2. SSE Events | HTTP/SSE | Step-level | No | No | Web frontends, Slack bots |
| 3. Async Progress Queue | stdout/callback | Step-level | No | No | Concurrent async pipelines |
| 4. Structured Logs | stdout/log sink | Step-level | No | No | Ops-monitored production agents |
| 5. ETA Progress Bar | stdout | Item-level | Yes | No | Batch CLI tools |
| 6. Hierarchical Progress | callback | Sub-step | No | Yes | Complex multi-phase workflows |
