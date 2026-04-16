---
title: "Agent Doesn't Implement Context Window Packing for Multi-Turn Summarization"
description: "Agents that retain full conversation history across many turns eventually fill the context window and either truncate early turns silently or fail entirely. Implement context window packing that summarizes older conversation segments into compact representations, preserving essential facts while freeing space for recent turns and new tool results."
date: 2026-04-16
difficulty: advanced
category: performance
slug: agent-doesnt-implement-context-window-packing-for-multi-turn-summarization
tags: [context-window, summarization, multi-turn, token-budget, conversation-compression, memory-management]
symptoms:
  - "Conversations fail or truncate after 20–30 turns as the context window fills"
  - "Agent forgets facts stated in early turns once they are silently truncated"
  - "No summarization of older turns — context is either kept in full or dropped entirely"
  - "Token usage grows linearly with conversation length rather than staying bounded"
  - "No measurement of how much context space is consumed by old vs recent turns"
---

## Why This Happens

LLM context windows are fixed-size. A naive implementation retains every message in full, so token consumption grows linearly with conversation length. At some point the history exceeds the window and either the system errors out or the oldest messages are silently dropped — losing facts the agent referenced earlier. Context window packing solves this by periodically summarizing the oldest N turns into a compact summary that is injected as a single synthetic message, then discarding the original turns. The summary preserves key entities, decisions, and constraints while consuming far fewer tokens.

## Solution 1: Conversation Turn Model

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class TurnRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"
    SUMMARY = "summary"    # synthetic — represents a compressed segment


