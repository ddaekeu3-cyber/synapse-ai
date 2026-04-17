---
title: "Agent Doesn't Implement Context Compression for Long Conversations"
description: "Agents that append every message to the context window without compression will hit the token limit on long conversations, forcing a hard cutoff that discards recent messages or aborts the session. Implement context compression that summarizes older conversation segments into compact representations, preserves critical facts and decisions in a structured memory, and reconstructs a compressed context that fits within the token budget without losing essential continuity."
date: 2026-04-16
difficulty: advanced
category: performance
slug: agent-doesnt-implement-context-compression-for-long-conversations
tags: [context-compression, conversation-summarization, token-budget, long-context, memory-management, summarization]
symptoms:
  - "Long conversations hit the context window limit and the agent aborts or truncates messages"
  - "The agent forgets decisions made earlier in the conversation after the window fills"
  - "No mechanism to compact old conversation segments — the window grows linearly with turn count"
  - "Summarization happens after the limit is hit, not proactively before pressure builds"
  - "Compressed context is a single flat summary — structured facts are mixed with narrative"
---

## Why This Happens

LLM context windows have fixed token limits. A conversation that adds two messages per turn grows by roughly 200–500 tokens per turn; after 50 turns, a modest conversation consumes 10,000–25,000 tokens. Without proactive compression, the window fills and either the oldest messages are truncated (losing context) or the session fails. Reactive truncation is the worst outcome because the model loses the beginning of the conversation — where goals, constraints, and key decisions are established — while retaining the most recent small-talk. Proactive compression summarizes older segments before pressure builds, preserving structured facts while reducing token count.

## Solution 1: Conversation Segment

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


@dataclass
class ConversationMessage:
    role: MessageRole
    content: str
    turn_index: int
    token_estimate: int = 0
    pinned: bool = False            # pinned messages are never compressed
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def __post_init__(self):
        if not self.token_estimate:
            self.token_estimate = max(1, int(len(self.content) * 0.25))


@dataclass
class ConversationSegment:
    """A contiguous slice of conversation turns to be compressed together."""
    messages: List[ConversationMessage]
    start_turn: int
    end_turn: int

    @property
    def total_tokens(self) -> int:
        return sum(m.token_estimate for m in self.messages)

    @property
    def turn_count(self) -> int:
        return self.end_turn - self.start_turn + 1
```

## Solution 2: Compression Trigger Policy

```python
from dataclasses import dataclass


@dataclass
class CompressionTriggerPolicy:
    token_budget: int = 100_000            # total context token limit
    compression_threshold_pct: float = 75.0  # compress when at this % of budget
    min_turns_before_compress: int = 10    # don't compress very short conversations
    segment_size_turns: int = 8            # turns to include in each compressed segment
    keep_recent_turns: int = 6             # most recent turns always kept uncompressed
    target_compression_ratio: float = 0.2  # aim for summary to be 20% of original

    @property
    def compression_trigger_tokens(self) -> int:
        return int(self.token_budget * self.compression_threshold_pct / 100)
```

## Solution 3: Segment Summarizer

```python
from typing import Any, Callable, List, Optional


class ConversationSegmentSummarizer:
    """
    Summarizes a conversation segment into a compact representation.
    Uses an LLM call with a dedicated summarization prompt.
    """

    SUMMARY_PROMPT = """Summarize the following conversation segment concisely.
Preserve: key decisions made, facts established, user goals stated, any errors or corrections.
Format: bullet points. Maximum 150 words.

Conversation:
{conversation}

Summary:"""

    def __init__(
        self,
        llm_fn: Callable[[str], str],      # async fn(prompt) -> summary text
        max_summary_tokens: int = 200,
    ):
        self._llm = llm_fn
        self._max_summary_tokens = max_summary_tokens

    async def summarize(self, segment: ConversationSegment) -> str:
        conversation_text = "\n".join(
            f"{msg.role.value.upper()}: {msg.content}"
            for msg in segment.messages
        )
        prompt = self.SUMMARY_PROMPT.format(conversation=conversation_text)
        try:
            summary = await self._llm(prompt)
            return summary.strip()
        except Exception:
            # Fallback: extract first sentence from each assistant turn
            assistant_messages = [m for m in segment.messages if m.role == MessageRole.ASSISTANT]
            if assistant_messages:
                return " | ".join(
                    m.content.split(".")[0].strip()
                    for m in assistant_messages[:3]
                    if m.content
                )
            return f"[{segment.turn_count} turns compressed]"
