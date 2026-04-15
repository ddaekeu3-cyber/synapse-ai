---
layout: solution
title: "Agent Doesn't Provide Progress Updates on Long-Running Tasks"
category: general
description: "An agent starts a multi-step task — analyzing 200 documents, running a pipeline, processing a large dataset — and goes silent for minutes. The user sees nothing. They don't know if the agent is working, stuck, or has encountered an error. They cancel and retry, causing duplicate work. The fix is to emit progress updates at meaningful milestones and stream partial results as they become available."
tags: [ux, progress, streaming, long-running, feedback, user-experience, multi-step, transparency]
---

# Agent Doesn't Provide Progress Updates on Long-Running Tasks

## Symptom
- Agent processes 200 documents for 4 minutes with no output — user thinks it crashed
- No indication of whether the agent is on step 2 or step 20 of a multi-step pipeline
- User cancels and retries a partially complete task because there's no feedback
- Error occurs at step 18 of 20 — user only learns about it after waiting for the full timeout
- "It's working..." is the only status message; no estimate of remaining time
- Dashboard shows the agent as "running" indefinitely even when it's stuck

## Root Cause
Agent implementations process everything internally and emit a single final response. Without instrumented milestones, users have no visibility into progress. For tasks taking more than a few seconds, the lack of feedback violates user expectations set by every other interactive system. The fix is to use streaming for LLM responses and emit structured progress events for multi-step pipelines.

## Fix

### Option 1: Streaming response — show tokens as they're generated

```python
import anthropic
import sys

client = anthropic.Anthropic()

def stream_response(user_message: str) -> str:
    """
    Stream Claude's response token-by-token.
    User sees output immediately instead of waiting for completion.
    """
    collected_text = []

    with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": user_message}]
    ) as stream:
        for text in stream.text_stream:
            print(text, end="", flush=True)
            collected_text.append(text)

    print()  # newline after stream ends
    return "".join(collected_text)


# Async streaming:
async def stream_response_async(user_message: str) -> str:
    import anthropic

    async_client = anthropic.AsyncAnthropic()
    collected = []

    async with async_client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": user_message}]
    ) as stream:
        async for text in stream.text_stream:
            # In a web context: yield text to SSE/WebSocket:
            print(text, end="", flush=True)
            collected.append(text)

    print()
    return "".join(collected)


# FastAPI SSE integration:
# from fastapi import FastAPI
# from fastapi.responses import StreamingResponse
# import json
#
# app = FastAPI()
#
# @app.post("/chat/stream")
# async def chat_stream(body: ChatRequest):
#     async def generate():
#         async with async_client.messages.stream(...) as stream:
#             async for text in stream.text_stream:
#                 yield f"data: {json.dumps({'text': text})}\n\n"
#         yield "data: [DONE]\n\n"
#
#     return StreamingResponse(generate(), media_type="text/event-stream")
```

### Option 2: Milestone progress events — structured updates for multi-step tasks