@dataclass
class ConversationTurn:
    role: TurnRole
    content: str
    turn_index: int
    token_count: Optional[int] = None
    is_summary: bool = False
    summarized_range: Optional[tuple] = None    # (start_idx, end_idx) of turns compressed
    metadata: Dict[str, Any] = field(default_factory=dict)

    def estimated_tokens(self) -> int:
        if self.token_count is not None:
            return self.token_count
        return max(1, len(self.content) // 4)
```

## Solution 2: Context Token Budget Tracker

```python
from typing import List, Tuple


class ContextTokenBudgetTracker:
    """
    Tracks token consumption across conversation turns and
    reports when the context approaches the packing threshold.
    """

    def __init__(
        self,
        context_window_size: int = 128_000,
        packing_trigger_fraction: float = 0.75,
        reserved_for_response: int = 4096,
    ):
        self._window = context_window_size
        self._trigger = packing_trigger_fraction
        self._reserved = reserved_for_response

    def total_tokens(self, turns: List[ConversationTurn]) -> int:
        return sum(t.estimated_tokens() for t in turns)

    def available_tokens(self, turns: List[ConversationTurn]) -> int:
        used = self.total_tokens(turns)
        return max(0, self._window - used - self._reserved)

    def needs_packing(self, turns: List[ConversationTurn]) -> bool:
        used = self.total_tokens(turns)
        return used >= self._window * self._trigger

    def packing_stats(self, turns: List[ConversationTurn]) -> dict:
        used = self.total_tokens(turns)
        summaries = [t for t in turns if t.is_summary]
        return {
            "total_turns": len(turns),
            "total_tokens": used,
            "fill_fraction": round(used / self._window, 4),
            "available_tokens": self.available_tokens(turns),
            "needs_packing": self.needs_packing(turns),
            "summary_turns": len(summaries),
            "summary_tokens": sum(t.estimated_tokens() for t in summaries),
        }
```

## Solution 3: Segment Summarizer

```python
from typing import Any, Callable, List, Optional


class ConversationSegmentSummarizer:
    """
    Summarizes a list of conversation turns into a single compact
    summary turn using an LLM call. The summary preserves key facts,
    decisions, constraints, and named entities from the segment.
    """

    SUMMARY_PROMPT = (
        "Summarize the following conversation segment concisely. "
        "Preserve: key facts stated by the user, decisions made, "
        "constraints given, named entities, and any numbers or dates. "
        "Output a single dense paragraph. Do not add commentary.\n\n"
        "Segment:\n{segment}"
    )

    def __init__(self, max_summary_tokens: int = 300):
        self._max_tokens = max_summary_tokens

    def _format_segment(self, turns: List[ConversationTurn]) -> str:
        lines = []
        for t in turns:
            role_label = t.role.value.upper()
            lines.append(f"{role_label}: {t.content}")
        return "\n".join(lines)

    async def summarize(
        self,
        turns: List[ConversationTurn],
        llm_fn: Callable[[str, int], str],
        start_idx: int,
        end_idx: int,
    ) -> ConversationTurn:
        segment_text = self._format_segment(turns)
        prompt = self.SUMMARY_PROMPT.format(segment=segment_text)
        summary_text = await llm_fn(prompt, self._max_tokens)

        return ConversationTurn(
            role=TurnRole.SUMMARY,
            content=f"[Summary of turns {start_idx}–{end_idx}]: {summary_text}",
            turn_index=start_idx,
            is_summary=True,
            summarized_range=(start_idx, end_idx),
        )
```

## Solution 4: Context Packing Scheduler

```python
from typing import Any, Callable, List, Tuple


class ContextPackingScheduler:
    """
    Decides which turns to summarize and triggers summarization.
    Preserves the system prompt, the most recent N turns, and
    any previously generated summaries.
    """

    def __init__(
        self,
        budget_tracker: ContextTokenBudgetTracker,
        summarizer: ConversationSegmentSummarizer,
        turns_to_preserve: int = 8,
        min_turns_to_summarize: int = 4,
    ):
        self._budget = budget_tracker
        self._summarizer = summarizer
        self._preserve = turns_to_preserve
        self._min_summarize = min_turns_to_summarize

    def _select_segment(
        self, turns: List[ConversationTurn]
    ) -> Tuple[List[ConversationTurn], int, int]:
        """Returns (turns_to_summarize, start_idx, end_idx)."""
        # Keep system turns and recent turns; summarize the middle
        system_turns = [t for t in turns if t.role == TurnRole.SYSTEM]
        non_system = [t for t in turns if t.role != TurnRole.SYSTEM]

        if len(non_system) <= self._preserve + self._min_summarize:
            return [], 0, 0

        candidate_end = len(non_system) - self._preserve
        segment = non_system[:candidate_end]
        if len(segment) < self._min_summarize:
            return [], 0, 0

        start_idx = segment[0].turn_index
        end_idx = segment[-1].turn_index
        return segment, start_idx, end_idx

    async def maybe_pack(
        self,
        turns: List[ConversationTurn],
        llm_fn: Callable,
    ) -> List[ConversationTurn]:
        if not self._budget.needs_packing(turns):
            return turns

        segment, start_idx, end_idx = self._select_segment(turns)
        if not segment:
            return turns

        summary_turn = await self._summarizer.summarize(
            segment, llm_fn, start_idx, end_idx
        )

        system_turns = [t for t in turns if t.role == TurnRole.SYSTEM]
        recent_turns = [t for t in turns
                        if t.role != TurnRole.SYSTEM
                        and t.turn_index > end_idx]

        return system_turns + [summary_turn] + recent_turns
```

## Solution 5: Packed Context Builder

```python
from typing import Any, Callable, List


class PackedContextBuilder:
    """
    High-level interface that maintains a conversation turn list,
    appends new turns, and packs the context when needed before
    returning the turns for LLM context injection.
    """

    def __init__(
        self,
        scheduler: ContextPackingScheduler,
        budget_tracker: ContextTokenBudgetTracker,
    ):
        self._scheduler = scheduler
        self._budget = budget_tracker
        self._turns: List[ConversationTurn] = []
        self._next_index = 0
        self._pack_count = 0

    def add_turn(self, role: TurnRole, content: str, token_count: int = 0) -> None:
        self._turns.append(ConversationTurn(
            role=role,
            content=content,
            turn_index=self._next_index,
            token_count=token_count or None,
        ))
        self._next_index += 1

    async def get_packed_turns(self, llm_fn: Callable) -> List[ConversationTurn]:
        packed = await self._scheduler.maybe_pack(self._turns, llm_fn)
        if len(packed) < len(self._turns):
            self._pack_count += 1
            self._turns = packed
        return self._turns

    def stats(self) -> dict:
        return {
            **self._budget.packing_stats(self._turns),
            "pack_operations": self._pack_count,
        }
```

## Solution 6: Context Packing Dashboard

```python
import time
from typing import List


class ContextPackingDashboard:
    """
    Tracks packing events across multiple conversations and surfaces
    compression ratios and token savings from summarization.
    """

    def __init__(self):
        self._events: List[dict] = []

    def record_pack(
        self,
        session_id: str,
        tokens_before: int,
        tokens_after: int,
        turns_before: int,
        turns_after: int,
    ) -> None:
        self._events.append({
            "ts": time.time(),
            "session_id": session_id,
            "tokens_before": tokens_before,
            "tokens_after": tokens_after,
            "tokens_saved": tokens_before - tokens_after,
            "turns_before": turns_before,
            "turns_after": turns_after,
        })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [e for e in self._events if e["ts"] >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "pack_events": 0}
        total_saved = sum(e["tokens_saved"] for e in recent)
        avg_ratio = sum(
            e["tokens_after"] / max(e["tokens_before"], 1) for e in recent
        ) / len(recent)
        return {
            "window_seconds": window_seconds,
            "pack_events": len(recent),
            "total_tokens_saved": total_saved,
            "avg_compression_ratio": round(avg_ratio, 3),
            "avg_tokens_saved_per_pack": round(total_saved / len(recent), 0),
        }
```

## Comparison

| Approach | Token Tracking | Pack Trigger | Segment Selection | LLM Summarization | Dashboard |
|---|---|---|---|---|---|
| ContextTokenBudgetTracker | Yes | Yes (threshold) | No | No | No |
| ConversationSegmentSummarizer | No | No | No | Yes | No |
| ContextPackingScheduler | Via tracker | Via tracker | Yes | Via summarizer | No |
| PackedContextBuilder | Via tracker | Via scheduler | Via scheduler | Via scheduler | No |
| ContextPackingDashboard | No | No | No | No | Yes |

**Best for production**: Set `packing_trigger_fraction=0.75` to pack before the window fills — packing at 90% leaves insufficient headroom for the summarization call itself. Preserve at least 8 recent turns (`turns_to_preserve=8`) so the model always has full fidelity on recent context; summaries carry forward facts but lose nuance in phrasing. Use a dedicated low-latency model (e.g., Haiku) for the summarization call to minimize packing overhead. Monitor `avg_compression_ratio` from `ContextPackingDashboard`: a ratio above 0.6 (less than 40% compression) means summary prompts are too verbose and should be tightened to produce denser output.
