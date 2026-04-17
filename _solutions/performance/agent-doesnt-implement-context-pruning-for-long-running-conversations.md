---
title: "Agent Doesn't Implement Context Pruning for Long-Running Conversations"
description: "Agents that accumulate every message and tool result in the context window hit token limits mid-conversation, causing hard failures or expensive truncation from the wrong end. Implement context pruning that scores messages by recency, relevance, and type, evicts low-value content while preserving the system prompt and recent turns, and maintains a rolling context budget."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-context-pruning-for-long-running-conversations
tags: [context-pruning, token-budget, long-context, message-eviction, context-window, conversation-management]
symptoms:
  - "Agent hits context window limit mid-conversation with no recovery strategy"
  - "Old tool results from the beginning of the conversation consume most of the token budget"
  - "Truncation always removes from the end, cutting off the most recent user message"
  - "No mechanism to summarize or compress older turns before evicting them"
  - "LLM calls fail with 'context length exceeded' rather than gracefully pruning"
---

## Why This Happens

LLM context windows are finite. Agents that append every message and tool result to a growing list will eventually exceed the limit. Naive implementations truncate from the tail — cutting the most recent messages — which is exactly backwards. Principled context pruning requires scoring each message by its value (recency, type, whether it was referenced in later turns) and evicting the lowest-value messages first while always preserving the system prompt, the current user message, and enough history for coherent responses.

## Solution 1: Message Value Scorer

```python
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional


class MessageRole(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL_RESULT = "tool_result"


@dataclass
class ScoredMessage:
    index: int                      # original position in conversation
    role: MessageRole
    content: str
    token_estimate: int
    score: float = 0.0              # higher = more valuable, keep last
    pinned: bool = False            # pinned messages are never evicted
    metadata: Dict[str, Any] = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


class MessageValueScorer:
    """
    Scores messages by recency, role importance, and content length.
    System messages and the most recent user/assistant pair are pinned.
    """

    ROLE_WEIGHTS = {
        MessageRole.SYSTEM: 1000.0,      # always pinned via weight
        MessageRole.USER: 3.0,
        MessageRole.ASSISTANT: 2.0,
        MessageRole.TOOL_RESULT: 1.0,    # tool results evicted first
    }
    RECENCY_DECAY = 0.85                 # score multiplier per position from end

    def score(self, messages: List[ScoredMessage]) -> List[ScoredMessage]:
        n = len(messages)
        for i, msg in enumerate(messages):
            recency = self.RECENCY_DECAY ** (n - 1 - i)
            role_weight = self.ROLE_WEIGHTS.get(msg.role, 1.0)
            msg.score = role_weight * recency

            # Pin system messages and last 2 turns unconditionally
            if msg.role == MessageRole.SYSTEM:
                msg.pinned = True
            elif i >= n - 4:  # last 2 user+assistant pairs
                msg.pinned = True

        return messages
```

## Solution 2: Token Budget Estimator

```python
import re
from typing import List


class TokenBudgetEstimator:
    """
    Estimates token counts for messages without calling the tokenizer API.
    Uses character-based heuristics (1 token ≈ 4 chars for English text).
    """

    CHARS_PER_TOKEN = 4.0
    OVERHEAD_PER_MESSAGE = 4        # role + formatting overhead

    def estimate(self, text: str) -> int:
        return max(1, int(len(text) / self.CHARS_PER_TOKEN) + self.OVERHEAD_PER_MESSAGE)

    def estimate_messages(self, messages: List[ScoredMessage]) -> int:
        return sum(m.token_estimate for m in messages)

    def annotate(self, messages: List[ScoredMessage]) -> List[ScoredMessage]:
        for msg in messages:
            msg.token_estimate = self.estimate(msg.content)
        return messages
```

## Solution 3: Context Pruner

```python
from typing import List, Tuple


class ContextPruner:
    """
    Evicts lowest-scoring unpinned messages until the total token
    count fits within the target budget.
    Returns the pruned message list and a pruning report.
    """

    def __init__(
        self,
        target_token_budget: int,
        min_messages_to_keep: int = 4,
    ):
        self._budget = target_token_budget
        self._min_keep = min_messages_to_keep

    def prune(
        self,
        messages: List[ScoredMessage],
    ) -> Tuple[List[ScoredMessage], dict]:
        original_count = len(messages)
        original_tokens = sum(m.token_estimate for m in messages)

        if original_tokens <= self._budget:
            return messages, {
                "pruned": False,
                "original_tokens": original_tokens,
                "final_tokens": original_tokens,
                "messages_removed": 0,
            }

        # Sort eviction candidates by score ascending (evict lowest first)
        evictable = sorted(
            [m for m in messages if not m.pinned],
            key=lambda m: m.score,
        )
        evicted_indices = set()
        current_tokens = original_tokens

        for candidate in evictable:
            if current_tokens <= self._budget:
                break
            pinned_count = sum(1 for m in messages if m.pinned)
            remaining = original_count - len(evicted_indices)
            if remaining - 1 < max(self._min_keep, pinned_count):
                break
            evicted_indices.add(candidate.index)
            current_tokens -= candidate.token_estimate

        pruned = [m for m in messages if m.index not in evicted_indices]
        return pruned, {
            "pruned": True,
            "original_count": original_count,
            "final_count": len(pruned),
            "messages_removed": len(evicted_indices),
            "original_tokens": original_tokens,
            "final_tokens": current_tokens,
            "tokens_freed": original_tokens - current_tokens,
        }
```

