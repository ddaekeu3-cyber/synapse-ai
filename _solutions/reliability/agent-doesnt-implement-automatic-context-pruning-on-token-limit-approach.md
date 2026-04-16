---
title: "Agent Doesn't Implement Automatic Context Pruning on Token Limit Approach"
description: "Agents that grow context indefinitely until hitting the hard token limit crash with a context length error mid-turn, losing the work in progress. Implement automatic context pruning that detects when token usage approaches the limit and removes lower-priority content — tool result details, redundant messages, old observations — before the limit is reached, keeping the agent operational."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-automatic-context-pruning-on-token-limit-approach
tags: [context-pruning, token-limit, auto-truncation, context-management, overflow-prevention, message-prioritization]
symptoms:
  - "Agent crashes with 'context length exceeded' error mid-turn on long tasks"
  - "No automatic reduction of context before the limit is hit"
  - "Long tool results are injected in full even when only a few lines are relevant"
  - "Context size is only checked after the error — never proactively"
  - "Losing the in-progress turn forces the user to restart from scratch"
---

## Why This Happens

Agents accumulate context turn by turn. Each tool result, each assistant message, each user clarification adds tokens. Without proactive monitoring, the context grows until the LLM API rejects the next call with a context length error — at which point the work in progress is lost. Automatic pruning requires estimating token counts before each call, identifying low-priority content that can be removed or compressed, and applying pruning in priority order until the context fits within a safe budget.

## Solution 1: Context Message with Priority

```python
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Dict, Optional


class ContentPriority(IntEnum):
    CRITICAL = 0     # system prompt, current user message — never prune
    HIGH = 1         # recent assistant turns, recent tool calls
    MEDIUM = 2       # older assistant turns, summarized observations
    LOW = 3          # detailed tool results, intermediate reasoning
    PURGEABLE = 4    # redundant content, duplicate tool results


@dataclass
class PrioritizedMessage:
    role: str
    content: str
    priority: ContentPriority = ContentPriority.MEDIUM
    token_count: Optional[int] = None
    message_id: str = ""
    turn_index: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def estimated_tokens(self) -> int:
        if self.token_count is not None:
            return self.token_count
        return max(1, len(self.content) // 4)
```

## Solution 2: Token Budget Monitor

```python
from typing import List, Optional


class ContextTokenBudgetMonitor:
    """
    Monitors token usage and signals when pruning is needed.
    """

    def __init__(
        self,
        context_window: int = 200_000,
        prune_trigger_fraction: float = 0.85,
        hard_limit_fraction: float = 0.95,
        response_reserve: int = 8192,
    ):
        self._window = context_window
        self._prune_trigger = prune_trigger_fraction
        self._hard_limit = hard_limit_fraction
        self._reserve = response_reserve

    def total_tokens(self, messages: List[PrioritizedMessage]) -> int:
        return sum(m.estimated_tokens() for m in messages)

    def needs_pruning(self, messages: List[PrioritizedMessage]) -> bool:
        used = self.total_tokens(messages)
        return used >= self._window * self._prune_trigger

    def at_hard_limit(self, messages: List[PrioritizedMessage]) -> bool:
        used = self.total_tokens(messages)
        return used >= self._window * self._hard_limit

    def available_for_response(self, messages: List[PrioritizedMessage]) -> int:
        return max(0, self._window - self.total_tokens(messages) - self._reserve)

    def prune_target_tokens(self, messages: List[PrioritizedMessage]) -> int:
        """How many tokens need to be freed to drop below prune_trigger."""
        used = self.total_tokens(messages)
        target = int(self._window * self._prune_trigger * 0.80)
        return max(0, used - target)

    def status(self, messages: List[PrioritizedMessage]) -> dict:
        used = self.total_tokens(messages)
        return {
            "used_tokens": used,
            "context_window": self._window,
            "fill_fraction": round(used / self._window, 4),
            "needs_pruning": self.needs_pruning(messages),
            "at_hard_limit": self.at_hard_limit(messages),
            "available_for_response": self.available_for_response(messages),
        }
```

## Solution 3: Priority-Based Pruner

```python
from typing import List, Tuple


class PriorityBasedContextPruner:
    """
    Removes messages in ascending priority order (PURGEABLE first,
    CRITICAL never) until the token target is met.
    Truncates long messages before removing them entirely.
    """

    def __init__(
        self,
        monitor: ContextTokenBudgetMonitor,
        truncate_long_results_to: int = 500,
    ):
        self._monitor = monitor
        self._truncate_to = truncate_long_results_to

    def prune(
        self,
        messages: List[PrioritizedMessage],
    ) -> Tuple[List[PrioritizedMessage], dict]:
        if not self._monitor.needs_pruning(messages):
            return messages, {"pruned": 0, "tokens_freed": 0}

        target = self._monitor.prune_target_tokens(messages)
        tokens_freed = 0
        pruned_count = 0
        result = list(messages)

        # Pass 1: truncate PURGEABLE and LOW long tool results
        for msg in result:
            if tokens_freed >= target:
                break
            if msg.priority >= ContentPriority.LOW:
                if msg.estimated_tokens() > self._truncate_to:
                    original = msg.estimated_tokens()
                    msg.content = msg.content[:self._truncate_to * 4] + "\n[truncated]"
                    msg.token_count = None
                    tokens_freed += original - msg.estimated_tokens()

        # Pass 2: remove PURGEABLE entirely
        if tokens_freed < target:
            keep = []
            for msg in result:
                if tokens_freed >= target or msg.priority < ContentPriority.PURGEABLE:
                    keep.append(msg)
                else:
                    tokens_freed += msg.estimated_tokens()
                    pruned_count += 1
            result = keep

        # Pass 3: remove LOW priority if still over target
        if tokens_freed < target:
            keep = []
            for msg in result:
                if tokens_freed >= target or msg.priority < ContentPriority.LOW:
                    keep.append(msg)
                else:
                    tokens_freed += msg.estimated_tokens()
                    pruned_count += 1
            result = keep

        return result, {
            "pruned_messages": pruned_count,
            "tokens_freed": tokens_freed,
            "remaining_messages": len(result),
            "remaining_tokens": self._monitor.total_tokens(result),
        }
```

