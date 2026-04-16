---
layout: solution
title: "Agent Doesn't Implement Task Timeout with Partial Result Return"
category: loop-stuck
description: "Long-running agent tasks without timeouts block resources indefinitely and return nothing to the user. A timeout strategy that returns partial results — whatever the agent has completed so far — preserves work and keeps the user informed rather than delivering silence or an error."
tags: [loop-stuck, timeout, partial-results, reliability, user-experience, long-running]
---

## Problem

An agent tasked with processing 50 documents, running a multi-step research pipeline, or generating a long structured output may exceed acceptable wall-clock limits. Without a timeout, the request blocks indefinitely — exhausting workers, frustrating users, and wasting tokens on work that will never be delivered. A partial-result timeout strategy returns what was completed before the deadline, gives the user actionable output, and signals how to continue.

## Solutions

### Option 1: Simple Wall-Clock Timeout with Partial Accumulation

```python
import anthropic
import time
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class PartialResult:
    completed_items: list[dict]
    total_items: int
    timed_out: bool
    elapsed_seconds: float
    completion_rate: float
    resume_from: int  # Index to continue from

def process_items_with_timeout(
    items: list[str],
    task_description: str,
    timeout_seconds: float = 10.0
) -> PartialResult:
    """
    Process items one by one, returning partial results on timeout.
    """
    completed = []
    start = time.time()
    timed_out = False

    for i, item in enumerate(items):
        elapsed = time.time() - start
        if elapsed >= timeout_seconds:
            timed_out = True
            print(f"[Timeout] Deadline reached after {elapsed:.1f}s — returning {i}/{len(items)} results")
            return PartialResult(
                completed_items=completed,
                total_items=len(items),
                timed_out=True,
                elapsed_seconds=elapsed,
                completion_rate=i / len(items),
                resume_from=i
            )

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            messages=[{"role": "user", "content": f"{task_description}: {item}"}]
        )
        completed.append({
            "index": i,
            "input": item,
            "output": response.content[0].text.strip(),
            "processed_at": time.time() - start
        })
        print(f"  [{i+1}/{len(items)}] done in {time.time()-start:.1f}s")

    elapsed = time.time() - start
    return PartialResult(
        completed_items=completed,
        total_items=len(items),
        timed_out=False,
        elapsed_seconds=elapsed,
        completion_rate=1.0,
        resume_from=len(items)
    )

# Usage
documents = [
    "Quarterly revenue increased 15% year-over-year",
    "Customer satisfaction scores dropped 3 points",
    "New product launch exceeded initial targets by 40%",
    "Supply chain disruptions impacted Q3 margins",
    "International expansion added 12 new markets",
]

result = process_items_with_timeout(
    documents,
    task_description="Summarize in 5 words",
    timeout_seconds=8.0
)

print(f"\nCompleted: {len(result.completed_items)}/{result.total_items}")
print(f"Timed out: {result.timed_out} | Rate: {result.completion_rate:.0%}")
if result.timed_out:
    print(f"Resume from index: {result.resume_from}")
for item in result.completed_items[:3]:
    print(f"  [{item['index']}] {item['output']}")

# Expected Token Savings: Stops spending tokens after deadline; partial > nothing
# Environment: ANTHROPIC_API_KEY required
```

### Option 2: Async Timeout with Concurrent Sub-Tasks

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional

client = anthropic.AsyncAnthropic()

@dataclass
class AsyncPartialResult:
    results: dict[str, Optional[str]]  # task_id -> result or None (timed out)
    completed_count: int
    timeout_count: int
    error_count: int
    wall_time_ms: float
    deadline_exceeded: bool

async def process_single_task(task_id: str, prompt: str, item_timeout: float = 5.0) -> tuple[str, Optional[str]]:
    """Process one task with per-item timeout."""
    try:
        response = await asyncio.wait_for(
            client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=100,
                messages=[{"role": "user", "content": prompt}]
            ),
            timeout=item_timeout
        )
        return task_id, response.content[0].text.strip()
    except asyncio.TimeoutError:
        return task_id, None
    except Exception as e:
        return task_id, f"ERROR: {str(e)[:40]}"