## Solution 4: Summarizing Context Compressor

```python
from typing import Any, Callable, List, Optional


class SummarizingContextCompressor:
    """
    Before evicting old messages, attempts to summarize them into
    a single compressed assistant message to preserve semantic content.
    Falls back to hard eviction if summarization fails.
    """

    SUMMARY_PROMPT = (
        "Summarize the following conversation history concisely, "
        "preserving key facts, decisions, and tool results:\n\n{history}"
    )

    def __init__(
        self,
        llm_fn: Callable[[str], str],
        max_messages_to_summarize: int = 10,
    ):
        self._llm = llm_fn
        self._max_summarize = max_messages_to_summarize

    async def compress(
        self,
        eviction_candidates: List[ScoredMessage],
    ) -> Optional[ScoredMessage]:
        if not eviction_candidates:
            return None

        candidates = eviction_candidates[:self._max_summarize]
        history = "\n".join(
            f"{m.role.value.upper()}: {m.content[:300]}"
            for m in candidates
        )
        prompt = self.SUMMARY_PROMPT.format(history=history)

        try:
            summary_text = await self._llm(prompt)
            return ScoredMessage(
                index=-1,
                role=MessageRole.ASSISTANT,
                content=f"[Compressed history summary]\n{summary_text}",
                token_estimate=len(summary_text) // 4,
                score=5.0,
                pinned=False,
                metadata={"compressed_from": len(candidates)},
            )
        except Exception:
            return None
```

## Solution 5: Rolling Context Manager

```python
from typing import Any, Dict, List, Optional


class RollingContextManager:
    """
    Maintains a rolling context window for a conversation.
    Automatically prunes when token budget is exceeded before each LLM call.
    """

    def __init__(
        self,
        scorer: MessageValueScorer,
        estimator: TokenBudgetEstimator,
        pruner: ContextPruner,
        token_budget: int = 100_000,
        prune_trigger_fraction: float = 0.90,
    ):
        self._scorer = scorer
        self._estimator = estimator
        self._pruner = pruner
        self._budget = token_budget
        self._trigger = prune_trigger_fraction
        self._messages: List[ScoredMessage] = []
        self._next_index = 0
        self._prune_events: List[dict] = []

    def add(self, role: MessageRole, content: str, metadata: Optional[dict] = None) -> None:
        msg = ScoredMessage(
            index=self._next_index,
            role=role,
            content=content,
            token_estimate=0,
            metadata=metadata or {},
        )
        self._next_index += 1
        self._messages.append(msg)

    def get_context(self) -> List[Dict[str, str]]:
        annotated = self._estimator.annotate(self._messages)
        scored = self._scorer.score(annotated)
        total = sum(m.token_estimate for m in scored)

        if total > self._budget * self._trigger:
            pruned, report = self._pruner.prune(scored)
            self._messages = pruned
            self._prune_events.append(report)

        return [
            {"role": m.role.value, "content": m.content}
            for m in self._messages
        ]

    def stats(self) -> dict:
        total_tokens = sum(m.token_estimate for m in self._messages)
        return {
            "message_count": len(self._messages),
            "estimated_tokens": total_tokens,
            "budget": self._budget,
            "utilization": round(total_tokens / self._budget, 3),
            "prune_events": len(self._prune_events),
        }
```

## Solution 6: Context Pruning Dashboard

```python
import time
from typing import List


class ContextPruningDashboard:
    """
    Surfaces context pruning frequency, tokens freed, and budget utilization
    across active conversations.
    """

    def __init__(self, managers: List[RollingContextManager]):
        self._managers = managers

    def render(self) -> dict:
        all_stats = [m.stats() for m in self._managers]
        total_prune_events = sum(s["prune_events"] for s in all_stats)
        avg_utilization = (
            sum(s["utilization"] for s in all_stats) / len(all_stats)
            if all_stats else 0.0
        )
        over_budget = [s for s in all_stats if s["utilization"] > 0.95]
        return {
            "generated_at": time.time(),
            "active_conversations": len(self._managers),
            "total_prune_events": total_prune_events,
            "avg_budget_utilization": round(avg_utilization, 3),
            "conversations_near_limit": len(over_budget),
            "per_conversation": all_stats,
        }
```

## Comparison

| Approach | Score-Based Eviction | Token Estimation | Summarization | Rolling Management | Dashboard |
|---|---|---|---|---|---|
| MessageValueScorer | Yes (recency+role) | No | No | No | No |
| TokenBudgetEstimator | No | Yes (char heuristic) | No | No | No |
| ContextPruner | Via scorer | Via estimator | No | No | No |
| SummarizingContextCompressor | No | No | Yes (LLM) | No | No |
| RollingContextManager | Via scorer | Via estimator | Optional | Yes | No |
| ContextPruningDashboard | No | No | No | No | Yes |

**Best for production**: Trigger pruning at 90% of the token budget rather than at 100% — leaving headroom prevents the next user message from immediately exceeding the limit after pruning. Always evict tool results before assistant messages: tool results are referenced in the assistant response that follows them, so once that response is in context the raw tool result adds minimal value. Use `SummarizingContextCompressor` only for long-running multi-hour sessions where semantic continuity matters more than cost — for typical sessions, hard eviction of old tool results is sufficient and avoids the summarization latency.
