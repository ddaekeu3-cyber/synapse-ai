---
title: "Agent Doesn't Implement Conversation History Pruning Strategy"
description: "Agents that grow conversation history unboundedly hit the context window limit mid-session, causing truncation errors or silent dropping of early messages. Implement a conversation history pruning strategy that trims history to fit the token budget while preserving the system prompt, the most recent turns, and high-importance messages identified by recency, relevance, or explicit pinning."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-conversation-history-pruning-strategy
tags: [conversation-pruning, context-window, history-management, token-budget, message-importance, context-compression]
symptoms:
  - "Context window exceeded error mid-session after many turns"
  - "Agent forgets earlier conversation context as history silently truncates from the front"
  - "No token counting before adding messages — overflow only detected at API call time"
  - "All messages treated equally — system prompt tokens compete with chat history"
  - "No way to pin important messages (tool results, user constraints) that must survive pruning"
---

## Why This Happens

Conversation history grows linearly with turns. Without proactive pruning, the agent eventually sends a prompt that exceeds the model's context window and receives a context-length error — or worse, the SDK silently truncates from the beginning, dropping system prompt content. A pruning strategy must reserve token budget for the system prompt and the completion, then allocate remaining capacity to conversation history using a priority scheme that keeps recent turns and high-value messages while compressing or dropping older, lower-value content.