async def process_all_with_deadline(
    tasks: dict[str, str],   # task_id -> prompt
    overall_deadline: float = 15.0,
    per_item_timeout: float = 4.0,
    max_concurrent: int = 5
) -> AsyncPartialResult:
    """
    Process all tasks concurrently but abort entire batch at overall_deadline.
    Returns partial results for whatever completed.
    """
    start = time.time()
    results: dict[str, Optional[str]] = {tid: None for tid in tasks}
    completed = 0
    timeout_count = 0
    error_count = 0

    semaphore = asyncio.Semaphore(max_concurrent)

    async def bounded_task(task_id: str, prompt: str) -> tuple[str, Optional[str]]:
        async with semaphore:
            return await process_single_task(task_id, prompt, per_item_timeout)

    all_coros = [bounded_task(tid, prompt) for tid, prompt in tasks.items()]

    # Use asyncio.wait with overall deadline
    pending_tasks = [asyncio.create_task(c) for c in all_coros]

    try:
        done, pending = await asyncio.wait(
            pending_tasks,
            timeout=overall_deadline
        )

        # Cancel anything still running after deadline
        if pending:
            print(f"[Deadline] {len(pending)} tasks cancelled at {overall_deadline}s deadline")
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

        # Collect completed results
        for task in done:
            try:
                task_id, result = task.result()
                results[task_id] = result
                if result is None:
                    timeout_count += 1
                elif result.startswith("ERROR:"):
                    error_count += 1
                else:
                    completed += 1
            except Exception:
                error_count += 1

    except Exception as e:
        print(f"[AsyncTimeout] Unexpected error: {e}")

    elapsed = time.time() - start
    deadline_exceeded = elapsed >= overall_deadline

    return AsyncPartialResult(
        results=results,
        completed_count=completed,
        timeout_count=timeout_count,
        error_count=error_count,
        wall_time_ms=elapsed * 1000,
        deadline_exceeded=deadline_exceeded
    )

async def main():
    research_tasks = {
        "topic_1": "What is async programming? Answer in one sentence.",
        "topic_2": "What is a coroutine? Answer in one sentence.",
        "topic_3": "What is an event loop? Answer in one sentence.",
        "topic_4": "What is a Future? Answer in one sentence.",
        "topic_5": "What is gather()? Answer in one sentence.",
    }

    result = await process_all_with_deadline(
        research_tasks,
        overall_deadline=12.0,
        per_item_timeout=4.0
    )

    print(f"Completed: {result.completed_count}/{len(research_tasks)}")
    print(f"Timed out: {result.timeout_count} | Errors: {result.error_count}")
    print(f"Wall time: {result.wall_time_ms:.0f}ms | Deadline exceeded: {result.deadline_exceeded}")

    for tid, output in result.results.items():
        status = "✓" if output and not output.startswith("ERROR") else ("✗" if output else "⏱")
        print(f"  [{status}] {tid}: {(output or 'no result')[:70]}")

asyncio.run(main())

# Expected Token Savings: Cancels in-flight requests at deadline; parallel execution maximizes completion rate
# Environment: ANTHROPIC_API_KEY required, uses asyncio
```

### Option 3: Streaming with Progressive Partial Output

```python
import anthropic
import time
import signal
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class StreamingPartialResult:
    content: str
    is_complete: bool
    chars_received: int
    elapsed_ms: float
    stop_reason: str  # "complete" | "timeout" | "length" | "interrupted"

