---
title: "Agent Doesn't Implement Prompt Compression for Long Contexts"
description: "Agents that pass full conversation histories and unabridged tool results to the LLM on every turn pay quadratic costs as context grows: a 50-turn conversation with verbose tool outputs can easily exceed 100k tokens, inflating latency and cost while diluting the model's attention. Implement prompt compression that removes redundant content, truncates verbose tool outputs, collapses repeated patterns, and summarizes distant history — reducing token count without losing semantic coverage."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-prompt-compression-for-long-contexts
tags: [prompt-compression, context-management, token-reduction, conversation-summarization, tool-output-truncation, cost-optimization]
symptoms:
  - "Token count grows linearly with conversation turns — costs spiral for long sessions"
  - "Verbose JSON tool outputs injected in full take up 80% of the context window"
  - "The same fact mentioned in five previous messages is repeated five times in context"
  - "No mechanism to compress old conversation turns while retaining semantic content"
  - "Context window hit after 30 turns — agent must truncate from the beginning with no summary"
---

## Why This Happens

LLM APIs charge per token and have finite context windows. Without compression, every token from every previous turn accumulates. Verbose tool outputs (a 10KB JSON API response injected in full) consume tokens far out of proportion to their information content. Repeated facts (the user's name mentioned in the first message and referenced in system context and in three tool results) waste tokens on redundancy. Prompt compression addresses this through multiple techniques: extracting only relevant fields from tool outputs, deduplicating repeated content, and summarizing turns older than a recency window using a fast, cheap model.

## Solution 1: Token Budget Estimator

```python
import re
from typing import Any, List, Union


class TokenBudgetEstimator:
    """
    Estimates token count from text using a character-based heuristic.
    Replace with a tiktoken-based estimator for production accuracy.
    """

    CHARS_PER_TOKEN = 4.0

    def estimate(self, text: str) -> int:
        return max(1, int(len(text) / self.CHARS_PER_TOKEN))

    def estimate_messages(self, messages: List[dict]) -> int:
        total = 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total += self.estimate(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        total += self.estimate(str(block.get("text", "") or block.get("content", "")))
        return total
```

## Solution 2: Tool Output Truncator

```python
import json
from typing import Any, Optional


class ToolOutputTruncator:
    """
    Reduces verbose tool outputs to their informational core.
    Truncates long strings, prunes list items beyond a maximum,
    and collapses deeply nested structures.
    """

    def __init__(
        self,
        max_string_length: int = 500,
        max_list_items: int = 10,
        max_depth: int = 3,
        max_total_chars: int = 2000,
    ):
        self._max_str = max_string_length
        self._max_list = max_list_items
        self._max_depth = max_depth
        self._max_total = max_total_chars

    def truncate(self, output: Any, depth: int = 0) -> Any:
        if depth > self._max_depth:
            return "...[truncated]"

        if isinstance(output, str):
            if len(output) > self._max_str:
                return output[:self._max_str] + f"...[{len(output) - self._max_str} chars omitted]"
            return output

        if isinstance(output, list):
            truncated = [self.truncate(item, depth + 1) for item in output[:self._max_list]]
            if len(output) > self._max_list:
                truncated.append(f"...[{len(output) - self._max_list} more items omitted]")
            return truncated

        if isinstance(output, dict):
            return {
                k: self.truncate(v, depth + 1)
                for k, v in output.items()
            }

        return output

    def truncate_to_str(self, output: Any) -> str:
        truncated = self.truncate(output)
        result = json.dumps(truncated, ensure_ascii=False) if not isinstance(truncated, str) else truncated
        if len(result) > self._max_total:
            result = result[:self._max_total] + f"\n...[total truncated at {self._max_total} chars]"
        return result
```

## Solution 3: Redundancy Eliminator

```python
import re
from typing import List


class MessageRedundancyEliminator:
    """
    Detects and removes near-duplicate content across messages.
    Tracks seen content fingerprints and marks redundant blocks
    for replacement with a reference stub.
    """

    def __init__(self, similarity_window_chars: int = 200):
        self._window = similarity_window_chars
        self._seen_fingerprints: set = set()

    def _fingerprint(self, text: str) -> str:
        import hashlib
        normalized = re.sub(r"\s+", " ", text.lower().strip())
        sample = normalized[:self._window]
        return hashlib.sha256(sample.encode()).hexdigest()[:16]

    def process_messages(self, messages: List[dict]) -> List[dict]:
        """
        Returns a new message list where duplicate content blocks
        are replaced with a compact reference.
        Resets seen fingerprints so each call is independent.
        """
        self._seen_fingerprints = set()
        result = []
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str) and len(content) > 100:
                fp = self._fingerprint(content)
                if fp in self._seen_fingerprints:
                    msg = {**msg, "content": "[duplicate content omitted]"}
                else:
                    self._seen_fingerprints.add(fp)
            result.append(msg)
        return result
```

## Solution 4: History Summarizer

```python
from typing import Any, Callable, List, Optional


class ConversationHistorySummarizer:
    """
    Summarizes conversation turns older than a recency window into
    a single compressed summary message using a cheap, fast model.
    Preserves the most recent N turns verbatim for full fidelity.
    """

    SUMMARY_ROLE = "system"
    SUMMARY_PREFIX = "[Conversation summary]"

    def __init__(
        self,
        summarize_fn: Callable[[str], str],   # (text_to_summarize) -> summary
        recent_turns_to_keep: int = 10,
        min_turns_to_summarize: int = 5,
    ):
        self._summarize = summarize_fn
        self._keep = recent_turns_to_keep
        self._min = min_turns_to_summarize

    async def compress(self, messages: List[dict]) -> List[dict]:
        """
        Returns a new message list with old turns replaced by a summary.
        System messages are always preserved.
        """
        system_msgs = [m for m in messages if m.get("role") == "system"]
        non_system = [m for m in messages if m.get("role") != "system"]

        if len(non_system) <= self._keep + self._min:
            return messages  # not enough to summarize

        to_summarize = non_system[:-self._keep]
        to_keep = non_system[-self._keep:]

        # Build text for summarization
        summary_input = "\n".join(
            f"{m.get('role', 'unknown').upper()}: {m.get('content', '')}"
            for m in to_summarize
            if isinstance(m.get("content"), str)
        )

        summary_text = await self._summarize(summary_input)
        summary_msg = {
            "role": self.SUMMARY_ROLE,
            "content": f"{self.SUMMARY_PREFIX} {summary_text}",
        }

        return system_msgs + [summary_msg] + to_keep
```

## Solution 5: Compression Pipeline

```python
from typing import Any, Callable, List


class PromptCompressionPipeline:
    """
    Applies tool output truncation, redundancy elimination, and
    history summarization in sequence to reduce context token count.
    Reports token savings.
    """

    def __init__(
        self,
        estimator: TokenBudgetEstimator,
        truncator: ToolOutputTruncator,
        deduplicator: MessageRedundancyEliminator,
        summarizer: Optional[ConversationHistorySummarizer] = None,
        target_token_budget: int = 50000,
    ):
        self._estimator = estimator
        self._truncator = truncator
        self._deduplicator = deduplicator
        self._summarizer = summarizer
        self._budget = target_token_budget

    async def compress(self, messages: List[dict]) -> dict:
        original_tokens = self._estimator.estimate_messages(messages)

        # Stage 1: Deduplicate
        messages = self._deduplicator.process_messages(messages)

        # Stage 2: Truncate tool outputs in tool_result messages
        compressed = []
        for msg in messages:
            if msg.get("role") == "tool" or "tool_result" in str(msg.get("type", "")):
                content = msg.get("content", "")
                if isinstance(content, str) and len(content) > 500:
                    msg = {**msg, "content": self._truncator.truncate_to_str(content)}
            compressed.append(msg)
        messages = compressed

        # Stage 3: Summarize history if still over budget
        after_dedup_tokens = self._estimator.estimate_messages(messages)
        if after_dedup_tokens > self._budget and self._summarizer:
            messages = await self._summarizer.compress(messages)

        final_tokens = self._estimator.estimate_messages(messages)

        return {
            "messages": messages,
            "original_tokens": original_tokens,
            "final_tokens": final_tokens,
            "tokens_saved": original_tokens - final_tokens,
            "compression_ratio": round(final_tokens / max(original_tokens, 1), 3),
        }
```

## Solution 6: Compression Savings Monitor

```python
import time
from typing import List


class CompressionSavingsMonitor:
    """
    Accumulates compression results to track token savings
    and compression ratio trends over time.
    """

    def __init__(self):
        self._reports: List[dict] = []
        self._recorded_at: List[float] = []

    def record(self, result: dict) -> None:
        self._reports.append(result)
        self._recorded_at.append(time.time())

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [
            r for r, ts in zip(self._reports, self._recorded_at)
            if ts >= cutoff
        ]
        if not recent:
            return {"window_seconds": window_seconds, "compressions": 0}
        total_saved = sum(r.get("tokens_saved", 0) for r in recent)
        avg_ratio = sum(r.get("compression_ratio", 1.0) for r in recent) / len(recent)
        return {
            "window_seconds": window_seconds,
            "compressions": len(recent),
            "total_tokens_saved": total_saved,
            "avg_compression_ratio": round(avg_ratio, 3),
            "avg_tokens_saved_per_call": round(total_saved / len(recent), 1),
        }
```

## Comparison

| Approach | Tool Output Truncation | Deduplication | History Summarization | Budget Enforcement | Savings Tracking |
|---|---|---|---|---|---|
| ToolOutputTruncator | Yes (depth + length) | No | No | No | No |
| MessageRedundancyEliminator | No | Yes (fingerprint) | No | No | No |
| ConversationHistorySummarizer | No | No | Yes (LLM-based) | No | No |
| PromptCompressionPipeline | Via truncator | Via deduplicator | Via summarizer | Yes (budget) | Per-call |
| CompressionSavingsMonitor | No | No | No | No | Yes (aggregate) |

**Best for production**: Apply `ToolOutputTruncator` to every tool result before context injection — this alone typically reduces context size by 40–60% for agents that use API-heavy tools. Set `max_total_chars=2000` for tool outputs: a summary of a large JSON response is almost always sufficient for the model. Use `ConversationHistorySummarizer` only when the context exceeds the token budget — summarization adds latency and should be a last resort. Use a fast, cheap model (e.g., claude-haiku) for summarization to minimize the latency cost. Monitor `avg_compression_ratio` over time: if it stays consistently below 0.5, your tool outputs are extremely verbose and their schemas should be redesigned to return only relevant fields.
