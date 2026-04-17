---
title: "Agent Doesn't Implement Conversation History Summarization Triggers"
description: "Agents that keep full conversation history in context until the window overflows either truncate important earlier turns or hit hard context limits. Without a summarization trigger, the agent silently drops turns when the window fills, losing critical early decisions and constraints. Implement history summarization triggers that detect approaching context limits, summarize older conversation segments before they are dropped, and replace raw turns with compact summaries that preserve key facts."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-conversation-history-summarization-triggers
tags: [history-summarization, context-management, token-budget, conversation-compression, trigger-policy, memory-management]
symptoms:
  - "Agent loses track of decisions made in earlier turns when history is truncated"
  - "Context window fills silently — earlier turns dropped without notice or summary"
  - "User refers to something said 20 turns ago and agent has no memory of it"
  - "No automatic mechanism to compact history before the window overflows"
  - "History management is purely reactive — truncation happens at the last moment"
---

## Why This Happens

Conversation history grows linearly with each turn. Most agents append turns indefinitely until the context window is full, at which point the framework drops the oldest turns to make room. This truncation is silent: the LLM receives a conversation that starts mid-discussion with no indication that earlier context was removed. Summarization triggers solve this proactively: before the window overflows, the agent detects that it is approaching the limit and collapses the oldest segment of history into a compact summary that preserves key decisions, facts, and constraints while consuming far fewer tokens.

## Solution 1: History Summarization Policy

```python
from dataclasses import dataclass


@dataclass
class HistorySummarizationPolicy:
    trigger_at_token_pct: float = 0.70    # trigger when history uses 70% of budget
    target_token_pct: float = 0.40        # compress until history is 40% of budget
    min_turns_before_summarize: int = 6   # never summarize fewer than 6 turns
    turns_to_keep_verbatim: int = 4       # always keep last N turns unsummarized
    summary_prefix: str = "[Earlier conversation summary]\n"
    tokens_per_char: float = 0.25

    def history_token_budget(self, total_window: int) -> int:
        return int(total_window * 0.30)   # 30% of window for history

    def trigger_threshold(self, budget: int) -> int:
        return int(budget * self.trigger_at_token_pct)

    def target_tokens(self, budget: int) -> int:
        return int(budget * self.target_token_pct)
```

## Solution 2: Trigger Detector

```python
from typing import List


@dataclass
class ConversationTurn:
    role: str        # "user" | "assistant"
    content: str
    turn_index: int
    token_count: int
    is_summary: bool = False


class SummarizationTriggerDetector:
    """
    Determines whether the current conversation history should be
    summarized based on token usage relative to the configured budget.
    """

    def __init__(self, policy: HistorySummarizationPolicy, total_window: int):
        self._policy = policy
        self._budget = policy.history_token_budget(total_window)
        self._trigger = policy.trigger_threshold(self._budget)

    def should_summarize(self, turns: List[ConversationTurn]) -> bool:
        if len(turns) < self._policy.min_turns_before_summarize:
            return False
        total_tokens = sum(t.token_count for t in turns)
        return total_tokens >= self._trigger

    def turns_to_summarize(
        self, turns: List[ConversationTurn]
    ) -> tuple:
        """
        Returns (turns_to_summarize, turns_to_keep).
        Always keeps the last N turns verbatim.
        """
        keep_count = self._policy.turns_to_keep_verbatim
        if len(turns) <= keep_count:
            return [], turns
        return turns[:-keep_count], turns[-keep_count:]
```

## Solution 3: History Segment Summarizer

```python
from typing import Any, Callable, List, Optional


class HistorySegmentSummarizer:
    """
    Calls the LLM to produce a compact summary of a conversation segment.
    Falls back to extractive summarization if the LLM call fails.
    """

    _SUMMARY_PROMPT = (
        "Summarize the following conversation segment concisely. "
        "Preserve: key decisions made, facts established, constraints set, "
        "user preferences expressed, and any explicit instructions. "
        "Omit: pleasantries, repetition, and content that was superseded.\n\n"
        "Conversation:\n{conversation}\n\nSummary:"
    )

    def __init__(
        self,
        llm_fn: Optional[Callable[[str], str]] = None,
        max_summary_chars: int = 1500,
    ):
        self._llm_fn = llm_fn
        self._max_chars = max_summary_chars

    def summarize(self, turns: List[ConversationTurn]) -> str:
        conversation_text = "\n".join(
            f"{t.role.upper()}: {t.content}" for t in turns
        )

        if self._llm_fn:
            try:
                prompt = self._SUMMARY_PROMPT.format(conversation=conversation_text[:8000])
                summary = self._llm_fn(prompt)
                return summary[: self._max_chars]
            except Exception:
                pass

        # Extractive fallback: keep first sentence of each turn
        lines = []
        for turn in turns:
            first_sentence = turn.content.split(".")[0].strip()
            if first_sentence:
                lines.append(f"{turn.role}: {first_sentence[:200]}")
        return "\n".join(lines)[: self._max_chars]
```

## Solution 4: History Compactor

