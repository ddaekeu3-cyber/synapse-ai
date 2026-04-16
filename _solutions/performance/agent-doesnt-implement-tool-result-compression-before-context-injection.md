---
title: "Agent Doesn't Implement Tool Result Compression Before Context Injection"
description: "Agents that inject raw tool results verbatim into the LLM context waste tokens on boilerplate, redundant fields, and formatting overhead — a JSON API response with 40 fields may yield only 3 fields the LLM needs. Implement tool result compression that strips irrelevant fields, summarizes long lists, collapses nested structures, and enforces per-tool token budgets before context injection, reducing prompt token cost without losing relevant signal."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-tool-result-compression-before-context-injection
tags: [context-compression, tool-results, token-reduction, field-pruning, context-injection, prompt-efficiency]
symptoms:
  - "Tool returns 200 fields but only 5 are referenced in the LLM response"
  - "Search result with 50 items is injected in full — 45 items are never mentioned"
  - "Nested JSON with 3 levels of metadata inflates context by 60% with no signal value"
  - "No per-tool token budget — a single large tool result can crowd out other context"
  - "Context window fills from tool results before the conversation history is included"
---

## Why This Happens

Tool APIs return data designed for applications, not LLMs — they include pagination metadata, internal IDs, timestamps, null fields, and deeply nested structures that are meaningless to the model. Without a compression layer between tool execution and context injection, every byte of the tool response is tokenized and charged. A compression pipeline that knows which fields matter for each tool, truncates long arrays to the top-k items, and flattens unnecessary nesting can reduce tool result token cost by 50–80% for typical API responses.

## Solution 1: Compression Strategy Definition

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set


class CompressionOp(str, Enum):
    KEEP_FIELDS = "keep_fields"         # whitelist of fields to retain
    DROP_FIELDS = "drop_fields"         # blacklist of fields to remove
    TRUNCATE_LIST = "truncate_list"     # keep only first N items
    FLATTEN = "flatten"                 # flatten one level of nesting
    SUMMARIZE_LIST = "summarize_list"   # replace long list with count + sample
    CUSTOM = "custom"                   # user-supplied transform fn


@dataclass
class ToolCompressionRule:
    tool_name: str
    operations: List[CompressionOp]
    keep_fields: Optional[Set[str]] = None       # for KEEP_FIELDS
    drop_fields: Optional[Set[str]] = None       # for DROP_FIELDS
    max_list_items: int = 10                     # for TRUNCATE_LIST / SUMMARIZE_LIST
    flatten_key: Optional[str] = None            # for FLATTEN — which key to hoist
    max_tokens_estimate: int = 2000              # soft budget per tool result
    custom_fn: Optional[Callable[[Any], Any]] = None
```

## Solution 2: Field Pruner

```python
from typing import Any, Dict, List, Optional, Set, Union


class FieldPruner:
    """
    Applies keep/drop field rules to dict-shaped tool results.
    Handles nested dicts by applying rules recursively to top-level only.
    """

    def prune(
        self,
        data: Any,
        keep: Optional[Set[str]] = None,
        drop: Optional[Set[str]] = None,
    ) -> Any:
        if isinstance(data, dict):
            if keep is not None:
                return {k: v for k, v in data.items() if k in keep}
            if drop is not None:
                return {k: v for k, v in data.items() if k not in drop}
            return data

        if isinstance(data, list):
            return [self.prune(item, keep, drop) for item in data]

        return data

    def drop_null_fields(self, data: Any) -> Any:
        if isinstance(data, dict):
            return {
                k: self.drop_null_fields(v)
                for k, v in data.items()
                if v is not None and v != "" and v != [] and v != {}
            }
        if isinstance(data, list):
            return [self.drop_null_fields(item) for item in data]
        return data
```

## Solution 3: List Compressor

```python
from typing import Any, List, Optional


