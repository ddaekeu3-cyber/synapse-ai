---
layout: solution
title: "Agent Doesn't Implement Graceful Partial Result Return on Timeout"
category: reliability
description: "When a deadline is hit mid-processing, return the best partial result computed so far instead of failing with an empty error — preserving user value and agent trust."
tags: [reliability, timeout, partial-results, graceful-degradation, deadline, async]
---

## Problem

Agents working on complex multi-step tasks frequently hit timeouts mid-way through. The naive response is to raise a `TimeoutError` and return nothing. The user gets zero value from a task that was 80% complete. A better pattern: checkpoint intermediate results, and when the deadline hits, return the best available partial result with a clear annotation that it is incomplete.

```python
# Naive: all-or-nothing — timeout means complete failure
async def analyze_all_documents(docs: list[str], timeout: float = 30.0) -> list[str]:
    async with asyncio.timeout(timeout):
        results = []
        for doc in docs:
            result = await analyze_one(doc)
            results.append(result)
        return results  # TimeoutError if deadline hit — user gets nothing
```

## Solution Options

### Option 1: Checkpoint-Based Partial Collection

After each completed subtask, save the result to a checkpoint list. On timeout, return all checkpointed results with a completion flag.

```python
import anthropic
import asyncio
from dataclasses import dataclass, field

@dataclass
class PartialResult:
    completed: list[str]
    pending_count: int
    is_complete: bool
    completion_pct: float

    def __str__(self) -> str:
        status = "complete" if self.is_complete else f"partial ({self.completion_pct:.0f}%)"
        return f"[{status}] {len(self.completed)} results | {self.pending_count} pending"

client = anthropic.AsyncAnthropic()

async def analyze_one(doc: str) -> str:
    r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": f"Summarize in one sentence: {doc}"}],
    )
    return r.content[0].text

async def analyze_with_partial_return(
    documents: list[str],
    timeout_seconds: float = 10.0,
) -> PartialResult:
    completed: list[str] = []
    total = len(documents)

    async def process_all() -> None:
        for doc in documents:
            result = await analyze_one(doc)
            completed.append(result)

    try:
        await asyncio.wait_for(process_all(), timeout=timeout_seconds)
        return PartialResult(
            completed=completed,
            pending_count=0,
            is_complete=True,
            completion_pct=100.0,
        )
    except asyncio.TimeoutError:
        pending = total - len(completed)
        pct = (len(completed) / total) * 100 if total > 0 else 0
        print(f"[TIMEOUT] Returning partial: {len(completed)}/{total} docs ({pct:.0f}%)")
        return PartialResult(
            completed=completed,
            pending_count=pending,
            is_complete=False,
            completion_pct=pct,
        )


async def main():
    docs = [
        "Python is a high-level, interpreted programming language known for its simplicity.",
        "Asyncio enables concurrent I/O-bound operations using coroutines.",
        "Type hints in Python improve code readability and enable static analysis.",
        "Dataclasses reduce boilerplate for classes that primarily store data.",
        "Context managers ensure proper resource cleanup using the with statement.",
    ]
    result = await analyze_with_partial_return(docs, timeout_seconds=5.0)
    print(result)
    for i, r in enumerate(result.completed):
        print(f"  [{i+1}] {r[:80]}")
    if not result.is_complete:
        print(f"  ... {result.pending_count} documents not processed due to timeout")

asyncio.run(main())

# Expected Token Savings: No extra tokens; partial return preserves already-spent token value
# Environment: ANTHROPIC_API_KEY
```

### Option 2: Deadline Propagation with Per-Subtask Remaining Time

Pass a deadline timestamp down to each subtask. Each subtask checks remaining time before starting and computes a dynamically adjusted `max_tokens` to fit within the remaining budget.

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass

@dataclass
class DeadlineContext:
    deadline: float    # absolute timestamp

    def remaining_seconds(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def is_expired(self) -> bool:
        return time.monotonic() >= self.deadline

    def child(self, fraction: float = 1.0) -> "DeadlineContext":
        """Create child deadline using a fraction of remaining time."""
        remaining = self.remaining_seconds()
        return DeadlineContext(deadline=time.monotonic() + remaining * fraction)

@dataclass
class SubtaskResult:
    index: int
    content: str
    completed: bool
    skipped_reason: str = ""

client = anthropic.AsyncAnthropic()

async def analyze_subtask(
    index: int,
    content: str,
    ctx: DeadlineContext,
    min_time_needed: float = 1.0,
) -> SubtaskResult:
    if ctx.remaining_seconds() < min_time_needed:
        return SubtaskResult(
            index=index, content="", completed=False,
            skipped_reason=f"Insufficient time ({ctx.remaining_seconds():.1f}s remaining)"
        )
    # Scale max_tokens to time remaining
    max_tokens = max(32, min(256, int(ctx.remaining_seconds() * 20)))
    try:
        r = await asyncio.wait_for(
            client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": f"Summarize: {content}"}],
            ),
            timeout=ctx.remaining_seconds() - 0.2,  # 200ms safety margin
        )
        return SubtaskResult(index=index, content=r.content[0].text, completed=True)
    except asyncio.TimeoutError:
        return SubtaskResult(index=index, content="", completed=False, skipped_reason="Timed out")

async def deadline_propagated_analysis(
    items: list[str],
    total_timeout: float = 8.0,
) -> dict:
    ctx = DeadlineContext(deadline=time.monotonic() + total_timeout)
    results: list[SubtaskResult] = []
    for i, item in enumerate(items):
        if ctx.is_expired():
            # Skip remaining without calling API
            for j in range(i, len(items)):
                results.append(SubtaskResult(j, "", False, "Deadline passed"))
            break
        result = await analyze_subtask(i, item, ctx)
        results.append(result)
        print(f"[{i+1}/{len(items)}] completed={result.completed} remaining={ctx.remaining_seconds():.1f}s")

    completed = [r for r in results if r.completed]
    skipped = [r for r in results if not r.completed]
    return {
        "completed": [r.content for r in completed],
        "skipped_count": len(skipped),
        "completion_rate": f"{len(completed)}/{len(results)}",
        "is_complete": len(skipped) == 0,
    }


async def main():
    items = [
        "Machine learning is a subset of artificial intelligence.",
        "Neural networks are inspired by the human brain structure.",
        "Deep learning uses multiple layers of neural networks.",
        "Transformers are a neural architecture using attention mechanisms.",
        "Large language models are trained on vast amounts of text data.",
    ]
    result = await deadline_propagated_analysis(items, total_timeout=6.0)
    print(f"\nCompletion: {result['completion_rate']} | complete={result['is_complete']}")
    for i, r in enumerate(result["completed"]):
        print(f"  [{i+1}] {r[:80]}")

asyncio.run(main())

# Expected Token Savings: Skipped subtasks consume 0 tokens; dynamic max_tokens prevents over-spend near deadline
# Environment: ANTHROPIC_API_KEY
```

### Option 3: Async Task Group with Best-Effort Collection

Run all subtasks concurrently. Collect whatever completes before the deadline. Return completed results immediately without waiting for stragglers.

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass

@dataclass
class BestEffortResult:
    total_requested: int
    completed: list[tuple[int, str]]  # (index, result)
    timed_out_indices: list[int]
    wall_time_seconds: float

    def ordered_results(self) -> list[str | None]:
        lookup = dict(self.completed)
        return [lookup.get(i) for i in range(self.total_requested)]

client = anthropic.AsyncAnthropic()

async def best_effort_parallel(
    queries: list[str],
    deadline_seconds: float = 8.0,
    model: str = "claude-haiku-4-5-20251001",
) -> BestEffortResult:
    t0 = time.monotonic()
    deadline = t0 + deadline_seconds

    async def run_one(index: int, query: str) -> tuple[int, str] | None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            r = await asyncio.wait_for(
                client.messages.create(
                    model=model,
                    max_tokens=200,
                    messages=[{"role": "user", "content": query}],
                ),
                timeout=remaining,
            )
            return index, r.content[0].text
        except asyncio.TimeoutError:
            return None

    tasks = [asyncio.create_task(run_one(i, q)) for i, q in enumerate(queries)]
    # Wait for the deadline, collect whatever finished
    done, pending = await asyncio.wait(
        tasks,
        timeout=deadline_seconds,
        return_when=asyncio.ALL_COMPLETED,
    )
    # Cancel stragglers
    for t in pending:
        t.cancel()
    await asyncio.gather(*pending, return_exceptions=True)

    completed = []
    timed_out = []
    for i, task in enumerate(tasks):
        if task.done() and not task.cancelled() and task.exception() is None:
            result = task.result()
            if result is not None:
                completed.append(result)
            else:
                timed_out.append(i)
        else:
            timed_out.append(i)

    return BestEffortResult(
        total_requested=len(queries),
        completed=completed,
        timed_out_indices=timed_out,
        wall_time_seconds=time.monotonic() - t0,
    )


async def main():
    queries = [
        "What is Python's GIL and why does it exist?",
        "Explain the difference between threads and coroutines.",
        "What are Python generators and how do they work?",
        "Describe the asyncio event loop architecture.",
        "What is a context manager and how do you implement one?",
    ]
    result = await best_effort_parallel(queries, deadline_seconds=6.0)
    print(f"Completed: {len(result.completed)}/{result.total_requested} in {result.wall_time_seconds:.1f}s")
    if result.timed_out_indices:
        print(f"Timed out: queries {result.timed_out_indices}")
    print("\nOrdered results (None = timed out):")
    for i, r in enumerate(result.ordered_results()):
        status = r[:80] if r else "[TIMED OUT — no result]"
        print(f"  [{i+1}] {status}")

asyncio.run(main())

# Expected Token Savings: Straggler cancellation prevents wasted tokens on over-time calls; parallel maximizes value per second
# Environment: ANTHROPIC_API_KEY
```