```python
import anthropic
import time
from dataclasses import dataclass
from typing import Callable

client = anthropic.Anthropic()

@dataclass
class ProgressEvent:
    step: int
    total_steps: int
    description: str
    elapsed_seconds: float
    eta_seconds: float | None = None

    @property
    def percent(self) -> float:
        return (self.step / self.total_steps) * 100 if self.total_steps > 0 else 0

    def format(self) -> str:
        eta_str = f" ETA: {self.eta_seconds:.0f}s" if self.eta_seconds else ""
        return (
            f"[{self.step}/{self.total_steps}] {self.percent:.0f}% "
            f"— {self.description} ({self.elapsed_seconds:.1f}s elapsed{eta_str})"
        )


class ProgressTracker:
    def __init__(self, total_steps: int, on_progress: Callable[[ProgressEvent], None] = print):
        self.total = total_steps
        self._on_progress = on_progress
        self._step = 0
        self._step_times: list[float] = []
        self._start = time.monotonic()

    def update(self, description: str) -> ProgressEvent:
        self._step += 1
        now = time.monotonic()
        elapsed = now - self._start
        self._step_times.append(elapsed)

        # ETA based on average step duration:
        eta = None
        if len(self._step_times) >= 2:
            avg_step = self._step_times[-1] / self._step
            remaining_steps = self.total - self._step
            eta = avg_step * remaining_steps

        event = ProgressEvent(
            step=self._step,
            total_steps=self.total,
            description=description,
            elapsed_seconds=elapsed,
            eta_seconds=eta
        )
        self._on_progress(event.format())
        return event


def process_documents_with_progress(
    documents: list[dict],
    progress_callback: Callable[[str], None] = print
) -> list[dict]:
    """
    Process documents with real-time progress updates at each milestone.
    """
    tracker = ProgressTracker(
        total_steps=len(documents),
        on_progress=progress_callback
    )
    results = []

    for doc in documents:
        # Emit progress BEFORE processing so user knows work is happening:
        tracker.update(f"Analyzing: {doc['id']}")

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{
                "role": "user",
                "content": f"Summarize in one sentence: {doc['content'][:500]}"
            }]
        )
        results.append({
            "id": doc["id"],
            "summary": response.content[0].text
        })

    progress_callback(f"Complete: processed {len(results)} documents")
    return results


# Usage:
docs = [{"id": f"doc_{i}", "content": f"Document {i} content..."} for i in range(20)]
results = process_documents_with_progress(docs)
```

### Option 3: Pipeline stage announcements — per-stage progress for complex workflows

```python
import anthropic
import time
from contextlib import contextmanager
from typing import Callable

client = anthropic.Anthropic()

class PipelineReporter:
    """
    Reports progress for a named pipeline with multiple stages.
    Each stage has a start, optional sub-steps, and an end.
    """
    def __init__(self, pipeline_name: str, emit: Callable[[str], None] = print):
        self.name = pipeline_name
        self._emit = emit
        self._stage_start = 0.0
        self._pipeline_start = time.monotonic()

    @contextmanager
    def stage(self, stage_name: str, total_items: int | None = None):
        """Context manager for a pipeline stage."""
        self._stage_start = time.monotonic()
        info = f"({total_items} items)" if total_items else ""
        self._emit(f"\n[{self.name}] ▶ Starting: {stage_name} {info}")
        try:
            yield self
            elapsed = time.monotonic() - self._stage_start
            self._emit(f"[{self.name}] ✓ Done: {stage_name} ({elapsed:.1f}s)")
        except Exception as e:
            elapsed = time.monotonic() - self._stage_start
            self._emit(f"[{self.name}] ✗ Failed: {stage_name} after {elapsed:.1f}s — {e}")
            raise

    def item(self, description: str, current: int | None = None, total: int | None = None):
        """Report progress on an individual item within a stage."""
        if current is not None and total is not None:
            pct = current / total * 100
            self._emit(f"  [{current}/{total} {pct:.0f}%] {description}")
        else:
            self._emit(f"  → {description}")

    def info(self, message: str):
        self._emit(f"[{self.name}] ℹ {message}")


def run_analysis_pipeline(items: list[str], emit: Callable[[str], None] = print) -> dict:
    """
    Multi-stage analysis pipeline with per-stage progress reporting.
    """
    reporter = PipelineReporter("analysis", emit=emit)
    results = {"fetched": [], "classified": [], "summarized": []}

    # Stage 1: Fetch
    with reporter.stage("Fetch", total_items=len(items)):
        for i, item in enumerate(items):
            reporter.item(f"Fetching: {item}", current=i + 1, total=len(items))
            time.sleep(0.01)  # simulated fetch
            results["fetched"].append({"item": item, "content": f"Content of {item}"})

    # Stage 2: Classify (with LLM)
    with reporter.stage("Classify", total_items=len(results["fetched"])):
        for i, doc in enumerate(results["fetched"]):
            reporter.item(f"Classifying: {doc['item']}", current=i + 1, total=len(results["fetched"]))
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=16,
                messages=[{"role": "user", "content": f"Classify as A or B: {doc['content']}"}]
            )
            results["classified"].append({**doc, "class": response.content[0].text.strip()})

    # Stage 3: Summarize
    with reporter.stage("Summarize"):
        reporter.info(f"Generating summary of {len(results['classified'])} classified items")
        all_content = "\n".join(f"- {d['item']}: {d['class']}" for d in results["classified"])
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=256,
            messages=[{"role": "user", "content": f"Summarize these classifications:\n{all_content}"}]
        )
        results["summarized"] = response.content[0].text

    total_elapsed = time.monotonic()
    reporter.info(f"Pipeline complete in {total_elapsed:.1f}s")
    return results
```

