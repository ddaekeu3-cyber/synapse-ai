---
title: "Agent Doesn't Implement Dynamic Context Window Partitioning"
description: "Agents that treat the context window as a single flat buffer cannot optimize token allocation across competing content types: system instructions, conversation history, tool results, and retrieved documents all compete for space with no priority ordering. When the window fills, important content is either silently truncated or the request fails. Implement dynamic context window partitioning that reserves fixed slots for high-priority content and allocates remaining space proportionally across lower-priority sections."
date: 2026-04-16
difficulty: advanced
category: performance
slug: agent-doesnt-implement-dynamic-context-window-partitioning
tags: [context-window, token-allocation, partitioning, prompt-budget, content-priority, context-management]
symptoms:
  - "Tool results truncated silently when multiple large results fill the context"
  - "Conversation history grows unbounded until the request fails with a token limit error"
  - "System instructions compete for space with retrieved documents on every request"
  - "No visibility into how context space is allocated across content types"
  - "Context overflow errors at runtime because no proactive budget was enforced"
---

## Why This Happens

Context windows have fixed size and every byte counts. Without partitioning, content is added in arrival order until the window is full. System instructions — the highest-priority content — may be followed by a large tool result that pushes conversation history out of the window. Partitioning requires measuring each content section's token cost, allocating a budget per section based on priority, and truncating or summarizing sections that exceed their budget before assembling the final prompt.

## Solution 1: Context Partition Definition

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class PartitionPriority(int, Enum):
    CRITICAL = 4      # system prompt, tool definitions — never truncate
    HIGH = 3          # most recent turns, active tool results
    MEDIUM = 2        # older conversation history, secondary documents
    LOW = 1           # background context, optional enrichment


@dataclass
class ContextPartition:
    name: str
    priority: PartitionPriority
    reserved_tokens: int = 0         # minimum guaranteed tokens
    max_tokens: Optional[int] = None # hard ceiling (None = no ceiling)
    current_tokens: int = 0
    content: str = ""

    def is_over_budget(self, budget: int) -> bool:
        return self.current_tokens > budget

    def utilization(self, budget: int) -> float:
        return round(self.current_tokens / max(budget, 1), 4)
```

## Solution 2: Token Budget Allocator

```python
from typing import Dict, List


class TokenBudgetAllocator:
    """
    Allocates the available context window budget across partitions.
    Critical partitions receive their full reserved amount first;
    remaining budget is distributed proportionally by priority weight.
    """

    _PRIORITY_WEIGHTS = {
        PartitionPriority.CRITICAL: 0,    # fully reserved, not weighted
        PartitionPriority.HIGH: 3,
        PartitionPriority.MEDIUM: 2,
        PartitionPriority.LOW: 1,
    }

    def allocate(
        self,
        partitions: List[ContextPartition],
        total_budget: int,
        reserve_for_output: int = 1000,
    ) -> Dict[str, int]:
        usable = total_budget - reserve_for_output

        # Step 1: Reserve for CRITICAL partitions
        allocations: Dict[str, int] = {}
        remaining = usable
        for p in partitions:
            if p.priority == PartitionPriority.CRITICAL:
                alloc = p.max_tokens if p.max_tokens else p.reserved_tokens
                allocations[p.name] = alloc
                remaining -= alloc

        remaining = max(0, remaining)

        # Step 2: Reserve minimums for non-critical
        for p in partitions:
            if p.priority != PartitionPriority.CRITICAL and p.reserved_tokens > 0:
                alloc = min(p.reserved_tokens, remaining)
                allocations[p.name] = allocations.get(p.name, 0) + alloc
                remaining -= alloc

        remaining = max(0, remaining)

        # Step 3: Distribute remainder by priority weight
        non_critical = [p for p in partitions if p.priority != PartitionPriority.CRITICAL]
        total_weight = sum(self._PRIORITY_WEIGHTS[p.priority] for p in non_critical)
        if total_weight > 0:
            for p in non_critical:
                weight = self._PRIORITY_WEIGHTS[p.priority]
                extra = int(remaining * weight / total_weight)
                allocations[p.name] = allocations.get(p.name, 0) + extra
                if p.max_tokens:
                    allocations[p.name] = min(allocations[p.name], p.max_tokens)

        return allocations
```

## Solution 3: Content Truncator

```python
import re
from typing import Optional


class ContextContentTruncator:
    """
    Truncates content to fit within a token budget.
    Preserves sentence boundaries when possible.
    Estimates tokens at 4 chars per token.
    """

    CHARS_PER_TOKEN = 4.0

    def estimate_tokens(self, text: str) -> int:
        return max(1, int(len(text) / self.CHARS_PER_TOKEN))

    def truncate(
        self,
        content: str,
        max_tokens: int,
        truncation_marker: str = "\n\n[... truncated to fit context budget ...]",
    ) -> str:
        if self.estimate_tokens(content) <= max_tokens:
            return content

        marker_tokens = self.estimate_tokens(truncation_marker)
        target_chars = int((max_tokens - marker_tokens) * self.CHARS_PER_TOKEN)

        if target_chars <= 0:
            return truncation_marker

        # Try to cut at sentence boundary
        truncated = content[:target_chars]
        last_period = max(truncated.rfind(". "), truncated.rfind(".\n"))
        if last_period > target_chars * 0.7:
            truncated = truncated[:last_period + 1]

        return truncated + truncation_marker

    def truncate_turns(
        self,
        turns: list,
        max_tokens: int,
        keep_recent: int = 4,
    ) -> list:
        """
        Truncates older conversation turns first, always keeping
        the N most recent turns intact.
        """
        if not turns:
            return turns

        recent = turns[-keep_recent:]
        older = turns[:-keep_recent]

        recent_tokens = sum(
            self.estimate_tokens(str(t.get("content", ""))) for t in recent
        )
        budget_for_older = max(0, max_tokens - recent_tokens)

        kept_older = []
        used = 0
        for turn in reversed(older):
            cost = self.estimate_tokens(str(turn.get("content", "")))
            if used + cost <= budget_for_older:
                kept_older.insert(0, turn)
                used += cost
            else:
                break

        return kept_older + recent