### Option 4: Progressive Quality Degradation on Approaching Deadline

As the deadline approaches, automatically downgrade the request — reducing max_tokens, switching to a faster model, or simplifying the prompt — to ensure some result is returned.

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass
from enum import Enum

class QualityTier(Enum):
    FULL = "full"          # > 70% time remaining
    REDUCED = "reduced"    # 30-70% time remaining
    MINIMAL = "minimal"    # 10-30% time remaining
    EMERGENCY = "emergency" # < 10% time remaining

@dataclass
class AdaptiveConfig:
    model: str
    max_tokens: int
    prompt_modifier: str
    tier: QualityTier

def select_quality_tier(remaining_fraction: float) -> AdaptiveConfig:
    if remaining_fraction > 0.70:
        return AdaptiveConfig(
            model="claude-sonnet-4-6", max_tokens=512,
            prompt_modifier="",
            tier=QualityTier.FULL,
        )
    elif remaining_fraction > 0.30:
        return AdaptiveConfig(
            model="claude-haiku-4-5-20251001", max_tokens=256,
            prompt_modifier=" Be concise.",
            tier=QualityTier.REDUCED,
        )
    elif remaining_fraction > 0.10:
        return AdaptiveConfig(
            model="claude-haiku-4-5-20251001", max_tokens=100,
            prompt_modifier=" Answer in 1-2 sentences only.",
            tier=QualityTier.MINIMAL,
        )
    else:
        return AdaptiveConfig(
            model="claude-haiku-4-5-20251001", max_tokens=40,
            prompt_modifier=" One sentence answer only.",
            tier=QualityTier.EMERGENCY,
        )

client = anthropic.AsyncAnthropic()

@dataclass
class AdaptiveResult:
    content: str
    quality_tier: QualityTier
    remaining_fraction_at_call: float
    tokens_used: int

async def adaptive_deadline_call(
    prompt: str,
    deadline: float,
    total_duration: float,
) -> AdaptiveResult:
    remaining = deadline - time.monotonic()
    remaining_fraction = remaining / max(total_duration, 0.001)
    config = select_quality_tier(remaining_fraction)
    print(f"[ADAPTIVE] tier={config.tier.value} remaining={remaining:.1f}s fraction={remaining_fraction:.0%}")

    r = await asyncio.wait_for(
        client.messages.create(
            model=config.model,
            max_tokens=config.max_tokens,
            messages=[{"role": "user", "content": prompt + config.prompt_modifier}],
        ),
        timeout=max(remaining - 0.1, 0.5),
    )
    return AdaptiveResult(
        content=r.content[0].text,
        quality_tier=config.tier,
        remaining_fraction_at_call=remaining_fraction,
        tokens_used=r.usage.input_tokens + r.usage.output_tokens,
    )

