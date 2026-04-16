---
title: "Agent Doesn't Implement Adaptive Context Truncation for Long Conversations"
description: "Agents that append every conversation turn to the context window without pruning hit the token limit after enough turns, then either hard-fail or silently drop the oldest messages. Implement adaptive context truncation that scores message importance, preserves high-value turns (tool results, key decisions, user constraints), and prunes low-value turns (pleasantries, redundant clarifications) to keep the context within budget while retaining maximum semantic value."
date: 2026-04-16
difficulty: advanced
category: performance
slug: agent-doesnt-implement-adaptive-context-truncation-for-long-conversations
tags: [context-truncation, conversation-management, context-window, message-pruning, token-budget, long-conversation]
symptoms:
  - "Agent hits context limit after 30 turns and returns a hard error"
  - "Oldest messages are silently dropped — agent forgets user constraints stated early in the conversation"
  - "Tool results from 10 turns ago are removed but were critical for answering current questions"
  - "No strategy for which messages to keep — truncation is purely chronological"
  - "Context budget is unknown until the API call fails with a context-length error"
---

## Why This Happens

The default truncation strategy is FIFO: drop the oldest messages when the context fills. This is wrong for agent conversations where early turns often contain the highest-value content: the user's original goal, stated constraints, tool results that established ground truth, and key decisions. Recent turns (confirmation messages, clarification exchanges, pleasantries) often have lower information density. Adaptive truncation scores each message, sorts by importance, and prunes the lowest-scoring messages first, preserving semantic continuity regardless of chronological position.

## Solution 1: Message Importance Scorer

```python
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional


class MessageRole(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass
class ScoredMessage:
    role: MessageRole
    content: str
    turn_index: int              # position in conversation (0 = first)
    importance_score: float = 1.0
    token_estimate: int = 0
    pinned: bool = False         # pinned messages are never pruned
    metadata: Dict[str, Any] = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}
        if self.token_estimate == 0:
            self.token_estimate = max(1, len(self.content) // 4)


class MessageImportanceScorer:
    """
    Scores messages on a 0.0–1.0 scale based on content signals.
    Higher scores = higher importance = preserved during truncation.
    """

    # Content signals that raise importance
    HIGH_VALUE_PATTERNS = [
        (re.compile(r"\b(?:must|require|constraint|never|always|critical|important)\b", re.I), 0.3),
        (re.compile(r"\b(?:error|fail|exception|traceback|cannot)\b", re.I), 0.2),
        (re.compile(r"```[\s\S]{50,}```"), 0.25),        # code blocks
        (re.compile(r"\b(?:result|output|found|retrieved)\b", re.I), 0.15),
        (re.compile(r"\b(?:goal|objective|task|please)\b", re.I), 0.1),
    ]

    LOW_VALUE_PATTERNS = [
        (re.compile(r"^(?:ok|sure|great|thanks|understood|got it)[.!]?$", re.I), -0.4),
        (re.compile(r"^(?:yes|no|okay)[.!]?$", re.I), -0.3),
        (re.compile(r"^(?:let me|i will|i'll|i can)\b", re.I), -0.15),
    ]

    def score(self, msg: ScoredMessage, total_turns: int) -> float:
        if msg.pinned:
            return 1.0

        score = 0.5   # baseline

        # Role weights
        if msg.role == MessageRole.SYSTEM:
            return 1.0   # always pin system messages
        if msg.role == MessageRole.TOOL:
            score += 0.2
        if msg.role == MessageRole.USER:
            score += 0.1

        # Recency bias: recent messages get a small boost
        recency = msg.turn_index / max(total_turns - 1, 1)
        score += recency * 0.1

        # Content signals
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        for pattern, delta in self.HIGH_VALUE_PATTERNS:
            if pattern.search(content):
                score += delta
        for pattern, delta in self.LOW_VALUE_PATTERNS:
            if pattern.search(content.strip()):
                score += delta

        return max(0.0, min(1.0, round(score, 4)))
```

## Solution 2: Adaptive Context Truncator

