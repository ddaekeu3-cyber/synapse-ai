---
title: "Agent Doesn't Implement Context-Aware Tool Result Truncation"
description: "Agents that inject full tool results into the LLM context regardless of size waste tokens on content that dilutes the signal: a database query returning 500 rows when the model needs 3, a web page response containing navigation menus and footers when only the article body matters, or a file read returning 10,000 lines when only 50 are relevant. Implement context-aware tool result truncation that measures content relevance, applies role-specific length budgets, and preserves the most informative portion of each result."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-context-aware-tool-result-truncation
tags: [result-truncation, context-efficiency, token-budget, relevance-truncation, content-pruning, context-injection]
symptoms:
  - "Tool results fill the context window with hundreds of rows when only a few are needed"
  - "Web scraping results inject full HTML including navigation, ads, and footers"
  - "Database results are injected verbatim — thousands of tokens for a simple query"
  - "No per-tool length budget — some tools routinely consume 80% of the context"
  - "Model's answer quality degrades because relevant content is buried in irrelevant padding"
---

## Why This Happens

Tools return what they return. A database tool executing `SELECT * FROM events` returns all matching rows. A web scraper returns the full page. A file reader returns the full file. Without a truncation layer between the tool and the context injection, every result competes for the same fixed context budget. The model's attention is bounded — injecting 4,000 tokens of a 100-row query result when the model only needs the top 3 rows means the relevant signal is diluted by 97 rows of noise. Context-aware truncation applies per-result length limits, preserves structure, and — when a relevance signal is available — selects the most relevant portion rather than the first N characters.

## Solution 1: Truncation Policy

```python
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class TruncationStrategy(str, Enum):
    HEAD = "head"             # keep first N tokens
    TAIL = "tail"             # keep last N tokens
    HEAD_AND_TAIL = "head_and_tail"  # keep first N/2 and last N/2
    RELEVANT_FIRST = "relevant_first"  # reorder by relevance score before truncating
    STRUCTURED = "structured"          # respect JSON/list structure boundaries


@dataclass
class TruncationPolicy:
    max_tokens: int = 2000
    strategy: TruncationStrategy = TruncationStrategy.HEAD
    head_fraction: float = 0.7    # for HEAD_AND_TAIL: proportion from head
    add_truncation_notice: bool = True
    tokens_per_char: float = 0.25
    min_tokens: int = 100          # never truncate below this

    def max_chars(self) -> int:
        return int(self.max_tokens / self.tokens_per_char)

    def estimate_tokens(self, text: str) -> int:
        return int(len(text) * self.tokens_per_char)
```

## Solution 2: Tool Result Truncator

```python
from typing import Optional


class ToolResultTruncator:
    """
    Applies a TruncationPolicy to a tool result string.
    Produces a truncated version that fits within the token budget.
    """

    NOTICE = "\n[... result truncated — {dropped} tokens omitted ...]"

    def truncate(self, text: str, policy: TruncationPolicy) -> str:
        estimated = policy.estimate_tokens(text)
        if estimated <= policy.max_tokens:
            return text

        max_chars = policy.max_chars()
        strategy = policy.strategy

        if strategy == TruncationStrategy.HEAD:
            truncated = text[:max_chars]

        elif strategy == TruncationStrategy.TAIL:
            truncated = text[-max_chars:]

        elif strategy == TruncationStrategy.HEAD_AND_TAIL:
            head_chars = int(max_chars * policy.head_fraction)
            tail_chars = max_chars - head_chars
            truncated = text[:head_chars] + "\n[...]\n" + text[-tail_chars:]

        elif strategy == TruncationStrategy.STRUCTURED:
            truncated = self._structured_truncate(text, max_chars)

        else:
            truncated = text[:max_chars]

        if policy.add_truncation_notice:
            dropped_tokens = estimated - policy.estimate_tokens(truncated)
            notice = self.NOTICE.format(dropped=dropped_tokens)
            truncated = truncated + notice

        return truncated

    def _structured_truncate(self, text: str, max_chars: int) -> str:
        """Try to break at a natural boundary (newline, JSON array item)."""
        if len(text) <= max_chars:
            return text
        candidate = text[:max_chars]
        # prefer breaking at a line boundary
        last_newline = candidate.rfind("\n")
        if last_newline > max_chars // 2:
            return candidate[:last_newline]
        return candidate
```

## Solution 3: Per-Tool Length Budget Registry

```python
import threading
from typing import Dict, Optional


class PerToolLengthBudgetRegistry:
    """
    Stores per-tool truncation policies and provides lookup
    with a fallback to the default policy.
    """

    def __init__(self, default_policy: Optional[TruncationPolicy] = None):
        self._default = default_policy or TruncationPolicy()
        self._policies: Dict[str, TruncationPolicy] = {}
        self._lock = threading.Lock()

    def register(self, tool_name: str, policy: TruncationPolicy) -> None:
        with self._lock:
            self._policies[tool_name] = policy

    def get(self, tool_name: str) -> TruncationPolicy:
        with self._lock:
            return self._policies.get(tool_name, self._default)

    def all_policies(self) -> Dict[str, TruncationPolicy]:
        with self._lock:
            return {**self._policies, "__default__": self._default}
```

## Solution 4: Relevance-Guided Truncator

