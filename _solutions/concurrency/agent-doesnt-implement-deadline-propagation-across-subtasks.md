---
layout: solution
title: "Agent Doesn't Implement Deadline Propagation Across Subtasks"
category: concurrency
description: "Agents that set a top-level timeout but don't propagate the remaining time budget into subtasks, tool calls, and parallel branches silently over-run their SLA — deadline propagation ensures every layer respects the clock."
tags: [concurrency, timeout, deadline, asyncio, context, distributed, sla]
---

# Agent Doesn't Implement Deadline Propagation Across Subtasks

## Problem

An agent sets a 30-second timeout at the top level. It then spawns three subtasks, each of which makes tool calls, model requests, and database lookups — all with their own internal timeouts of 20 seconds each. The total wall time blows past the 30-second SLA because each layer resets the clock. Without deadline propagation, the outer timeout has no teeth: subtasks don't know how much time is left on the budget, so they can't pre-empt themselves or choose faster paths when time is running short.

The correct pattern: pass a deadline timestamp (not a duration) through every layer. Each subtask checks the remaining budget before starting work and cancels immediately when the deadline is reached.

## Solutions

### Option 1: Deadline Context with asyncio

Pass a deadline timestamp through `contextvars.ContextVar`. Every subtask reads remaining time before starting and cancels if the clock is exhausted.

```python
import asyncio
import time
import anthropic
from contextvars import ContextVar
from typing import Optional

client = anthropic.AsyncAnthropic()

# ContextVar carries the deadline through the entire async call graph
_deadline: ContextVar[Optional[float]] = ContextVar("deadline", default=None)

def set_deadline(timeout_seconds: float) -> float:
    """Set a deadline relative to now. Returns the absolute deadline timestamp."""
    deadline = time.monotonic() + timeout_seconds
    _deadline.set(deadline)
    return deadline

def remaining_seconds() -> float:
    """How many seconds remain before the deadline. Returns inf if no deadline set."""
    d = _deadline.get()
    if d is None:
        return float("inf")
    return max(0.0, d - time.monotonic())

def deadline_exceeded() -> bool:
    return remaining_seconds() <= 0.0

async def await_with_deadline(coro, step_name: str = ""):
    """Wrap any coroutine with the current deadline budget."""
    remaining = remaining_seconds()
    if remaining <= 0:
        raise asyncio.TimeoutError(f"Deadline exceeded before starting '{step_name}'")
    try:
        return await asyncio.wait_for(coro, timeout=remaining)
    except asyncio.TimeoutError:
        raise asyncio.TimeoutError(f"Step '{step_name}' exceeded deadline (had {remaining:.2f}s)")

async def tool_call_with_deadline(tool_name: str, delay: float) -> str:
    """Simulate a tool call that respects the propagated deadline."""
    remaining = remaining_seconds()
    print(f"  [{tool_name}] starting, {remaining:.2f}s remaining on budget")
    await asyncio.sleep(delay)  # Simulate tool execution time
    return f"{tool_name} result"

async def model_call_with_deadline(prompt: str) -> str:
    remaining = remaining_seconds()
    if remaining < 2.0:
        raise asyncio.TimeoutError(f"Not enough time for model call ({remaining:.2f}s left)")

    async def _call():
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text

    return await await_with_deadline(_call(), "model_call")

async def subtask_a() -> str:
    result1 = await await_with_deadline(
        tool_call_with_deadline("search", 0.5), "search"
    )
    result2 = await await_with_deadline(
        tool_call_with_deadline("fetch_doc", 0.3), "fetch_doc"
    )
    return f"subtask_a: {result1}, {result2}"

async def subtask_b() -> str:
    return await await_with_deadline(
        model_call_with_deadline("Summarize key Python async patterns in one sentence."),
        "model_summarize"
    )

async def run_with_budget(total_timeout: float = 10.0) -> dict:
    set_deadline(total_timeout)
    start = time.monotonic()

    try:
        # Run subtasks respecting the shared deadline
        results = await asyncio.gather(
            await_with_deadline(subtask_a(), "subtask_a"),
            await_with_deadline(subtask_b(), "subtask_b"),
        )
        elapsed = time.monotonic() - start
        return {"status": "ok", "results": results, "elapsed": round(elapsed, 2)}
    except asyncio.TimeoutError as e:
        elapsed = time.monotonic() - start
        return {"status": "timeout", "error": str(e), "elapsed": round(elapsed, 2)}

result = asyncio.run(run_with_budget(total_timeout=8.0))
print(f"Status: {result['status']} | elapsed: {result['elapsed']}s")
if result.get("results"):
    for r in result["results"]:
        print(f"  {r}")
# Expected Token Savings: Avoids wasted model calls that would exceed SLA anyway
# Environment: Any async agent with multi-step pipelines and SLA requirements
```

