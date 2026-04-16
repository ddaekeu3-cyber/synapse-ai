---
title: "Agent Doesn't Implement Output Token Budget Enforcement"
description: "Agents that set a fixed max_tokens ceiling on every LLM call waste money on short answers that need only 50 tokens when max_tokens is 2048, and fail to produce complete answers when a task genuinely requires 1500 tokens but max_tokens was set too low. Implement dynamic output token budget enforcement that estimates required output length from the task type, adjusts max_tokens per call, and tracks actual vs budgeted token consumption to improve estimates over time."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-output-token-budget-enforcement
tags: [output-tokens, max-tokens, token-budget, cost-control, dynamic-token-allocation, completion-quality]
symptoms:
  - "All LLM calls use the same max_tokens=2048 regardless of expected output length"
  - "Short-answer queries pay for 2048-token allocations but use only 30 tokens"
  - "Long structured outputs are truncated because max_tokens was set too conservatively"
  - "No feedback loop between actual output token usage and future budget allocation"
  - "Token costs are uniform across task types even though complexity varies greatly"
---

## Why This Happens

Most agents set `max_tokens` once in configuration and apply it universally. This is safe but wasteful: a simple lookup query is allocated the same token budget as a multi-step reasoning task. LLM APIs bill for generated tokens, and over-allocation on simple queries accumulates real cost at scale. Under-allocation on complex queries produces truncated outputs that are incomplete or misleading. Dynamic budgeting requires classifying the task type, looking up a historical p95 completion length for that type, adding a safety margin, and capping at the model's hard limit.

## Solution 1: Task Output Budget Profile

```python
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TaskOutputBudgetProfile:
    task_type: str
    estimated_tokens: int           # starting estimate
    min_tokens: int = 50
    max_tokens: int = 4096
    safety_margin_pct: float = 0.20  # add 20% over the p95 estimate
    observed_p95: Optional[int] = None  # updated from actuals

    def budget(self) -> int:
        base = self.observed_p95 if self.observed_p95 else self.estimated_tokens
        with_margin = int(base * (1 + self.safety_margin_pct))
        return max(self.min_tokens, min(with_margin, self.max_tokens))
```

## Solution 2: Task Type Classifier

```python
import re
from typing import Dict, List, Tuple


TASK_TYPE_PATTERNS: List[Tuple[str, str]] = [
    ("lookup", r"\bwhat is\b|\bwho is\b|\bwhen did\b|\bwhere is\b|\bdefinition\b"),
    ("yesno", r"\bis it\b|\bare there\b|\bdoes it\b|\bcan you confirm\b"),
    ("list", r"\blist\b|\benumerate\b|\bgive me all\b|\bname all\b"),
    ("summary", r"\bsummariz\b|\bbriefly\b|\boverview\b|\bdigest\b"),
    ("analysis", r"\banalyz\b|\bexplain why\b|\bcompare\b|\bevaluat\b"),
    ("code", r"\bwrite\b.*\bcode\b|\bimplement\b|\bscript\b|\bfunction\b"),
    ("plan", r"\bplan\b|\bsteps to\b|\bhow to\b|\bstrategy\b"),
    ("creative", r"\bwrite a\b.*\bstory\b|\bpoem\b|\bessay\b|\bdraft\b"),
]


class TaskTypeClassifier:
    """
    Classifies a user message into a task type used to look up
    the appropriate output token budget profile.
    """

    DEFAULT_TYPE = "analysis"

    def classify(self, user_message: str) -> str:
        msg = user_message.lower()
        for task_type, pattern in TASK_TYPE_PATTERNS:
            if re.search(pattern, msg, re.IGNORECASE):
                return task_type
        return self.DEFAULT_TYPE
```

## Solution 3: Output Token Usage Recorder

```python
import time
from collections import defaultdict, deque
from threading import Lock
from typing import Deque, Dict, List, Optional, Tuple


class OutputTokenUsageRecorder:
    """
    Records actual output token counts per task type.
    Computes rolling p95 to update budget profiles.
    """

    def __init__(self, window_size: int = 500):
        self._window = window_size
        self._observations: Dict[str, Deque[int]] = defaultdict(
            lambda: deque(maxlen=window_size)
        )
        self._lock = Lock()

    def record(self, task_type: str, tokens_used: int) -> None:
        with self._lock:
            self._observations[task_type].append(tokens_used)

    def p95(self, task_type: str) -> Optional[int]:
        with self._lock:
            obs = list(self._observations.get(task_type, []))
        if len(obs) < 10:
            return None
        sorted_obs = sorted(obs)
        idx = int(len(sorted_obs) * 0.95)
        return sorted_obs[min(idx, len(sorted_obs) - 1)]

    def update_profile(self, profile: TaskOutputBudgetProfile) -> bool:
        p95 = self.p95(profile.task_type)
        if p95 is not None:
            profile.observed_p95 = p95
            return True
        return False

    def stats(self) -> dict:
        with self._lock:
            return {
                task_type: {
                    "observations": len(obs),
                    "p95": sorted(obs)[int(len(obs) * 0.95)] if len(obs) >= 10 else None,
                    "mean": round(sum(obs) / len(obs), 1) if obs else None,
                }
                for task_type, obs in self._observations.items()
            }
```

## Solution 4: Dynamic Token Budget Allocator