## Solution 1: Message Record

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class MessageRole(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class MessageImportance(str, Enum):
    CRITICAL = "critical"   # never pruned (system prompt, pinned constraints)
    HIGH = "high"           # pruned last
    NORMAL = "normal"       # standard pruning order
    LOW = "low"             # pruned first


@dataclass
class ConversationMessage:
    role: MessageRole
    content: str
    importance: MessageImportance = MessageImportance.NORMAL
    token_count: Optional[int] = None
    turn_index: int = 0          # position in conversation
    pinned: bool = False         # pinned messages survive all pruning
    metadata: Dict[str, Any] = field(default_factory=dict)

    def estimated_tokens(self, chars_per_token: float = 4.0) -> int:
        if self.token_count is not None:
            return self.token_count
        return max(1, int(len(self.content) / chars_per_token)) + 4  # +4 for role overhead
```

## Solution 2: Token Budget Allocator

```python
from dataclasses import dataclass


@dataclass
class ContextTokenBudget:
    total_context_window: int    # model's total context window
    max_completion_tokens: int   # reserved for the model's response
    system_prompt_tokens: int    # reserved for system prompt
    safety_margin: int = 200     # buffer to avoid off-by-one overflows

    @property
    def available_for_history(self) -> int:
        used = self.system_prompt_tokens + self.max_completion_tokens + self.safety_margin
        return max(self.total_context_window - used, 0)

    @property
    def total_available_for_prompt(self) -> int:
        return self.total_context_window - self.max_completion_tokens - self.safety_margin


class TokenBudgetAllocator:
    """
    Computes available token budgets for each component of the prompt
    given a model context window and usage targets.
    """

    def __init__(
        self,
        context_window: int,
        max_completion_tokens: int,
        safety_margin: int = 200,
    ):
        self._window = context_window
        self._completion = max_completion_tokens
        self._margin = safety_margin

    def compute(self, system_prompt_tokens: int) -> ContextTokenBudget:
        return ContextTokenBudget(
            total_context_window=self._window,
            max_completion_tokens=self._completion,
            system_prompt_tokens=system_prompt_tokens,
            safety_margin=self._margin,
        )
```

## Solution 3: Message Importance Scorer

```python
import time
from typing import List


class MessageImportanceScorer:
    """
    Assigns a pruning priority score to each message.
    Higher score = keep longer. Combines recency, role, importance tag,
    and content signals (tool results, constraints).
    """

    def score(self, message: ConversationMessage, total_turns: int) -> float:
        if message.pinned:
            return float("inf")

        base = {
            MessageImportance.CRITICAL: 1000.0,
            MessageImportance.HIGH: 100.0,
            MessageImportance.NORMAL: 10.0,
            MessageImportance.LOW: 1.0,
        }[message.importance]

        # Recency bonus: most recent turns score higher
        recency = message.turn_index / max(total_turns, 1)
        base += recency * 50.0

        # Role bonus
        if message.role == MessageRole.SYSTEM:
            base += 500.0
        elif message.role == MessageRole.TOOL:
            base += 20.0  # tool results often contain key facts

        # Content signal: explicit constraints or numbered lists are high-value
        if any(kw in message.content.lower() for kw in ("must", "never", "always", "constraint", "requirement")):
            base += 15.0

        return base

    def rank(self, messages: List[ConversationMessage]) -> List[ConversationMessage]:
        total = len(messages)
        return sorted(messages, key=lambda m: self.score(m, total), reverse=True)
```

## Solution 4: Conversation History Pruner

```python
from typing import List, Tuple


class ConversationHistoryPruner:
    """
    Prunes conversation history to fit within the available token budget.
    Preserves pinned and CRITICAL messages unconditionally.
    Drops NORMAL/LOW importance messages in reverse recency order.
    """

    def __init__(
        self,
        scorer: MessageImportanceScorer,
        chars_per_token: float = 4.0,
    ):
        self._scorer = scorer
        self._chars_per_token = chars_per_token

    def prune(
        self,
        messages: List[ConversationMessage],
        token_budget: int,
    ) -> Tuple[List[ConversationMessage], dict]:
        """
        Returns (pruned_messages, stats).
        Preserves chronological order after pruning.
        """
        total_turns = len(messages)
        ranked = self._scorer.rank(messages)

        selected: List[ConversationMessage] = []
        used_tokens = 0
        dropped = 0

        for msg in ranked:
            tokens = msg.estimated_tokens(self._chars_per_token)
            if msg.pinned or msg.importance == MessageImportance.CRITICAL:
                selected.append(msg)
                used_tokens += tokens
            elif used_tokens + tokens <= token_budget:
                selected.append(msg)
                used_tokens += tokens
            else:
                dropped += 1

        # Restore chronological order
        selected.sort(key=lambda m: m.turn_index)

        stats = {
            "original_count": len(messages),
            "kept_count": len(selected),
            "dropped_count": dropped,
            "used_tokens_est": used_tokens,
            "token_budget": token_budget,
            "utilization": round(used_tokens / max(token_budget, 1), 4),
        }
        return selected, stats
```

## Solution 5: Pruning-Aware History Manager

```python
from typing import List, Optional


class PruningAwareHistoryManager:
    """
    Maintains the conversation history and applies pruning automatically
    before each LLM call. Supports adding messages with importance tags
    and pinning critical messages.
    """

    def __init__(
        self,
        pruner: ConversationHistoryPruner,
        allocator: TokenBudgetAllocator,
        system_prompt: str = "",
    ):
        self._pruner = pruner
        self._allocator = allocator
        self._system_prompt = system_prompt
        self._messages: List[ConversationMessage] = []
        self._turn_counter = 0
        self._prune_stats: List[dict] = []

    def add(
        self,
        role: MessageRole,
        content: str,
        importance: MessageImportance = MessageImportance.NORMAL,
        pinned: bool = False,
        token_count: Optional[int] = None,
    ) -> ConversationMessage:
        msg = ConversationMessage(
            role=role,
            content=content,
            importance=importance,
            token_count=token_count,
            turn_index=self._turn_counter,
            pinned=pinned,
        )
        self._messages.append(msg)
        self._turn_counter += 1
        return msg

    def get_pruned_history(self, system_prompt_tokens: int) -> List[ConversationMessage]:
        budget = self._allocator.compute(system_prompt_tokens)
        pruned, stats = self._pruner.prune(
            self._messages, budget.available_for_history
        )
        self._prune_stats.append(stats)
        return pruned

    def pin(self, turn_index: int) -> None:
        for msg in self._messages:
            if msg.turn_index == turn_index:
                msg.pinned = True

    def pruning_summary(self) -> dict:
        if not self._prune_stats:
            return {"calls": 0}
        drops = [s["dropped_count"] for s in self._prune_stats]
        return {
            "calls": len(self._prune_stats),
            "total_dropped": sum(drops),
            "mean_dropped_per_call": round(sum(drops) / len(drops), 2),
            "last_utilization": self._prune_stats[-1]["utilization"],
        }
```

## Solution 6: History Pruning Dashboard

```python
import time


class HistoryPruningDashboard:
    """
    Combines history manager stats with budget allocation into a
    view that shows context pressure and pruning frequency.
    """

    def __init__(
        self,
        manager: PruningAwareHistoryManager,
        allocator: TokenBudgetAllocator,
        system_prompt_tokens: int,
    ):
        self._manager = manager
        self._allocator = allocator
        self._sys_tokens = system_prompt_tokens

    def render(self) -> dict:
        budget = self._allocator.compute(self._sys_tokens)
        return {
            "generated_at": time.time(),
            "token_budget": {
                "context_window": budget.total_context_window,
                "reserved_completion": budget.max_completion_tokens,
                "reserved_system": budget.system_prompt_tokens,
                "available_for_history": budget.available_for_history,
            },
            "history": {
                "total_messages": len(self._manager._messages),
                "turn_count": self._manager._turn_counter,
                "pinned_count": sum(1 for m in self._manager._messages if m.pinned),
            },
            "pruning": self._manager.pruning_summary(),
        }
```

## Comparison

| Approach | Token Counting | Importance Scoring | Chronological Restore | Pinning | Dashboard |
|---|---|---|---|---|---|
| TokenBudgetAllocator | Yes (budget math) | No | No | No | No |
| MessageImportanceScorer | No | Yes (5 signals) | No | Yes (inf score) | No |
| ConversationHistoryPruner | Via messages | Via scorer | Yes | Yes | No |
| PruningAwareHistoryManager | Via pruner | Via pruner | Via pruner | Yes | No |
| HistoryPruningDashboard | Via allocator | No | No | No | Yes |

**Best for production**: Reserve at least 20% of the context window for completion tokens and set a hard `safety_margin=300` to avoid off-by-one overflows from token estimation errors. Pin tool results that contain user-specified constraints or confirmed facts — these are the messages most costly to lose. Use `MessageImportance.LOW` for agent scratchpad thoughts and intermediate reasoning steps; they are safe to prune first. Monitor `mean_dropped_per_call` — above 3 messages per call means sessions are routinely exceeding the budget and retrieval-augmented compression (summarizing older turns into a single message) should replace simple dropping.