### Option 2: Deadline Token Pattern for Sync Code

Pass a `DeadlineToken` object explicitly through function arguments — no contextvars needed, works with sync and async equally.

```python
import time
import anthropic
from dataclasses import dataclass, field
from typing import Optional

client = anthropic.Anthropic()

@dataclass
class DeadlineToken:
    """Immutable deadline token passed through the call graph."""
    deadline: float  # monotonic timestamp

    @classmethod
    def from_timeout(cls, seconds: float) -> "DeadlineToken":
        return cls(deadline=time.monotonic() + seconds)

    @classmethod
    def no_deadline(cls) -> "DeadlineToken":
        return cls(deadline=float("inf"))

    @property
    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    @property
    def exceeded(self) -> bool:
        return self.remaining <= 0.0

    def check(self, step: str = ""):
        if self.exceeded:
            raise TimeoutError(f"Deadline exceeded{f' at step: {step}' if step else ''}")

    def child(self, max_fraction: float = 1.0) -> "DeadlineToken":
        """Create a child token with at most max_fraction of remaining time."""
        child_deadline = time.monotonic() + self.remaining * max_fraction
        return DeadlineToken(deadline=min(self.deadline, child_deadline))

    def __repr__(self):
        return f"DeadlineToken(remaining={self.remaining:.2f}s)"

def call_tool(name: str, dl: DeadlineToken, simulated_latency: float = 0.2) -> str:
    dl.check(f"before_{name}")
    print(f"  Tool '{name}': {dl.remaining:.2f}s remaining")
    time.sleep(simulated_latency)
    dl.check(f"after_{name}")
    return f"{name}_result"

def call_model(prompt: str, dl: DeadlineToken, max_tokens: int = 100) -> str:
    dl.check("before_model_call")
    remaining = dl.remaining
    if remaining < 1.5:
        raise TimeoutError(f"Insufficient time for model call: {remaining:.2f}s")

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=min(max_tokens, 100),
        messages=[{"role": "user", "content": prompt}],
        timeout=min(remaining - 0.5, 30.0),  # Leave 0.5s margin
    )
    dl.check("after_model_call")
    return resp.content[0].text

def research_subtask(topic: str, dl: DeadlineToken) -> dict:
    """A subtask that uses a child deadline (max 60% of parent's budget)."""
    child_dl = dl.child(max_fraction=0.6)
    print(f"Research subtask: {child_dl.remaining:.2f}s budget")

    search_result = call_tool("search", child_dl, simulated_latency=0.1)
    summary = call_model(f"Summarize: {topic} in one sentence.", child_dl)

    return {"search": search_result, "summary": summary}

def formatting_subtask(content: str, dl: DeadlineToken) -> str:
    dl.check("formatting")
    result = call_tool("format", dl, simulated_latency=0.05)
    return f"formatted: {result}"

def run_agent_task(user_request: str, timeout_seconds: float = 10.0) -> dict:
    dl = DeadlineToken.from_timeout(timeout_seconds)
    start = time.monotonic()

    try:
        research = research_subtask(user_request, dl)
        dl.check("between_steps")
        formatted = formatting_subtask(research["summary"], dl)

        return {
            "status": "ok",
            "output": formatted,
            "elapsed": round(time.monotonic() - start, 2),
            "remaining": round(dl.remaining, 2),
        }
    except TimeoutError as e:
        return {
            "status": "timeout",
            "error": str(e),
            "elapsed": round(time.monotonic() - start, 2),
        }

result = run_agent_task("Python asyncio patterns", timeout_seconds=8.0)
print(f"Status: {result['status']} | elapsed: {result['elapsed']}s | remaining: {result.get('remaining', 0)}s")
if result.get("output"):
    print(f"Output: {result['output'][:100]}")
# Expected Token Savings: Prevents model calls launched when <1s remains (which always timeout)
# Environment: Sync Python agents, batch processors, CLI tools with timeout requirements
```

### Option 3: Parallel Subtasks with Shared Deadline and First-Complete Strategy