class ListCompressor:
    """
    Compresses long lists to fit within token budgets.
    Supports hard truncation and summarization (count + sample).
    """

    def truncate(self, items: List[Any], max_items: int) -> List[Any]:
        return items[:max_items]

    def summarize(
        self,
        items: List[Any],
        max_sample: int = 3,
        key_field: Optional[str] = None,
    ) -> dict:
        """Replace a long list with a count + sample summary."""
        total = len(items)
        sample = items[:max_sample]

        if key_field:
            sample = [
                item.get(key_field, item) if isinstance(item, dict) else item
                for item in sample
            ]

        return {
            "total_count": total,
            "showing": min(max_sample, total),
            "sample": sample,
            "truncated": total > max_sample,
        }

    def compress_nested_lists(
        self,
        data: Any,
        max_items: int = 5,
        depth: int = 0,
        max_depth: int = 2,
    ) -> Any:
        if depth > max_depth:
            return data
        if isinstance(data, list) and len(data) > max_items:
            return self.truncate(data, max_items)
        if isinstance(data, dict):
            return {
                k: self.compress_nested_lists(v, max_items, depth + 1, max_depth)
                for k, v in data.items()
            }
        return data
```

## Solution 4: Tool Result Compressor

```python
import json
from typing import Any, Dict, List, Optional


class ToolResultCompressor:
    """
    Applies a ToolCompressionRule to a raw tool result.
    Applies operations in declared order and estimates output token count.
    """

    CHARS_PER_TOKEN = 4.0

    def __init__(self) -> None:
        self._pruner = FieldPruner()
        self._list_compressor = ListCompressor()
        self._rules: Dict[str, ToolCompressionRule] = {}

    def register(self, rule: ToolCompressionRule) -> None:
        self._rules[rule.tool_name] = rule

    def compress(self, tool_name: str, raw_result: Any) -> dict:
        rule = self._rules.get(tool_name)
        if rule is None:
            # Default: drop nulls, truncate any top-level lists to 20 items
            result = self._pruner.drop_null_fields(raw_result)
            if isinstance(result, list):
                result = self._list_compressor.truncate(result, 20)
            return {"result": result, "compressed": False, "rule_applied": "default"}

        result = raw_result

        for op in rule.operations:
            if op == CompressionOp.KEEP_FIELDS and rule.keep_fields:
                result = self._pruner.prune(result, keep=rule.keep_fields)

            elif op == CompressionOp.DROP_FIELDS and rule.drop_fields:
                result = self._pruner.prune(result, drop=rule.drop_fields)
                result = self._pruner.drop_null_fields(result)

            elif op == CompressionOp.TRUNCATE_LIST:
                if isinstance(result, list):
                    result = self._list_compressor.truncate(result, rule.max_list_items)
                elif isinstance(result, dict):
                    result = self._list_compressor.compress_nested_lists(result, rule.max_list_items)

            elif op == CompressionOp.SUMMARIZE_LIST:
                if isinstance(result, list) and len(result) > rule.max_list_items:
                    result = self._list_compressor.summarize(result, rule.max_list_items)

            elif op == CompressionOp.FLATTEN and rule.flatten_key:
                if isinstance(result, dict) and rule.flatten_key in result:
                    result = result[rule.flatten_key]

            elif op == CompressionOp.CUSTOM and rule.custom_fn:
                result = rule.custom_fn(result)

        serialized = json.dumps(result, default=str)
        token_estimate = int(len(serialized) / self.CHARS_PER_TOKEN)
        original_estimate = int(len(json.dumps(raw_result, default=str)) / self.CHARS_PER_TOKEN)

        return {
            "result": result,
            "compressed": True,
            "rule_applied": tool_name,
            "token_estimate": token_estimate,
            "original_token_estimate": original_estimate,
            "reduction_pct": round((1 - token_estimate / max(original_estimate, 1)) * 100, 1),
        }
```

## Solution 5: Token Budget Enforcer

```python
import json
from typing import Any, Dict, List, Tuple


