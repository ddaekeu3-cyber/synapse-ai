---
title: "Agent Doesn't Implement Tool Output Field Selection"
description: "Agents that inject complete tool responses into the LLM context — including dozens of irrelevant fields returned by APIs — waste tokens on data the model never uses. A REST API returning a user record with 40 fields when only 3 are relevant consumes 10× the tokens needed. Implement tool output field selection that extracts only the fields the agent needs per tool call, reducing context token count and improving model focus on relevant information."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-tool-output-field-selection
tags: [field-selection, tool-output, token-reduction, context-efficiency, api-response-filtering, prompt-engineering]
symptoms:
  - "API response with 50 fields injected in full when only 3 fields are referenced in the answer"
  - "Context window consumed by nested JSON metadata that the model never cites"
  - "Tool responses include pagination metadata, rate limit headers, and debug fields injected verbatim"
  - "Same tool used for different purposes needs different field subsets but gets everything"
  - "No mechanism to declare which fields each tool use should extract before injection"
---

## Why This Happens

Tool developers design APIs for general consumption — they return all potentially useful data. Agent developers often copy the full response into the context for convenience. The LLM receives verbose API payloads that include UUIDs, internal metadata, pagination cursors, audit timestamps, and rarely-relevant fields alongside the three fields it actually needs to answer the question. Field selection applies a projection to the tool response before context injection, extracting only the declared fields. This is analogous to `SELECT name, email FROM users` versus `SELECT *` — the result is semantically equivalent for the use case but dramatically smaller.

## Solution 1: Field Selection Rule

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Union


class FieldSelectionMode(str, Enum):
    INCLUDE = "include"     # only listed fields are kept
    EXCLUDE = "exclude"     # listed fields are removed, rest kept
    PATH = "path"           # dot-notation paths for nested extraction


@dataclass
class FieldSelectionRule:
    tool_name: str
    mode: FieldSelectionMode
    fields: List[str]           # field names or dot-notation paths
    use_case: str = ""          # optional: rules can be tagged by use case
    max_output_chars: int = 0   # 0 = no limit; truncate if exceeded
    include_metadata: bool = False  # whether to include a field count summary

    def applies_to(self, tool_name: str, use_case: str = "") -> bool:
        if self.tool_name != tool_name:
            return False
        if self.use_case and use_case and self.use_case != use_case:
            return False
        return True
```

## Solution 2: Field Extractor

```python
import json
from typing import Any, Dict, List, Optional


class ToolOutputFieldExtractor:
    """
    Applies a FieldSelectionRule to a tool response dict.
    Supports include, exclude, and dot-notation path extraction.
    """

    def extract(
        self,
        response: Any,
        rule: FieldSelectionRule,
    ) -> Any:
        if isinstance(response, str):
            try:
                response = json.loads(response)
            except (json.JSONDecodeError, ValueError):
                return response  # non-JSON string, pass through

        if not isinstance(response, dict):
            if isinstance(response, list):
                return [self.extract(item, rule) for item in response[:50]]
            return response

        if rule.mode == FieldSelectionMode.INCLUDE:
            return self._include(response, rule.fields)
        if rule.mode == FieldSelectionMode.EXCLUDE:
            return self._exclude(response, rule.fields)
        if rule.mode == FieldSelectionMode.PATH:
            return self._extract_paths(response, rule.fields)
        return response

    def _include(self, obj: dict, fields: List[str]) -> dict:
        return {k: v for k, v in obj.items() if k in fields}

    def _exclude(self, obj: dict, fields: List[str]) -> dict:
        return {k: v for k, v in obj.items() if k not in fields}

    def _extract_paths(self, obj: dict, paths: List[str]) -> dict:
        result = {}
        for path in paths:
            parts = path.split(".")
            value = obj
            for part in parts:
                if isinstance(value, dict):
                    value = value.get(part)
                elif isinstance(value, list) and part.isdigit():
                    idx = int(part)
                    value = value[idx] if idx < len(value) else None
                else:
                    value = None
                    break
            if value is not None:
                result[parts[-1]] = value
        return result
```

## Solution 3: Field Selection Registry

```python
from typing import Dict, List, Optional


class FieldSelectionRegistry:
    """
    Stores field selection rules per tool name and use case.
    Lookup returns the most specific matching rule.
    """

    def __init__(self):
        self._rules: List[FieldSelectionRule] = []

    def register(self, rule: FieldSelectionRule) -> None:
        self._rules.append(rule)

    def lookup(
        self,
        tool_name: str,
        use_case: str = "",
    ) -> Optional[FieldSelectionRule]:
        # First try exact use_case match
        for rule in self._rules:
            if rule.tool_name == tool_name and rule.use_case == use_case:
                return rule
        # Fall back to tool-level rule with no use case
        for rule in self._rules:
            if rule.tool_name == tool_name and not rule.use_case:
                return rule
        return None

    def registered_tools(self) -> List[str]:
        return list({r.tool_name for r in self._rules})
```

## Solution 4: Field-Selecting Tool Interceptor

```python
import json
import time
from typing import Any, Callable, Dict, Optional