When running parallel subtasks, use the first successful result and cancel the rest — deadline-aware competitive execution.

```python
import asyncio
import time
import anthropic
from contextvars import ContextVar

client = anthropic.AsyncAnthropic()
_deadline: ContextVar[float] = ContextVar("deadline", default=float("inf"))

def remaining() -> float:
    return max(0.0, _deadline.get() - time.monotonic())

async def parallel_with_deadline(
    coroutines: list,
    names: list[str],
    strategy: str = "all",  # "all" or "first_success"
) -> list:
    deadline = _deadline.get()

    async def with_name(coro, name: str):
        rem = remaining()
        if rem <= 0:
            raise asyncio.TimeoutError(f"No time left for '{name}'")
        try:
            result = await asyncio.wait_for(coro, timeout=rem)
            return (name, "ok", result)
        except asyncio.TimeoutError:
            return (name, "timeout", None)
        except Exception as e:
            return (name, "error", str(e))

    tasks = [
        asyncio.create_task(with_name(coro, name))
        for coro, name in zip(coroutines, names)
    ]

    if strategy == "first_success":
        # Return first successful result, cancel others
        try:
            for future in asyncio.as_completed(tasks):
                name, status, result = await future
                if status == "ok":
                    for t in tasks:
                        t.cancel()
                    return [(name, "ok", result)]
        except asyncio.CancelledError:
            pass
        return [(n, "timeout", None) for n in names]

    # Default: wait for all with deadline
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return [(r if not isinstance(r, Exception) else (n, "error", str(r)))
            for r, n in zip(results, names)]

async def fetch_from_source(source_name: str, latency: float, prompt: str) -> str:
    await asyncio.sleep(latency)
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=80,
        messages=[{"role": "user", "content": f"[{source_name}] {prompt}"}],
    )
    return resp.content[0].text

async def multi_source_agent(question: str, total_timeout: float = 12.0) -> dict:
    _deadline.set(time.monotonic() + total_timeout)
    start = time.monotonic()

    # Run 3 parallel research paths; take first success
    research_tasks = [
        fetch_from_source("source_A", 0.5, question),
        fetch_from_source("source_B", 0.8, question),
        fetch_from_source("source_C", 0.3, question),
    ]
    research_names = ["source_A", "source_B", "source_C"]

    research_results = await parallel_with_deadline(
        research_tasks, research_names, strategy="all"
    )

    successful = [(n, r) for n, s, r in research_results if s == "ok"]
    timed_out = [n for n, s, _ in research_results if s == "timeout"]

    if not successful:
        return {"status": "timeout", "elapsed": round(time.monotonic() - start, 2)}

    # Synthesize with remaining budget
    combined_context = " | ".join(r for _, r in successful[:2])
    rem = remaining()
    print(f"Synthesis step: {rem:.2f}s remaining")

    if rem < 1.0:
        return {"status": "partial", "result": successful[0][1], "sources": [successful[0][0]]}

    synthesis = await asyncio.wait_for(
        client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{
                "role": "user",
                "content": f"Synthesize in one sentence: {combined_context}"
            }],
        ),
        timeout=rem - 0.3,
    )

    return {
        "status": "ok",
        "result": synthesis.content[0].text,
        "sources_used": [n for n, _ in successful],
        "sources_timed_out": timed_out,
        "elapsed": round(time.monotonic() - start, 2),
        "remaining": round(remaining(), 2),
    }

result = asyncio.run(multi_source_agent("What are Python coroutines?", total_timeout=10.0))
print(f"Status: {result['status']} | elapsed: {result['elapsed']}s")
if result.get("result"):
    print(f"Result: {result['result'][:150]}")
# Expected Token Savings: First-success pattern avoids redundant slow completions
# Environment: Multi-source retrieval, parallel model calls, competitive execution
```

### Option 4: gRPC-Style Deadline Header Propagation for HTTP Tool Calls

When tool calls go over HTTP, propagate the deadline as a header so downstream services can respect it too.

