---
title: "Agent Doesn't Implement Response Compression for Large Tool Outputs"
description: "Agents that inject full tool outputs into the LLM context — complete API responses, entire documents, verbose JSON payloads — consume tokens on boilerplate and redundant fields that add no reasoning value. Implement response compression that extracts semantically relevant fields, summarizes verbose content, removes duplicated structure, and reports token savings before injection."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-response-compression-for-large-tool-outputs
tags: [response-compression, tool-output-compression, token-reduction, json-pruning, content-summarization, context-efficiency]
symptoms:
  - "Tool responses with 50 JSON fields inject all 50 fields even when only 3 are relevant"
  - "API responses with pagination metadata, headers, and status fields consume context unnecessarily"
  - "Large tool outputs crowd out subsequent tool results in the context window"
  - "No mechanism to summarize a 10,000-word document before injecting it"
  - "Token usage per turn grows proportionally with tool output verbosity"
---

## Why This Happens

Tools return what their APIs return — which is optimized for machine consumption, not LLM context efficiency. A REST API response includes status codes, rate limit headers, pagination cursors, nested metadata, and null fields — none of which help the LLM answer the user's question. Without a compression layer, every token of API boilerplate is a token not available for reasoning. Response compression requires field extraction (keep only relevant fields), structure flattening (remove nesting), and optionally content summarization (condense long text) before injection.

## Solution 1: Field Extraction Schema

```python
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set


@dataclass
class FieldExtractionRule:
    field_path: str             # dot-separated path: "data.user.name"
    alias: Optional[str] = None  # rename in output: "user_name"
    transform: Optional[Callable[[Any], Any]] = None  # value transformation
    required: bool = False      # if True, missing field raises an error


@dataclass
class ResponseCompressionSchema:
    tool_name: str
    include_fields: List[FieldExtractionRule] = field(default_factory=list)
    exclude_fields: Set[str] = field(default_factory=set)
    max_text_chars: int = 2000      # truncate long string values
    max_list_items: int = 10        # truncate long lists
    flatten_depth: int = 2          # levels of nesting to flatten
    summarize_text_fields: Set[str] = field(default_factory=set)

    def has_field_rules(self) -> bool:
        return bool(self.include_fields)
```

## Solution 2: JSON Field Extractor

```python
from typing import Any, Dict, List, Optional


class JSONFieldExtractor:
    """
    Extracts a subset of fields from a nested JSON response
    according to dot-path rules, with optional renaming and transformation.
    """

    def _get_nested(self, obj: Any, path: str) -> Optional[Any]:
        parts = path.split(".")
        current = obj
        for part in parts:
            if isinstance(current, dict):
                current = current.get(part)
            elif isinstance(current, list) and part.isdigit():
                idx = int(part)
                current = current[idx] if idx < len(current) else None
            else:
                return None
            if current is None:
                return None
        return current

    def extract(
        self,
        response: Any,
        rules: List[FieldExtractionRule],
    ) -> Dict[str, Any]:
        result = {}
        for rule in rules:
            value = self._get_nested(response, rule.field_path)
            if value is None and rule.required:
                raise ValueError(f"Required field '{rule.field_path}' not found in response")
            if value is not None:
                if rule.transform:
                    value = rule.transform(value)
                key = rule.alias or rule.field_path.split(".")[-1]
                result[key] = value
        return result
```

## Solution 3: Structure Flattener

```python
from typing import Any, Dict


class ResponseStructureFlattener:
    """
    Flattens nested JSON structures up to a specified depth.
    Deep nesting adds no value for LLM context — flattening makes
    the content more readable and reduces token overhead from indentation.
    """

    def flatten(
        self,
        obj: Any,
        prefix: str = "",
        depth: int = 2,
        current_depth: int = 0,
    ) -> Dict[str, Any]:
        result = {}
        if not isinstance(obj, dict) or current_depth >= depth:
            return {prefix: obj} if prefix else {"value": obj}

        for key, value in obj.items():
            full_key = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict) and current_depth + 1 < depth:
                nested = self.flatten(value, full_key, depth, current_depth + 1)
                result.update(nested)
            else:
                result[full_key] = value

        return result

    def truncate_lists(self, obj: Any, max_items: int = 10) -> Any:
        if isinstance(obj, list):
            truncated = obj[:max_items]
            if len(obj) > max_items:
                return truncated + [f"... ({len(obj) - max_items} more items)"]
            return truncated
        if isinstance(obj, dict):
            return {k: self.truncate_lists(v, max_items) for k, v in obj.items()}
        return obj
```

## Solution 4: Content Summarizer

```python
import re
from typing import Any, Callable, Optional


class ToolResponseContentSummarizer:
    """
    Summarizes long text fields in tool responses to fit within a character budget.
    Uses LLM summarization for high-value content, truncation for low-value content.
    """

    def __init__(
        self,
        llm_summarize_fn: Optional[Callable[[str, int], str]] = None,
        char_limit: int = 500,
        summarize_threshold: int = 1000,
    ):
        self._llm_fn = llm_summarize_fn
        self._char_limit = char_limit
        self._summarize_threshold = summarize_threshold

    def compress_text(self, text: str, field_name: str = "") -> str:
        if len(text) <= self._char_limit:
            return text

        if len(text) >= self._summarize_threshold and self._llm_fn:
            try:
                return self._llm_fn(text, self._char_limit)
            except Exception:
                pass

        # Fallback: intelligent truncation preserving beginning and end
        half = self._char_limit // 2
        return text[:half] + f"\n...[{len(text) - self._char_limit} chars omitted]...\n" + text[-half:]

    def compress_value(self, value: Any, field_name: str = "") -> Any:
        if isinstance(value, str):
            return self.compress_text(value, field_name)
        if isinstance(value, dict):
            return {k: self.compress_value(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [self.compress_value(item) for item in value]
        return value
```

