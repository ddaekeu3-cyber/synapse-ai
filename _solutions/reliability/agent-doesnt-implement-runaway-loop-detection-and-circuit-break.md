---
title: "Agent Doesn't Implement Runaway Loop Detection and Circuit Break"
description: "AI agents executing multi-step tool chains can enter infinite loops—repeatedly calling the same tool with the same arguments, cycling between two tools, or accumulating tool calls without making progress. Without loop detection, runaway agents exhaust their token budget, deplete API rate limits, and incur unbounded cost before timing out. Loop detection tracks tool call patterns and circuit-breaks the agent when repetition or stagnation is detected."
date: 2025-02-23
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-runaway-loop-detection-and-circuit-break
tags:
  - loop-detection
  - infinite-loop
  - circuit-break
  - tool-chain
  - reliability
  - cost-control
  - agent-safety
symptoms:
  - "Agent calls web_search with identical queries 15 times in a row before hitting token limit"
  - "Agent cycles between 'search' and 'clarify' tools indefinitely without producing output"
  - "Single runaway session consumes $50 in API costs before the context window fills up"
  - "No mechanism to detect when tool calls are not making progress toward task completion"
  - "Agent accumulates 200 tool calls in a single turn without any response to the user"
---

## Problem

LLM-driven tool-calling loops can spin indefinitely when: the model receives an unsatisfying tool result and retries with the same arguments; two tools each recommend invoking the other; or the model misinterprets a subtask and keeps refining without converging. Python and asyncio have no built-in loop detection for agent step sequences. Without explicit detection, the only circuit breakers are the context window limit (expensive) and wall-clock timeout (slow to trigger). Active loop detection inspects the tool call history at each step for patterns—exact argument repetition, alternating tool pairs, total step count, and progress signals—and raises an exception before costs escalate.

---

## Solution 1: ToolCallHistoryTracker — Step-Level Repetition Detection

```python
import hashlib
import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class ToolCallRecord:
    tool_name: str
    arguments: Dict[str, Any]
    step: int

    @property
    def fingerprint(self) -> str:
        canonical = json.dumps(
            {"tool": self.tool_name, "args": self.arguments},
            sort_keys=True, ensure_ascii=False,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()[:12]


class ToolCallHistoryTracker:
    """
    Records every tool call and detects repetition patterns:
    - Exact duplicate: same tool + same arguments called again
    - Near-duplicate: same tool called N times in a row
    - Cycling: pattern like [A, B, A, B, ...] repeating
    - Volume: total tool calls exceeding a hard limit

    Usage:
        tracker = ToolCallHistoryTracker(
            max_exact_repeats=3,
            max_consecutive_same_tool=5,
            max_total_calls=50,
        )
        tracker.record("web_search", {"query": "AI safety"})
        if tracker.is_looping():
            raise AgentLoopError(tracker.loop_reason())
    """

    def __init__(
        self,
        max_exact_repeats: int = 3,
        max_consecutive_same_tool: int = 5,
        max_total_calls: int = 50,
        cycle_window: int = 10,
    ):
        self._max_exact = max_exact_repeats
        self._max_consecutive = max_consecutive_same_tool
        self._max_total = max_total_calls
        self._cycle_window = cycle_window
        self._history: List[ToolCallRecord] = []
        self._fingerprint_counts: Counter = Counter()
        self._loop_reason: Optional[str] = None

    def record(self, tool_name: str, arguments: Dict[str, Any]):
        step = len(self._history)
        record = ToolCallRecord(tool_name=tool_name, arguments=arguments, step=step)
        self._history.append(record)
        self._fingerprint_counts[record.fingerprint] += 1
        self._check_patterns()

    def _check_patterns(self):
        n = len(self._history)

        # Volume check
        if n >= self._max_total:
            self._loop_reason = f"Exceeded max tool calls ({n} >= {self._max_total})"
            return

        # Exact repeat check
        last_fp = self._history[-1].fingerprint
        count = self._fingerprint_counts[last_fp]
        if count >= self._max_exact:
            self._loop_reason = (
                f"Tool '{self._history[-1].tool_name}' called with identical arguments "
                f"{count} times (max {self._max_exact})"
            )
            return

        # Consecutive same-tool check
        if n >= self._max_consecutive:
            recent_tools = [r.tool_name for r in self._history[-self._max_consecutive:]]
            if len(set(recent_tools)) == 1:
                self._loop_reason = (
                    f"Tool '{recent_tools[0]}' called consecutively "
                    f"{self._max_consecutive} times"
                )
                return

        # Cycle detection: look for repeating 2-element patterns in last window
        if n >= self._cycle_window:
            recent = [r.fingerprint for r in self._history[-self._cycle_window:]]
            for period in range(2, self._cycle_window // 2 + 1):
                pattern = recent[-period:]
                candidate = recent[-period * 2: -period]
                if pattern == candidate:
                    tools = [r.tool_name for r in self._history[-period:]]
                    self._loop_reason = (
                        f"Detected cycling pattern of length {period}: {tools}"
                    )
                    return

    def is_looping(self) -> bool:
        return self._loop_reason is not None

    def loop_reason(self) -> Optional[str]:
        return self._loop_reason

    def summary(self) -> Dict[str, Any]:
        tool_counts = Counter(r.tool_name for r in self._history)
        return {
            "total_calls": len(self._history),
            "unique_calls": len(self._fingerprint_counts),
            "tool_distribution": dict(tool_counts.most_common(10)),
            "looping": self.is_looping(),
            "loop_reason": self._loop_reason,
        }
```

