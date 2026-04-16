---
layout: solution
title: "Agent Doesn't Implement Structured Progress Reporting for Long-Running Tasks"
category: general
description: "Emit structured progress events during multi-step agent tasks so that users, orchestrators, and monitoring systems receive real-time visibility into what the agent is doing and how far along it is."
tags: [general, progress, reporting, long-running, observability, ux, multi-step]
---

# Agent Doesn't Implement Structured Progress Reporting for Long-Running Tasks

## Problem

Long-running agent tasks — research pipelines, batch processing, multi-step reasoning chains — produce no feedback until they complete. Users see a spinner and have no idea if the agent is working, stuck, or about to fail. Orchestrators cannot adapt to slow subtasks. Monitoring systems cannot detect hung agents. Structured progress reporting turns the agent from a black box into an observable process.

## Solutions

### Option 1: Step-Based Progress with Percentage Completion

Emit a structured progress event after each completed step, including step name, percentage, and elapsed time.

```python
import anthropic
import time
from dataclasses import dataclass, field

client = anthropic.Anthropic()


@dataclass
class ProgressEvent:
    step_name: str
    step_index: int
    total_steps: int
    percent: float
    elapsed_seconds: float
    status: str         # "started" | "complete" | "failed"
    message: str = ""

    def display(self) -> str:
        bar_len = 20
        filled = int(self.percent / 100 * bar_len)
        bar = "█" * filled + "░" * (bar_len - filled)
        return (
            f"[{bar}] {self.percent:5.1f}% "
            f"Step {self.step_index}/{self.total_steps}: {self.step_name} "
            f"({self.elapsed_seconds:.1f}s) {self.status}"
            + (f" — {self.message}" if self.message else "")
        )


class ProgressReporter:
    def __init__(self, steps: list[str], on_progress=None) -> None:
        self.steps = steps
        self.total = len(steps)
        self.current = 0
        self.started_at = time.monotonic()
        self.on_progress = on_progress or (lambda e: print(f"  {e.display()}"))

    def emit(self, status: str, message: str = "") -> ProgressEvent:
        elapsed = time.monotonic() - self.started_at
        event = ProgressEvent(
            step_name=self.steps[self.current],
            step_index=self.current + 1,
            total_steps=self.total,
            percent=((self.current + (1 if status == "complete" else 0)) / self.total) * 100,
            elapsed_seconds=round(elapsed, 2),
            status=status,
            message=message[:80] if message else "",
        )
        self.on_progress(event)
        return event

    def start_step(self) -> ProgressEvent:
        return self.emit("started")

    def complete_step(self, message: str = "") -> ProgressEvent:
        event = self.emit("complete", message)
        self.current = min(self.current + 1, self.total - 1)
        return event

    def fail_step(self, message: str = "") -> ProgressEvent:
        return self.emit("failed", message)


def run_research_pipeline(topic: str) -> dict:
    steps = [
        "gather_background",
        "identify_key_concepts",
        "analyze_implications",
        "draft_summary",
        "review_and_refine",
    ]
    reporter = ProgressReporter(steps)
    results: dict[str, str] = {}
    context = ""

    for step in steps:
        reporter.start_step()
        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                messages=[{
                    "role": "user",
                    "content": f"Topic: {topic}\nContext: {context[:300]}\nTask ({step}): complete this step concisely.",
                }],
            )
            result = resp.content[0].text
            results[step] = result
            context += f"\n{step}: {result[:150]}"
            reporter.complete_step(result[:60])
        except Exception as e:
            reporter.fail_step(str(e))
            raise

    return results


if __name__ == "__main__":
    results = run_research_pipeline("the impact of large language models on education")
    print(f"\nCompleted {len(results)} steps.")

# Expected Token Savings: No extra tokens; progress is emitted from existing step results
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 2: Async SSE-Style Progress Stream

Stream structured progress events as Server-Sent Events (SSE) format for real-time frontend consumption.

```python
import anthropic
import asyncio
import json
import time
from dataclasses import dataclass, asdict, field
from typing import AsyncIterator

client = anthropic.AsyncAnthropic()


@dataclass
class ProgressEvent:
    event_type: str     # "progress" | "step_result" | "complete" | "error"
    task_id: str
    step: str
    index: int
    total: int
    percent: float
    timestamp: float = field(default_factory=time.time)
    data: dict = field(default_factory=dict)

    def to_sse(self) -> str:
        return f"event: {self.event_type}\ndata: {json.dumps(asdict(self))}\n\n"