```

## Solution 4: Structured Fact Extractor

```python
import re
from typing import List


@dataclass
class ExtractedFact:
    fact: str
    turn_index: int
    category: str    # "decision" | "constraint" | "goal" | "correction" | "entity"


class StructuredFactExtractor:
    """
    Extracts structured facts from conversation segments before compression.
    Facts are preserved separately from the narrative summary.
    """

    DECISION_PATTERNS = [
        r"(we decided|let's go with|I'll use|you should use|the plan is)\s+(.{10,100})",
        r"(confirmed|agreed|settled on)\s*:?\s*(.{10,100})",
    ]
    CONSTRAINT_PATTERNS = [
        r"(must not|should not|never|avoid|do not)\s+(.{10,100})",
        r"(requirement|constraint|rule)\s*:?\s*(.{10,100})",
    ]
    GOAL_PATTERNS = [
        r"(I want|I need|my goal is|the objective is|we're trying to)\s+(.{10,100})",
    ]

    def extract(self, segment: ConversationSegment) -> List[ExtractedFact]:
        facts = []
        for msg in segment.messages:
            facts.extend(self._scan(msg.content, msg.turn_index, self.DECISION_PATTERNS, "decision"))
            facts.extend(self._scan(msg.content, msg.turn_index, self.CONSTRAINT_PATTERNS, "constraint"))
            if msg.role == MessageRole.USER:
                facts.extend(self._scan(msg.content, msg.turn_index, self.GOAL_PATTERNS, "goal"))
        return facts

    def _scan(self, text: str, turn_index: int, patterns: list, category: str) -> List[ExtractedFact]:
        results = []
        for pattern in patterns:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                fact_text = match.group(0).strip()[:150]
                results.append(ExtractedFact(fact=fact_text, turn_index=turn_index, category=category))
        return results
```

## Solution 5: Context Compressor

```python
import time
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Tuple


@dataclass
class CompressedContext:
    compressed_messages: List[ConversationMessage]   # compressed + recent messages
    total_tokens: int
    original_tokens: int
    compressed_segments: int
    extracted_facts: List[ExtractedFact]
    compression_ratio: float
    compressed_at: float = field(default_factory=time.time)