```

## Solution 4: Context Window Assembler

```python
from typing import Any, Dict, List, Optional


class ContextWindowAssembler:
    """
    Assembles prompt components into a final prompt that respects
    partition budgets. Returns the assembled prompt and a budget report.
    """

    def __init__(
        self,
        allocator: TokenBudgetAllocator,
        truncator: ContextContentTruncator,
        total_context_tokens: int = 200_000,
        output_reserve_tokens: int = 4_000,
    ):
        self._allocator = allocator
        self._truncator = truncator
        self._total = total_context_tokens
        self._output_reserve = output_reserve_tokens

    def assemble(
        self,
        partitions: List[ContextPartition],
    ) -> dict:
        budgets = self._allocator.allocate(
            partitions, self._total, self._output_reserve
        )

        assembled_parts = {}
        budget_report = {}

        for partition in partitions:
            budget = budgets.get(partition.name, 0)
            actual_tokens = self._truncator.estimate_tokens(partition.content)

            if actual_tokens > budget:
                safe_content = self._truncator.truncate(partition.content, budget)
                used_tokens = self._truncator.estimate_tokens(safe_content)
                truncated = True
            else:
                safe_content = partition.content
                used_tokens = actual_tokens
                truncated = False

            assembled_parts[partition.name] = safe_content
            budget_report[partition.name] = {
                "budget_tokens": budget,
                "original_tokens": actual_tokens,
                "used_tokens": used_tokens,
                "truncated": truncated,
                "utilization": round(used_tokens / max(budget, 1), 4),
            }

        return {
            "parts": assembled_parts,
            "budget_report": budget_report,
            "total_used": sum(r["used_tokens"] for r in budget_report.values()),
            "total_budget": self._total - self._output_reserve,
        }
```

## Solution 5: Context Pressure Monitor

```python
import time
from collections import deque
from threading import Lock
from typing import Deque


class ContextPressureMonitor:
    """
    Tracks context window utilization over time and alerts when
    any partition is consistently hitting its budget ceiling.
    """

    def __init__(self, window_seconds: float = 600.0):
        self._window = window_seconds
        self._records: Deque[dict] = deque()
        self._lock = Lock()

    def record(self, assembly_result: dict) -> None:
        with self._lock:
            self._records.append({
                "ts": time.time(),
                "total_used": assembly_result.get("total_used", 0),
                "total_budget": assembly_result.get("total_budget", 1),
                "truncated_partitions": [
                    name for name, r in assembly_result.get("budget_report", {}).items()
                    if r.get("truncated")
                ],
            })

    def summary(self) -> dict:
        cutoff = time.time() - self._window
        with self._lock:
            recent = [r for r in self._records if r["ts"] >= cutoff]
        if not recent:
            return {"requests": 0}

        truncation_rates = {}
        for record in recent:
            for name in record.get("truncated_partitions", []):
                truncation_rates[name] = truncation_rates.get(name, 0) + 1

        avg_utilization = sum(
            r["total_used"] / max(r["total_budget"], 1) for r in recent
        ) / len(recent)

        return {
            "requests": len(recent),
            "avg_utilization": round(avg_utilization, 4),
            "truncation_counts": truncation_rates,
            "high_pressure": avg_utilization > 0.85,
        }
```

## Solution 6: Context Partitioning Dashboard

```python
import time


class ContextPartitioningDashboard:
    """
    Renders partition budgets, utilization, truncation rates,
    and pressure alerts for operational visibility.
    """

    def __init__(
        self,
        assembler: ContextWindowAssembler,
        monitor: ContextPressureMonitor,
    ):
        self._assembler = assembler
        self._monitor = monitor

    def render(self, last_assembly: dict) -> dict:
        return {
            "generated_at": time.time(),
            "last_assembly": {
                "total_used": last_assembly.get("total_used"),
                "total_budget": last_assembly.get("total_budget"),
                "partitions": last_assembly.get("budget_report", {}),
            },
            "pressure_summary": self._monitor.summary(),
            "config": {
                "total_context_tokens": self._assembler._total,
                "output_reserve_tokens": self._assembler._output_reserve,
            },
        }
```

## Comparison

| Approach | Priority Budgeting | Content Truncation | Turn Pruning | Utilization Tracking | Dashboard |
|---|---|---|---|---|---|
| TokenBudgetAllocator | Yes (weighted) | No | No | No | No |
| ContextContentTruncator | No | Yes (sentence boundary) | Yes (keep_recent) | No | No |
| ContextWindowAssembler | Via allocator | Via truncator | No | Via report | No |
| ContextPressureMonitor | No | No | No | Yes | No |
| ContextPartitioningDashboard | No | No | No | Via monitor | Yes |

**Best for production**: Reserve a CRITICAL partition for the system prompt and tool definitions — these must never be truncated. Set `keep_recent=6` for conversation turn pruning: the last 3 user+assistant pairs provide sufficient context for most tasks, and older history rarely changes the response. Alert via `ContextPressureMonitor` when `avg_utilization > 0.85` sustained over 10 minutes — this is the warning sign before truncation starts degrading responses. Treat `truncated=True` in the budget report as a soft SLO violation: if a content type is being truncated on every request, its budget allocation is too small and should be increased.