```python
import asyncio
import time
import httpx
import anthropic
import json
from typing import Any

client = anthropic.AsyncAnthropic()

DEADLINE_HEADER = "X-Request-Deadline"
REQUEST_ID_HEADER = "X-Request-Id"

class DeadlineAwareHTTPClient:
    """HTTP client that automatically sets deadline headers and respects them."""

    def __init__(self, base_url: str = "https://httpbin.org"):
        self.base_url = base_url
        self._deadline: float = float("inf")

    def set_deadline(self, seconds_from_now: float):
        self._deadline = time.monotonic() + seconds_from_now

    @property
    def remaining(self) -> float:
        return max(0.0, self._deadline - time.monotonic())

    async def get(self, path: str, **kwargs) -> dict:
        remaining = self.remaining
        if remaining <= 0:
            raise TimeoutError(f"Deadline exceeded before HTTP call to {path}")

        deadline_epoch = time.time() + remaining
        headers = {
            DEADLINE_HEADER: str(deadline_epoch),
            "Content-Type": "application/json",
            **kwargs.pop("headers", {}),
        }

        timeout = httpx.Timeout(min(remaining - 0.1, 30.0))

        async with httpx.AsyncClient(timeout=timeout) as http:
            try:
                resp = await http.get(
                    f"{self.base_url}{path}",
                    headers=headers,
                    **kwargs,
                )
                resp.raise_for_status()
                return resp.json()
            except httpx.TimeoutException:
                raise TimeoutError(f"HTTP call to {path} timed out (had {remaining:.2f}s)")

async def tool_get_json(http_client: DeadlineAwareHTTPClient, url_path: str) -> dict:
    """Tool wrapper that propagates deadline via HTTP headers."""
    print(f"  HTTP GET {url_path}: {http_client.remaining:.2f}s remaining")
    return await http_client.get(url_path)

TOOLS = [
    {
        "name": "http_get",
        "description": "Fetch data from an HTTP endpoint.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "URL path to fetch"}
            },
            "required": ["path"]
        }
    }
]

async def deadline_aware_agent(question: str, timeout_seconds: float = 15.0) -> dict:
    http_client = DeadlineAwareHTTPClient("https://httpbin.org")
    http_client.set_deadline(timeout_seconds)
    start = time.monotonic()

    messages = [{"role": "user", "content": question}]

    while True:
        remaining = http_client.remaining
        if remaining <= 0:
            return {"status": "timeout", "elapsed": round(time.monotonic() - start, 2)}

        try:
            resp = await asyncio.wait_for(
                client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=200,
                    tools=TOOLS,
                    messages=messages,
                ),
                timeout=min(remaining - 0.5, 30.0),
            )
        except asyncio.TimeoutError:
            return {"status": "timeout", "elapsed": round(time.monotonic() - start, 2)}

        if resp.stop_reason == "end_turn":
            answer = next((b.text for b in resp.content if hasattr(b, "text")), "")
            return {
                "status": "ok",
                "answer": answer,
                "elapsed": round(time.monotonic() - start, 2),
                "remaining": round(http_client.remaining, 2),
            }

        if resp.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": resp.content})
            tool_results = []
            for block in resp.content:
                if block.type == "tool_use":
                    try:
                        result = await tool_get_json(http_client, block.input.get("path", "/get"))
                    except TimeoutError as e:
                        result = {"error": str(e)}
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result)[:500],
                    })
            messages.append({"role": "user", "content": tool_results})

result = asyncio.run(deadline_aware_agent(
    "Fetch /get from httpbin and tell me the origin IP.",
    timeout_seconds=10.0,
))
print(f"Status: {result['status']} | elapsed={result.get('elapsed')}s")
if result.get("answer"):
    print(f"Answer: {result['answer'][:150]}")
# Expected Token Savings: Downstream services that respect deadline header avoid futile work
# Environment: Microservice tool backends, HTTP-based tool call pipelines
```

### Option 5: Hierarchical Budget Allocation

Divide the total time budget explicitly across phases (planning → execution → synthesis). Each phase gets a fixed fraction of the budget.