```python
from typing import List, Tuple


class AdaptiveContextTruncator:
    """
    Prunes conversation messages to fit within a token budget.
    Scoring-based: sorts messages by importance and drops the least important
    until the total token count is within budget.
    System messages and explicitly pinned messages are always preserved.
    """

    def __init__(
        self,
        scorer: MessageImportanceScorer,
        token_budget: int = 8192,
        min_messages_to_keep: int = 4,
        preserve_last_n_turns: int = 2,
    ):
        self._scorer = scorer
        self._budget = token_budget
        self._min_keep = min_messages_to_keep
        self._preserve_last = preserve_last_n_turns

    def truncate(
        self,
        messages: List[ScoredMessage],
    ) -> Tuple[List[ScoredMessage], "TruncationReport"]:
        total_turns = len(messages)

        # Score all messages
        for msg in messages:
            if not msg.pinned and msg.role != MessageRole.SYSTEM:
                msg.importance_score = self._scorer.score(msg, total_turns)

        # Pin last N turns and system messages
        for msg in messages[-self._preserve_last:]:
            msg.pinned = True

        total_tokens = sum(m.token_estimate for m in messages)
        if total_tokens <= self._budget:
            return messages, TruncationReport(
                original_count=len(messages),
                retained_count=len(messages),
                pruned_count=0,
                tokens_before=total_tokens,
                tokens_after=total_tokens,
                budget=self._budget,
            )

        # Sort prunable messages by importance ascending (prune lowest first)
        prunable = [
            m for m in messages
            if not m.pinned and m.role != MessageRole.SYSTEM
        ]
        prunable.sort(key=lambda m: m.importance_score)

        pruned_set = set()
        for candidate in prunable:
            if total_tokens <= self._budget:
                break
            if len(messages) - len(pruned_set) <= self._min_keep:
                break
            pruned_set.add(id(candidate))
            total_tokens -= candidate.token_estimate

        retained = [m for m in messages if id(m) not in pruned_set]
        tokens_after = sum(m.token_estimate for m in retained)

        return retained, TruncationReport(
            original_count=len(messages),
            retained_count=len(retained),
            pruned_count=len(pruned_set),
            tokens_before=sum(m.token_estimate for m in messages),
            tokens_after=tokens_after,
            budget=self._budget,
        )


from dataclasses import dataclass


@dataclass
class TruncationReport:
    original_count: int
    retained_count: int
    pruned_count: int
    tokens_before: int
    tokens_after: int
    budget: int

    def within_budget(self) -> bool:
        return self.tokens_after <= self.budget

    def reduction_pct(self) -> float:
        return round(self.pruned_count / max(self.original_count, 1) * 100, 1)
```

## Solution 3: Summary Injection for Pruned Context

```python
from typing import List, Optional


class PrunedContextSummarizer:
    """
    When messages are pruned, generates a compact summary of what was removed
    and injects it as a synthetic message so the LLM knows context was truncated.
    The summary is a plain-text list of key facts derived from pruned messages.
    """

    def __init__(self, max_summary_tokens: int = 300):
        self._max = max_summary_tokens

    def build_summary_message(
        self,
        pruned_messages: List[ScoredMessage],
        report: TruncationReport,
    ) -> Optional[ScoredMessage]:
        if not pruned_messages:
            return None

        lines = [
            f"[Context Truncation Note: {report.pruned_count} earlier messages were "
            f"condensed to fit the context window. Key points from removed messages:]"
        ]
        char_budget = self._max * 4  # rough token-to-char conversion
        for msg in sorted(pruned_messages, key=lambda m: -m.importance_score):
            snippet = (msg.content if isinstance(msg.content, str) else str(msg.content))[:200]
            line = f"- [{msg.role}] {snippet}"
            if sum(len(l) for l in lines) + len(line) > char_budget:
                break
            lines.append(line)

        summary_text = "\n".join(lines)
        return ScoredMessage(
            role=MessageRole.SYSTEM,
            content=summary_text,
            turn_index=-1,
            importance_score=0.9,
            pinned=True,
        )
```

## Solution 4: Conversation Window Manager

