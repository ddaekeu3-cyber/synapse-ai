---
title: "Agent Doesn't Implement Tool Result Compression Before Context Injection"
description: "Agents that inject raw tool results into the LLM context waste tokens on verbose formatting, redundant headers, and deeply nested JSON that the model could have processed from a compact representation. Implement tool result compression that strips irrelevant fields, flattens nested structures, truncates oversized arrays, and summarizes boilerplate sections — reducing context consumption without losing the information the agent needs to act."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-tool-result-compression-before-context-injection
tags: [context-compression, tool-result, token-efficiency, json-trimming, context-injection, llm-context]
symptoms:
  - "Tool results consume 80% of the context window with verbose JSON the model rarely references"
  - "Deeply nested API responses injected verbatim — most fields are never mentioned in the model's output"
  - "Large arrays of records injected in full when only the first few are relevant"
  - "HTTP response headers and metadata fields included in context alongside the payload"
  - "No per-tool configuration for which fields to keep or drop before injection"
---

## Why This Happens

HTTP APIs return JSON designed for application consumption, not LLM context windows. A typical REST response includes pagination metadata, hypermedia links, audit timestamps, internal IDs, and deeply nested sub-objects — none of which the agent needs to reason about the task. Without a compression step, the raw result goes directly into the prompt, consuming tokens that could hold more tool calls, conversation history, or retrieved documents. Compression requires per-tool schema awareness: knowing which fields are load-bearing for the agent's decision and which are safe to strip or summarize.

## Solution 1: Field Projection Filter

```python
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set


@dataclass
class FieldProjectionConfig:
    keep_fields: Optional[Set[str]] = None     # if set, whitelist — drop all others
    drop_fields: Set[str] = field(default_factory=set)  # explicit blacklist
    max_string_length: int = 500               # truncate long string values
    max_array_items: int = 20                  # truncate long arrays
    drop_null_fields: bool = True
    drop_empty_collections: bool = True


class FieldProjectionFilter:
    """
    Applies keep/drop field rules to a dict, truncates strings and arrays,
    and removes null/empty values — reducing JSON size before context injection.
    """

    def __init__(self, config: FieldProjectionConfig):
        self._cfg = config

    def apply(self, obj: Any, depth: int = 0) -> Any:
        if isinstance(obj, dict):
            return self._filter_dict(obj, depth)
        if isinstance(obj, list):
            trimmed = obj[: self._cfg.max_array_items]
            result = [self.apply(item, depth) for item in trimmed]
            if self._cfg.drop_empty_collections and not result:
                return None
            return result
        if isinstance(obj, str) and len(obj) > self._cfg.max_string_length:
            return obj[: self._cfg.max_string_length] + "…[truncated]"
        return obj

    def _filter_dict(self, obj: dict, depth: int) -> dict:
        result = {}
        for key, value in obj.items():
            if key in self._cfg.drop_fields:
                continue
            if self._cfg.keep_fields is not None and key not in self._cfg.keep_fields:
                continue
            processed = self.apply(value, depth + 1)
            if self._cfg.drop_null_fields and processed is None:
                continue
            if self._cfg.drop_empty_collections and processed in ([], {}):
                continue
            result[key] = processed
        return result
```

## Solution 2: JSON Structure Flattener

```python
from typing import Any, Dict, Optional


class JSONStructureFlattener:
    """
    Flattens nested JSON objects into dot-notation keys up to a configurable depth.
    Reduces nesting overhead and makes values directly readable without traversal.
    Example: {"user": {"id": 1, "name": "Alice"}} -> {"user.id": 1, "user.name": "Alice"}
    """

    def __init__(self, max_depth: int = 3, separator: str = "."):
        self._max_depth = max_depth
        self._sep = separator

    def flatten(self, obj: Any, prefix: str = "", depth: int = 0) -> Dict[str, Any]:
        if not isinstance(obj, dict) or depth >= self._max_depth:
            return {prefix: obj} if prefix else {"value": obj}

        result: Dict[str, Any] = {}
        for key, value in obj.items():
            full_key = f"{prefix}{self._sep}{key}" if prefix else key
            if isinstance(value, dict) and depth + 1 < self._max_depth:
                result.update(self.flatten(value, full_key, depth + 1))
            elif isinstance(value, list):
                result[full_key] = value   # keep lists as-is
            else:
                result[full_key] = value
        return result
```

