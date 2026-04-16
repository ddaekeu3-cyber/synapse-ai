---
title: "Agent Doesn't Implement Automatic Context Pruning When Approaching Token Limit"
description: "Agents that accumulate conversation history without pruning will eventually hit the model's context limit mid-session, causing hard errors or silent content truncation that corrupts the agent's reasoning. Implement automatic context pruning that detects when the token budget is approaching its limit and removes lower-priority content — old tool results, redundant messages, or summarizable history — before each LLM call."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-automatic-context-pruning-when-approaching-token-limit
tags: [context-pruning, token-limit, context-management, conversation-history, sliding-window, context-overflow]
symptoms:
  - "Long sessions fail with context-length errors after 50+ turns"
  - "Model silently truncates context from the beginning, losing system instructions"
  - "No pruning strategy — context grows until the API call fails"
  - "Tool results from early turns are retained in full even though they are no longer relevant"
  - "Cannot determine current context utilization before making an LLM call"
---

## Why This Happens

Most agent frameworks append messages to a list and pass the full list to the API on every call. As turns accumulate, the list grows. At some threshold the API either rejects the request (hard error) or truncates from the beginning (soft corruption that removes system prompts). Automatic pruning detects high utilization before the API call and removes content in priority order: first old tool results, then assistant reasoning from early turns, then user messages beyond a recency window — while always preserving system instructions, the current user request, and recent context needed for coherence.

## Solution 1: Message Priority Classifier

```python
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Dict, Optional


class MessagePriority(IntEnum):
    CRITICAL = 0       # system prompt, current user message — never pruned
    HIGH = 1           # recent user/assistant turns (last N)
    MEDIUM = 2         # older assistant reasoning
    LOW = 3            # old tool results, verbose responses
    PRUNEABLE = 4      # confirmed redundant content


@dataclass
class ClassifiedMessage:
    message: Dict[str, Any]
    priority: MessagePriority
    token_estimate: int
    turn_index: int
    is_tool_result: bool = False
    is_system: bool = False
```

## Solution 2: Context Token Estimator

```python
from typing import Any, Dict, List


class ContextTokenEstimator:
    """
    Estimates total token usage for a message list using character ratio.
    Override with a tiktoken-backed implementation for accuracy.
    """

    def __init__(self, chars_per_token: float = 4.0):
        self._cpt = chars_per_token

    def estimate_message(self, message: Dict[str, Any]) -> int:
        content = message.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                block.get("text", "") if isinstance(block, dict) else str(block)
                for block in content
            )
        return max(1, int(len(str(content)) / self._cpt))

    def estimate_messages(self, messages: List[Dict[str, Any]]) -> int:
        return sum(self.estimate_message(m) for m in messages)
```

## Solution 3: Message Priority Classifier

```python
from typing import Any, Dict, List


class MessagePriorityClassifier:
    """
    Assigns pruning priority to each message based on role, position,
    and content type. System messages and the current user message
    are always CRITICAL; old tool results are LOW.
    """

    def __init__(
        self,
        recent_turns_high_priority: int = 6,
        estimator: ContextTokenEstimator = None,
    ):
        self._recent = recent_turns_high_priority
        self._estimator = estimator or ContextTokenEstimator()

    def classify(self, messages: List[Dict[str, Any]]) -> List[ClassifiedMessage]:
        classified = []
        user_turn_index = 0

        for i, msg in enumerate(messages):
            role = msg.get("role", "")
            is_system = role == "system"
            is_tool_result = role == "tool"
            is_last = i == len(messages) - 1

            if role == "user":
                user_turn_index += 1

            tokens = self._estimator.estimate_message(msg)
            turns_from_end = len(messages) - i

            if is_system or is_last:
                priority = MessagePriority.CRITICAL
            elif turns_from_end <= self._recent * 2:
                priority = MessagePriority.HIGH
            elif is_tool_result:
                priority = MessagePriority.LOW
            elif role == "assistant" and turns_from_end > self._recent * 2:
                priority = MessagePriority.MEDIUM
            else:
                priority = MessagePriority.MEDIUM

            classified.append(ClassifiedMessage(
                message=msg,
                priority=priority,
                token_estimate=tokens,
                turn_index=i,
                is_tool_result=is_tool_result,
                is_system=is_system,
            ))

        return classified
```

## Solution 4: Context Pruner