class ContextTokenBudgetEnforcer:
    """
    Applies per-tool and total context token budgets to compressed results.
    If a compressed result still exceeds its budget, applies emergency truncation.
    """

    CHARS_PER_TOKEN = 4.0

    def __init__(
        self,
        total_context_budget_tokens: int = 8000,
        default_per_tool_budget_tokens: int = 2000,
    ) -> None:
        self._total_budget = total_context_budget_tokens
        self._default_per_tool = default_per_tool_budget_tokens

    def _estimate_tokens(self, data: Any) -> int:
        return int(len(json.dumps(data, default=str)) / self.CHARS_PER_TOKEN)

    def _emergency_truncate(self, data: Any, token_budget: int) -> Any:
        """Last-resort truncation: serialize and cut to budget."""
        serialized = json.dumps(data, default=str)
        max_chars = int(token_budget * self.CHARS_PER_TOKEN)
        if len(serialized) > max_chars:
            return serialized[:max_chars] + "... [truncated]"
        return data

    def enforce(
        self,
        compressed_results: List[Tuple[str, Any]],
        per_tool_budgets: Optional[Dict[str, int]] = None,
    ) -> List[Tuple[str, Any]]:
        """
        compressed_results: list of (tool_name, compressed_data)
        Returns results that fit within total budget, with per-tool enforcement.
        """
        per_tool = per_tool_budgets or {}
        enforced = []
        total_used = 0

        for tool_name, data in compressed_results:
            budget = per_tool.get(tool_name, self._default_per_tool)
            tokens = self._estimate_tokens(data)

            if tokens > budget:
                data = self._emergency_truncate(data, budget)
                tokens = budget

            if total_used + tokens > self._total_budget:
                remaining = self._total_budget - total_used
                if remaining <= 50:
                    break
                data = self._emergency_truncate(data, remaining)
                tokens = remaining

            enforced.append((tool_name, data))
            total_used += tokens

        return enforced
```

## Solution 6: Compression Savings Dashboard

```python
import time
from typing import List


class CompressionSavingsDashboard:
    """
    Tracks compression savings across all tool invocations,
    measuring token reduction per tool and overall context efficiency.
    """

    def __init__(self) -> None:
        self._records: List[dict] = []

    def record(self, compression_result: dict) -> None:
        if compression_result.get("compressed"):
            self._records.append({
                "tool": compression_result.get("rule_applied", "unknown"),
                "original_tokens": compression_result.get("original_token_estimate", 0),
                "compressed_tokens": compression_result.get("token_estimate", 0),
                "reduction_pct": compression_result.get("reduction_pct", 0.0),
                "recorded_at": time.time(),
            })

    def render(self) -> dict:
        if not self._records:
            return {"generated_at": time.time(), "invocations": 0}

        total_original = sum(r["original_tokens"] for r in self._records)
        total_compressed = sum(r["compressed_tokens"] for r in self._records)
        tokens_saved = total_original - total_compressed

        by_tool: dict = {}
        for r in self._records:
            t = r["tool"]
            if t not in by_tool:
                by_tool[t] = {"original": 0, "compressed": 0, "count": 0}
            by_tool[t]["original"] += r["original_tokens"]
            by_tool[t]["compressed"] += r["compressed_tokens"]
            by_tool[t]["count"] += 1

        return {
            "generated_at": time.time(),
            "invocations": len(self._records),
            "total_tokens_saved": tokens_saved,
            "avg_reduction_pct": round(
                sum(r["reduction_pct"] for r in self._records) / len(self._records), 1
            ),
            "by_tool": {
                t: {
                    "avg_reduction_pct": round(
                        (1 - v["compressed"] / max(v["original"], 1)) * 100, 1
                    ),
                    "invocations": v["count"],
                }
                for t, v in by_tool.items()
            },
        }
```

## Comparison

| Approach | Field Pruning | List Compression | Token Budget | Custom Rules | Savings Tracking |
|---|---|---|---|---|---|
| FieldPruner | Yes (keep/drop/null) | No | No | No | No |
| ListCompressor | No | Yes (truncate/summarize) | No | No | No |
| ToolResultCompressor | Via pruner | Via list compressor | No | Yes | No |
| ContextTokenBudgetEnforcer | No | No | Yes (per-tool + total) | No | No |
| CompressionSavingsDashboard | No | No | No | No | Yes |

**Best for production**: Register `ToolCompressionRule` for every tool that returns structured data. Start with `keep_fields` whitelists rather than `drop_fields` blacklists — it is safer to explicitly declare what the LLM needs than to assume all remaining fields are safe to include. Use `SUMMARIZE_LIST` for search results and database queries where the LLM needs to know "there are 847 results, here are the top 3" rather than seeing all 847. Target 60%+ token reduction per tool — if a tool achieves less than 30% reduction with your rules, review whether the remaining fields are genuinely necessary for LLM reasoning.