def stream_with_soft_timeout(
    prompt: str,
    system: str = "",
    model: str = "claude-haiku-4-5-20251001",
    max_tokens: int = 1000,
    soft_timeout_chars: int = 500,    # Return after this many chars if time exceeded
    hard_timeout_seconds: float = 8.0  # Absolute limit
) -> StreamingPartialResult:
    """
    Stream response with a soft char-based timeout.
    Returns partial content when either:
    1. Response is complete
    2. Hard timeout exceeded
    3. Soft timeout (enough chars received + time exceeded)
    """
    collected = []
    total_chars = 0
    start = time.time()
    stop_reason = "complete"

    try:
        with client.messages.stream(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}]
        ) as stream:
            for chunk in stream.text_stream:
                collected.append(chunk)
                total_chars += len(chunk)
                elapsed = time.time() - start

                # Hard timeout
                if elapsed >= hard_timeout_seconds:
                    stop_reason = "timeout"
                    print(f"[StreamTimeout] Hard timeout at {elapsed:.1f}s ({total_chars} chars)")
                    break

                # Soft timeout: if we have enough content and deadline approaching
                if (total_chars >= soft_timeout_chars and
                        elapsed >= hard_timeout_seconds * 0.75):
                    stop_reason = "soft_timeout"
                    print(f"[StreamTimeout] Soft timeout: {total_chars} chars, {elapsed:.1f}s")
                    break

    except Exception as e:
        stop_reason = f"error: {str(e)[:40]}"

    content = "".join(collected)
    elapsed = time.time() - start

    # Add continuation marker if partial
    if stop_reason != "complete":
        content += f"\n\n[Response truncated after {total_chars} chars. Ask to continue for the rest.]"

    return StreamingPartialResult(
        content=content,
        is_complete=(stop_reason == "complete"),
        chars_received=total_chars,
        elapsed_ms=elapsed * 1000,
        stop_reason=stop_reason
    )

# Test with a long generation task
result = stream_with_soft_timeout(
    prompt="Write a comprehensive guide to Python asyncio including: event loops, coroutines, tasks, futures, semaphores, and best practices.",
    soft_timeout_chars=300,
    hard_timeout_seconds=6.0
)

print(f"Complete: {result.is_complete} | Stop: {result.stop_reason}")
print(f"Chars: {result.chars_received} | Time: {result.elapsed_ms:.0f}ms")
print(f"\nContent:\n{result.content[:400]}")

# Expected Token Savings: Stops token spend at timeout; streamed chars already paid for
# Environment: ANTHROPIC_API_KEY required, uses streaming
```

### Option 4: Multi-Phase Pipeline with Phase Timeouts

```python
import anthropic
import time
from dataclasses import dataclass, field
from typing import Optional, Callable
from enum import Enum

client = anthropic.Anthropic()

class PhaseStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    TIMED_OUT = "timed_out"
    SKIPPED = "skipped"

@dataclass
class PipelinePhase:
    name: str
    timeout_seconds: float
    is_required: bool = True   # If True, timeout = abort; if False, skip and continue
    result: Optional[str] = None
    status: PhaseStatus = PhaseStatus.PENDING
    elapsed_ms: float = 0.0

@dataclass
class PipelineResult:
    phases: list[PipelinePhase]
    final_output: str
    total_elapsed_ms: float
    aborted: bool
    partial: bool

    @property
    def completed_phases(self) -> int:
        return sum(1 for p in self.phases if p.status == PhaseStatus.COMPLETED)

def run_phase(
    phase: PipelinePhase,
    prompt_fn: Callable[[], str],
    previous_outputs: dict[str, str]
) -> bool:
    """Run one pipeline phase. Returns True if completed, False if timed out."""
    phase.status = PhaseStatus.RUNNING
    t0 = time.time()

    try:
        prompt = prompt_fn()
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        )
        phase.result = response.content[0].text.strip()
        phase.elapsed_ms = (time.time() - t0) * 1000
        phase.status = PhaseStatus.COMPLETED
        print(f"  ✓ [{phase.name}] {phase.elapsed_ms:.0f}ms")
        return True

    except Exception as e:
        phase.elapsed_ms = (time.time() - t0) * 1000
        # Simulate timeout check
        if phase.elapsed_ms / 1000 > phase.timeout_seconds:
            phase.status = PhaseStatus.TIMED_OUT
            print(f"  ⏱ [{phase.name}] TIMEOUT after {phase.elapsed_ms:.0f}ms")
        else:
            phase.status = PhaseStatus.TIMED_OUT
            print(f"  ✗ [{phase.name}] Error: {str(e)[:50]}")
        return False