```python
import asyncio
import time
import anthropic
from dataclasses import dataclass

client = anthropic.AsyncAnthropic()

@dataclass
class TimeBudget:
    """Hierarchical time budget splitter."""
    total_seconds: float
    start: float = 0.0

    def __post_init__(self):
        self.start = time.monotonic()

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.start

    @property
    def remaining(self) -> float:
        return max(0.0, self.total_seconds - self.elapsed)

    def allocate(self, fraction: float, name: str) -> "PhaseToken":
        allotted = self.remaining * fraction
        return PhaseToken(name=name, seconds=allotted)

    def check(self, step: str = ""):
        if self.remaining <= 0:
            raise asyncio.TimeoutError(f"Global budget exhausted at: {step}")

@dataclass
class PhaseToken:
    name: str
    seconds: float
    _start: float = 0.0

    def __post_init__(self):
        self._start = time.monotonic()

    @property
    def remaining(self) -> float:
        return max(0.0, self.seconds - (time.monotonic() - self._start))

    def check(self):
        if self.remaining <= 0:
            raise asyncio.TimeoutError(f"Phase '{self.name}' budget ({self.seconds:.1f}s) exhausted")

async def planning_phase(question: str, budget: PhaseToken) -> list[str]:
    budget.check()
    print(f"[planning] {budget.remaining:.2f}s budget")
    resp = await asyncio.wait_for(
        client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{"role": "user", "content": f"List 3 sub-questions for: {question}. Return a numbered list only."}],
        ),
        timeout=budget.remaining,
    )
    lines = [l.strip() for l in resp.content[0].text.splitlines() if l.strip()]
    return lines[:3]

async def execution_phase(sub_questions: list[str], budget: PhaseToken) -> list[str]:
    per_question = budget.remaining / max(len(sub_questions), 1)
    results = []
    for q in sub_questions:
        budget.check()
        try:
            resp = await asyncio.wait_for(
                client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=80,
                    messages=[{"role": "user", "content": f"Answer briefly: {q}"}],
                ),
                timeout=min(per_question * 0.8, budget.remaining),
            )
            results.append(resp.content[0].text.strip())
        except asyncio.TimeoutError:
            results.append(f"[timeout: {q}]")
    return results

async def synthesis_phase(results: list[str], budget: PhaseToken) -> str:
    budget.check()
    combined = " | ".join(results[:3])
    resp = await asyncio.wait_for(
        client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=120,
            messages=[{"role": "user", "content": f"Synthesize into one paragraph: {combined}"}],
        ),
        timeout=budget.remaining,
    )
    return resp.content[0].text

async def hierarchical_budget_agent(question: str, total_timeout: float = 15.0) -> dict:
    global_budget = TimeBudget(total_seconds=total_timeout)
    start = time.monotonic()

    try:
        # Phase allocation: 20% planning, 50% execution, 30% synthesis
        plan_budget = global_budget.allocate(0.20, "planning")
        sub_questions = await planning_phase(question, plan_budget)
        print(f"Planned {len(sub_questions)} sub-questions | {global_budget.remaining:.2f}s global remaining")

        global_budget.check("after_planning")
        exec_budget = global_budget.allocate(0.60, "execution")
        answers = await execution_phase(sub_questions, exec_budget)
        print(f"Executed {len(answers)} answers | {global_budget.remaining:.2f}s global remaining")

        global_budget.check("after_execution")
        synth_budget = global_budget.allocate(0.90, "synthesis")  # Use most of what's left
        synthesis = await synthesis_phase(answers, synth_budget)

        return {
            "status": "ok",
            "synthesis": synthesis,
            "sub_questions": len(sub_questions),
            "elapsed": round(time.monotonic() - start, 2),
            "remaining": round(global_budget.remaining, 2),
        }

    except asyncio.TimeoutError as e:
        return {"status": "timeout", "error": str(e), "elapsed": round(time.monotonic() - start, 2)}

result = asyncio.run(hierarchical_budget_agent("What makes Python good for data science?", total_timeout=20.0))
print(f"\nStatus: {result['status']} | elapsed={result['elapsed']}s | remaining={result.get('remaining', 0)}s")
if result.get("synthesis"):
    print(f"Synthesis: {result['synthesis'][:200]}")
# Expected Token Savings: Phase budgeting ensures synthesis always gets time vs being starved
# Environment: Multi-phase research agents, plan-execute-synthesize pipelines
```

### Option 6: Deadline Propagation with Observability

Instrument every deadline-aware call with trace spans so you can observe where time budget is spent and detect which steps habitually over-run.