async def run_task_with_sse(
    task_id: str,
    topic: str,
    steps: list[tuple[str, str]],
) -> AsyncIterator[str]:
    """Yields SSE-formatted progress strings."""
    total = len(steps)
    context = ""

    for i, (step_name, prompt) in enumerate(steps):
        # emit step start
        yield ProgressEvent(
            event_type="progress",
            task_id=task_id,
            step=step_name,
            index=i,
            total=total,
            percent=round(i / total * 100, 1),
            data={"status": "started"},
        ).to_sse()

        try:
            resp = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                messages=[{
                    "role": "user",
                    "content": f"Topic: {topic}\nContext: {context[:300]}\nTask: {prompt}",
                }],
            )
            result = resp.content[0].text
            context += f"\n{step_name}: {result[:150]}"

            yield ProgressEvent(
                event_type="step_result",
                task_id=task_id,
                step=step_name,
                index=i + 1,
                total=total,
                percent=round((i + 1) / total * 100, 1),
                data={"result": result[:100], "status": "complete"},
            ).to_sse()

        except Exception as e:
            yield ProgressEvent(
                event_type="error",
                task_id=task_id,
                step=step_name,
                index=i,
                total=total,
                percent=round(i / total * 100, 1),
                data={"error": str(e), "status": "failed"},
            ).to_sse()
            return

    yield ProgressEvent(
        event_type="complete",
        task_id=task_id,
        step="done",
        index=total,
        total=total,
        percent=100.0,
        data={"status": "success"},
    ).to_sse()


async def consume_progress_stream(task_id: str, topic: str) -> None:
    steps = [
        ("research",   "List 3 key facts about this topic."),
        ("analyze",    "Identify 2 important trends or implications."),
        ("summarize",  "Write a 2-sentence summary."),
    ]
    print(f"Starting task {task_id}...\n")
    async for sse_chunk in run_task_with_sse(task_id, topic, steps):
        # parse and display (in production: send to HTTP response stream)
        for line in sse_chunk.strip().split("\n"):
            if line.startswith("data:"):
                data = json.loads(line[5:])
                print(f"  [{data['percent']:5.1f}%] {data['step']:15s} → {data.get('data', {}).get('status', '')}")


if __name__ == "__main__":
    asyncio.run(consume_progress_stream("task_001", "quantum computing applications"))

# Expected Token Savings: Zero overhead; SSE events carry no extra model cost
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 3: Hierarchical Progress for Nested Sub-Tasks

Report progress at multiple levels: top-level task → sub-tasks → individual operations, with rolled-up percentage.

```python
import anthropic
import time
from dataclasses import dataclass, field
from typing import Callable

client = anthropic.Anthropic()


@dataclass
class TaskNode:
    name: str
    weight: float = 1.0
    children: list["TaskNode"] = field(default_factory=list)
    _done: float = 0.0     # 0.0–1.0 completion fraction
    _depth: int = 0

    def set_progress(self, fraction: float) -> None:
        self._done = max(0.0, min(1.0, fraction))

    def completion(self) -> float:
        if not self.children:
            return self._done
        total_weight = sum(c.weight for c in self.children)
        if total_weight == 0:
            return 0.0
        return sum(c.weight * c.completion() for c in self.children) / total_weight

    def display(self, indent: int = 0) -> str:
        pct = self.completion() * 100
        bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
        prefix = "  " * indent
        line = f"{prefix}[{bar}] {pct:5.1f}% {self.name}"
        child_lines = [c.display(indent + 1) for c in self.children]
        return "\n".join([line] + child_lines)


class HierarchicalProgressReporter:
    def __init__(self, root: TaskNode, refresh_fn: Callable | None = None) -> None:
        self.root = root
        self.refresh_fn = refresh_fn or self._default_display
        self.started_at = time.monotonic()

    def _default_display(self) -> None:
        elapsed = time.monotonic() - self.started_at
        print(f"\n=== Progress ({elapsed:.1f}s) ===")
        print(self.root.display())

    def update(self) -> None:
        self.refresh_fn()


def run_multi_stage_pipeline(topic: str) -> dict:
    # define task hierarchy
    root = TaskNode("full_pipeline", weight=1.0)
    research = TaskNode("research",  weight=0.4, children=[
        TaskNode("web_facts",     weight=1.0),
        TaskNode("key_concepts",  weight=1.0),
    ])
    analysis = TaskNode("analysis", weight=0.4, children=[
        TaskNode("trends",        weight=1.0),
        TaskNode("implications",  weight=1.0),
    ])
    output = TaskNode("output",   weight=0.2, children=[
        TaskNode("draft",         weight=1.0),
    ])
    root.children = [research, analysis, output]

    reporter = HierarchicalProgressReporter(root)
    results: dict = {}

    def llm(prompt: str) -> str:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{"role": "user", "content": f"Topic: {topic}\n\n{prompt}"}],
        )
        return resp.content[0].text

    # research stage
    research.children[0].set_progress(0.0)
    reporter.update()
    results["web_facts"] = llm("List 3 key facts.")
    research.children[0].set_progress(1.0)
    reporter.update()

    results["key_concepts"] = llm("Identify 3 key concepts.")
    research.children[1].set_progress(1.0)
    reporter.update()

    # analysis stage
    ctx = f"Facts: {results['web_facts'][:100]}\nConcepts: {results['key_concepts'][:100]}"
    results["trends"] = llm(f"{ctx}\n\nDescribe 2 current trends.")
    analysis.children[0].set_progress(1.0)
    reporter.update()

    results["implications"] = llm(f"{ctx}\n\nDescribe 2 key implications.")
    analysis.children[1].set_progress(1.0)
    reporter.update()

    # output stage
    summary_ctx = "\n".join(f"{k}: {v[:80]}" for k, v in results.items())
    results["draft"] = llm(f"{summary_ctx}\n\nWrite a 3-sentence summary.")
    output.children[0].set_progress(1.0)
    reporter.update()

    return results


if __name__ == "__main__":
    results = run_multi_stage_pipeline("renewable energy adoption")
    print(f"\nFinal output: {results.get('draft', '')[:200]}")

# Expected Token Savings: Zero; hierarchical view adds no tokens, only clarity
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 4: Progress with ETA and Throughput Metrics

Track historical step durations to compute estimated time to completion and tokens-per-second throughput.

```python
import anthropic
import time
from dataclasses import dataclass, field
from collections import deque