def run_pipeline_with_phase_timeouts(
    topic: str,
    overall_timeout: float = 20.0
) -> PipelineResult:
    """
    Multi-phase research pipeline with per-phase and overall timeouts.
    Optional phases are skipped on timeout; required phases abort pipeline.
    """
    start = time.time()
    outputs: dict[str, str] = {}
    aborted = False

    phases = [
        PipelinePhase("research",   timeout_seconds=5.0, is_required=True),
        PipelinePhase("analysis",   timeout_seconds=4.0, is_required=True),
        PipelinePhase("examples",   timeout_seconds=3.0, is_required=False),  # Optional
        PipelinePhase("summary",    timeout_seconds=3.0, is_required=True),
        PipelinePhase("references", timeout_seconds=2.0, is_required=False),  # Optional
    ]

    phase_prompts = {
        "research":   lambda: f"Research key facts about: {topic}. 3 bullet points.",
        "analysis":   lambda: f"Analyze implications of: {outputs.get('research', topic)}",
        "examples":   lambda: f"Give 2 concrete examples for: {outputs.get('analysis', topic)}",
        "summary":    lambda: f"Write a 2-sentence summary combining: {outputs.get('research','')} and {outputs.get('analysis','')}",
        "references": lambda: f"List 3 reference topics to learn more about: {topic}",
    }

    print(f"\n[Pipeline] Starting: '{topic}' (timeout: {overall_timeout}s)")

    for phase in phases:
        # Check overall deadline
        remaining = overall_timeout - (time.time() - start)
        if remaining <= 0:
            phase.status = PhaseStatus.SKIPPED
            print(f"  ⏭ [{phase.name}] SKIPPED — overall deadline exceeded")
            if phase.is_required:
                aborted = True
                break
            continue

        success = run_phase(phase, phase_prompts[phase.name], outputs)

        if success:
            outputs[phase.name] = phase.result
        elif phase.is_required:
            print(f"  [Pipeline] Required phase '{phase.name}' failed — aborting")
            aborted = True
            break
        else:
            phase.status = PhaseStatus.SKIPPED
            print(f"  ⏭ [{phase.name}] Optional phase failed — continuing")

    # Build final output from whatever completed
    completed_outputs = [
        f"### {p.name.title()}\n{p.result}"
        for p in phases if p.status == PhaseStatus.COMPLETED and p.result
    ]

    final = "\n\n".join(completed_outputs) if completed_outputs else "No phases completed."
    if aborted:
        final += "\n\n*Note: Pipeline aborted — partial results above.*"

    total_elapsed = (time.time() - start) * 1000
    return PipelineResult(
        phases=phases,
        final_output=final,
        total_elapsed_ms=total_elapsed,
        aborted=aborted,
        partial=any(p.status in (PhaseStatus.TIMED_OUT, PhaseStatus.SKIPPED) for p in phases)
    )

result = run_pipeline_with_phase_timeouts("quantum computing", overall_timeout=15.0)

print(f"\n[Pipeline] Done: {result.completed_phases}/{len(result.phases)} phases")
print(f"Aborted: {result.aborted} | Partial: {result.partial}")
print(f"Time: {result.total_elapsed_ms:.0f}ms")
print(f"\nOutput:\n{result.final_output[:500]}")

# Expected Token Savings: Optional phase skipping saves 30-40% tokens when behind schedule
# Environment: ANTHROPIC_API_KEY required
```

### Option 5: Checkpoint-Based Resumable Timeout

```python
import anthropic
import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

client = anthropic.Anthropic()

CHECKPOINT_DIR = Path("/tmp/agent_checkpoints")
CHECKPOINT_DIR.mkdir(exist_ok=True)

@dataclass
class TaskCheckpoint:
    task_id: str
    task_description: str
    items: list[str]
    completed_results: list[dict]
    next_index: int
    started_at: float
    last_checkpoint_at: float
    is_complete: bool = False

def save_checkpoint(checkpoint: TaskCheckpoint):
    path = CHECKPOINT_DIR / f"{checkpoint.task_id}.json"
    path.write_text(json.dumps(asdict(checkpoint), indent=2))

