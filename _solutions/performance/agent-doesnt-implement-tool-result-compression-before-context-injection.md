---
title: "Agent Doesn't Implement Tool Result Compression Before Context Injection"
description: "Agents that inject raw tool results verbatim into the LLM context waste tokens on boilerplate, repeated field names, verbose JSON structure, and prose that can be summarized. A database result with 50 rows and 20 columns consumes 4,000 tokens when the relevant signal is 200 tokens. Implement tool result compression that extracts high-value content, truncates low-value fields, and produces a compact representation before context injection."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-tool-result-compression-before-context-injection
tags: [result-compression, token-reduction, context-efficiency, json-compaction, table-compression, rag-optimization]
symptoms:
  - "Database query returning 50 rows injected verbatim consumes entire context window"
  - "API response with deeply nested JSON uses 3× more tokens than the relevant fields"
  - "Tool results include verbose metadata, timestamps, and IDs the LLM never uses"
  - "No token budget enforced on individual tool result injections"
  - "LLM's attention diluted by low-signal content surrounding the relevant answer"
---

## Why This Happens

Tool results are designed for machine consumption — full JSON objects with all fields, database rows with all columns, API responses with full metadata. Injecting them as-is into an LLM context is inefficient: the model must parse verbose structure to find the relevant signal. Compression before injection means selecting relevant fields, truncating long strings, collapsing repeated structures into summaries, and converting JSON objects to natural-language or compact table representations. The goal is maximum information per token, not maximum fidelity.

## Solution 1: Compression Policy

```python
from dataclasses import dataclass, field
from typing import Any, List, Optional, Set


@dataclass
class CompressionPolicy:
    max_output_tokens: int = 500          # hard ceiling for compressed result
    include_fields: Optional[Set[str]] = None   # if set, only these fields
    exclude_fields: Set[str] = field(default_factory=set)
    max_string_length: int = 200          # truncate long string values
    max_list_items: int = 10              # truncate long lists
    max_dict_depth: int = 2              # flatten deeply nested dicts
    tokens_per_char: float = 0.25
    summarize_remainder: bool = True      # append "... N more items" when truncated
```

## Solution 2: JSON Field Compressor

```python
import json
from typing import Any, Dict, List, Optional


class JSONFieldCompressor:
    """
    Reduces a JSON-serializable tool result to a compact subset
    according to a CompressionPolicy. Returns both the compressed
    object and an estimated token count.
    """

    def compress(
        self,
        data: Any,
        policy: CompressionPolicy,
        depth: int = 0,
    ) -> Any:
        if data is None:
            return None
        if isinstance(data, bool):
            return data
        if isinstance(data, (int, float)):
            return data
        if isinstance(data, str):
            if len(data) > policy.max_string_length:
                return data[: policy.max_string_length] + f"… (+{len(data) - policy.max_string_length} chars)"
            return data
        if isinstance(data, list):
            compressed_items = [
                self.compress(item, policy, depth) for item in data[: policy.max_list_items]
            ]
            remainder = len(data) - policy.max_list_items
            if remainder > 0 and policy.summarize_remainder:
                compressed_items.append(f"… {remainder} more items")
            return compressed_items
        if isinstance(data, dict):
            if depth >= policy.max_dict_depth:
                return f"{{… {len(data)} fields}}"
            result = {}
            for key, value in data.items():
                if policy.include_fields and key not in policy.include_fields:
                    continue
                if key in policy.exclude_fields:
                    continue
                result[key] = self.compress(value, policy, depth + 1)
            return result
        return str(data)

    def compress_to_string(self, data: Any, policy: CompressionPolicy) -> str:
        compressed = self.compress(data, policy)
        if isinstance(compressed, (dict, list)):
            return json.dumps(compressed, ensure_ascii=False, separators=(",", ":"))
        return str(compressed)

    def estimate_tokens(self, text: str, tokens_per_char: float = 0.25) -> int:
        return max(1, int(len(text) * tokens_per_char))
```

## Solution 3: Table Result Compressor

```python
from typing import Any, Dict, List, Optional


class TableResultCompressor:
    """
    Converts a list of dicts (database rows, API result sets) into
    a compact markdown table, respecting max_list_items and include_fields.
    """

    def compress(
        self,
        rows: List[Dict[str, Any]],
        policy: CompressionPolicy,
    ) -> str:
        if not rows:
            return "(empty result)"

        # Determine columns
        if policy.include_fields:
            columns = [c for c in policy.include_fields if any(c in r for r in rows)]
        else:
            all_keys: list = []
            seen: set = set()
            for row in rows[:5]:
                for k in row:
                    if k not in seen and k not in policy.exclude_fields:
                        all_keys.append(k)
                        seen.add(k)
            columns = all_keys

        truncated_rows = rows[: policy.max_list_items]
        remainder = len(rows) - policy.max_list_items

        # Build markdown table
        header = "| " + " | ".join(columns) + " |"
        separator = "| " + " | ".join("---" for _ in columns) + " |"
        data_rows = []
        for row in truncated_rows:
            cells = []
            for col in columns:
                val = str(row.get(col, ""))
                if len(val) > 40:
                    val = val[:40] + "…"
                cells.append(val)
            data_rows.append("| " + " | ".join(cells) + " |")

        lines = [header, separator] + data_rows
        if remainder > 0 and policy.summarize_remainder:
            lines.append(f"_(+{remainder} more rows)_")
        return "\n".join(lines)
```

