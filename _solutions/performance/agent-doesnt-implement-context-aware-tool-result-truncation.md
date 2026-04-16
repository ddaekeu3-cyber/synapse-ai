---
title: "Agent Doesn't Implement Context-Aware Tool Result Truncation"
description: "Agents that inject full tool results into the LLM context regardless of remaining space will overflow the context window when results are large — causing hard errors or silent truncation by the model. Context-aware truncation measures available context space before injection, selects a truncation strategy based on result type and remaining budget, and preserves the highest-value content while staying within limits."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-context-aware-tool-result-truncation
tags: [context-truncation, context-window, token-budget, result-trimming, context-management, overflow-prevention]
symptoms:
  - "Tool results exceeding 10K tokens cause context overflow errors mid-session"
  - "Large API responses are injected verbatim, crowding out earlier conversation context"
  - "No measurement of remaining context budget before tool result injection"
  - "Truncation is applied uniformly — no consideration of which part of a result is most valuable"
  - "Long lists in tool results are injected in full even when only the first few items matter"
---

## Why This Happens

Tool results arrive with arbitrary sizes — a web scrape might return 50K characters, a database query might return 500 rows. The agent framework typically serializes the result to a string and appends it to the conversation. When no budget check occurs before injection, the combined context can exceed the model's limit. Context-aware truncation requires knowing three things: how many tokens are currently used, how many tokens the model supports, and how many tokens the incoming result consumes. With those numbers, a truncation strategy can preserve the most useful portion — the beginning of text, the first N list items, or the highest-scored rows — while fitting in the remaining budget.

## Solution 1: Context Budget Estimator

```python
from dataclasses import dataclass
from typing import Optional


@dataclass
class ContextBudgetState:
    model_max_tokens: int
    current_used_tokens: int
    reserved_for_output: int = 2048
    reserved_for_system: int = 500

    @property
    def available_for_injection(self) -> int:
        reserved = self.reserved_for_output + self.reserved_for_system
        return max(0, self.model_max_tokens - self.current_used_tokens - reserved)

    @property
    def utilization(self) -> float:
        return round(self.current_used_tokens / max(self.model_max_tokens, 1), 4)


class ContextBudgetEstimator:
    """
    Estimates token usage for a string using a character-ratio heuristic
    or a provided token-counting function.
    """

    def __init__(
        self,
        chars_per_token: float = 4.0,
        count_fn=None,
    ):
        self._chars_per_token = chars_per_token
        self._count_fn = count_fn

    def estimate(self, text: str) -> int:
        if self._count_fn:
            try:
                return self._count_fn(text)
            except Exception:
                pass
        return max(1, int(len(text) / self._chars_per_token))
```

## Solution 2: Truncation Strategy Selector

```python
from enum import Enum
from typing import Any


class TruncationStrategy(str, Enum):
    HEAD = "head"              # keep first N tokens of text
    TAIL = "tail"              # keep last N tokens of text
    HEAD_TAIL = "head_tail"    # keep first and last portions
    TOP_N_ITEMS = "top_n"      # keep first N items of a list
    SCORED_FILTER = "scored"   # keep highest-scored items
    SUMMARIZE_HINT = "hint"    # return a hint to summarize (no truncation)
    NONE = "none"              # do not truncate


class TruncationStrategySelector:
    """
    Selects a truncation strategy based on result type and available budget.
    """

    def select(self, result: Any, available_tokens: int, estimated_tokens: int) -> TruncationStrategy:
        if available_tokens >= estimated_tokens:
            return TruncationStrategy.NONE

        if isinstance(result, list):
            return TruncationStrategy.TOP_N_ITEMS

        if isinstance(result, dict):
            return TruncationStrategy.HEAD

        if isinstance(result, str):
            ratio = estimated_tokens / max(available_tokens, 1)
            if ratio > 5:
                return TruncationStrategy.HEAD_TAIL
            return TruncationStrategy.HEAD

        return TruncationStrategy.HEAD
```

## Solution 3: Result Truncator

```python
import json
from typing import Any, List, Optional


class ToolResultTruncator:
    """
    Applies a truncation strategy to a tool result given a token budget.
    Returns the truncated result and metadata about what was removed.
    """

    def __init__(self, estimator: ContextBudgetEstimator):
        self._estimator = estimator

    def truncate(
        self,
        result: Any,
        strategy: TruncationStrategy,
        budget_tokens: int,
    ) -> tuple[Any, dict]:
        if strategy == TruncationStrategy.NONE:
            return result, {"truncated": False}

        if isinstance(result, str):
            return self._truncate_string(result, strategy, budget_tokens)

        if isinstance(result, list):
            return self._truncate_list(result, budget_tokens)

        if isinstance(result, dict):
            serialized = json.dumps(result)
            truncated_str, meta = self._truncate_string(serialized, strategy, budget_tokens)
            return truncated_str, meta

        return result, {"truncated": False}

    def _truncate_string(
        self, text: str, strategy: TruncationStrategy, budget_tokens: int
    ) -> tuple[str, dict]:
        budget_chars = int(budget_tokens * 4)   # ~4 chars/token
        if len(text) <= budget_chars:
            return text, {"truncated": False}

        original_len = len(text)
        if strategy == TruncationStrategy.HEAD:
            truncated = text[:budget_chars] + f"\n[... {original_len - budget_chars} chars truncated]"
        elif strategy == TruncationStrategy.TAIL:
            truncated = f"[{original_len - budget_chars} chars omitted ...]\n" + text[-budget_chars:]
        elif strategy == TruncationStrategy.HEAD_TAIL:
            half = budget_chars // 2
            truncated = (
                text[:half]
                + f"\n[... {original_len - budget_chars} chars omitted ...]\n"
                + text[-half:]
            )
        else:
            truncated = text[:budget_chars] + f"\n[truncated]"

        return truncated, {
            "truncated": True,
            "original_chars": original_len,
            "retained_chars": budget_chars,
            "strategy": strategy.value,
        }

    def _truncate_list(self, items: list, budget_tokens: int) -> tuple[list, dict]:
        kept = []
        tokens_used = 0
        for item in items:
            item_str = json.dumps(item)
            item_tokens = self._estimator.estimate(item_str)
            if tokens_used + item_tokens > budget_tokens:
                break
            kept.append(item)
            tokens_used += item_tokens

        dropped = len(items) - len(kept)
        return kept, {
            "truncated": dropped > 0,
            "original_count": len(items),
            "retained_count": len(kept),
            "dropped_count": dropped,
            "strategy": TruncationStrategy.TOP_N_ITEMS.value,
        }
```