```python
from typing import Dict, Optional


class DynamicTokenBudgetAllocator:
    """
    Determines max_tokens for each LLM call based on task type classification
    and historical p95 usage. Falls back to the profile's estimated_tokens
    until enough observations are available.
    """

    def __init__(
        self,
        classifier: TaskTypeClassifier,
        profiles: Dict[str, TaskOutputBudgetProfile],
        recorder: OutputTokenUsageRecorder,
        model_hard_limit: int = 8192,
    ):
        self._classifier = classifier
        self._profiles = profiles
        self._recorder = recorder
        self._hard_limit = model_hard_limit

    def _default_profile(self) -> TaskOutputBudgetProfile:
        return TaskOutputBudgetProfile(
            task_type="default",
            estimated_tokens=1024,
            max_tokens=self._hard_limit,
        )

    def allocate(self, user_message: str) -> dict:
        task_type = self._classifier.classify(user_message)
        profile = self._profiles.get(task_type, self._default_profile())
        self._recorder.update_profile(profile)
        budget = min(profile.budget(), self._hard_limit)
        return {
            "task_type": task_type,
            "max_tokens": budget,
            "profile_estimated": profile.estimated_tokens,
            "profile_p95": profile.observed_p95,
            "safety_margin_pct": profile.safety_margin_pct,
        }

    def record_actual(self, task_type: str, tokens_used: int) -> None:
        self._recorder.record(task_type, tokens_used)
```

## Solution 5: Budget-Enforced LLM Caller

```python
import time
from typing import Any, Callable, Dict, List, Optional


class BudgetEnforcedLLMCaller:
    """
    Wraps an LLM call with dynamic token budget allocation.
    Records actual usage after each call to improve future estimates.
    """

    def __init__(
        self,
        allocator: DynamicTokenBudgetAllocator,
        call_fn: Callable,
    ):
        self._allocator = allocator
        self._call_fn = call_fn
        self._call_log: List[dict] = []

    async def call(
        self,
        messages: List[Dict[str, Any]],
        user_message: str,
        *,
        system: Optional[str] = None,
        extra_params: Optional[dict] = None,
    ) -> dict:
        allocation = self._allocator.allocate(user_message)

        params = {
            "messages": messages,
            "max_tokens": allocation["max_tokens"],
            **(extra_params or {}),
        }
        if system:
            params["system"] = system

        start = time.time()
        response = await self._call_fn(**params)
        latency_ms = round((time.time() - start) * 1000, 2)

        # Extract actual token usage from response
        actual_tokens = getattr(
            getattr(response, "usage", None), "output_tokens", None
        ) or allocation["max_tokens"]

        self._allocator.record_actual(allocation["task_type"], actual_tokens)

        self._call_log.append({
            "task_type": allocation["task_type"],
            "budgeted_tokens": allocation["max_tokens"],
            "actual_tokens": actual_tokens,
            "utilization": round(actual_tokens / max(allocation["max_tokens"], 1), 3),
            "latency_ms": latency_ms,
        })

        return {
            "response": response,
            "task_type": allocation["task_type"],
            "budgeted_tokens": allocation["max_tokens"],
            "actual_tokens": actual_tokens,
        }

    def efficiency_stats(self) -> dict:
        if not self._call_log:
            return {"calls": 0}
        total_budgeted = sum(r["budgeted_tokens"] for r in self._call_log)
        total_actual = sum(r["actual_tokens"] for r in self._call_log)
        avg_util = sum(r["utilization"] for r in self._call_log) / len(self._call_log)
        return {
            "calls": len(self._call_log),
            "total_budgeted_tokens": total_budgeted,
            "total_actual_tokens": total_actual,
            "tokens_saved_est": total_budgeted - total_actual,
            "avg_utilization": round(avg_util, 3),
        }
```

## Solution 6: Token Budget Dashboard

```python
import time


class OutputTokenBudgetDashboard:
    """
    Combines allocation efficiency, per-task-type statistics,
    and budget profile accuracy into an operational report.
    """

    def __init__(
        self,
        caller: BudgetEnforcedLLMCaller,
        recorder: OutputTokenUsageRecorder,
        profiles: dict,
    ):
        self._caller = caller
        self._recorder = recorder
        self._profiles = profiles

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "efficiency": self._caller.efficiency_stats(),
            "per_task_stats": self._recorder.stats(),
            "profile_budgets": {
                task_type: {
                    "current_budget": profile.budget(),
                    "estimated_tokens": profile.estimated_tokens,
                    "observed_p95": profile.observed_p95,
                }
                for task_type, profile in self._profiles.items()
            },
        }
```

## Comparison

| Approach | Task Classification | Dynamic Budget | Usage Recording | Efficiency Tracking | Dashboard |
|---|---|---|---|---|---|
| TaskTypeClassifier | Yes (regex) | No | No | No | No |
| OutputTokenUsageRecorder | No | No | Yes (p95) | No | No |
| DynamicTokenBudgetAllocator | Via classifier | Yes (p95+margin) | Via recorder | No | No |
| BudgetEnforcedLLMCaller | Via allocator | Via allocator | Via allocator | Yes | No |
| OutputTokenBudgetDashboard | No | No | No | Via caller | Yes |

**Best for production**: Start with generous `estimated_tokens` for each task type (e.g., `lookup=200`, `code=2000`) — the system will tighten estimates automatically as observations accumulate. Require at least 10 observations before switching from `estimated_tokens` to `observed_p95` so early outliers do not create an unrealistically low budget. Monitor `avg_utilization` from `BudgetEnforcedLLMCaller.efficiency_stats()`: utilization consistently below 0.4 means budgets are too generous and costs can be reduced; above 0.95 means budgets are too tight and outputs are likely being truncated.