---

## Solution 2: ProgressMonitor — Detect Stagnation via Output Quality

```python
import logging
import time
from typing import Any, Callable, List, Optional

logger = logging.getLogger(__name__)


class ProgressMonitor:
    """
    Detects agent stagnation by measuring whether each tool call
    produces meaningfully new information. Compares tool result content
    against recently seen results; if results are too similar for too
    many consecutive steps, the agent is considered stuck.

    Usage:
        monitor = ProgressMonitor(
            similarity_threshold=0.95,
            max_stagnant_steps=4,
        )
        monitor.record_result("web_search", result_text)
        if monitor.is_stagnant():
            raise AgentLoopError("No progress: tool results unchanged for 4 steps")
    """

    def __init__(
        self,
        similarity_threshold: float = 0.95,
        max_stagnant_steps: int = 4,
        window_size: int = 8,
    ):
        self._sim_threshold = similarity_threshold
        self._max_stagnant = max_stagnant_steps
        self._window = window_size
        self._results: List[str] = []
        self._stagnant_count = 0
        self._stagnation_reason: Optional[str] = None

    def _similarity(self, a: str, b: str) -> float:
        """Token Jaccard similarity — fast, no external deps."""
        tokens_a = set(a.lower().split())
        tokens_b = set(b.lower().split())
        if not tokens_a or not tokens_b:
            return 0.0
        intersection = tokens_a & tokens_b
        union = tokens_a | tokens_b
        return len(intersection) / len(union)

    def record_result(self, tool_name: str, result: Any):
        result_str = str(result)
        self._results.append(result_str)

        if len(self._results) < 2:
            return

        # Compare against recent results in window
        recent = self._results[-min(self._window, len(self._results) - 1): -1]
        max_sim = max(self._similarity(result_str, prev) for prev in recent)

        if max_sim >= self._sim_threshold:
            self._stagnant_count += 1
            logger.warning(
                "progress_monitor_stagnant tool=%s similarity=%.2f consecutive=%d",
                tool_name, max_sim, self._stagnant_count,
            )
            if self._stagnant_count >= self._max_stagnant:
                self._stagnation_reason = (
                    f"Tool '{tool_name}' results unchanged for {self._stagnant_count} "
                    f"steps (similarity={max_sim:.2f})"
                )
        else:
            self._stagnant_count = 0  # progress made — reset

    def is_stagnant(self) -> bool:
        return self._stagnation_reason is not None

    def stagnation_reason(self) -> Optional[str]:
        return self._stagnation_reason
```

---

## Solution 3: LoopCircuitBreaker — Raise on Detection, Inject Escape Instruction