```python
from typing import Any, Dict, List, Optional, Tuple


class ContextPruner:
    """
    Removes messages in priority order (lowest first) until the
    estimated token count is within the target budget.
    Never removes CRITICAL messages.
    """

    def prune(
        self,
        classified: List[ClassifiedMessage],
        target_tokens: int,
    ) -> Tuple[List[Dict[str, Any]], dict]:
        current_tokens = sum(c.token_estimate for c in classified)

        if current_tokens <= target_tokens:
            return [c.message for c in classified], {
                "pruned": False,
                "original_tokens": current_tokens,
                "final_tokens": current_tokens,
                "removed_count": 0,
            }

        # Sort pruneable messages lowest priority first, oldest first within tier
        candidates = sorted(
            [c for c in classified if c.priority > MessagePriority.HIGH],
            key=lambda c: (-c.priority, c.turn_index),
        )

        pruned_ids = set()
        tokens_removed = 0
        needed = current_tokens - target_tokens

        for candidate in candidates:
            if tokens_removed >= needed:
                break
            if candidate.priority == MessagePriority.CRITICAL:
                continue
            pruned_ids.add(id(candidate))
            tokens_removed += candidate.token_estimate

        remaining = [c.message for c in classified if id(c) not in pruned_ids]
        final_tokens = current_tokens - tokens_removed

        return remaining, {
            "pruned": True,
            "original_tokens": current_tokens,
            "final_tokens": final_tokens,
            "removed_count": len(pruned_ids),
            "tokens_removed": tokens_removed,
        }
```

## Solution 5: Auto-Pruning Context Manager

```python
from typing import Any, Dict, List, Optional


class AutoPruningContextManager:
    """
    Wraps message list management with automatic pruning.
    Checks token utilization before each LLM call and prunes
    if utilization exceeds the configured threshold.
    """

    def __init__(
        self,
        model_max_tokens: int,
        prune_threshold: float = 0.80,
        target_utilization: float = 0.65,
        estimator: ContextTokenEstimator = None,
        classifier: MessagePriorityClassifier = None,
        pruner: ContextPruner = None,
    ):
        self._max = model_max_tokens
        self._prune_at = prune_threshold
        self._target = target_utilization
        self._estimator = estimator or ContextTokenEstimator()
        self._classifier = classifier or MessagePriorityClassifier()
        self._pruner = pruner or ContextPruner()
        self._prune_count = 0
        self._total_tokens_removed = 0

    def prepare(self, messages: List[Dict[str, Any]]) -> tuple[List[Dict[str, Any]], dict]:
        current_tokens = self._estimator.estimate_messages(messages)
        utilization = current_tokens / max(self._max, 1)

        if utilization < self._prune_at:
            return messages, {
                "pruned": False,
                "utilization": round(utilization, 4),
                "current_tokens": current_tokens,
            }

        target_tokens = int(self._max * self._target)
        classified = self._classifier.classify(messages)
        pruned_messages, prune_meta = self._pruner.prune(classified, target_tokens)

        self._prune_count += 1
        self._total_tokens_removed += prune_meta.get("tokens_removed", 0)

        return pruned_messages, {
            **prune_meta,
            "utilization_before": round(utilization, 4),
            "utilization_after": round(prune_meta["final_tokens"] / max(self._max, 1), 4),
        }

    def stats(self) -> dict:
        return {
            "total_prune_operations": self._prune_count,
            "total_tokens_removed": self._total_tokens_removed,
        }
```

## Solution 6: Context Pruning Dashboard

```python
import time


class ContextPruningDashboard:
    """
    Reports pruning frequency, token savings, and current context
    health for operational monitoring.
    """

    def __init__(
        self,
        manager: AutoPruningContextManager,
        estimator: ContextTokenEstimator,
    ):
        self._manager = manager
        self._estimator = estimator

    def render(self, current_messages: list = None) -> dict:
        stats = self._manager.stats()
        current_tokens = (
            self._estimator.estimate_messages(current_messages)
            if current_messages else None
        )
        return {
            "generated_at": time.time(),
            "pruning_stats": stats,
            "current_context": {
                "estimated_tokens": current_tokens,
                "utilization": round(current_tokens / max(self._manager._max, 1), 4)
                if current_tokens else None,
            },
            "model_max_tokens": self._manager._max,
            "prune_threshold": self._manager._prune_at,
        }
```

## Comparison

| Approach | Token Estimation | Priority Classification | Priority-Order Pruning | Auto-Trigger | Dashboard |
|---|---|---|---|---|---|
| ContextTokenEstimator | Yes | No | No | No | No |
| MessagePriorityClassifier | Via estimator | Yes (5 tiers) | No | No | No |
| ContextPruner | No | No | Yes (lowest first) | No | No |
| AutoPruningContextManager | Via estimator | Via classifier | Via pruner | Yes (threshold) | No |
| ContextPruningDashboard | No | No | No | No | Yes |

**Best for production**: Set `prune_threshold=0.80` and `target_utilization=0.65` — this triggers pruning when 80% full and prunes to 65%, giving 15% headroom before the next prune cycle. Always classify the most recent user message as CRITICAL and preserve it regardless of how aggressive the pruning is — losing the current request is worse than losing historical context. Use `recent_turns_high_priority=6` (last 3 user-assistant pairs) to ensure the immediate conversation context is always preserved. Monitor `total_prune_operations` over time: a rising rate indicates sessions are getting longer and the prune threshold may need to be lowered to prevent edge cases where a single turn's token count exceeds the headroom between prune cycles.