client = anthropic.Anthropic()


@dataclass
class MetricsTracker:
    step_durations: deque = field(default_factory=lambda: deque(maxlen=10))
    step_tokens:    deque = field(default_factory=lambda: deque(maxlen=10))
    total_tokens:   int = 0
    started_at:     float = field(default_factory=time.monotonic)

    def record(self, duration: float, tokens: int) -> None:
        self.step_durations.append(duration)
        self.step_tokens.append(tokens)
        self.total_tokens += tokens

    def avg_step_duration(self) -> float:
        if not self.step_durations:
            return 0.0
        return sum(self.step_durations) / len(self.step_durations)

    def tokens_per_second(self) -> float:
        elapsed = time.monotonic() - self.started_at
        return self.total_tokens / max(elapsed, 0.001)

    def eta_seconds(self, remaining_steps: int) -> float:
        avg = self.avg_step_duration()
        return avg * remaining_steps if avg else 0.0

    def elapsed(self) -> float:
        return time.monotonic() - self.started_at


def format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    return f"{seconds // 60:.0f}m{seconds % 60:.0f}s"


class ETAReporter:
    def __init__(self, steps: list[str]) -> None:
        self.steps = steps
        self.total = len(steps)
        self.done  = 0
        self.metrics = MetricsTracker()

    def report(self, step_name: str, duration: float, tokens: int) -> None:
        self.done += 1
        self.metrics.record(duration, tokens)
        remaining = self.total - self.done
        eta = self.metrics.eta_seconds(remaining)
        tps = self.metrics.tokens_per_second()
        elapsed = self.metrics.elapsed()

        pct = self.done / self.total * 100
        bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
        print(
            f"  [{bar}] {pct:5.1f}% | "
            f"step={step_name:<20} | "
            f"elapsed={format_duration(elapsed)} | "
            f"ETA={format_duration(eta)} | "
            f"{tps:.0f} tok/s | "
            f"total_tokens={self.metrics.total_tokens}"
        )


def run_pipeline_with_eta(topic: str, steps: list[tuple[str, str]]) -> dict:
    reporter = ETAReporter([s[0] for s in steps])
    results: dict = {}
    context = ""

    for step_name, prompt in steps:
        step_start = time.monotonic()
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": f"Topic: {topic}\nContext: {context[:200]}\n\n{prompt}",
            }],
        )
        duration = time.monotonic() - step_start
        tokens = resp.usage.input_tokens + resp.usage.output_tokens
        result = resp.content[0].text

        results[step_name] = result
        context += f"\n{step_name}: {result[:100]}"
        reporter.report(step_name, duration, tokens)

    return results


if __name__ == "__main__":
    STEPS = [
        ("define",    "Define the topic in one sentence."),
        ("history",   "Give a 2-sentence history."),
        ("trends",    "Describe 2 current trends."),
        ("challenges","List 2 main challenges."),
        ("outlook",   "Give a 2-sentence future outlook."),
    ]
    results = run_pipeline_with_eta("artificial intelligence in healthcare", STEPS)
    print(f"\nPipeline complete. Steps: {list(results.keys())}")