```python
import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class AgentLoopError(Exception):
    """Raised when a runaway loop is detected in the agent tool chain."""
    def __init__(self, reason: str, step_count: int = 0):
        super().__init__(f"Agent loop detected at step {step_count}: {reason}")
        self.reason = reason
        self.step_count = step_count


class LoopCircuitBreaker:
    """
    Combines ToolCallHistoryTracker and ProgressMonitor into a single
    circuit breaker that can either raise an exception or inject an
    escape instruction into the next LLM prompt to guide the agent out
    of the loop without aborting the session.

    Usage:
        breaker = LoopCircuitBreaker(
            max_total_calls=40,
            max_exact_repeats=3,
            inject_escape=True,  # add warning to next prompt instead of raising
        )
        # In the agent loop:
        breaker.check_before_tool_call(tool_name, args, step=turn_count)
    """

    ESCAPE_INSTRUCTION = (
        "\n\n[SYSTEM NOTICE: You appear to be repeating tool calls without making progress. "
        "Stop and synthesize what you have learned so far into a final answer. "
        "Do not call any more tools — provide your best response with current information.]"
    )

    def __init__(
        self,
        max_total_calls: int = 40,
        max_exact_repeats: int = 3,
        max_consecutive_same_tool: int = 5,
        max_stagnant_steps: int = 4,
        inject_escape: bool = False,
        on_loop_detected: Optional[Callable] = None,
    ):
        self._tracker = ToolCallHistoryTracker(
            max_exact_repeats=max_exact_repeats,
            max_consecutive_same_tool=max_consecutive_same_tool,
            max_total_calls=max_total_calls,
        )
        self._monitor = ProgressMonitor(max_stagnant_steps=max_stagnant_steps)
        self._inject_escape = inject_escape
        self._callback = on_loop_detected
        self._escape_injected = False

    def record_tool_call(self, tool_name: str, arguments: Dict[str, Any], result: Any = None):
        self._tracker.record(tool_name, arguments)
        if result is not None:
            self._monitor.record_result(tool_name, result)

    def check(self, step: int = 0) -> Optional[str]:
        """Returns escape instruction string if looping, None if clean."""
        reason = None
        if self._tracker.is_looping():
            reason = self._tracker.loop_reason()
        elif self._monitor.is_stagnant():
            reason = self._monitor.stagnation_reason()

        if reason:
            logger.warning("loop_detected step=%d reason=%s", step, reason)
            if self._callback:
                try:
                    self._callback({"step": step, "reason": reason,
                                     "summary": self._tracker.summary()})
                except Exception:
                    pass
            if self._inject_escape and not self._escape_injected:
                self._escape_injected = True
                return self.ESCAPE_INSTRUCTION
            raise AgentLoopError(reason, step_count=step)
        return None

    @property
    def stats(self) -> Dict[str, Any]:
        return self._tracker.summary()
```

---

## Solution 4: BudgetAwareStepLimiter — Token and Cost Budget Enforcement