async def multi_step_with_degradation(steps: list[str], total_timeout: float = 10.0):
    start = time.monotonic()
    deadline = start + total_timeout
    results = []
    for i, step in enumerate(steps):
        remaining = deadline - time.monotonic()
        if remaining < 0.3:
            print(f"[SKIP] Step {i+1} skipped — deadline imminent")
            results.append(AdaptiveResult("[skipped]", QualityTier.EMERGENCY, 0, 0))
            continue
        try:
            result = await adaptive_deadline_call(step, deadline, total_timeout)
            results.append(result)
            print(f"  Step {i+1}/{len(steps)}: {result.quality_tier.value} | {result.content[:80]}")
        except asyncio.TimeoutError:
            results.append(AdaptiveResult("[timeout]", QualityTier.EMERGENCY, 0, 0))
    return results

async def main():
    steps = [
        "Summarize the key benefits of Python for data science.",
        "List the top 3 Python data science libraries and their uses.",
        "Explain what a DataFrame is in the context of pandas.",
        "Describe what NumPy arrays are used for.",
        "What is matplotlib used for in data science?",
    ]
    results = await multi_step_with_degradation(steps, total_timeout=8.0)
    tiers = [r.quality_tier.value for r in results]
    print(f"\nQuality tiers: {tiers}")
    total_tokens = sum(r.tokens_used for r in results)
    print(f"Total tokens used: {total_tokens}")

asyncio.run(main())

# Expected Token Savings: Degraded calls use 4-8× fewer tokens; emergency tier gives minimum viable result at minimal cost
# Environment: ANTHROPIC_API_KEY
```

### Option 5: Streaming Partial Response with Forced Finalization

Use streaming to capture tokens as they arrive. If the deadline hits mid-stream, inject a forced finalization prompt to get a syntactically complete partial response.

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass

@dataclass
class StreamResult:
    content: str
    is_complete: bool
    was_forcefully_finalized: bool
    tokens_captured: int

client = anthropic.AsyncAnthropic()

async def streaming_with_deadline(
    prompt: str,
    deadline_seconds: float = 5.0,
    finalization_buffer_seconds: float = 0.8,
) -> StreamResult:
    deadline = time.monotonic() + deadline_seconds
    finalization_deadline = deadline - finalization_buffer_seconds
    collected_text = ""
    was_finalized = False
    is_complete = False

    try:
        async with client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            async for text in stream.text_stream:
                collected_text += text
                now = time.monotonic()

                # Approaching deadline — try to gracefully stop
                if now >= finalization_deadline and not was_finalized:
                    was_finalized = True
                    print(f"[DEADLINE] Approaching — forcing finalization after {len(collected_text)} chars")
                    # The stream will naturally complete; we stop processing further
                    break

                if now >= deadline:
                    break

            # Check if stream completed naturally
            try:
                msg = await asyncio.wait_for(stream.get_final_message(), timeout=0.1)
                is_complete = msg.stop_reason == "end_turn"
            except (asyncio.TimeoutError, Exception):
                is_complete = False

    except asyncio.TimeoutError:
        is_complete = False

    # Ensure text ends at a sentence boundary if truncated
    if not is_complete and collected_text:
        last_period = max(
            collected_text.rfind(". "),
            collected_text.rfind(".\n"),
            collected_text.rfind("! "),
            collected_text.rfind("? "),
        )
        if last_period > len(collected_text) // 2:
            collected_text = collected_text[:last_period + 1]
        collected_text += " [response truncated at deadline]"

    return StreamResult(
        content=collected_text,
        is_complete=is_complete,
        was_forcefully_finalized=was_finalized,
        tokens_captured=len(collected_text.split()),
    )


async def main():
    prompts = [
        "Write a detailed explanation of how asyncio event loops work in Python.",
        "Explain the history of Python and its major version milestones.",
    ]
    for prompt in prompts:
        print(f"\nPrompt: {prompt[:60]}")
        result = await streaming_with_deadline(prompt, deadline_seconds=3.0)
        print(f"Complete: {result.is_complete} | Finalized: {result.was_forcefully_finalized}")
        print(f"Content: {result.content[:300]}")

asyncio.run(main())

# Expected Token Savings: Streaming captures value proportional to time spent; no wasted tokens on dropped responses
# Environment: ANTHROPIC_API_KEY
```