### Option 4: WebSocket progress push — real-time updates to browser clients

```python
import anthropic
import asyncio
import json
import time

client = anthropic.AsyncAnthropic()

# WebSocket-based progress for browser clients.
# Requires: pip install websockets fastapi

class WebSocketProgressEmitter:
    """
    Emits progress events over WebSocket to a browser client.
    Falls back to logging if WebSocket not connected.
    """
    def __init__(self, websocket=None):
        self.ws = websocket
        self._messages: list[dict] = []

    async def emit(self, event_type: str, **kwargs):
        event = {"type": event_type, "timestamp": time.time(), **kwargs}
        self._messages.append(event)
        if self.ws:
            try:
                await self.ws.send_text(json.dumps(event))
            except Exception:
                pass  # client disconnected, continue processing


async def run_long_task_with_ws_progress(
    task_items: list[str],
    emitter: WebSocketProgressEmitter
) -> list[dict]:
    """
    Long-running task that emits WebSocket progress events.
    """
    await emitter.emit("task_started", total=len(task_items))
    results = []

    for i, item in enumerate(task_items):
        await emitter.emit(
            "item_started",
            current=i + 1,
            total=len(task_items),
            item=item,
            pct=round((i / len(task_items)) * 100)
        )

        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=[{"role": "user", "content": f"Process: {item}"}]
        )
        result = {"item": item, "output": response.content[0].text}
        results.append(result)

        await emitter.emit(
            "item_completed",
            current=i + 1,
            total=len(task_items),
            item=item,
            pct=round(((i + 1) / len(task_items)) * 100)
        )

    await emitter.emit("task_completed", total=len(task_items), results_count=len(results))
    return results


# FastAPI + WebSocket:
# @app.websocket("/ws/task/{task_id}")
# async def websocket_task(websocket: WebSocket, task_id: str):
#     await websocket.accept()
#     emitter = WebSocketProgressEmitter(websocket)
#     items = await load_task_items(task_id)
#     results = await run_long_task_with_ws_progress(items, emitter)
#     await websocket.close()
```

### Option 5: Heartbeat pattern — prove the agent is alive during silent stretches

```python
import anthropic
import asyncio
import time
from typing import Callable

client = anthropic.AsyncAnthropic()

class HeartbeatMonitor:
    """
    Emits periodic "still working" signals during long operations.
    Prevents UI timeouts and reassures users during silent LLM calls.
    """
    def __init__(
        self,
        emit: Callable[[str], None],
        interval_seconds: float = 5.0
    ):
        self._emit = emit
        self._interval = interval_seconds
        self._task: asyncio.Task | None = None
        self._start = time.monotonic()

    async def _beat(self):
        while True:
            await asyncio.sleep(self._interval)
            elapsed = time.monotonic() - self._start
            self._emit(f"[heartbeat] Still processing... ({elapsed:.0f}s elapsed)")

    def start(self):
        self._start = time.monotonic()
        self._task = asyncio.create_task(self._beat())

    def stop(self):
        if self._task:
            self._task.cancel()
            self._task = None


async def long_llm_call_with_heartbeat(
    prompt: str,
    emit: Callable[[str], None] = print
) -> str:
    """
    LLM call with heartbeat so users know it's still running.
    """
    monitor = HeartbeatMonitor(emit=emit, interval_seconds=5.0)
    monitor.start()
    try:
        response = await client.messages.create(
            model="claude-opus-4-6",
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text
    finally:
        monitor.stop()
        elapsed = time.monotonic() - monitor._start
        emit(f"[complete] Finished in {elapsed:.1f}s")


async def batch_with_heartbeat(items: list[dict], emit: Callable = print) -> list[dict]:
    """
    Process a batch with both heartbeat and item-level progress.
    """
    results = []
    monitor = HeartbeatMonitor(emit=emit, interval_seconds=10.0)
    monitor.start()

    try:
        for i, item in enumerate(items):
            emit(f"[progress] {i + 1}/{len(items)}: {item['id']}")
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=128,
                messages=[{"role": "user", "content": item["content"]}]
            )
            results.append({"id": item["id"], "result": response.content[0].text})
    finally:
        monitor.stop()

    return results
```