## Solution 3: Boilerplate Section Summarizer

```python
import json
from typing import Any, Dict, List, Optional


_BOILERPLATE_KEYS = frozenset({
    "links", "_links", "href", "self", "next", "prev", "first", "last",
    "pagination", "paging", "meta", "_meta", "x-request-id", "x-trace-id",
    "request_id", "trace_id", "deprecation", "sunset", "etag", "cache_control",
    "rate_limit_remaining", "rate_limit_reset", "x-ratelimit-limit",
})


class BoilerplateSectionSummarizer:
    """
    Detects and collapses known boilerplate sections (pagination, hypermedia links,
    rate limit headers, tracing IDs) into a single summary token rather than
    expanding them verbatim into the context.
    """

    def __init__(self, extra_boilerplate_keys: Optional[List[str]] = None):
        self._boilerplate = _BOILERPLATE_KEYS | set(extra_boilerplate_keys or [])

    def compress(self, obj: Dict[str, Any]) -> Dict[str, Any]:
        result = {}
        collapsed_count = 0
        for key, value in obj.items():
            if key.lower() in self._boilerplate:
                collapsed_count += 1
            else:
                result[key] = value
        if collapsed_count:
            result["_compressed"] = f"[{collapsed_count} boilerplate field(s) removed]"
        return result
```

## Solution 4: Per-Tool Compression Profile

```python
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class ToolCompressionProfile:
    tool_name: str
    keep_fields: Optional[set] = None
    drop_fields: set = field(default_factory=set)
    max_array_items: int = 20
    max_string_length: int = 500
    flatten_depth: int = 0              # 0 = no flattening
    strip_boilerplate: bool = True
    enabled: bool = True


class PerToolCompressionProfileRegistry:
    """
    Stores per-tool compression profiles and returns the appropriate
    profile for a given tool name, falling back to a default profile.
    """

    def __init__(self, default_profile: Optional[ToolCompressionProfile] = None):
        self._profiles: Dict[str, ToolCompressionProfile] = {}
        self._default = default_profile or ToolCompressionProfile(
            tool_name="__default__",
            max_array_items=20,
            max_string_length=500,
            strip_boilerplate=True,
        )

    def register(self, profile: ToolCompressionProfile) -> None:
        self._profiles[profile.tool_name] = profile

    def get(self, tool_name: str) -> ToolCompressionProfile:
        return self._profiles.get(tool_name, self._default)
```

## Solution 5: Tool Result Compression Pipeline

```python
import json
from typing import Any, Optional


class ToolResultCompressionPipeline:
    """
    Applies field projection, boilerplate removal, and optional flattening
    to a raw tool result before it is serialized into LLM context.
    Reports original vs compressed token estimates.
    """

    def __init__(
        self,
        profile_registry: PerToolCompressionProfileRegistry,
        tokens_per_char: float = 0.25,
    ):
        self._registry = profile_registry
        self._tpc = tokens_per_char

    def _to_dict(self, result: Any) -> Any:
        if isinstance(result, str):
            try:
                return json.loads(result)
            except (json.JSONDecodeError, ValueError):
                return result
        return result

    def compress(self, tool_name: str, raw_result: Any) -> dict:
        profile = self._registry.get(tool_name)
        if not profile.enabled:
            serialized = json.dumps(raw_result, default=str)
            tokens = int(len(serialized) * self._tpc)
            return {
                "compressed_result": raw_result,
                "original_tokens_est": tokens,
                "compressed_tokens_est": tokens,
                "tokens_saved_est": 0,
            }

        obj = self._to_dict(raw_result)
        original_serialized = json.dumps(obj, default=str)
        original_tokens = int(len(original_serialized) * self._tpc)

        # Stage 1: boilerplate
        if profile.strip_boilerplate and isinstance(obj, dict):
            summarizer = BoilerplateSectionSummarizer()
            obj = summarizer.compress(obj)

        # Stage 2: field projection
        proj_config = FieldProjectionConfig(
            keep_fields=profile.keep_fields,
            drop_fields=profile.drop_fields,
            max_string_length=profile.max_string_length,
            max_array_items=profile.max_array_items,
        )
        obj = FieldProjectionFilter(proj_config).apply(obj)

        # Stage 3: flattening
        if profile.flatten_depth > 0 and isinstance(obj, dict):
            obj = JSONStructureFlattener(max_depth=profile.flatten_depth).flatten(obj)

        compressed_serialized = json.dumps(obj, default=str)
        compressed_tokens = int(len(compressed_serialized) * self._tpc)

        return {
            "compressed_result": obj,
            "original_tokens_est": original_tokens,
            "compressed_tokens_est": compressed_tokens,
            "tokens_saved_est": max(0, original_tokens - compressed_tokens),
            "compression_ratio": round(
                compressed_tokens / max(original_tokens, 1), 3
            ),
        }
```