## Solution 4: Per-Tool Compression Registry

```python
from typing import Dict, Optional


class PerToolCompressionRegistry:
    """
    Maps tool names to their specific CompressionPolicy.
    Falls back to a default policy for unregistered tools.
    """

    def __init__(self, default_policy: Optional[CompressionPolicy] = None):
        self._policies: Dict[str, CompressionPolicy] = {}
        self._default = default_policy or CompressionPolicy()

    def register(self, tool_name: str, policy: CompressionPolicy) -> None:
        self._policies[tool_name] = policy

    def get(self, tool_name: str) -> CompressionPolicy:
        return self._policies.get(tool_name, self._default)
```

## Solution 5: Tool Result Compression Pipeline

```python
from typing import Any, List


class ToolResultCompressionPipeline:
    """
    Applies JSON field compression or table compression based on
    result type, then enforces the token ceiling with a final truncation.
    """

    def __init__(
        self,
        registry: PerToolCompressionRegistry,
        json_compressor: JSONFieldCompressor,
        table_compressor: TableResultCompressor,
    ):
        self._registry = registry
        self._json = json_compressor
        self._table = table_compressor
        self._total_tokens_saved = 0

    def compress(self, tool_name: str, raw_result: Any) -> dict:
        policy = self._registry.get(tool_name)
        raw_str = str(raw_result)
        raw_tokens = self._json.estimate_tokens(raw_str, policy.tokens_per_char)

        # Choose compression strategy
        if isinstance(raw_result, list) and all(isinstance(r, dict) for r in raw_result):
            compressed_str = self._table.compress(raw_result, policy)
        else:
            compressed_str = self._json.compress_to_string(raw_result, policy)

        # Hard token ceiling
        max_chars = int(policy.max_output_tokens / policy.tokens_per_char)
        if len(compressed_str) > max_chars:
            compressed_str = compressed_str[:max_chars] + "… [truncated]"

        compressed_tokens = self._json.estimate_tokens(compressed_str, policy.tokens_per_char)
        tokens_saved = raw_tokens - compressed_tokens
        self._total_tokens_saved += max(0, tokens_saved)

        return {
            "tool_name": tool_name,
            "compressed_result": compressed_str,
            "raw_tokens_est": raw_tokens,
            "compressed_tokens_est": compressed_tokens,
            "tokens_saved_est": max(0, tokens_saved),
            "compression_ratio": round(compressed_tokens / max(raw_tokens, 1), 3),
        }

    def total_tokens_saved(self) -> int:
        return self._total_tokens_saved
```

## Solution 6: Compression Savings Monitor

```python
import time
from threading import Lock
from typing import List


class CompressionSavingsMonitor:
    """
    Accumulates compression pipeline results and reports aggregate savings.
    """

    def __init__(self):
        self._records: List[dict] = []
        self._lock = Lock()

    def record(self, compression_result: dict) -> None:
        with self._lock:
            self._records.append({"ts": time.time(), **compression_result})
            if len(self._records) > 50000:
                self._records.pop(0)

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [r for r in self._records if r["ts"] >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "compressions": 0}

        total_raw = sum(r.get("raw_tokens_est", 0) for r in recent)
        total_compressed = sum(r.get("compressed_tokens_est", 0) for r in recent)
        total_saved = sum(r.get("tokens_saved_est", 0) for r in recent)

        by_tool: dict = {}
        for r in recent:
            name = r.get("tool_name", "unknown")
            by_tool.setdefault(name, {"compressions": 0, "tokens_saved": 0})
            by_tool[name]["compressions"] += 1
            by_tool[name]["tokens_saved"] += r.get("tokens_saved_est", 0)

        return {
            "window_seconds": window_seconds,
            "compressions": len(recent),
            "total_raw_tokens": total_raw,
            "total_compressed_tokens": total_compressed,
            "total_tokens_saved": total_saved,
            "savings_pct": round(total_saved / max(total_raw, 1) * 100, 1),
            "by_tool": by_tool,
        }
```

## Comparison

| Approach | Field Selection | List Truncation | Table Format | Token Ceiling | Savings Tracking |
|---|---|---|---|---|---|
| JSONFieldCompressor | Yes | Yes | No | No | No |
| TableResultCompressor | Via policy | Yes | Yes (markdown) | No | No |
| PerToolCompressionRegistry | No | No | No | No | No |
| ToolResultCompressionPipeline | Via compressors | Via compressors | Via table | Yes | Per-call |
| CompressionSavingsMonitor | No | No | No | No | Yes (aggregate) |

**Best for production**: Register per-tool `include_fields` policies for every structured tool — a database query tool that returns 30 columns rarely needs more than 5 in the LLM context. Set `max_list_items=10` globally and `max_output_tokens=500` per tool; most tool results that require more than 500 tokens in context should be summarized by the tool itself, not by the injection layer. Monitor `savings_pct` via `CompressionSavingsMonitor`: consistently above 60% savings means the raw tool results are very verbose and field allowlists should be tightened. A `compression_ratio` near 1.0 for a tool means its results are already compact — no policy adjustment needed.
