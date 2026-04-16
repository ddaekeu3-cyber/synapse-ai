---
title: "Agent Doesn't Implement Agent Reasoning Step Count Tracking"
description: "Agents that don't count reasoning steps — tool calls, LLM turns, sub-agent invocations — cannot detect when a task is stuck in an infinite loop, alert on runaway multi-step plans, or measure task complexity distribution. Implement reasoning step count tracking that records every step type, enforces configurable step budgets, and surfaces step count distributions for capacity planning."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-agent-reasoning-step-count-tracking
tags: [step-count, reasoning-steps, loop-detection, step-budget, task-complexity, agentic-loop]
symptoms:
  - "Agent enters an infinite tool-call loop that is never detected or stopped"
  - "No measurement of how many LLM turns or tool calls a typical task requires"
  - "Step budget is not enforced — a task can run indefinitely"
  - "High-step tasks are invisible in metrics — cannot identify unusually complex queries"
  - "Cannot distinguish a 3-step task from a 30-step task in performance analysis"
---

## Why This Happens

Agentic loops are open-ended by design — the agent continues until it decides the task is done. Without step counting, there is no mechanism to detect when the agent has taken an unreasonable number of steps, which can indicate a planning loop, a tool failure retry storm, or a prompt that produces circular reasoning. Step tracking requires incrementing a counter per step type, checking the counter against a budget before each step, and recording the distribution of step counts per task type.

## Solution 1: Step Type Registry

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict


class StepType(str, Enum):
    LLM_CALL = "llm_call"
    TOOL_CALL = "tool_call"
    SUB_AGENT_INVOCATION = "sub_agent_invocation"
    CONTEXT_REFRESH = "context_refresh"
    RETRIEVAL = "retrieval"
    VALIDATION = "validation"
    HUMAN_IN_THE_LOOP = "human_in_the_loop"


@dataclass
class StepRecord:
    step_type: StepType
    step_index: int          # sequential within the task
    tool_name: str = ""      # populated for TOOL_CALL and RETRIEVAL
    latency_ms: float = 0.0
    success: bool = True
    metadata: Dict[str, Any] = field(default_factory=dict)
```

## Solution 2: Per-Task Step Counter

```python
import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Dict, List, Optional


@dataclass
class TaskStepBudget:
    max_total_steps: int = 50
    max_llm_calls: int = 20
    max_tool_calls: int = 40
    max_sub_agents: int = 5


class PerTaskStepCounter:
    """
    Tracks step counts per task with configurable budgets per step type.
    Raises StepBudgetExceededError when any budget is exceeded.
    """

    def __init__(
        self,
        task_id: str,
        budget: TaskStepBudget,
    ):
        self._task_id = task_id
        self._budget = budget
        self._steps: List[StepRecord] = []
        self._counts: Dict[str, int] = {}
        self._lock = Lock()
        self._started_at = time.time()

    def record_step(self, step_type: StepType, **kwargs) -> StepRecord:
        with self._lock:
            total = len(self._steps)
            count_for_type = self._counts.get(step_type.value, 0)

            # Check budgets before recording
            if total >= self._budget.max_total_steps:
                raise StepBudgetExceededError(
                    self._task_id, "total", total, self._budget.max_total_steps
                )
            if step_type == StepType.LLM_CALL and count_for_type >= self._budget.max_llm_calls:
                raise StepBudgetExceededError(
                    self._task_id, "llm_calls", count_for_type, self._budget.max_llm_calls
                )
            if step_type == StepType.TOOL_CALL and count_for_type >= self._budget.max_tool_calls:
                raise StepBudgetExceededError(
                    self._task_id, "tool_calls", count_for_type, self._budget.max_tool_calls
                )
            if step_type == StepType.SUB_AGENT_INVOCATION and count_for_type >= self._budget.max_sub_agents:
                raise StepBudgetExceededError(
                    self._task_id, "sub_agents", count_for_type, self._budget.max_sub_agents
                )

            record = StepRecord(
                step_type=step_type,
                step_index=total,
                **kwargs,
            )
            self._steps.append(record)
            self._counts[step_type.value] = count_for_type + 1
            return record

    def total_steps(self) -> int:
        with self._lock:
            return len(self._steps)

    def steps_by_type(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._counts)

    def elapsed_seconds(self) -> float:
        return time.time() - self._started_at

    def summary(self) -> dict:
        with self._lock:
            return {
                "task_id": self._task_id,
                "total_steps": len(self._steps),
                "by_type": dict(self._counts),
                "elapsed_seconds": round(self.elapsed_seconds(), 2),
                "budget": {
                    "max_total": self._budget.max_total_steps,
                    "max_llm_calls": self._budget.max_llm_calls,
                    "max_tool_calls": self._budget.max_tool_calls,
                },
            }


class StepBudgetExceededError(Exception):
    def __init__(self, task_id: str, budget_type: str, count: int, limit: int):
        super().__init__(
            f"task '{task_id}' exceeded {budget_type} budget: {count}/{limit} steps"
        )
        self.task_id = task_id
        self.budget_type = budget_type
        self.count = count
        self.limit = limit
```

## Solution 3: Loop Pattern Detector

```python
from typing import List, Optional