## Solution 6: Compression Savings Tracker

```python
import time
from typing import List


class CompressionSavingsTracker:
    """
    Accumulates per-tool compression results and reports aggregate savings
    and per-tool efficiency for optimization decisions.
    """

    def __init__(self):
        self._records: List[dict] = []
        self._recorded_at: List[float] = []

    def record(self, tool_name: str, pipeline_result: dict) -> None:
        self._records.append({
            "tool_name": tool_name,
            "original": pipeline_result.get("original_tokens_est", 0),
            "compressed": pipeline_result.get("compressed_tokens_est", 0),
            "saved": pipeline_result.get("tokens_saved_est", 0),
        })
        self._recorded_at.append(time.time())

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [
            r for r, ts in zip(self._records, self._recorded_at) if ts >= cutoff
        ]
        if not recent:
            return {"window_seconds": window_seconds, "calls": 0}

        by_tool: dict = {}
        for r in recent:
            t = r["tool_name"]
            if t not in by_tool:
                by_tool[t] = {"calls": 0, "total_saved": 0, "total_original": 0}
            by_tool[t]["calls"] += 1
            by_tool[t]["total_saved"] += r["saved"]
            by_tool[t]["total_original"] += r["original"]

        total_saved = sum(r["saved"] for r in recent)
        total_original = sum(r["original"] for r in recent)

        return {
            "window_seconds": window_seconds,
            "calls": len(recent),
            "total_tokens_saved_est": total_saved,
            "savings_pct": round(total_saved / max(total_original, 1) * 100, 1),
            "per_tool": {
                t: {
                    "calls": v["calls"],
                    "total_saved": v["total_saved"],
                    "savings_pct": round(
                        v["total_saved"] / max(v["total_original"], 1) * 100, 1
                    ),
                }
                for t, v in sorted(
                    by_tool.items(), key=lambda x: x[1]["total_saved"], reverse=True
                )
            },
        }
```

## Comparison

| Approach | Field Whitelist/Blacklist | Boilerplate Removal | Array Truncation | Flattening | Savings Tracking |
|---|---|---|---|---|---|
| FieldProjectionFilter | Yes (keep + drop) | No | Yes | No | No |
| JSONStructureFlattener | No | No | No | Yes | No |
| BoilerplateSectionSummarizer | No | Yes (key-based) | No | No | No |
| PerToolCompressionProfileRegistry | Via profile | Via profile | Via profile | Via profile | No |
| ToolResultCompressionPipeline | Via profile | Via summarizer | Via filter | Via flattener | Inline |
| CompressionSavingsTracker | No | No | No | No | Yes (aggregate) |

**Best for production**: Register per-tool profiles for the highest-volume tools first — a search tool returning 50 result objects with nested metadata is typically the biggest offender. Set `keep_fields` rather than `drop_fields` for external API tools where the schema can change: a whitelist approach means new fields are dropped by default rather than accidentally injected. Use `flatten_depth=2` for API responses that nest identifiers under `{"data": {"id": ..., "attributes": {...}}}` patterns — flattening eliminates one level of visual noise without losing values. Monitor `CompressionSavingsTracker.summary()`: tools with savings below 10% can have compression disabled without impact, while tools above 50% savings are worth investing in tighter `keep_fields` lists.
