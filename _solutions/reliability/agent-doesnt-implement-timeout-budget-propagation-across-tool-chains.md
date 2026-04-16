---
title: "Agent Doesn't Implement Timeout Budget Propagation Across Tool Chains"
description: "AI agents that set a 30-second top-level timeout but call three sequential tools each with their own independent 30-second timeout can spend 90 seconds on one request. Timeout budget propagation tracks the remaining time from the top-level deadline and passes a shrinking per-tool budget to each downstream call, ensuring the entire chain completes or fails within the original budget."
date: 2025-02-15
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-timeout-budget-propagation-across-tool-chains
tags:
  - timeout
  - deadline-propagation
  - budget
  - tool-chain
  - reliability
  - asyncio
  - context
symptoms:
  - "A 30-second user-facing timeout is violated because 3 sequential tools each have 30s timeouts"
  - "The total agent response time sometimes exceeds the SLA even though individual tools succeed"
  - "No way to know how much time budget remains when calling the third tool in a chain"
  - "Downstream tool called with 25-second timeout even though only 2 seconds remain in the budget"
  - "Timeout configuration is duplicated in every tool call instead of derived from a shared deadline"
---

## Problem

Timeout budgets must be hierarchical: the top-level SLA (e.g., 10 seconds for a user-facing API) imposes a deadline on the full chain. Each tool invocation should receive a timeout equal to `remaining_budget - safety_margin`, not a fixed independent value. Without propagation, each tool believes it has N seconds when the true remaining budget is already exhausted. Budget propagation threads a `Deadline` context through every call in the chain; each tool reads the remaining time and uses it as its own timeout, aborting immediately if the budget is already exceeded.

---

## Solution 1: DeadlineContext — Propagate Remaining Budget

```python
import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Optional


@dataclass
class Deadline:
    """Tracks an absolute wall-clock deadline and computes remaining budget."""
    _deadline: float   # monotonic time

    @classmethod
    def from_now(cls, seconds: float) -> "Deadline":
        return cls(_deadline=time.monotonic() + seconds)

    @classmethod
    def already_expired(cls) -> "Deadline":
        return cls(_deadline=time.monotonic() - 1)

    def remaining_s(self, safety_margin_s: float = 0.1) -> float:
        """Remaining seconds minus safety margin. Returns 0 if expired."""
        remaining = self._deadline - time.monotonic() - safety_margin_s
        return max(remaining, 0.0)

    def is_expired(self) -> bool:
        return time.monotonic() >= self._deadline

    def check(self):
        """Raise asyncio.TimeoutError immediately if deadline is exceeded."""
        if self.is_expired():
            raise asyncio.TimeoutError("Deadline exceeded before tool invocation")

    def child(self, max_seconds: float) -> "Deadline":
        """
        Return a child deadline that respects both the parent deadline
        and the specified maximum. The tighter constraint wins.
        """
        child_deadline = time.monotonic() + max_seconds
        return Deadline(_deadline=min(self._deadline, child_deadline))

    def __repr__(self) -> str:
        r = self.remaining_s(0)
        return f"Deadline(remaining={r:.2f}s)"
```

---

## Solution 2: BudgetedToolCaller — Execute Tools with Propagated Budget

```python
import asyncio
import logging
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class BudgetedToolCaller:
    """
    Wraps tool calls to automatically apply the remaining deadline budget.
    Each call receives the exact remaining time, not a fixed independent timeout.

    Usage:
        caller = BudgetedToolCaller(safety_margin_s=0.1)
        deadline = Deadline.from_now(10.0)

        result = await caller.call(deadline, "web_search", web_search_fn, query=q)
        result2 = await caller.call(deadline, "parse", parse_fn, html=result)
        # Total time cannot exceed 10 seconds regardless of how many tools are called.
    """

    def __init__(self, safety_margin_s: float = 0.1,
                 min_budget_s: float = 0.05):
        self._margin = safety_margin_s
        self._min = min_budget_s

    async def call(self, deadline: Deadline, tool_name: str,
                    fn: Callable, *args, **kwargs) -> Any:
        deadline.check()   # Fail fast if already over budget
        budget = deadline.remaining_s(self._margin)
        if budget < self._min:
            raise asyncio.TimeoutError(
                f"Insufficient budget ({budget:.3f}s) for '{tool_name}'"
            )
        t0 = time.monotonic()
        try:
            result = await asyncio.wait_for(fn(*args, **kwargs), timeout=budget)
            elapsed = (time.monotonic() - t0) * 1000
            logger.debug(
                "tool_call tool=%s budget_s=%.2f elapsed_ms=%.0f remaining_s=%.2f",
                tool_name, budget, elapsed, deadline.remaining_s(0),
            )
            return result
        except asyncio.TimeoutError:
            elapsed = (time.monotonic() - t0) * 1000
            logger.warning(
                "tool_timeout tool=%s budget_s=%.2f elapsed_ms=%.0f",
                tool_name, budget, elapsed,
            )
            raise asyncio.TimeoutError(
                f"Tool '{tool_name}' exceeded budget of {budget:.2f}s"
            )
```