```python
import time
from typing import Any, Callable, List, Optional


@dataclass
class CompactionResult:
    original_turn_count: int
    original_token_count: int
    compacted_turns: List[ConversationTurn]
    compacted_token_count: int
    summary_text: str
    tokens_saved: int
    compacted_at: float


class ConversationHistoryCompactor:
    """
    Orchestrates the full compaction cycle: detect trigger, split history,
    summarize the older segment, and return the new compacted turn list.
    """

    def __init__(
        self,
        policy: HistorySummarizationPolicy,
        detector: SummarizationTriggerDetector,
        summarizer: HistorySegmentSummarizer,
        tokens_per_char: float = 0.25,
    ):
        self._policy = policy
        self._detector = detector
        self._summarizer = summarizer
        self._tpc = tokens_per_char
        self._compaction_count = 0

    def _estimate_tokens(self, text: str) -> int:
        return max(1, int(len(text) * self._tpc))

    def maybe_compact(
        self, turns: List[ConversationTurn]
    ) -> tuple:
        """
        Returns (compacted_turns, compaction_result_or_None).
        """
        if not self._detector.should_summarize(turns):
            return turns, None

        to_summarize, to_keep = self._detector.turns_to_summarize(turns)
        if not to_summarize:
            return turns, None

        summary_text = self._summarizer.summarize(to_summarize)
        summary_turn = ConversationTurn(
            role="system",
            content=self._policy.summary_prefix + summary_text,
            turn_index=to_summarize[0].turn_index,
            token_count=self._estimate_tokens(summary_text),
            is_summary=True,
        )

        compacted = [summary_turn] + to_keep
        original_tokens = sum(t.token_count for t in turns)
        compacted_tokens = sum(t.token_count for t in compacted)

        self._compaction_count += 1
        result = CompactionResult(
            original_turn_count=len(turns),
            original_token_count=original_tokens,
            compacted_turns=compacted,
            compacted_token_count=compacted_tokens,
            summary_text=summary_text,
            tokens_saved=original_tokens - compacted_tokens,
            compacted_at=time.time(),
        )
        return compacted, result

    def compaction_count(self) -> int:
        return self._compaction_count
```

## Solution 5: Compaction Stats Monitor

```python
import time
from threading import Lock
from typing import List


class CompactionStatsMonitor:
    """
    Tracks history compaction events over time for capacity planning
    and prompt engineering decisions.
    """

    def __init__(self):
        self._lock = Lock()
        self._results: List[CompactionResult] = []

    def record(self, result: CompactionResult) -> None:
        with self._lock:
            self._results.append(result)

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [r for r in self._results if r.compacted_at >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "compactions": 0}

        total_saved = sum(r.tokens_saved for r in recent)
        avg_reduction = sum(
            r.tokens_saved / max(r.original_token_count, 1) for r in recent
        ) / len(recent)

        return {
            "window_seconds": window_seconds,
            "compactions": len(recent),
            "total_tokens_saved": total_saved,
            "avg_token_reduction_pct": round(avg_reduction * 100, 1),
            "avg_turns_before_compact": round(
                sum(r.original_turn_count for r in recent) / len(recent), 1
            ),
        }
```

## Solution 6: Compaction Dashboard

```python
import time


class HistoryCompactionDashboard:
    """
    Combines compaction trigger status, recent compaction stats,
    and policy configuration into a single operational view.
    """

    def __init__(
        self,
        policy: HistorySummarizationPolicy,
        compactor: ConversationHistoryCompactor,
        monitor: CompactionStatsMonitor,
        total_window: int,
    ):
        self._policy = policy
        self._compactor = compactor
        self._monitor = monitor
        self._window = total_window

    def render(self) -> dict:
        budget = self._policy.history_token_budget(self._window)
        return {
            "generated_at": time.time(),
            "policy": {
                "history_token_budget": budget,
                "trigger_threshold_tokens": self._policy.trigger_threshold(budget),
                "target_tokens_after_compact": self._policy.target_tokens(budget),
                "turns_kept_verbatim": self._policy.turns_to_keep_verbatim,
            },
            "total_compactions": self._compactor.compaction_count(),
            "stats_last_hour": self._monitor.summary(window_seconds=3600.0),
        }
```

## Comparison

| Approach | Trigger Detection | Segment Split | LLM Summarization | Fallback Extract | Stats Tracking |
|---|---|---|---|---|---|
| SummarizationTriggerDetector | Yes (token %) | Yes | No | No | No |
| HistorySegmentSummarizer | No | No | Yes (LLM) | Yes (extractive) | No |
| ConversationHistoryCompactor | Via detector | Via detector | Via summarizer | Via summarizer | No |
| CompactionStatsMonitor | No | No | No | No | Yes |
| HistoryCompactionDashboard | No | No | No | No | Yes (aggregate) |

**Best for production**: Set `trigger_at_token_pct=0.70` and `turns_to_keep_verbatim=4` — this ensures summarization runs with a 30% buffer before the window fills, and keeps the last four turns (two user/assistant exchanges) verbatim for conversational coherence. Use a fast, cheap model for summarization (not the same model used for the main conversation) to minimize the latency of the compaction step. Monitor `avg_turns_before_compact` in `CompactionStatsMonitor`: if this falls below 10, your history token budget is too small for the conversation patterns you serve and should be increased.