class ConversationContextCompressor:
    """
    Orchestrates segment selection, summarization, fact extraction, and
    context reconstruction to produce a compressed conversation context.
    """

    def __init__(
        self,
        policy: CompressionTriggerPolicy,
        summarizer: ConversationSegmentSummarizer,
        fact_extractor: StructuredFactExtractor,
    ):
        self._policy = policy
        self._summarizer = summarizer
        self._extractor = fact_extractor

    def needs_compression(self, messages: List[ConversationMessage]) -> bool:
        total_tokens = sum(m.token_estimate for m in messages)
        turn_count = len([m for m in messages if m.role == MessageRole.USER])
        return (
            total_tokens >= self._policy.compression_trigger_tokens
            and turn_count >= self._policy.min_turns_before_compress
        )

    async def compress(
        self, messages: List[ConversationMessage]
    ) -> CompressedContext:
        original_tokens = sum(m.token_estimate for m in messages)
        pinned = [m for m in messages if m.pinned]
        compressible = [m for m in messages if not m.pinned]

        keep_recent = self._policy.keep_recent_turns * 2  # user+assistant pairs
        to_compress = compressible[:-keep_recent] if len(compressible) > keep_recent else []
        recent = compressible[-keep_recent:] if len(compressible) > keep_recent else compressible

        all_facts: List[ExtractedFact] = []
        summary_messages: List[ConversationMessage] = []
        seg_size = self._policy.segment_size_turns * 2

        for i in range(0, len(to_compress), seg_size):
            chunk = to_compress[i:i + seg_size]
            if not chunk:
                continue
            segment = ConversationSegment(
                messages=chunk,
                start_turn=chunk[0].turn_index,
                end_turn=chunk[-1].turn_index,
            )
            facts = self._extractor.extract(segment)
            all_facts.extend(facts)
            summary_text = await self._summarizer.summarize(segment)
            summary_msg = ConversationMessage(
                role=MessageRole.SYSTEM,
                content=f"[Summary of turns {segment.start_turn}–{segment.end_turn}]: {summary_text}",
                turn_index=chunk[0].turn_index,
                pinned=True,
            )
            summary_messages.append(summary_msg)

        compressed = pinned + summary_messages + recent
        total_tokens = sum(m.token_estimate for m in compressed)
        ratio = round(total_tokens / max(original_tokens, 1), 4)

        return CompressedContext(
            compressed_messages=compressed,
            total_tokens=total_tokens,
            original_tokens=original_tokens,
            compressed_segments=len(summary_messages),
            extracted_facts=all_facts,
            compression_ratio=ratio,
        )
```

## Solution 6: Compression Stats Monitor

```python
import time
from typing import List


class CompressionStatsMonitor:
    """
    Tracks compression events over time to measure token savings and
    detect sessions that require unusually frequent compression.
    """

    def __init__(self, max_records: int = 2000):
        self._max = max_records
        self._records: List[dict] = []

    def record(self, compressed: CompressedContext, session_id: str = "") -> None:
        if len(self._records) >= self._max:
            self._records.pop(0)
        self._records.append({
            "ts": time.time(),
            "session_id": session_id,
            "original_tokens": compressed.original_tokens,
            "compressed_tokens": compressed.total_tokens,
            "compression_ratio": compressed.compression_ratio,
            "segments": compressed.compressed_segments,
            "facts_extracted": len(compressed.extracted_facts),
        })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "compressions": 0}
        total_saved = sum(r["original_tokens"] - r["compressed_tokens"] for r in recent)
        avg_ratio = sum(r["compression_ratio"] for r in recent) / len(recent)
        return {
            "window_seconds": window_seconds,
            "compressions": len(recent),
            "total_tokens_saved": total_saved,
            "avg_compression_ratio": round(avg_ratio, 4),
            "avg_facts_per_compression": round(
                sum(r["facts_extracted"] for r in recent) / len(recent), 1
            ),
        }
```

## Comparison

| Approach | Trigger Policy | Segment Summarization | Fact Extraction | Reconstruction | Stats Tracking |
|---|---|---|---|---|---|
| CompressionTriggerPolicy | Yes (threshold + turns) | No | No | No | No |
| ConversationSegmentSummarizer | No | Yes (LLM call) | No | No | No |
| StructuredFactExtractor | No | No | Yes (regex patterns) | No | No |
| ConversationContextCompressor | Via policy | Via summarizer | Via extractor | Yes | No |
| CompressedContext | No | No | No | No | No |
| CompressionStatsMonitor | No | No | No | No | Yes |

**Best for production**: Trigger compression at 75% of the token budget — compressing at 90% leaves too little headroom and may require a second compression before the current context is consumed. Always pin system prompt messages and the first user message (which typically states the session goal) so they are never compressed. Use a dedicated low-latency model for summarization (not the full agent model) to minimize the latency cost of compression. Monitor `avg_compression_ratio` via `CompressionStatsMonitor`: a ratio above 0.5 means summaries are too verbose and the `max_summary_tokens` limit should be reduced; a ratio below 0.1 may indicate facts are being lost and the summarizer prompt needs refinement.