---

## Solution 3: DeadlineContextVar — Thread/Task-Local Deadline Propagation

```python
import asyncio
import contextvars
import time
from contextlib import asynccontextmanager
from typing import Any, Callable, Optional

_DEADLINE_VAR: contextvars.ContextVar[Optional[Deadline]] = (
    contextvars.ContextVar("agent_deadline", default=None)
)


class DeadlineContextVar:
    """
    Stores the active Deadline in a ContextVar so it is automatically
    inherited by all child tasks and coroutines without explicit passing.

    Usage:
        async with DeadlineContextVar.set(10.0):
            result = await tool_a()   # tool_a can read the deadline
            result2 = await tool_b()  # remaining budget is automatically reduced

        # In tool implementations:
        deadline = DeadlineContextVar.current()
        if deadline:
            timeout = deadline.remaining_s()
        else:
            timeout = 30.0
    """

    @staticmethod
    @asynccontextmanager
    async def set(total_s: float):
        token = _DEADLINE_VAR.set(Deadline.from_now(total_s))
        try:
            yield
        finally:
            _DEADLINE_VAR.reset(token)

    @staticmethod
    def current() -> Optional[Deadline]:
        return _DEADLINE_VAR.get()

    @staticmethod
    def remaining_s(default_s: float = 30.0) -> float:
        deadline = _DEADLINE_VAR.get()
        if deadline is None:
            return default_s
        return deadline.remaining_s()

    @staticmethod
    def check():
        deadline = _DEADLINE_VAR.get()
        if deadline:
            deadline.check()
```

---

## Solution 4: PropagatingToolWrapper — Automatic Budget Extraction from Context

```python
import asyncio
import functools
from typing import Any, Callable, Optional


def budget_aware(tool_name: Optional[str] = None,
                  safety_margin_s: float = 0.1):
    """
    Decorator that makes a tool function automatically read the deadline
    from the ContextVar and apply the remaining budget as its timeout.
    No changes required at call sites.

    Usage:
        @budget_aware("web_search")
        async def web_search(query: str) -> list:
            return await live_search(query)

        # Caller sets the deadline once:
        async with DeadlineContextVar.set(10.0):
            results = await web_search(query="SSRF")
    """
    def decorator(fn: Callable) -> Callable:
        name = tool_name or fn.__name__

        @functools.wraps(fn)
        async def wrapper(*args, **kwargs) -> Any:
            DeadlineContextVar.check()
            remaining = DeadlineContextVar.remaining_s(default_s=30.0)
            budget = max(remaining - safety_margin_s, 0.01)
            try:
                return await asyncio.wait_for(fn(*args, **kwargs), timeout=budget)
            except asyncio.TimeoutError:
                raise asyncio.TimeoutError(
                    f"Tool '{name}' exceeded its budget of {budget:.2f}s"
                )
        return wrapper
    return decorator
```

---

## Solution 5: BudgetAwareAgentExecutor — Full Chain Execution with Budget Tracking