```python
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class BudgetAwareStepLimiter:
    """
    Limits agent steps based on both step count and estimated token/cost
    budget. Prevents runaway loops from incurring unbounded API costs
    even when individual tool calls succeed and return distinct results.

    Usage:
        limiter = BudgetAwareStepLimiter(
            max_steps=30,
            max_input_tokens=50_000,
            max_cost_usd=1.00,
            cost_per_1k_input=0.003,   # claude-sonnet pricing
            cost_per_1k_output=0.015,
        )
        limiter.record_step(input_tokens=800, output_tokens=200)
        if limiter.budget_exhausted():
            raise AgentLoopError(limiter.exhaustion_reason())
    """

    def __init__(
        self,
        max_steps: int = 30,
        max_input_tokens: int = 100_000,
        max_output_tokens: int = 20_000,
        max_cost_usd: float = 2.0,
        cost_per_1k_input: float = 0.003,
        cost_per_1k_output: float = 0.015,
    ):
        self._max_steps = max_steps
        self._max_input_tokens = max_input_tokens
        self._max_output_tokens = max_output_tokens
        self._max_cost = max_cost_usd
        self._cpi = cost_per_1k_input
        self._cpo = cost_per_1k_output

        self._steps = 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._reason: Optional[str] = None

    def record_step(self, input_tokens: int = 0, output_tokens: int = 0):
        self._steps += 1
        self._input_tokens += input_tokens
        self._output_tokens += output_tokens
        self._check()

    def _check(self):
        if self._steps >= self._max_steps:
            self._reason = f"Step limit reached: {self._steps}/{self._max_steps}"
        elif self._input_tokens >= self._max_input_tokens:
            self._reason = f"Input token limit: {self._input_tokens}/{self._max_input_tokens}"
        elif self._output_tokens >= self._max_output_tokens:
            self._reason = f"Output token limit: {self._output_tokens}/{self._max_output_tokens}"
        elif self.estimated_cost_usd >= self._max_cost:
            self._reason = (
                f"Cost limit: ${self.estimated_cost_usd:.4f}/${self._max_cost:.2f}"
            )

    @property
    def estimated_cost_usd(self) -> float:
        return (self._input_tokens / 1000 * self._cpi +
                self._output_tokens / 1000 * self._cpo)

    def budget_exhausted(self) -> bool:
        return self._reason is not None

    def exhaustion_reason(self) -> Optional[str]:
        return self._reason

    def status(self) -> Dict[str, Any]:
        return {
            "steps": self._steps,
            "input_tokens": self._input_tokens,
            "output_tokens": self._output_tokens,
            "estimated_cost_usd": round(self.estimated_cost_usd, 6),
            "budget_exhausted": self.budget_exhausted(),
            "reason": self._reason,
        }
```

---

## Solution 5: LoopPatternLibrary — Named Pattern Detectors for Common Anti-Patterns

```python
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class LoopPatternLibrary:
    """
    A library of named loop pattern detectors for common agent anti-patterns.
    Each detector takes the recent tool call history and returns a description
    of the detected pattern if present, None otherwise.

    Usage:
        lib = LoopPatternLibrary()
        for step_history in agent.tool_history_window(10):
            pattern = lib.detect_any(step_history)
            if pattern:
                logger.warning("loop_pattern name=%s", pattern)
                agent.inject_escape_prompt()
    """

    def detect_ping_pong(self, history: List[Tuple[str, str]]) -> Optional[str]:
        """Detect A-B-A-B alternating pattern."""
        if len(history) < 4:
            return None
        tools = [h[0] for h in history]
        if len(set(tools[-4:])) == 2 and tools[-1] == tools[-3] and tools[-2] == tools[-4]:
            return f"ping-pong: alternating {tools[-2]} ↔ {tools[-1]}"
        return None

    def detect_clarification_spiral(self, history: List[Tuple[str, str]]) -> Optional[str]:
        """Detect repeated ask_user or clarify calls — user isn't responding."""
        clarify_tools = {"ask_user", "clarify", "request_clarification", "ask_for_input"}
        recent = [h[0] for h in history[-6:]]
        clarify_count = sum(1 for t in recent if t in clarify_tools)
        if clarify_count >= 3:
            return f"clarification-spiral: {clarify_count} clarification requests in last 6 steps"
        return None

    def detect_search_refinement_loop(self, history: List[Tuple[str, str]]) -> Optional[str]:
        """Detect web_search called many times with minor query variations."""
        search_tools = {"web_search", "search", "google_search", "bing_search"}
        recent_searches = [(t, a) for t, a in history[-10:] if t in search_tools]
        if len(recent_searches) >= 5:
            return f"search-loop: {len(recent_searches)} searches in last 10 steps"
        return None

    def detect_write_read_cycle(self, history: List[Tuple[str, str]]) -> Optional[str]:
        """Detect write-then-immediately-read-same-resource pattern."""
        if len(history) < 4:
            return None
        write_tools = {"write_file", "save_file", "update_db", "upsert"}
        read_tools = {"read_file", "load_file", "read_db", "fetch"}
        for i in range(len(history) - 3):
            window = [h[0] for h in history[i: i + 4]]
            write_count = sum(1 for t in window if t in write_tools)
            read_count = sum(1 for t in window if t in read_tools)
            if write_count >= 2 and read_count >= 2:
                return f"write-read-cycle in window: {window}"
        return None

    def detect_any(self, history: List[Tuple[str, str]]) -> Optional[str]:
        for detector in (
            self.detect_ping_pong,
            self.detect_clarification_spiral,
            self.detect_search_refinement_loop,
            self.detect_write_read_cycle,
        ):
            result = detector(history)
            if result:
                return result
        return None
```