```python
import asyncio
import time
import uuid
import json
import anthropic
from dataclasses import dataclass, field
from typing import Optional

client = anthropic.AsyncAnthropic()

@dataclass
class TraceSpan:
    name: str
    span_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    parent_id: Optional[str] = None
    start: float = field(default_factory=time.monotonic)
    end: Optional[float] = None
    budget_at_start: float = 0.0
    budget_at_end: float = 0.0
    status: str = "running"

    def finish(self, status: str = "ok"):
        self.end = time.monotonic()
        self.status = status

    @property
    def duration(self) -> float:
        if self.end:
            return self.end - self.start
        return time.monotonic() - self.start

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "duration_ms": round(self.duration * 1000),
            "budget_at_start_ms": round(self.budget_at_start * 1000),
            "budget_at_end_ms": round(self.budget_at_end * 1000),
            "budget_consumed_ms": round((self.budget_at_start - self.budget_at_end) * 1000),
            "status": self.status,
        }

class DeadlineTracer:
    def __init__(self, total_seconds: float):
        self.deadline = time.monotonic() + total_seconds
        self.spans: list[TraceSpan] = []
        self.root_id = str(uuid.uuid4())[:8]

    @property
    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def start_span(self, name: str, parent_id: Optional[str] = None) -> TraceSpan:
        span = TraceSpan(
            name=name,
            parent_id=parent_id or self.root_id,
            budget_at_start=self.remaining,
        )
        self.spans.append(span)
        return span

    async def trace(self, name: str, coro, parent_id: Optional[str] = None):
        span = self.start_span(name, parent_id)
        try:
            result = await asyncio.wait_for(coro, timeout=self.remaining)
            span.budget_at_end = self.remaining
            span.finish("ok")
            return result
        except asyncio.TimeoutError:
            span.budget_at_end = 0.0
            span.finish("timeout")
            raise
        except Exception as e:
            span.budget_at_end = self.remaining
            span.finish("error")
            raise

    def report(self) -> dict:
        total = sum(s.duration for s in self.spans)
        return {
            "spans": [s.to_dict() for s in self.spans],
            "total_tracked_ms": round(total * 1000),
            "remaining_ms": round(self.remaining * 1000),
            "budget_breakdown": {
                s.name: round(s.duration * 1000)
                for s in sorted(self.spans, key=lambda x: x.duration, reverse=True)
            },
        }

async def traced_agent(question: str, timeout_seconds: float = 12.0) -> dict:
    tracer = DeadlineTracer(total_seconds=timeout_seconds)
    start = time.monotonic()

    try:
        async def step1():
            await asyncio.sleep(0.2)  # Simulate tool
            return "context_gathered"

        async def step2(context: str):
            resp = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=100,
                messages=[{"role": "user", "content": f"Context: {context}. {question}"}],
            )
            return resp.content[0].text

        async def step3(answer: str):
            await asyncio.sleep(0.1)  # Simulate formatting
            return f"formatted: {answer[:100]}"

        ctx = await tracer.trace("gather_context", step1())
        ans = await tracer.trace("model_call", step2(ctx))
        final = await tracer.trace("format_output", step3(ans))

        report = tracer.report()
        return {
            "status": "ok",
            "answer": final,
            "elapsed": round(time.monotonic() - start, 2),
            "trace": report,
        }

    except asyncio.TimeoutError as e:
        report = tracer.report()
        return {
            "status": "timeout",
            "elapsed": round(time.monotonic() - start, 2),
            "trace": report,
            "last_span": tracer.spans[-1].name if tracer.spans else None,
        }

result = asyncio.run(traced_agent("Explain Python decorators.", timeout_seconds=10.0))
print(f"Status: {result['status']} | elapsed={result['elapsed']}s")
print("\nBudget breakdown (ms):")
for name, ms in result["trace"]["budget_breakdown"].items():
    bar = "█" * min(40, ms // 10)
    print(f"  {name:20} {bar} {ms}ms")
# Expected Token Savings: Observability reveals budget hogs; targeted optimization reduces wasted calls
# Environment: Production agents with SLA monitoring; latency optimization; distributed tracing
```

## Comparison Table

| Option | Propagation Mechanism | Sync/Async | Observability | Best For |
|--------|----------------------|-----------|--------------|----------|
| 1: ContextVar | Python contextvars | Async only | None | Async-native FastAPI/asyncio agents |
| 2: DeadlineToken | Explicit arg passing | Both | None | Sync code, mixed sync/async pipelines |
| 3: Parallel + First-Success | gather + as_completed | Async | Basic | Multi-source parallel retrieval |
| 4: HTTP Header Propagation | X-Request-Deadline header | Async | Via headers | Microservice tool backends |
| 5: Hierarchical Budget | Fraction-based allocation | Async | Phase splits | Multi-phase plan-execute-synthesize agents |
| 6: Deadline Tracer | Span instrumentation | Async | Full span tree | Production SLA monitoring, optimization |