## Solution 4: Context-Aware Injection Pipeline

```python
from typing import Any, Callable, Optional


class ContextAwareInjectionPipeline:
    """
    Measures available context budget, selects a truncation strategy,
    applies truncation, and returns the injection-ready result.
    """

    def __init__(
        self,
        estimator: ContextBudgetEstimator,
        selector: TruncationStrategySelector,
        truncator: ToolResultTruncator,
    ):
        self._estimator = estimator
        self._selector = selector
        self._truncator = truncator
        self._total_injections = 0
        self._truncated_injections = 0

    def prepare(
        self,
        tool_name: str,
        result: Any,
        budget: ContextBudgetState,
    ) -> dict:
        import json
        result_str = json.dumps(result) if not isinstance(result, str) else result
        estimated_tokens = self._estimator.estimate(result_str)
        available = budget.available_for_injection

        strategy = self._selector.select(result, available, estimated_tokens)
        truncated_result, meta = self._truncator.truncate(result, strategy, available)

        self._total_injections += 1
        if meta.get("truncated"):
            self._truncated_injections += 1

        return {
            "tool_name": tool_name,
            "result": truncated_result,
            "estimated_tokens": estimated_tokens,
            "available_tokens": available,
            "truncation_meta": meta,
            "fits_in_budget": estimated_tokens <= available,
        }

    def stats(self) -> dict:
        return {
            "total_injections": self._total_injections,
            "truncated_injections": self._truncated_injections,
            "truncation_rate": round(
                self._truncated_injections / max(self._total_injections, 1), 4
            ),
        }
```

## Solution 5: Multi-Result Budget Allocator

```python
from typing import Any, Dict, List


class MultiResultBudgetAllocator:
    """
    Distributes a total token budget across multiple tool results
    in a single turn. Allocates proportionally by estimated size,
    with a minimum floor per result.
    """

    def __init__(self, min_tokens_per_result: int = 200):
        self._min = min_tokens_per_result

    def allocate(
        self,
        results: List[tuple[str, Any]],
        total_budget: int,
        estimator: ContextBudgetEstimator,
    ) -> Dict[str, int]:
        import json
        sizes = {}
        for tool_name, result in results:
            s = json.dumps(result) if not isinstance(result, str) else result
            sizes[tool_name] = estimator.estimate(s)

        total_estimated = sum(sizes.values())
        allocations = {}
        remaining = total_budget

        if total_estimated <= total_budget:
            return {name: sizes[name] for name, _ in results}

        for tool_name, estimated in sizes.items():
            proportion = estimated / max(total_estimated, 1)
            allocated = max(self._min, int(total_budget * proportion))
            allocations[tool_name] = allocated

        return allocations
```

## Solution 6: Injection Budget Dashboard

```python
import time


class ContextAwareTruncationDashboard:
    """
    Combines pipeline stats and budget utilization into an
    operational report for context management tuning.
    """

    def __init__(
        self,
        pipeline: ContextAwareInjectionPipeline,
    ):
        self._pipeline = pipeline

    def render(self) -> dict:
        stats = self._pipeline.stats()
        return {
            "generated_at": time.time(),
            "injection_stats": stats,
            "health": {
                "truncation_rate": stats["truncation_rate"],
                "note": (
                    "high truncation rate suggests tool results are consistently oversized"
                    if stats["truncation_rate"] > 0.3 else "ok"
                ),
            },
        }
```

## Comparison

| Approach | Budget Measurement | Strategy Selection | String Truncation | List Truncation | Multi-Result Allocation |
|---|---|---|---|---|---|
| ContextBudgetEstimator | Yes (estimate) | No | No | No | No |
| TruncationStrategySelector | No | Yes (type-aware) | No | No | No |
| ToolResultTruncator | No | No | Yes (head/tail/both) | Yes (top-N) | No |
| ContextAwareInjectionPipeline | Via estimator | Via selector | Via truncator | Via truncator | No |
| MultiResultBudgetAllocator | No | No | No | No | Yes (proportional) |

**Best for production**: Set `reserved_for_output=4096` for complex reasoning tasks and `reserved_for_system=1000` to ensure system instructions survive context pressure from large tool results. Use `HEAD_TAIL` strategy for structured text (API docs, web pages) — the beginning contains metadata and the end contains the conclusion, both more useful than the middle. Monitor `truncation_rate`: above 0.30 means tools are returning results that are consistently too large and should be asked for smaller responses via their API parameters (e.g., `limit=10`, `fields=id,name`) before falling back to truncation.