## Solution 4: Tool Result Compressor

```python
import re
from typing import Optional


class ToolResultCompressor:
    """
    Compresses verbose tool results before they are added to context.
    Strips boilerplate, extracts key-value pairs, and truncates
    at a configurable character limit.
    """

    def __init__(self, max_chars: int = 2000, max_list_items: int = 10):
        self._max_chars = max_chars
        self._max_items = max_list_items

    def compress(self, result_text: str, tool_name: str = "") -> str:
        if len(result_text) <= self._max_chars:
            return result_text

        # Strip repeated whitespace and empty lines
        compressed = re.sub(r"\n{3,}", "\n\n", result_text.strip())
        compressed = re.sub(r"[ \t]{2,}", " ", compressed)

        # For list-like results, keep only first N items
        lines = compressed.splitlines()
        if len(lines) > self._max_items * 2:
            kept = lines[:self._max_items]
            omitted = len(lines) - self._max_items
            kept.append(f"... [{omitted} more lines omitted]")
            compressed = "\n".join(kept)

        # Hard truncation as final fallback
        if len(compressed) > self._max_chars:
            compressed = compressed[:self._max_chars] + f"\n[{len(result_text) - self._max_chars} chars truncated]"

        return compressed
```

## Solution 5: Auto-Pruning Context Manager

```python
from typing import List, Optional


class AutoPruningContextManager:
    """
    Wraps a message list and automatically prunes before each LLM call.
    New messages are added through this manager so token tracking stays current.
    """

    def __init__(
        self,
        monitor: ContextTokenBudgetMonitor,
        pruner: PriorityBasedContextPruner,
        compressor: ToolResultCompressor,
    ):
        self._monitor = monitor
        self._pruner = pruner
        self._compressor = compressor
        self._messages: List[PrioritizedMessage] = []
        self._prune_log: list = []

    def add(self, message: PrioritizedMessage) -> None:
        if message.priority >= ContentPriority.LOW and message.role == "tool":
            message.content = self._compressor.compress(message.content, message.role)
            message.token_count = None
        self._messages.append(message)

    def prepare_for_call(self) -> List[PrioritizedMessage]:
        if self._monitor.needs_pruning(self._messages):
            pruned, stats = self._pruner.prune(self._messages)
            self._messages = pruned
            self._prune_log.append(stats)
        return list(self._messages)

    def token_status(self) -> dict:
        return self._monitor.status(self._messages)

    def prune_history(self) -> list:
        return list(self._prune_log)
```

## Solution 6: Context Pruning Dashboard

```python
import time
from typing import List


class ContextPruningDashboard:
    """
    Tracks pruning events across sessions and surfaces how often
    pruning is triggered and how much space is reclaimed.
    """

    def __init__(self):
        self._events: List[dict] = []

    def record(self, prune_stats: dict, session_id: str = "") -> None:
        if prune_stats.get("pruned_messages", 0) > 0 or prune_stats.get("tokens_freed", 0) > 0:
            self._events.append({
                "ts": time.time(),
                "session_id": session_id,
                **prune_stats,
            })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [e for e in self._events if e["ts"] >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "prune_events": 0}
        return {
            "window_seconds": window_seconds,
            "prune_events": len(recent),
            "total_tokens_freed": sum(e.get("tokens_freed", 0) for e in recent),
            "avg_tokens_freed": round(
                sum(e.get("tokens_freed", 0) for e in recent) / len(recent), 0
            ),
            "total_messages_pruned": sum(e.get("pruned_messages", 0) for e in recent),
        }
```

## Comparison

| Approach | Token Monitoring | Priority Ordering | Truncation | Auto-Trigger | Dashboard |
|---|---|---|---|---|---|
| ContextTokenBudgetMonitor | Yes | No | No | Yes (trigger check) | No |
| PriorityBasedContextPruner | Via monitor | Yes (5 levels) | Yes | No | No |
| ToolResultCompressor | No | No | Yes (pre-add) | No | No |
| AutoPruningContextManager | Via monitor | Via pruner | Via compressor | Yes | No |
| ContextPruningDashboard | No | No | No | No | Yes |

**Best for production**: Set `prune_trigger_fraction=0.85` and target pruning down to 68% fill (0.85 × 0.80) — this gives 17 percentage points of headroom between the prune trigger and the hard limit so large tool results added between prune cycles don't immediately exceed the limit. Mark tool results `ContentPriority.LOW` and intermediate reasoning `ContentPriority.PURGEABLE` — these are the highest-volume and lowest-information-density content. Compress tool results at insertion time with `ToolResultCompressor` rather than waiting for pruning; compressing at insertion is cheaper and preserves more signal than truncating old results under time pressure.