class LoopPatternDetector:
    """
    Detects repeating tool call sequences that indicate the agent
    is stuck in a reasoning loop. Uses a sliding window to find
    repeated patterns in the step history.
    """

    def __init__(self, window_size: int = 6, min_repetitions: int = 2):
        self._window = window_size
        self._min_reps = min_repetitions

    def detect(self, counter: PerTaskStepCounter) -> Optional[dict]:
        steps = [s.tool_name or s.step_type.value for s in counter._steps]
        if len(steps) < self._window * self._min_reps:
            return None

        for pattern_len in range(2, self._window // 2 + 1):
            pattern = steps[-pattern_len:]
            matches = 0
            pos = len(steps) - pattern_len * 2
            while pos >= 0:
                if steps[pos:pos + pattern_len] == pattern:
                    matches += 1
                    pos -= pattern_len
                else:
                    break

            if matches >= self._min_reps - 1:
                return {
                    "loop_detected": True,
                    "pattern": pattern,
                    "pattern_length": pattern_len,
                    "repetitions": matches + 1,
                    "total_steps": counter.total_steps(),
                }

        return None
```

## Solution 4: Step Count Distribution Tracker

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, Dict, List, Optional, Tuple


class StepCountDistributionTracker:
    """
    Accumulates completed task step counts and computes
    percentile distributions for capacity planning.
    """

    def __init__(self, max_records: int = 10000):
        self._max = max_records
        # (recorded_at, total_steps, task_type)
        self._records: Deque[Tuple[float, int, str]] = deque()
        self._lock = Lock()

    def record_completion(
        self, total_steps: int, task_type: str = ""
    ) -> None:
        with self._lock:
            self._records.append((time.time(), total_steps, task_type))
            if len(self._records) > self._max:
                self._records.popleft()

    def percentile(self, pct: float, window_seconds: float = 3600.0) -> Optional[float]:
        cutoff = time.time() - window_seconds
        with self._lock:
            values = sorted(
                steps for ts, steps, _ in self._records if ts >= cutoff
            )
        if not values:
            return None
        idx = min(int(len(values) * pct / 100.0), len(values) - 1)
        return values[idx]

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [(ts, s, t) for ts, s, t in self._records if ts >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "tasks": 0}
        steps_list = [s for _, s, _ in recent]
        return {
            "window_seconds": window_seconds,
            "tasks": len(recent),
            "p50_steps": self.percentile(50, window_seconds),
            "p95_steps": self.percentile(95, window_seconds),
            "p99_steps": self.percentile(99, window_seconds),
            "mean_steps": round(sum(steps_list) / len(steps_list), 2),
            "max_steps": max(steps_list),
        }
```

## Solution 5: Step Budget Policy Registry

```python
from typing import Dict, Optional


class StepBudgetPolicyRegistry:
    """
    Maps task types to step budgets. Allows different complexity
    allowances for different agent workflows.
    """

    def __init__(
        self,
        policies: Optional[Dict[str, TaskStepBudget]] = None,
        default: Optional[TaskStepBudget] = None,
    ):
        self._policies = policies or {}
        self._default = default or TaskStepBudget()

    def register(self, task_type: str, budget: TaskStepBudget) -> None:
        self._policies[task_type] = budget

    def get(self, task_type: str) -> TaskStepBudget:
        return self._policies.get(task_type, self._default)

    def all_budgets(self) -> Dict[str, dict]:
        return {
            task_type: {
                "max_total_steps": b.max_total_steps,
                "max_llm_calls": b.max_llm_calls,
                "max_tool_calls": b.max_tool_calls,
            }
            for task_type, b in self._policies.items()
        }
```

## Solution 6: Reasoning Step Dashboard

```python
import time


class AgentReasoningStepDashboard:
    """
    Combines step distribution, loop detection metrics, and budget policy
    into a single operational report.
    """

    def __init__(
        self,
        distribution_tracker: StepCountDistributionTracker,
        budget_registry: StepBudgetPolicyRegistry,
    ):
        self._distribution = distribution_tracker
        self._registry = budget_registry
        self._budget_exceeded_count = 0
        self._loop_detected_count = 0

    def record_budget_exceeded(self) -> None:
        self._budget_exceeded_count += 1

    def record_loop_detected(self) -> None:
        self._loop_detected_count += 1

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "step_distribution": self._distribution.summary(window_seconds=3600.0),
            "budget_exceeded_count": self._budget_exceeded_count,
            "loop_detected_count": self._loop_detected_count,
            "registered_budgets": self._registry.all_budgets(),
        }
```

## Comparison

| Approach | Per-Task Counting | Budget Enforcement | Loop Detection | Distribution P95/P99 | Dashboard |
|---|---|---|---|---|---|
| PerTaskStepCounter | Yes | Yes (raises) | No | No | No |
| LoopPatternDetector | Via counter | No | Yes (pattern) | No | No |
| StepCountDistributionTracker | No | No | No | Yes | No |
| StepBudgetPolicyRegistry | No | Via counter | No | No | No |
| AgentReasoningStepDashboard | No | No | No | Via tracker | Yes |

**Best for production**: Set `max_total_steps=50` as the default hard limit — tasks requiring more than 50 steps are almost always stuck in a loop or dealing with a pathological input. Use `LoopPatternDetector` with `window_size=6` to catch the most common 2- and 3-step loops; call it every 5 steps so loops are caught before they waste significant tokens. Monitor `p95_steps` from `StepCountDistributionTracker`: a rising P95 over time indicates your agent is handling increasingly complex queries or that a recent prompt change causes more tool calls. Alert when `budget_exceeded_count` is non-zero in production — it means real user tasks hit the step limit and either the budget is too tight or the agent has a planning defect.