---

## Solution 6: AgentLoopGuard — Unified Loop Protection for Agent Entrypoint

```python
import asyncio
import functools
import logging
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class AgentLoopGuard:
    """
    Wraps an agent's tool dispatch function with all loop detection
    mechanisms. Checks before each tool call; raises AgentLoopError
    or injects escape instruction into the next prompt when a loop
    is detected. Designed to be inserted at the tool dispatch layer
    without modifying agent logic.

    Usage:
        guard = AgentLoopGuard(
            max_total_calls=40,
            max_exact_repeats=3,
            max_cost_usd=1.50,
            inject_escape=True,
        )
        result = await guard.execute_tool("web_search", {"query": q},
                                           executor=tool_registry.execute,
                                           step=turn_number)
    """

    def __init__(
        self,
        max_total_calls: int = 40,
        max_exact_repeats: int = 3,
        max_consecutive_same_tool: int = 5,
        max_stagnant_steps: int = 4,
        max_cost_usd: float = 2.0,
        inject_escape: bool = False,
    ):
        self._breaker = LoopCircuitBreaker(
            max_total_calls=max_total_calls,
            max_exact_repeats=max_exact_repeats,
            max_consecutive_same_tool=max_consecutive_same_tool,
            max_stagnant_steps=max_stagnant_steps,
            inject_escape=inject_escape,
        )
        self._budget = BudgetAwareStepLimiter(max_cost_usd=max_cost_usd)
        self._pattern_lib = LoopPatternLibrary()
        self._tool_history: List[tuple] = []
        self._step = 0

    async def execute_tool(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        executor: Callable,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> Any:
        self._step += 1
        self._budget.record_step(input_tokens=input_tokens, output_tokens=output_tokens)

        # Budget check
        if self._budget.budget_exhausted():
            raise AgentLoopError(self._budget.exhaustion_reason(), self._step)

        # Pattern library check
        self._tool_history.append((tool_name, str(arguments)))
        pattern = self._pattern_lib.detect_any(self._tool_history[-12:])
        if pattern:
            logger.warning("named_loop_pattern step=%d pattern=%s", self._step, pattern)

        # Execute tool
        result = await executor(tool_name, arguments) \
            if asyncio.iscoroutinefunction(executor) \
            else executor(tool_name, arguments)

        # Record and check
        self._breaker.record_tool_call(tool_name, arguments, result)
        escape = self._breaker.check(step=self._step)
        if escape:
            logger.warning("loop_escape_injected step=%d", self._step)

        return result

    @property
    def stats(self) -> Dict[str, Any]:
        return {
            "steps": self._step,
            "breaker": self._breaker.stats,
            "budget": self._budget.status(),
        }
```

---

## Comparison

| Approach | Exact Repeat Detection | Cycling Detection | Stagnation | Budget Guard | Named Patterns | Escape Injection |
|---|---|---|---|---|---|---|
| **ToolCallHistoryTracker** | Yes | Yes | No | No | No | No |
| **ProgressMonitor** | No | No | Yes | No | No | No |
| **LoopCircuitBreaker** | Via tracker | Via tracker | Via monitor | No | No | Yes |
| **BudgetAwareStepLimiter** | No | No | No | Yes | No | No |
| **LoopPatternLibrary** | No | Via patterns | No | No | Yes | No |
| **AgentLoopGuard** | Yes | Yes | Yes | Yes | Yes | Yes |

**Key insight**: the minimum safe configuration is `ToolCallHistoryTracker(max_exact_repeats=3, max_total_calls=30)` inserted before every tool dispatch. This catches the most common runaway loop—identical argument repetition—and hard limits total tool calls, capping worst-case session cost regardless of what the LLM decides to do. Add `inject_escape=True` to `LoopCircuitBreaker` for production: instead of raising an exception (which terminates the session), it appends a system notice to the next prompt instructing the LLM to stop looping and synthesize a response, recovering gracefully without losing the accumulated context.