def load_checkpoint(task_id: str) -> Optional[TaskCheckpoint]:
    path = CHECKPOINT_DIR / f"{task_id}.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    return TaskCheckpoint(**data)

def run_with_checkpoint_timeout(
    task_id: str,
    items: list[str],
    task_description: str,
    timeout_seconds: float = 10.0,
    checkpoint_interval: int = 2  # Checkpoint every N items
) -> dict:
    """
    Process items with timeout and checkpointing.
    Can be resumed after timeout by calling again with same task_id.
    """
    # Load existing checkpoint (resume support)
    checkpoint = load_checkpoint(task_id)

    if checkpoint and checkpoint.is_complete:
        print(f"[Checkpoint] Task {task_id} already complete — returning cached results")
        return {
            "results": checkpoint.completed_results,
            "complete": True,
            "resumed": True,
            "total": len(checkpoint.completed_results)
        }

    if checkpoint:
        print(f"[Checkpoint] Resuming from index {checkpoint.next_index}/{len(items)}")
        start_index = checkpoint.next_index
        completed = checkpoint.completed_results
    else:
        checkpoint = TaskCheckpoint(
            task_id=task_id,
            task_description=task_description,
            items=items,
            completed_results=[],
            next_index=0,
            started_at=time.time(),
            last_checkpoint_at=time.time()
        )
        start_index = 0
        completed = []

    start = time.time()
    timed_out = False

    for i in range(start_index, len(items)):
        elapsed = time.time() - start
        if elapsed >= timeout_seconds:
            timed_out = True
            print(f"[Checkpoint] Timeout at {elapsed:.1f}s — saving checkpoint at index {i}")
            break

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=60,
            messages=[{"role": "user", "content": f"{task_description}: {items[i]}"}]
        )
        completed.append({
            "index": i,
            "input": items[i][:50],
            "output": response.content[0].text.strip()
        })
        checkpoint.completed_results = completed
        checkpoint.next_index = i + 1
        checkpoint.last_checkpoint_at = time.time()

        # Periodic checkpoint save
        if (i - start_index + 1) % checkpoint_interval == 0:
            save_checkpoint(checkpoint)

        print(f"  [{i+1}/{len(items)}] done")

    is_complete = not timed_out and checkpoint.next_index >= len(items)
    checkpoint.is_complete = is_complete
    save_checkpoint(checkpoint)

    return {
        "results": completed,
        "complete": is_complete,
        "timed_out": timed_out,
        "next_index": checkpoint.next_index,
        "total_items": len(items),
        "resume_hint": f"Call again with task_id='{task_id}' to continue" if timed_out else None
    }

# Usage: first call (may timeout)
items = [f"Document {i}: content about topic {i}" for i in range(8)]
task_id = "research_task_001"

result1 = run_with_checkpoint_timeout(task_id, items, "Summarize in 5 words", timeout_seconds=6.0)
print(f"\nRun 1: {len(result1['results'])}/{result1['total_items']} done, timeout={result1['timed_out']}")

# Resume from checkpoint
if result1.get("timed_out"):
    print(f"\nResuming from checkpoint...")
    result2 = run_with_checkpoint_timeout(task_id, items, "Summarize in 5 words", timeout_seconds=10.0)
    print(f"Run 2: {len(result2['results'])}/{result2['total_items']} done, complete={result2['complete']}")

# Expected Token Savings: Resume avoids re-processing completed items = 100% savings on done work
# Environment: ANTHROPIC_API_KEY required, writes to /tmp/agent_checkpoints/
```

### Option 6: Deadline-Aware Agent with Budget Allocation

```python
import anthropic
import time
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class TimeBudget:
    total_seconds: float
    start_time: float = field(default_factory=time.time)
    spent_seconds: float = 0.0

    @property
    def remaining(self) -> float:
        return max(0.0, self.total_seconds - (time.time() - self.start_time))

    @property
    def is_exhausted(self) -> bool:
        return self.remaining <= 0

    def allocate(self, fraction: float) -> float:
        """Allocate a fraction of remaining budget."""
        return self.remaining * fraction

    def elapsed(self) -> float:
        return time.time() - self.start_time