### Option 6: Hierarchical Task Tree with Leaf-First Timeout Collection

Model the work as a tree of tasks. When timeout hits, complete any in-flight leaf tasks and collect all completed nodes — returning a structurally valid partial tree.

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class TaskNode:
    id: str
    prompt: str
    children: list["TaskNode"] = field(default_factory=list)
    result: Optional[str] = None
    completed: bool = False
    skipped: bool = False

    def completion_rate(self) -> float:
        all_nodes = self._all_nodes()
        completed = sum(1 for n in all_nodes if n.completed)
        return completed / max(len(all_nodes), 1)

    def _all_nodes(self) -> list["TaskNode"]:
        nodes = [self]
        for child in self.children:
            nodes.extend(child._all_nodes())
        return nodes

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "completed": self.completed,
            "result": (self.result[:80] + "...") if self.result and len(self.result) > 80 else self.result,
            "children": [c.to_dict() for c in self.children],
        }

client = anthropic.AsyncAnthropic()

async def execute_node(node: TaskNode, deadline: float) -> None:
    if time.monotonic() >= deadline:
        node.skipped = True
        return
    remaining = deadline - time.monotonic()
    try:
        r = await asyncio.wait_for(
            client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=100,
                messages=[{"role": "user", "content": node.prompt}],
            ),
            timeout=max(remaining - 0.1, 0.2),
        )
        node.result = r.content[0].text
        node.completed = True
        # Execute children concurrently after parent completes
        if node.children and time.monotonic() < deadline:
            await asyncio.gather(*[execute_node(child, deadline) for child in node.children])
    except (asyncio.TimeoutError, Exception) as e:
        node.skipped = True
        print(f"[SKIP] {node.id}: {type(e).__name__}")

async def execute_tree_with_timeout(root: TaskNode, timeout_seconds: float) -> TaskNode:
    deadline = time.monotonic() + timeout_seconds
    try:
        await asyncio.wait_for(execute_node(root, deadline), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        pass
    rate = root.completion_rate()
    print(f"[TREE] Completion rate: {rate:.0%}")
    return root


async def main():
    # Build a task tree: analyze Python ecosystem
    root = TaskNode("root", "Describe Python's ecosystem in one sentence.")
    root.children = [
        TaskNode("web", "Name the top Python web frameworks in one sentence."),
        TaskNode("data", "Name the top Python data science libraries in one sentence."),
        TaskNode("devops", "Name popular Python DevOps tools in one sentence."),
    ]
    root.children[0].children = [
        TaskNode("fastapi", "Describe FastAPI in one sentence."),
        TaskNode("django", "Describe Django in one sentence."),
    ]
    root.children[1].children = [
        TaskNode("pandas", "Describe pandas in one sentence."),
        TaskNode("numpy", "Describe NumPy in one sentence."),
    ]

    result = await execute_tree_with_timeout(root, timeout_seconds=8.0)
    import json
    print(json.dumps(result.to_dict(), indent=2))

asyncio.run(main())

# Expected Token Savings: Skipped nodes consume 0 tokens; completed subtree returned with full value
# Environment: ANTHROPIC_API_KEY
```

## Comparison

| Option | Approach | Parallelism | Result Structure | Best For |
|--------|----------|------------|-----------------|----------|
| 1. Checkpoint Collection | Sequential + checkpoint | No | Flat list | Simple sequential pipelines |
| 2. Deadline Propagation | Sequential + per-task budget | No | Flat list | Multi-step with variable subtask cost |
| 3. Best-Effort Parallel | Concurrent + deadline collect | Yes | Ordered with gaps | Independent parallel subtasks |
| 4. Progressive Degradation | Sequential + quality downgrade | No | Flat list | Tasks requiring some answer always |
| 5. Streaming Finalization | Streaming + forced stop | No | Single response | Long single-response generation |
| 6. Task Tree | Hierarchical + leaf-first | Partial | Tree structure | Complex nested task graphs |

**Recommended**: Option 3 (best-effort parallel) for independent subtasks. Option 4 (progressive degradation) when a low-quality answer is always better than no answer. Option 2 (deadline propagation) for sequential multi-step pipelines.