## Solution 5: Tool Response Compressor Pipeline

```python
import json
from typing import Any, Dict, Optional


class ToolResponseCompressorPipeline:
    """
    Applies field extraction, structure flattening, list truncation,
    and text summarization in sequence. Reports token savings.
    """

    CHARS_PER_TOKEN = 4.0

    def __init__(
        self,
        extractor: JSONFieldExtractor,
        flattener: ResponseStructureFlattener,
        summarizer: ToolResponseContentSummarizer,
        schemas: Dict[str, ResponseCompressionSchema],
    ):
        self._extractor = extractor
        self._flattener = flattener
        self._summarizer = summarizer
        self._schemas = schemas
        self._total_tokens_saved = 0
        self._runs = 0

    def _estimate_tokens(self, obj: Any) -> int:
        return max(1, int(len(json.dumps(obj, default=str)) / self.CHARS_PER_TOKEN))

    def compress(self, tool_name: str, response: Any) -> dict:
        self._runs += 1
        original_tokens = self._estimate_tokens(response)
        schema = self._schemas.get(tool_name)

        result = response

        if schema:
            # Step 1: Field extraction
            if schema.has_field_rules():
                try:
                    result = self._extractor.extract(result, schema.include_fields)
                except Exception:
                    pass  # fall through to full response if extraction fails

            # Step 2: Flatten structure
            if isinstance(result, dict):
                result = self._flattener.flatten(result, depth=schema.flatten_depth)

            # Step 3: Truncate lists
            result = self._flattener.truncate_lists(result, schema.max_list_items)

            # Step 4: Summarize text fields
            if schema.summarize_text_fields and isinstance(result, dict):
                for field_name in schema.summarize_text_fields:
                    if field_name in result and isinstance(result[field_name], str):
                        result[field_name] = self._summarizer.compress_text(
                            result[field_name], field_name
                        )

        final_tokens = self._estimate_tokens(result)
        saved = original_tokens - final_tokens
        self._total_tokens_saved += max(0, saved)

        return {
            "compressed": result,
            "original_tokens_est": original_tokens,
            "final_tokens_est": final_tokens,
            "tokens_saved_est": max(0, saved),
            "compression_ratio": round(original_tokens / max(final_tokens, 1), 2),
        }

    def stats(self) -> dict:
        return {
            "total_runs": self._runs,
            "total_tokens_saved_est": self._total_tokens_saved,
        }
```

## Solution 6: Compression Savings Dashboard

```python
import time
from typing import List


class ResponseCompressionSavingsDashboard:
    """
    Tracks compression savings per tool over time to identify
    which tools benefit most from schema refinement.
    """

    def __init__(self, pipeline: ToolResponseCompressorPipeline):
        self._pipeline = pipeline
        self._run_reports: List[dict] = []

    def record(self, tool_name: str, report: dict) -> None:
        self._run_reports.append({
            "tool_name": tool_name,
            "ts": time.time(),
            **report,
        })

    def render(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._run_reports if r.get("ts", 0) >= cutoff]

        by_tool: dict = {}
        for r in recent:
            t = r["tool_name"]
            if t not in by_tool:
                by_tool[t] = {"runs": 0, "tokens_saved": 0, "ratios": []}
            by_tool[t]["runs"] += 1
            by_tool[t]["tokens_saved"] += r.get("tokens_saved_est", 0)
            by_tool[t]["ratios"].append(r.get("compression_ratio", 1.0))

        tool_summary = {
            t: {
                "runs": v["runs"],
                "tokens_saved": v["tokens_saved"],
                "avg_compression_ratio": round(
                    sum(v["ratios"]) / len(v["ratios"]), 2
                ),
            }
            for t, v in by_tool.items()
        }

        return {
            "generated_at": time.time(),
            "window_seconds": window_seconds,
            "total_runs": len(recent),
            "total_tokens_saved_est": sum(r.get("tokens_saved_est", 0) for r in recent),
            "pipeline_stats": self._pipeline.stats(),
            "by_tool": tool_summary,
        }
```

## Comparison

| Approach | Field Extraction | Structure Flattening | Text Summarization | Token Savings Report | Dashboard |
|---|---|---|---|---|---|
| JSONFieldExtractor | Yes (dot-path) | No | No | No | No |
| ResponseStructureFlattener | No | Yes (configurable depth) | No | No | No |
| ToolResponseContentSummarizer | No | No | Yes (LLM + truncation) | No | No |
| ToolResponseCompressorPipeline | Via extractor | Via flattener | Via summarizer | Yes | No |
| ResponseCompressionSavingsDashboard | No | No | No | No | Yes |

**Best for production**: Define `ResponseCompressionSchema` for every tool that returns more than 1,000 characters — this is where the token savings are largest. Start with `include_fields` extraction before reaching for LLM summarization: extracting 5 relevant fields from a 50-field JSON response is free and achieves 90% token reduction without any additional LLM call. Use LLM summarization only for free-text fields (article body, user bio, document content) where field extraction cannot reduce size. Monitor `compression_ratio` per tool in the dashboard — a ratio below 1.5x suggests the schema needs more aggressive field pruning.