```python
import re
from typing import List, Optional, Tuple


class RelevanceGuidedTruncator:
    """
    Splits a tool result into segments, scores each against a query,
    and selects the highest-scoring segments up to the token budget.
    Used when TruncationStrategy.RELEVANT_FIRST is selected.
    """

    def __init__(self, segment_size_chars: int = 300):
        self._seg_size = segment_size_chars

    def _split(self, text: str) -> List[str]:
        segments = []
        for i in range(0, len(text), self._seg_size):
            segments.append(text[i:i + self._seg_size])
        return segments

    def _score(self, segment: str, query_terms: List[str]) -> float:
        lower = segment.lower()
        return sum(1.0 for term in query_terms if term.lower() in lower)

    def truncate(
        self,
        text: str,
        policy: TruncationPolicy,
        query: str = "",
    ) -> str:
        if policy.estimate_tokens(text) <= policy.max_tokens:
            return text

        query_terms = re.findall(r"\w+", query) if query else []
        segments = self._split(text)

        if query_terms:
            scored = sorted(
                enumerate(segments),
                key=lambda iv: self._score(iv[1], query_terms),
                reverse=True,
            )
        else:
            scored = list(enumerate(segments))

        selected: List[Tuple[int, str]] = []
        budget = policy.max_chars()
        used = 0

        for orig_idx, seg in scored:
            if used + len(seg) > budget:
                break
            selected.append((orig_idx, seg))
            used += len(seg)

        # Restore original order for coherence
        selected.sort(key=lambda iv: iv[0])
        result = "\n".join(seg for _, seg in selected)

        if policy.add_truncation_notice and len(selected) < len(segments):
            dropped = len(segments) - len(selected)
            result += f"\n[... {dropped} segments omitted by relevance truncation ...]"

        return result
```

## Solution 5: Context Budget Allocator

```python
from typing import Dict, List, Tuple


class ContextBudgetAllocator:
    """
    Allocates the total context budget across multiple tool results
    proportionally or by priority, then truncates each to its allocation.
    """

    def __init__(
        self,
        truncator: ToolResultTruncator,
        total_budget_tokens: int = 8000,
    ):
        self._truncator = truncator
        self._budget = total_budget_tokens

    def allocate_and_truncate(
        self,
        results: List[Tuple[str, str, int]],  # (tool_name, content, priority)
    ) -> List[Tuple[str, str]]:
        """
        results: list of (tool_name, content, priority_weight)
        Returns: list of (tool_name, truncated_content)
        """
        if not results:
            return []

        total_weight = sum(w for _, _, w in results)
        output = []

        for tool_name, content, weight in results:
            share = int(self._budget * (weight / max(total_weight, 1)))
            share = max(share, 100)  # minimum allocation
            policy = TruncationPolicy(
                max_tokens=share,
                strategy=TruncationStrategy.HEAD_AND_TAIL,
            )
            truncated = self._truncator.truncate(content, policy)
            output.append((tool_name, truncated))

        return output
```

## Solution 6: Truncation Savings Monitor

```python
import time
from threading import Lock
from typing import List


class TruncationSavingsMonitor:
    """
    Records tokens saved by truncation across all tool results and
    surfaces which tools produce the most oversized results.
    """

    def __init__(self):
        self._events: List[dict] = []
        self._lock = Lock()

    def record(
        self,
        tool_name: str,
        original_tokens: int,
        final_tokens: int,
    ) -> None:
        with self._lock:
            self._events.append({
                "tool_name": tool_name,
                "original_tokens": original_tokens,
                "final_tokens": final_tokens,
                "saved_tokens": max(0, original_tokens - final_tokens),
                "ts": time.time(),
            })
            if len(self._events) > 50000:
                self._events.pop(0)

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [e for e in self._events if e["ts"] >= cutoff]

        if not recent:
            return {"window_seconds": window_seconds, "truncations": 0}

        from collections import defaultdict
        by_tool: dict = defaultdict(lambda: {"count": 0, "saved": 0})
        for e in recent:
            by_tool[e["tool_name"]]["count"] += 1
            by_tool[e["tool_name"]]["saved"] += e["saved_tokens"]

        total_saved = sum(e["saved_tokens"] for e in recent)
        total_original = sum(e["original_tokens"] for e in recent)

        return {
            "window_seconds": window_seconds,
            "truncations": len(recent),
            "total_tokens_saved": total_saved,
            "savings_pct": round(total_saved / max(total_original, 1) * 100, 1),
            "by_tool": {
                tool: {"truncations": d["count"], "tokens_saved": d["saved"]}
                for tool, d in sorted(
                    by_tool.items(), key=lambda kv: kv[1]["saved"], reverse=True
                )[:10]
            },
        }
```

## Comparison

| Approach | Strategy-Based Cut | Relevance Ordering | Budget Allocation | Per-Tool Policy | Savings Tracking |
|---|---|---|---|---|---|
| ToolResultTruncator | Yes (4 strategies) | No | No | Via policy | No |
| RelevanceGuidedTruncator | No | Yes (query terms) | No | Via policy | No |
| PerToolLengthBudgetRegistry | No | No | No | Yes | No |
| ContextBudgetAllocator | Via truncator | No | Yes (proportional) | No | No |
| TruncationSavingsMonitor | No | No | No | No | Yes |

**Best for production**: Use `TruncationStrategy.HEAD_AND_TAIL` as the default — it preserves context structure (preamble + conclusion) better than a pure head cut for most tool result types. Assign `TruncationStrategy.RELEVANT_FIRST` only for tools that return large, homogeneous collections (search results, database rows) where ordering by relevance meaningfully improves signal. Monitor `savings_pct` via `TruncationSavingsMonitor`: consistently above 40% for a specific tool means that tool's default result size is far larger than what the agent actually consumes, and the tool's query scope should be narrowed at the source rather than compensating with truncation.