# Expected Token Savings: ETA helps users cancel early if estimates exceed budget
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 5: Progress Reporter with Webhook Delivery

Push progress events to a webhook endpoint so external systems (dashboards, Slack, monitoring) receive updates.

```python
import anthropic
import asyncio
import json
import time
from dataclasses import dataclass, asdict, field
from urllib.request import urlopen, Request
from urllib.error import URLError

client = anthropic.AsyncAnthropic()

WEBHOOK_URL = "https://webhook.site/your-id-here"  # replace with real URL for testing


@dataclass
class WebhookEvent:
    task_id: str
    event_type: str
    step: str
    percent: float
    message: str
    timestamp: float = field(default_factory=time.time)
    metadata: dict = field(default_factory=dict)


def send_webhook(url: str, event: WebhookEvent, timeout: float = 3.0) -> bool:
    try:
        body = json.dumps(asdict(event)).encode()
        req = Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        urlopen(req, timeout=timeout)
        return True
    except (URLError, Exception):
        return False   # webhook failure never blocks the agent


class WebhookProgressReporter:
    def __init__(
        self,
        task_id: str,
        steps: list[str],
        webhook_url: str,
        console: bool = True,
    ) -> None:
        self.task_id     = task_id
        self.steps       = steps
        self.total       = len(steps)
        self.done        = 0
        self.webhook_url = webhook_url
        self.console     = console

    def _emit(self, event_type: str, step: str, message: str, metadata: dict = None) -> None:
        pct = (self.done / self.total) * 100
        event = WebhookEvent(
            task_id=self.task_id,
            event_type=event_type,
            step=step,
            percent=round(pct, 1),
            message=message[:200],
            metadata=metadata or {},
        )
        if self.console:
            print(f"  [{pct:5.1f}%] {event_type:12s} | {step}: {message[:60]}")
        # fire-and-forget in background
        asyncio.get_event_loop().run_in_executor(None, send_webhook, self.webhook_url, event)

    def step_started(self, step: str) -> None:
        self._emit("step_started", step, "Processing...")

    def step_done(self, step: str, result_preview: str) -> None:
        self.done += 1
        self._emit("step_complete", step, result_preview)

    def task_complete(self) -> None:
        self._emit("task_complete", "done", "All steps finished", {"total_steps": self.total})

    def task_failed(self, step: str, error: str) -> None:
        self._emit("task_failed", step, error)


async def run_with_webhook(task_id: str, topic: str) -> dict:
    steps = ["research", "analyze", "conclude"]
    reporter = WebhookProgressReporter(task_id, steps, WEBHOOK_URL)
    results: dict = {}
    context = ""

    step_prompts = {
        "research":  "List 3 key facts about this topic.",
        "analyze":   "Analyze the most important trend.",
        "conclude":  "Write a 2-sentence conclusion.",
    }

    for step in steps:
        reporter.step_started(step)
        try:
            resp = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=150,
                messages=[{"role": "user", "content": f"Topic: {topic}\nContext: {context[:200]}\n\n{step_prompts[step]}"}],
            )
            result = resp.content[0].text
            results[step] = result
            context += f"\n{step}: {result[:100]}"
            reporter.step_done(step, result[:60])
        except Exception as e:
            reporter.task_failed(step, str(e))
            raise

    reporter.task_complete()
    return results


if __name__ == "__main__":
    async def main():
        results = await run_with_webhook("task_" + str(int(time.time())), "climate technology")
        print(f"\nDone: {list(results.keys())}")

    asyncio.run(main())

# Expected Token Savings: Zero extra tokens; webhook events are local metadata only
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 6: Rich Progress State Machine with Pause/Resume/Cancel

Full progress state machine supporting pause, resume, and cancellation, with persistent state for long-running tasks.

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum

client = anthropic.AsyncAnthropic()


class TaskState(str, Enum):
    PENDING   = "pending"
    RUNNING   = "running"
    PAUSED    = "paused"
    COMPLETE  = "complete"
    CANCELLED = "cancelled"
    FAILED    = "failed"


@dataclass
class ProgressStateMachine:
    task_id: str
    steps: list[str]
    state: TaskState = TaskState.PENDING
    current_step: int = 0
    results: dict = field(default_factory=dict)
    started_at: float = 0.0
    _pause_event: asyncio.Event = field(default_factory=lambda: asyncio.Event())
    _cancel_event: asyncio.Event = field(default_factory=asyncio.Event)

    def __post_init__(self) -> None:
        self._pause_event.set()   # not paused initially

    @property
    def total(self) -> int:
        return len(self.steps)

    @property
    def percent(self) -> float:
        return (self.current_step / self.total) * 100

    def display(self) -> str:
        elapsed = time.monotonic() - self.started_at if self.started_at else 0
        bar_filled = int(self.percent / 5)
        bar = "█" * bar_filled + "░" * (20 - bar_filled)
        step_name = self.steps[min(self.current_step, self.total - 1)]
        return (
            f"[{bar}] {self.percent:5.1f}% | "
            f"state={self.state.value:10s} | "
            f"step={self.current_step}/{self.total} ({step_name}) | "
            f"elapsed={elapsed:.1f}s"
        )

    def pause(self) -> None:
        if self.state == TaskState.RUNNING:
            self.state = TaskState.PAUSED
            self._pause_event.clear()
            print(f"  [PAUSED] {self.display()}")

    def resume(self) -> None:
        if self.state == TaskState.PAUSED:
            self.state = TaskState.RUNNING
            self._pause_event.set()
            print(f"  [RESUMED] {self.display()}")

    def cancel(self) -> None:
        self.state = TaskState.CANCELLED
        self._cancel_event.set()
        self._pause_event.set()   # unblock if paused
        print(f"  [CANCELLED] at step {self.current_step}/{self.total}")

    async def wait_if_paused(self) -> bool:
        """Returns False if cancelled while waiting."""
        await self._pause_event.wait()
        return not self._cancel_event.is_set()


async def run_controllable_pipeline(
    task_id: str,
    topic: str,
    steps: list[tuple[str, str]],
    controller: ProgressStateMachine | None = None,
) -> dict:
    if controller is None:
        controller = ProgressStateMachine(task_id=task_id, steps=[s[0] for s in steps])

    controller.state = TaskState.RUNNING
    controller.started_at = time.monotonic()
    context = ""

    for i, (step_name, prompt) in enumerate(steps):
        # check for cancellation
        if controller._cancel_event.is_set():
            controller.state = TaskState.CANCELLED
            return controller.results

        # wait if paused
        if not await controller.wait_if_paused():
            controller.state = TaskState.CANCELLED
            return controller.results

        controller.current_step = i
        print(f"  {controller.display()}")

        try:
            resp = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=150,
                messages=[{
                    "role": "user",
                    "content": f"Topic: {topic}\nContext: {context[:200]}\n\n{prompt}",
                }],
            )
            result = resp.content[0].text
            controller.results[step_name] = result
            context += f"\n{step_name}: {result[:100]}"

        except Exception as e:
            controller.state = TaskState.FAILED
            print(f"  [FAILED] {step_name}: {e}")
            return controller.results

    controller.current_step = len(steps)
    controller.state = TaskState.COMPLETE
    print(f"  {controller.display()}")
    return controller.results


async def demo_with_controls() -> None:
    STEPS = [
        ("intro",     "Write 1 sentence introducing the topic."),
        ("facts",     "List 2 facts about the topic."),
        ("analysis",  "Write 1 sentence of analysis."),
        ("conclusion","Write 1 concluding sentence."),
    ]

    psm = ProgressStateMachine(task_id="demo_001", steps=[s[0] for s in STEPS])

    # simulate pause after 1s, resume after 2s
    async def controller_sim() -> None:
        await asyncio.sleep(1.0)
        psm.pause()
        await asyncio.sleep(0.5)
        psm.resume()

    results, _ = await asyncio.gather(
        run_controllable_pipeline("demo_001", "ocean conservation", STEPS, psm),
        controller_sim(),
    )
    print(f"\nFinal state: {psm.state.value} | Steps complete: {list(results.keys())}")


if __name__ == "__main__":
    asyncio.run(demo_with_controls())

# Expected Token Savings: Pause/cancel prevents token waste when user abandons task mid-run
# Environment: ANTHROPIC_API_KEY must be set
```

---

## Comparison

| Option | Progress Model | Real-Time | ETA | External Push | Pause/Cancel | Best For |
|--------|---------------|-----------|-----|---------------|-------------|----------|
| 1 | Step % + bar | Console | No | No | No | CLI agents, simple pipelines |
| 2 | SSE stream | Yes | No | Browser/HTTP | No | Web frontends |
| 3 | Hierarchical tree | Console | No | No | No | Nested multi-stage pipelines |
| 4 | ETA + throughput | Console | Yes | No | No | Long-running batch jobs |
| 5 | Webhook events | Yes | No | Yes | No | Dashboard/Slack integration |
| 6 | State machine | Console | No | No | Yes | Interactive, user-controllable tasks |
