---
title: "Agent Doesn't Implement Response Compression for Large Tool Outputs"
description: "Agents that pass raw tool outputs directly into the context window waste tokens on verbose JSON envelopes, repeated field names, and irrelevant data. A database query returning 200 rows, an API response with deeply nested metadata, or a search result with full HTML bodies can exhaust the context budget before the LLM can reason. Implement response compression that trims irrelevant fields, summarizes bulk rows, and enforces per-tool output budgets before injection."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-response-compression-for-large-tool-outputs
tags: [response-compression, context-window, token-budget, tool-output-trimming, field-pruning, output-summarization]
symptoms:
  - "Database tool returns 500-row JSON payload that fills half the context window"
  - "Search tool responses include full HTML body text instead of extracted content"
  - "API tool output contains deeply nested metadata that is never referenced by the LLM"
  - "Context window exhausts after three tool calls because outputs are not trimmed"
  - "No per-tool output token budget — each tool can inject arbitrarily large content"
---

## Why This Happens

Tool output is typically injected into the context window verbatim. The tool contract specifies what data to return, but nothing in the calling loop limits how large that data can be. A single SQL result set or a paginated API response can consume thousands of tokens with zero signal value. Response compression inserts a post-processing stage between tool execution and context injection: it prunes fields the LLM will not use, truncates oversized lists, and summarizes bulk results into a compact representation. The LLM reasons over the compressed output while the raw data remains available for follow-up queries if needed.

## Solution 1: Tool Output Compression Policy

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set


class CompressionStrategy(str, Enum):
    PASSTHROUGH = "passthrough"       # no compression
    FIELD_PRUNE = "field_prune"       # keep only allowed fields
    LIST_TRUNCATE = "list_truncate"   # cap list length
    ROW_SUMMARIZE = "row_summarize"   # summarize bulk rows to stats + sample
    SCHEMA_ONLY = "schema_only"       # return keys/types, not values
    CUSTOM = "custom"                 # caller-supplied compressor function


@dataclass
class ToolOutputCompressionPolicy:
    tool_name: str
    strategy: CompressionStrategy
    max_tokens: int = 2048            # hard cap on injected token estimate
    allowed_fields: Optional[Set[str]] = None    # for FIELD_PRUNE
    blocked_fields: Set[str] = field(default_factory=set)
    max_list_items: int = 20          # for LIST_TRUNCATE / ROW_SUMMARIZE
    summary_sample_rows: int = 3      # rows to include in ROW_SUMMARIZE sample
    include_compression_note: bool = True  # tell LLM data was compressed
```

## Solution 2: Field Pruner

```python
from typing import Any, Dict, List, Optional, Set, Union


class FieldPruner:
    """
    Recursively strips disallowed or blocked fields from a dict/list structure.
    If allowed_fields is set, only those top-level keys are retained.
    blocked_fields are removed at every nesting level.
    """

    def __init__(
        self,
        allowed_fields: Optional[Set[str]] = None,
        blocked_fields: Optional[Set[str]] = None,
    ):
        self._allowed = allowed_fields
        self._blocked = blocked_fields or set()

    def prune(self, value: Any) -> Any:
        if isinstance(value, dict):
            return self._prune_dict(value)
        if isinstance(value, list):
            return [self.prune(item) for item in value]
        return value

    def _prune_dict(self, d: dict) -> dict:
        result = {}
        for k, v in d.items():
            if k in self._blocked:
                continue
            if self._allowed is not None and k not in self._allowed:
                continue
            result[k] = self.prune(v)
        return result

    def estimate_size_reduction(self, original: Any, pruned: Any) -> dict:
        import json
        original_len = len(json.dumps(original, default=str))
        pruned_len = len(json.dumps(pruned, default=str))
        return {
            "original_chars": original_len,
            "pruned_chars": pruned_len,
            "reduction_pct": round((1 - pruned_len / max(original_len, 1)) * 100, 1),
        }
```

## Solution 3: List Truncator and Row Summarizer

```python
import json
from typing import Any, Dict, List, Optional, Tuple


class ListTruncator:
    """
    Caps list-valued fields at max_items and appends a truncation notice.
    Works on the top-level value if it is a list, or on list-valued dict fields.
    """

    def __init__(self, max_items: int = 20):
        self._max = max_items

    def truncate(self, value: Any) -> Tuple[Any, int]:
        """Returns (truncated_value, items_removed)."""
        if isinstance(value, list):
            if len(value) <= self._max:
                return value, 0
            removed = len(value) - self._max
            return value[: self._max], removed
        if isinstance(value, dict):
            result = {}
            total_removed = 0
            for k, v in value.items():
                trimmed, removed = self.truncate(v)
                result[k] = trimmed
                total_removed += removed
            return result, total_removed
        return value, 0