### Option 6: Estimated time + progress bar — terminal/CLI feedback

```python
import anthropic
import time
import sys

client = anthropic.Anthropic()

def render_progress_bar(current: int, total: int, width: int = 40) -> str:
    """Render a progress bar string."""
    filled = int((current / total) * width)
    bar = "█" * filled + "░" * (width - filled)
    pct = (current / total) * 100
    return f"|{bar}| {pct:.1f}%"


class CLIProgressReporter:
    """
    Terminal progress reporter with ETA and progress bar.
    Uses carriage return to update in-place without scrolling.
    """
    def __init__(self, total: int, description: str = "Processing"):
        self.total = total
        self.description = description
        self._current = 0
        self._start = time.monotonic()
        self._step_times: list[float] = []

    def update(self, item_description: str = ""):
        self._current += 1
        now = time.monotonic()
        elapsed = now - self._start
        self._step_times.append(elapsed)

        # ETA calculation:
        if len(self._step_times) >= 2:
            avg = elapsed / self._current
            remaining = avg * (self.total - self._current)
            eta = f"ETA: {remaining:.0f}s"
        else:
            eta = "ETA: calculating..."

        bar = render_progress_bar(self._current, self.total)
        line = f"\r{self.description} {bar} {self._current}/{self.total} {eta}"
        if item_description:
            line += f" | {item_description[:30]}"

        sys.stdout.write(line)
        sys.stdout.flush()

        if self._current >= self.total:
            print()  # newline when done

    def error(self, item_description: str, error: str):
        print(f"\n  [ERROR] {item_description}: {error}")


def process_with_cli_progress(items: list[dict]) -> list[dict]:
    reporter = CLIProgressReporter(total=len(items), description="Analyzing")
    results = []

    for item in items:
        try:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=64,
                messages=[{"role": "user", "content": item["content"][:300]}]
            )
            results.append({"id": item["id"], "result": response.content[0].text})
            reporter.update(item_description=item["id"])
        except Exception as e:
            reporter.error(item["id"], str(e))
            results.append({"id": item["id"], "error": str(e)})

    return results
```

## Progress Pattern Selection Guide

| Context | Pattern | Implementation |
|---|---|---|
| Single LLM response | Token streaming | Option 1 — built-in SDK support |
| Multi-step pipeline | Milestone events | Option 2/3 — ProgressTracker |
| Browser/web client | WebSocket push | Option 4 — SSE or WebSocket |
| Long single LLM call | Heartbeat | Option 5 — timed keep-alive |
| CLI/terminal tool | Progress bar with ETA | Option 6 — carriage return update |
| Background job | Job status endpoint | Poll /status/<job_id> endpoint |

## Expected Token Savings
Progress updates themselves are zero-cost — they don't add tokens to LLM calls. The savings come from preventing user-triggered retries: a user who cancels a silent 3-minute task after 1 minute restarts from scratch, doubling the total token spend. Visible progress eliminates most premature cancellations.

## Environment
- Any agent task taking longer than 5 seconds; mandatory for document processing pipelines, web scraping, batch analysis, and any multi-step workflow; token streaming (Option 1) should be the default for all interactive agents; milestone events (Option 2) are appropriate for any task with more than 3 discrete steps; heartbeat (Option 5) is a minimal fallback when streaming isn't possible; always show elapsed time and an ETA once you have enough data to estimate