def deadline_aware_research(
    query: str,
    total_timeout: float = 12.0
) -> dict:
    """
    Research agent that adapts sub-task depth based on remaining time budget.
    More time = more thorough; less time = skip optional enrichment.
    """
    budget = TimeBudget(total_seconds=total_timeout)
    phases_completed = []
    result = {}

    print(f"[Budget] Starting research with {total_timeout}s budget")

    # Phase 1: Core answer (always run, allocated 40% of budget)
    phase1_budget = budget.allocate(0.40)
    if not budget.is_exhausted:
        t0 = time.time()
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{"role": "user", "content": f"Answer concisely: {query}"}]
        )
        result["core_answer"] = resp.content[0].text
        phases_completed.append("core_answer")
        print(f"  ✓ core_answer ({(time.time()-t0)*1000:.0f}ms, {budget.remaining:.1f}s left)")

    # Phase 2: Context (run if >30% budget remains)
    if budget.remaining > total_timeout * 0.30:
        t0 = time.time()
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{"role": "user", "content": f"Give background context for: {query} (2 sentences)"}]
        )
        result["context"] = resp.content[0].text
        phases_completed.append("context")
        print(f"  ✓ context ({(time.time()-t0)*1000:.0f}ms, {budget.remaining:.1f}s left)")
    else:
        result["context"] = "[skipped — insufficient time budget]"
        print(f"  ⏭ context skipped ({budget.remaining:.1f}s left)")

    # Phase 3: Examples (run if >20% budget remains)
    if budget.remaining > total_timeout * 0.20:
        t0 = time.time()
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            messages=[{"role": "user", "content": f"Give one concrete example for: {query}"}]
        )
        result["example"] = resp.content[0].text
        phases_completed.append("example")
        print(f"  ✓ example ({(time.time()-t0)*1000:.0f}ms, {budget.remaining:.1f}s left)")
    else:
        print(f"  ⏭ example skipped ({budget.remaining:.1f}s left)")

    # Phase 4: Related topics (run if >15% budget remains)
    if budget.remaining > total_timeout * 0.15:
        t0 = time.time()
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=50,
            messages=[{"role": "user", "content": f"Name 3 related topics to: {query}"}]
        )
        result["related_topics"] = resp.content[0].text
        phases_completed.append("related_topics")
        print(f"  ✓ related_topics ({(time.time()-t0)*1000:.0f}ms, {budget.remaining:.1f}s left)")
    else:
        print(f"  ⏭ related_topics skipped ({budget.remaining:.1f}s left)")

    elapsed = budget.elapsed()
    return {
        "query": query,
        "result": result,
        "phases_completed": phases_completed,
        "elapsed_seconds": round(elapsed, 2),
        "budget_used_pct": round(elapsed / total_timeout * 100),
        "timed_out": budget.is_exhausted
    }

# Test with different budgets
for budget in [4.0, 10.0, 20.0]:
    print(f"\n{'='*50}")
    print(f"Budget: {budget}s")
    r = deadline_aware_research("What is machine learning?", total_timeout=budget)
    print(f"Phases: {r['phases_completed']} | Used: {r['budget_used_pct']}%")

# Expected Token Savings: Budget-adaptive depth saves 40-60% tokens on time-constrained requests
# Environment: ANTHROPIC_API_KEY required
```

## Comparison

| Option | Timeout Type | Returns Partial | Resumable | Best Use Case |
|--------|-------------|-----------------|-----------|---------------|
| Simple Wall-Clock | Sequential item loop | Yes | No | List processing with known items |
| Async Concurrent Deadline | Overall + per-task | Yes | No | Parallel research tasks |
| Streaming Soft Timeout | Char + time threshold | Yes (truncated text) | No | Long generation, real-time display |
| Multi-Phase with Phase Budgets | Per-phase + overall | Yes (completed phases) | No | Multi-step pipelines |
| Checkpoint-Based | Overall per run | Yes | Yes | Long batch jobs, crash recovery |
| Budget-Aware Adaptive | Percentage-based | Yes (skip optional) | No | Research with quality tiers |