class RowSummarizer:
    """
    Converts a list of homogeneous dicts into a compact summary:
    total count, field names, numeric stats, and a small sample.
    """

    def __init__(self, sample_rows: int = 3):
        self._sample = sample_rows

    def summarize(self, rows: List[dict]) -> dict:
        if not rows or not isinstance(rows[0], dict):
            return {"rows": rows[: self._sample], "total": len(rows)}

        fields = list(rows[0].keys())
        numeric_stats: Dict[str, dict] = {}
        for field in fields:
            values = [r[field] for r in rows if isinstance(r.get(field), (int, float))]
            if values:
                numeric_stats[field] = {
                    "min": min(values),
                    "max": max(values),
                    "avg": round(sum(values) / len(values), 4),
                }

        return {
            "total_rows": len(rows),
            "fields": fields,
            "numeric_stats": numeric_stats,
            "sample": rows[: self._sample],
            "_compression_note": f"Showing {self._sample} of {len(rows)} rows with aggregate stats",
        }
```

## Solution 4: Token-Aware Output Compressor

```python
import json
from typing import Any, Callable, Dict, Optional


def _estimate_tokens(text: str) -> int:
    """Rough estimate: 1 token ≈ 4 chars for English/JSON content."""
    return max(1, len(text) // 4)


class TokenAwareOutputCompressor:
    """
    Applies the compression policy for a tool and enforces a hard token cap.
    If the output still exceeds max_tokens after policy compression,
    falls back to schema-only mode to guarantee the cap is met.
    """

    def __init__(self):
        self._pruner_cache: Dict[str, FieldPruner] = {}
        self._truncator_cache: Dict[str, ListTruncator] = {}
        self._summarizer_cache: Dict[str, RowSummarizer] = {}

    def compress(
        self,
        tool_name: str,
        raw_output: Any,
        policy: ToolOutputCompressionPolicy,
        custom_fn: Optional[Callable[[Any], Any]] = None,
    ) -> dict:
        compressed = raw_output
        note_parts = []

        strategy = policy.strategy

        if strategy == CompressionStrategy.FIELD_PRUNE:
            pruner = FieldPruner(
                allowed_fields=policy.allowed_fields,
                blocked_fields=policy.blocked_fields,
            )
            compressed = pruner.prune(raw_output)
            note_parts.append("fields pruned")

        elif strategy == CompressionStrategy.LIST_TRUNCATE:
            truncator = ListTruncator(max_items=policy.max_list_items)
            compressed, removed = truncator.truncate(raw_output)
            if removed:
                note_parts.append(f"{removed} items removed from lists")

        elif strategy == CompressionStrategy.ROW_SUMMARIZE:
            rows = raw_output if isinstance(raw_output, list) else raw_output.get("rows", [])
            summarizer = RowSummarizer(sample_rows=policy.summary_sample_rows)
            compressed = summarizer.summarize(rows) if rows else raw_output
            note_parts.append("bulk rows summarized")

        elif strategy == CompressionStrategy.SCHEMA_ONLY:
            compressed = self._to_schema(raw_output)
            note_parts.append("schema-only mode")

        elif strategy == CompressionStrategy.CUSTOM and custom_fn:
            compressed = custom_fn(raw_output)
            note_parts.append("custom compression applied")

        # Hard token cap enforcement
        serialized = json.dumps(compressed, default=str)
        token_estimate = _estimate_tokens(serialized)
        if token_estimate > policy.max_tokens:
            compressed = self._to_schema(raw_output)
            note_parts.append(
                f"exceeded {policy.max_tokens} token cap — schema-only fallback"
            )

        result = {"data": compressed}
        if policy.include_compression_note and note_parts:
            result["_compression"] = {"tool": tool_name, "applied": note_parts}
        return result

    def _to_schema(self, value: Any, depth: int = 0) -> Any:
        if depth > 4:
            return "<truncated>"
        if isinstance(value, dict):
            return {k: self._to_schema(v, depth + 1) for k, v in list(value.items())[:20]}
        if isinstance(value, list):
            if not value:
                return []
            return [f"<list of {len(value)} items, type: {type(value[0]).__name__}>"]
        return type(value).__name__
```

## Solution 5: Compression-Gated Tool Output Injector

```python
import json
from typing import Any, Callable, Dict, Optional


class CompressionGatedToolOutputInjector:
    """
    Sits between tool execution and context injection.
    Applies the registered compression policy before the output
    is formatted for the LLM message.
    """

    def __init__(self, compressor: TokenAwareOutputCompressor):
        self._compressor = compressor
        self._policies: Dict[str, ToolOutputCompressionPolicy] = {}
        self._custom_fns: Dict[str, Callable[[Any], Any]] = {}
        self._stats: Dict[str, dict] = {}

    def register(
        self,
        policy: ToolOutputCompressionPolicy,
        custom_fn: Optional[Callable[[Any], Any]] = None,
    ) -> None:
        self._policies[policy.tool_name] = policy
        if custom_fn:
            self._custom_fns[policy.tool_name] = custom_fn

    def inject(self, tool_name: str, raw_output: Any) -> str:
        """
        Returns the compressed output as a JSON string ready for context injection.
        Falls back to passthrough with truncation if no policy is registered.
        """
        policy = self._policies.get(
            tool_name,
            ToolOutputCompressionPolicy(
                tool_name=tool_name,
                strategy=CompressionStrategy.LIST_TRUNCATE,
                max_tokens=2048,
            ),
        )
        custom_fn = self._custom_fns.get(tool_name)
        compressed = self._compressor.compress(tool_name, raw_output, policy, custom_fn)

        raw_tokens = max(1, len(json.dumps(raw_output, default=str)) // 4)
        compressed_tokens = max(1, len(json.dumps(compressed, default=str)) // 4)
        self._stats[tool_name] = {
            "calls": self._stats.get(tool_name, {}).get("calls", 0) + 1,
            "last_raw_tokens": raw_tokens,
            "last_compressed_tokens": compressed_tokens,
            "last_savings_pct": round((1 - compressed_tokens / raw_tokens) * 100, 1),
        }

        return json.dumps(compressed, default=str)

    def compression_stats(self) -> dict:
        return dict(self._stats)
```

## Solution 6: Output Compression Dashboard

```python
import time
from typing import Dict, List


class OutputCompressionDashboard:
    """
    Aggregates compression statistics across tools and sessions.
    Identifies which tools produce the largest raw outputs and
    whether compression policies are achieving adequate reduction.
    """

    def __init__(
        self,
        injector: CompressionGatedToolOutputInjector,
        target_savings_pct: float = 50.0,
    ):
        self._injector = injector
        self._target = target_savings_pct
        self._history: List[dict] = []

    def snapshot(self) -> dict:
        stats = self._injector.compression_stats()
        alerts = []

        for tool_name, s in stats.items():
            savings = s.get("last_savings_pct", 0)
            if s["last_raw_tokens"] > 500 and savings < self._target:
                alerts.append({
                    "tool": tool_name,
                    "type": "low_compression",
                    "raw_tokens": s["last_raw_tokens"],
                    "savings_pct": savings,
                    "target_pct": self._target,
                    "recommendation": (
                        f"'{tool_name}' outputs {s['last_raw_tokens']} tokens but only "
                        f"{savings}% is compressed away. Consider ROW_SUMMARIZE or FIELD_PRUNE strategy."
                    ),
                })

        top_consumers = sorted(
            stats.items(),
            key=lambda x: x[1].get("last_raw_tokens", 0),
            reverse=True,
        )[:5]

        report = {
            "generated_at": time.time(),
            "tool_stats": stats,
            "top_raw_token_consumers": [
                {"tool": name, "raw_tokens": s["last_raw_tokens"]}
                for name, s in top_consumers
            ],
            "alerts": alerts,
            "healthy": len(alerts) == 0,
        }
        self._history.append({"ts": time.time(), "alert_count": len(alerts)})
        return report
```

## Comparison

| Approach | Field Pruning | List Truncation | Row Summarization | Token Cap | Stats |
|---|---|---|---|---|---|
| FieldPruner | Yes | No | No | No | Size reduction |
| ListTruncator | No | Yes | No | No | Items removed |
| RowSummarizer | No | No | Yes (stats + sample) | No | No |
| TokenAwareOutputCompressor | Via policy | Via policy | Via policy | Yes (schema fallback) | No |
| CompressionGatedToolOutputInjector | Via compressor | Via compressor | Via compressor | Via compressor | Per-tool |
| OutputCompressionDashboard | No | No | No | No | Fleet-wide |

**Best for production**: Register a `ToolOutputCompressionPolicy` for every tool that returns variable-length data — database queries, search results, API list endpoints. Default unregistered tools to `LIST_TRUNCATE` with `max_tokens=2048` so new tools cannot blow up the context window silently. Use `ROW_SUMMARIZE` for query results that return more than 10 rows: numeric stats plus a 3-row sample give the LLM enough signal to generate correct follow-up queries without consuming hundreds of tokens. Set `include_compression_note=True` so the LLM knows data was compressed and can request the full result if needed. Monitor `OutputCompressionDashboard` for tools where savings are below 50% on large payloads — those are candidates for custom compressor functions.