```python
from typing import Any, Dict, List, Optional, Tuple


class ConversationWindowManager:
    """
    Manages the full conversation lifecycle: adding turns, scoring,
    truncating to budget, and optionally injecting a pruning summary.
    """

    def __init__(
        self,
        truncator: AdaptiveContextTruncator,
        summarizer: Optional[PrunedContextSummarizer] = None,
    ):
        self._truncator = truncator
        self._summarizer = summarizer
        self._messages: List[ScoredMessage] = []
        self._turn_counter = 0
        self._total_pruned = 0

    def add_message(
        self,
        role: MessageRole,
        content: Any,
        pinned: bool = False,
    ) -> ScoredMessage:
        msg = ScoredMessage(
            role=role,
            content=content,
            turn_index=self._turn_counter,
            pinned=pinned,
        )
        self._messages.append(msg)
        self._turn_counter += 1
        return msg

    def get_context(self) -> Tuple[List[ScoredMessage], TruncationReport]:
        retained, report = self._truncator.truncate(list(self._messages))
        pruned = [m for m in self._messages if m not in retained]
        self._total_pruned += report.pruned_count

        if self._summarizer and pruned:
            summary = self._summarizer.build_summary_message(pruned, report)
            if summary:
                retained.insert(1, summary)  # after system message

        return retained, report

    def to_api_messages(self) -> List[dict]:
        retained, _ = self.get_context()
        result = []
        for m in retained:
            content = m.content if isinstance(m.content, str) else str(m.content)
            result.append({"role": m.role.value, "content": content})
        return result

    def stats(self) -> dict:
        return {
            "total_turns": self._turn_counter,
            "current_messages": len(self._messages),
            "total_pruned": self._total_pruned,
            "current_tokens": sum(m.token_estimate for m in self._messages),
        }
```

## Solution 5: Truncation Effectiveness Tracker

```python
import time
from collections import deque
from typing import Deque


class TruncationEffectivenessTracker:
    """
    Tracks truncation statistics across conversations.
    Alerts when prune rate is high (context is consistently over budget)
    or when minimum keep threshold is frequently hit.
    """

    def __init__(self, window_seconds: float = 3600.0):
        self._window = window_seconds
        self._events: Deque[dict] = deque()

    def record(self, report: TruncationReport) -> None:
        self._events.append({
            "ts": time.time(),
            "pruned": report.pruned_count,
            "retained": report.retained_count,
            "within_budget": report.within_budget(),
            "reduction_pct": report.reduction_pct(),
        })

    def _trim(self) -> None:
        cutoff = time.time() - self._window
        while self._events and self._events[0]["ts"] < cutoff:
            self._events.popleft()

    def stats(self) -> dict:
        self._trim()
        if not self._events:
            return {"calls": 0}
        over_budget = sum(1 for e in self._events if not e["within_budget"])
        avg_reduction = sum(e["reduction_pct"] for e in self._events) / len(self._events)
        alerts = []
        if over_budget > 0:
            alerts.append({
                "type": "over_budget",
                "count": over_budget,
                "message": f"{over_budget} truncations still exceeded budget — consider lowering token_budget or increasing preserve_last_n_turns.",
            })
        return {
            "calls": len(self._events),
            "over_budget_calls": over_budget,
            "avg_reduction_pct": round(avg_reduction, 1),
            "alerts": alerts,
        }
```

## Solution 6: Context Truncation Dashboard

```python
import time


class ContextTruncationDashboard:
    def __init__(
        self,
        manager: ConversationWindowManager,
        tracker: TruncationEffectivenessTracker,
    ):
        self._manager = manager
        self._tracker = tracker

    def render(self) -> dict:
        stats = self._manager.stats()
        effectiveness = self._tracker.stats()
        return {
            "generated_at": time.time(),
            "conversation": stats,
            "truncation_effectiveness": effectiveness,
            "healthy": len(effectiveness.get("alerts", [])) == 0,
        }
```

## Comparison

| Approach | Importance Scoring | Budget Enforcement | Summary Injection | Stats Tracking | Dashboard |
|---|---|---|---|---|---|
| MessageImportanceScorer | Yes (content signals) | No | No | No | No |
| AdaptiveContextTruncator | Via scorer | Yes | No | No | No |
| PrunedContextSummarizer | No | No | Yes | No | No |
| ConversationWindowManager | Via truncator | Via truncator | Via summarizer | Yes | No |
| TruncationEffectivenessTracker | No | No | No | Yes | No |

**Best for production**: Set `token_budget` conservatively at 70% of the model's context limit to leave room for the response. Always pin system messages and the first user message (which typically contains the original goal). Use `preserve_last_n_turns=3` to guarantee the most recent exchange is always present. Enable `PrunedContextSummarizer` so the LLM knows context was truncated — without this note, the LLM may confidently answer questions based on incomplete context without flagging the gap.