class FieldSelectingToolInterceptor:
    """
    Wraps tool execution and applies field selection to every response
    before it is returned to the agent for context injection.
    """

    def __init__(
        self,
        registry: FieldSelectionRegistry,
        extractor: ToolOutputFieldExtractor,
    ):
        self._registry = registry
        self._extractor = extractor
        self._total_calls = 0
        self._total_chars_before = 0
        self._total_chars_after = 0

    async def intercept(
        self,
        tool_name: str,
        tool_fn: Callable,
        args: Dict[str, Any],
        use_case: str = "",
    ) -> dict:
        self._total_calls += 1
        result = await tool_fn(**args)

        raw_str = json.dumps(result) if not isinstance(result, str) else result
        chars_before = len(raw_str)
        self._total_chars_before += chars_before

        rule = self._registry.lookup(tool_name, use_case)
        if rule is None:
            self._total_chars_after += chars_before
            return {
                "result": result,
                "field_selection_applied": False,
                "chars": chars_before,
            }

        extracted = self._extractor.extract(result, rule)
        extracted_str = json.dumps(extracted) if not isinstance(extracted, str) else extracted

        # Apply max_output_chars if configured
        if rule.max_output_chars > 0 and len(extracted_str) > rule.max_output_chars:
            extracted_str = extracted_str[:rule.max_output_chars] + "...[truncated]"
            extracted = extracted_str

        chars_after = len(extracted_str)
        self._total_chars_after += chars_after

        return {
            "result": extracted,
            "field_selection_applied": True,
            "rule": {"tool": tool_name, "mode": rule.mode.value, "use_case": rule.use_case},
            "chars_before": chars_before,
            "chars_after": chars_after,
            "chars_saved": chars_before - chars_after,
        }

    def savings_stats(self) -> dict:
        return {
            "total_calls": self._total_calls,
            "total_chars_before": self._total_chars_before,
            "total_chars_after": self._total_chars_after,
            "total_chars_saved": self._total_chars_before - self._total_chars_after,
            "reduction_rate": round(
                1 - self._total_chars_after / max(self._total_chars_before, 1), 3
            ),
        }
```

## Solution 5: Response Schema Analyzer

```python
import json
from collections import Counter
from typing import Any, Dict, List


class ToolResponseSchemaAnalyzer:
    """
    Analyzes tool response samples to identify which fields appear most
    frequently and which are rarely populated — informing field selection rules.
    """

    def __init__(self):
        self._field_counts: Counter = Counter()
        self._sample_count = 0

    def observe(self, response: Any) -> None:
        if isinstance(response, str):
            try:
                response = json.loads(response)
            except (json.JSONDecodeError, ValueError):
                return
        if isinstance(response, dict):
            self._sample_count += 1
            for key in response:
                self._field_counts[key] += 1

    def field_frequency(self) -> Dict[str, float]:
        if not self._sample_count:
            return {}
        return {
            field: round(count / self._sample_count, 3)
            for field, count in self._field_counts.most_common()
        }

    def suggest_include_rule(self, min_frequency: float = 0.8) -> List[str]:
        """Fields that appear in >= min_frequency fraction of samples."""
        return [
            field for field, freq in self.field_frequency().items()
            if freq >= min_frequency
        ]

    def suggest_exclude_rule(self, max_frequency: float = 0.1) -> List[str]:
        """Fields that appear in <= max_frequency fraction of samples (likely noise)."""
        return [
            field for field, freq in self.field_frequency().items()
            if freq <= max_frequency
        ]
```

## Solution 6: Field Selection Savings Dashboard

```python
import time


class FieldSelectionSavingsDashboard:
    """
    Reports field selection token savings for cost and performance tracking.
    """

    def __init__(
        self,
        interceptor: FieldSelectingToolInterceptor,
        registry: FieldSelectionRegistry,
    ):
        self._interceptor = interceptor
        self._registry = registry

    def render(self) -> dict:
        stats = self._interceptor.savings_stats()
        tokens_saved_est = stats["total_chars_saved"] // 4  # rough 4 chars/token
        return {
            "generated_at": time.time(),
            "field_selection_stats": stats,
            "estimated_tokens_saved": tokens_saved_est,
            "registered_tools": self._registry.registered_tools(),
        }
```

## Comparison

| Approach | Include Mode | Exclude Mode | Path Extraction | Rule Registry | Savings Tracking |
|---|---|---|---|---|---|
| ToolOutputFieldExtractor | Yes | Yes | Yes (dot-notation) | No | No |
| FieldSelectionRegistry | No | No | No | Yes (use-case aware) | No |
| FieldSelectingToolInterceptor | Via extractor | Via extractor | Via extractor | Via registry | Yes |
| ToolResponseSchemaAnalyzer | No | No | No | No | No (analysis only) |
| FieldSelectionSavingsDashboard | No | No | No | No | Yes |

**Best for production**: Start with `FieldSelectionMode.EXCLUDE` to remove fields you know are useless (audit timestamps, internal IDs, pagination cursors) — this is the lowest-effort improvement. Graduate to `FieldSelectionMode.INCLUDE` for tools where you have thoroughly analyzed what the model needs. Use `ToolResponseSchemaAnalyzer` in staging to observe real API responses and generate suggested rules automatically — fields that appear in less than 10% of responses are excellent exclusion candidates. Monitor `reduction_rate` per tool: above 0.7 (70% size reduction) indicates the tool returns extremely verbose responses and the API should be queried with explicit field selection parameters if the provider supports it (e.g., GraphQL, Notion API field lists, Salesforce SOQL SELECT).