```python
import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class StepTrace:
    tool_name: str
    budget_s: float
    elapsed_ms: float
    success: bool
    remaining_s: float


class BudgetAwareAgentExecutor:
    """
    Executes a sequence of tool calls against a shared deadline budget.
    Records per-step timing and budget consumption for post-run analysis.

    Usage:
        executor = BudgetAwareAgentExecutor(total_budget_s=8.0)

        async with executor.run() as ctx:
            docs = await ctx.call("fetch", fetch_fn, url=url)
            chunks = await ctx.call("chunk", chunk_fn, text=docs)
            answer = await ctx.call("llm", llm_fn, messages=chunks)

        trace = executor.trace()
        # [StepTrace(tool="fetch", elapsed_ms=120, ...), ...]
    """

    def __init__(self, total_budget_s: float = 10.0,
                  safety_margin_s: float = 0.1):
        self._total = total_budget_s
        self._margin = safety_margin_s
        self._caller = BudgetedToolCaller(safety_margin_s)
        self._steps: List[StepTrace] = []

    class _Context:
        def __init__(self, deadline: Deadline,
                      caller: BudgetedToolCaller,
                      steps: List[StepTrace]):
            self._deadline = deadline
            self._caller = caller
            self._steps = steps

        async def call(self, tool_name: str, fn: Callable,
                        *args, **kwargs) -> Any:
            budget = self._deadline.remaining_s(self._caller._margin)
            t0 = time.monotonic()
            try:
                result = await self._caller.call(
                    self._deadline, tool_name, fn, *args, **kwargs
                )
                self._steps.append(StepTrace(
                    tool_name=tool_name, budget_s=budget,
                    elapsed_ms=(time.monotonic() - t0) * 1000,
                    success=True,
                    remaining_s=self._deadline.remaining_s(0),
                ))
                return result
            except Exception:
                self._steps.append(StepTrace(
                    tool_name=tool_name, budget_s=budget,
                    elapsed_ms=(time.monotonic() - t0) * 1000,
                    success=False,
                    remaining_s=self._deadline.remaining_s(0),
                ))
                raise

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def run(self):
        self._steps.clear()
        deadline = Deadline.from_now(self._total)
        ctx = self._Context(deadline, self._caller, self._steps)
        yield ctx

    def trace(self) -> List[StepTrace]:
        return list(self._steps)

    def budget_report(self) -> Dict[str, Any]:
        total_ms = sum(s.elapsed_ms for s in self._steps)
        return {
            "total_budget_s": self._total,
            "consumed_ms": round(total_ms, 1),
            "steps": [
                {"tool": s.tool_name, "ms": round(s.elapsed_ms, 1),
                 "ok": s.success}
                for s in self._steps
            ],
        }
```

---

## Solution 6: GRPCDeadlineForwarder — Propagate Deadlines to External Services

```python
import time
from typing import Any, Dict, Optional


class GRPCDeadlineForwarder:
    """
    Converts an agent Deadline into gRPC metadata / HTTP headers
    for forwarding to downstream microservices that honour deadlines.
    Downstream services that respect `grpc-timeout` or `X-Request-Deadline`
    will abort their own processing if they cannot respond in time.

    Usage:
        forwarder = GRPCDeadlineForwarder()
        deadline = Deadline.from_now(8.0)

        # For gRPC calls:
        grpc_timeout = forwarder.to_grpc_timeout(deadline)
        stub.GetUser(request, timeout=grpc_timeout)

        # For HTTP calls (httpx/aiohttp):
        headers = forwarder.to_http_headers(deadline)
        await client.get(url, headers=headers, timeout=grpc_timeout)
    """

    def to_grpc_timeout(self, deadline: Deadline,
                         safety_margin_s: float = 0.1) -> float:
        """Returns timeout in seconds for gRPC stub calls."""
        return max(deadline.remaining_s(safety_margin_s), 0.001)

    def to_http_headers(self, deadline: Deadline) -> Dict[str, str]:
        """
        Returns HTTP headers encoding the deadline for downstream services.
        Uses the de-facto X-Request-Deadline (Unix timestamp) and
        X-Request-Timeout (seconds remaining) conventions.
        """
        remaining = deadline.remaining_s(0)
        abs_deadline = time.time() + remaining
        return {
            "X-Request-Deadline": f"{abs_deadline:.3f}",
            "X-Request-Timeout": f"{remaining:.3f}",
            "X-Request-Budget-Ms": f"{remaining * 1000:.0f}",
        }

    def from_http_headers(self, headers: Dict[str, str]) -> Optional[Deadline]:
        """Reconstruct a Deadline from incoming request headers."""
        raw = headers.get("X-Request-Deadline")
        if raw:
            abs_wall = float(raw)
            remaining = abs_wall - time.time()
            if remaining > 0:
                return Deadline.from_now(remaining)
        raw_timeout = headers.get("X-Request-Timeout")
        if raw_timeout:
            return Deadline.from_now(float(raw_timeout))
        return None
```

---

## Comparison

| Approach | Budget Tracking | Context Propagation | Per-Step Trace | External Forwarding | Decorator |
|---|---|---|---|---|---|
| **DeadlineContext** | Yes | No | No | No | No |
| **BudgetedToolCaller** | Yes | Explicit | No | No | No |
| **DeadlineContextVar** | Via var | Automatic | No | No | No |
| **PropagatingToolWrapper** | Via ContextVar | Automatic | No | No | Yes |
| **BudgetAwareAgentExecutor** | Yes | Explicit | Yes | No | No |
| **GRPCDeadlineForwarder** | Converts | No | No | Yes | No |

**Key insight**: set one deadline at the agent entry point (`Deadline.from_now(10.0)`) and propagate it through every downstream call. Each tool should read `deadline.remaining_s()` and use that as its `asyncio.wait_for` timeout — never a hardcoded value. Use `DeadlineContextVar` to avoid threading the deadline through every function signature; use `GRPCDeadlineForwarder` to propagate it to downstream microservices that honour HTTP deadline headers or gRPC timeouts.
