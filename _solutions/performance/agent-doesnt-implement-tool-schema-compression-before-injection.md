---
title: "Agent Doesn't Implement Tool Schema Compression Before Injection"
description: "Agents that inject full verbose JSON schemas for all available tools into every prompt spend hundreds of tokens per tool on descriptions, examples, and boilerplate that rarely affects model behavior. Implement tool schema compression that strips redundant fields, abbreviates descriptions, and injects only the schemas for tools the current query is likely to need — reducing tool schema token cost by 60-80%."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-tool-schema-compression-before-injection
tags: [tool-schema, schema-compression, token-reduction, tool-selection, schema-pruning, prompt-efficiency]
symptoms:
  - "Tool schemas consume 30-40% of the context window before any conversation history is included"
  - "Verbose description fields with examples are included in every prompt regardless of relevance"
  - "All 50 registered tools are injected even when the query only needs 2-3 of them"
  - "No per-query tool selection — the full tool registry is always included"
  - "Optional schema fields like 'examples', 'default', and verbose 'description' bloat the schema"
---

## Why This Happens

Tool schemas are designed for human readability and include extensive documentation — long descriptions, usage examples, default values, and detailed parameter explanations. When injected into LLM prompts, this documentation consumes tokens that could be used for conversation history or retrieved context. Most of this verbose content does not improve tool selection accuracy; the model needs the parameter names, types, and required fields, but rarely needs the full prose description. Schema compression removes the verbose fields and injects only the minimal representation needed for the model to call the tool correctly.

## Solution 1: Schema Compression Policy

```python
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class SchemaCompressionPolicy:
    strip_description_to_chars: int = 80     # truncate descriptions beyond this
    strip_examples: bool = True              # remove 'examples' fields
    strip_defaults: bool = False             # keep defaults (helps model)
    strip_optional_params: bool = False      # keep optional params
    strip_deprecated_params: bool = True     # remove deprecated fields
    max_enum_values: int = 10               # truncate long enum lists
    required_only_mode: bool = False         # extreme: required params only
    preserve_tool_names: List[str] = field(default_factory=list)  # never compress these
```

## Solution 2: Tool Schema Compressor

```python
import copy
import json
from typing import Any, Dict, List, Optional


class ToolSchemaCompressor:
    """
    Applies a SchemaCompressionPolicy to a tool schema dict,
    returning a compressed version with reduced token footprint.
    """

    def __init__(self, policy: SchemaCompressionPolicy):
        self._policy = policy

    def compress(self, schema: Dict[str, Any]) -> Dict[str, Any]:
        if schema.get("name") in self._policy.preserve_tool_names:
            return schema

        compressed = copy.deepcopy(schema)

        # Truncate top-level description
        if "description" in compressed:
            compressed["description"] = self._truncate(
                compressed["description"],
                self._policy.strip_description_to_chars,
            )

        # Process parameters
        params = compressed.get("parameters", {})
        if "properties" in params:
            compressed["parameters"] = self._compress_params(params)

        return compressed

    def _truncate(self, text: str, max_chars: int) -> str:
        if not text or len(text) <= max_chars:
            return text
        return text[:max_chars].rstrip() + "…"

    def _compress_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        result = copy.deepcopy(params)
        properties = result.get("properties", {})
        required = set(result.get("required", []))
        compressed_props = {}

        for name, prop in properties.items():
            # Strip deprecated params
            if self._policy.strip_deprecated_params and prop.get("deprecated"):
                continue
            # Strip optional params in required-only mode
            if self._policy.required_only_mode and name not in required:
                continue

            compressed_prop = {}
            # Always keep type
            if "type" in prop:
                compressed_prop["type"] = prop["type"]
            # Truncate description
            if "description" in prop:
                compressed_prop["description"] = self._truncate(
                    prop["description"],
                    self._policy.strip_description_to_chars,
                )
            # Truncate enum values
            if "enum" in prop:
                enum_vals = prop["enum"]
                if len(enum_vals) > self._policy.max_enum_values:
                    enum_vals = enum_vals[:self._policy.max_enum_values]
                    compressed_prop["enum"] = enum_vals
                    compressed_prop["_enum_truncated"] = True
                else:
                    compressed_prop["enum"] = enum_vals
            # Optionally strip examples
            if not self._policy.strip_examples and "examples" in prop:
                compressed_prop["examples"] = prop["examples"]
            # Optionally strip defaults
            if not self._policy.strip_defaults and "default" in prop:
                compressed_prop["default"] = prop["default"]
            # Keep items for arrays
            if "items" in prop:
                compressed_prop["items"] = prop["items"]

            compressed_props[name] = compressed_prop

        result["properties"] = compressed_props
        return result

    def compress_all(self, schemas: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [self.compress(s) for s in schemas]

    def token_estimate(self, schema: Dict[str, Any]) -> int:
        return len(json.dumps(schema)) // 4
```

## Solution 3: Query-Relevant Tool Selector

```python
import re
from typing import Any, Dict, List, Set


TOOL_CATEGORY_KEYWORDS: Dict[str, List[str]] = {
    "web": ["search", "fetch", "url", "website", "browse", "scrape"],
    "database": ["query", "sql", "record", "table", "db", "database", "insert"],
    "file": ["file", "read", "write", "path", "directory", "upload", "download"],
    "calendar": ["calendar", "schedule", "event", "meeting", "appointment", "date"],
    "email": ["email", "send", "message", "inbox", "mail", "smtp"],
    "code": ["code", "run", "execute", "script", "function", "compile"],
    "math": ["calculate", "compute", "math", "sum", "average", "convert"],
    "image": ["image", "photo", "picture", "generate", "vision", "ocr"],
}


class QueryRelevantToolSelector:
    """
    Selects the subset of tool schemas most likely to be needed
    for a given query, reducing the schema injection footprint.
    """

    def __init__(
        self,
        always_include: List[str] = None,   # tool names always injected
        max_tools: int = 10,
    ):
        self._always_include = set(always_include or [])
        self._max_tools = max_tools
        self._tool_categories: Dict[str, Set[str]] = {}  # tool_name -> categories

    def register_tool_categories(
        self, tool_name: str, categories: List[str]
    ) -> None:
        self._tool_categories[tool_name] = set(categories)

    def select(
        self,
        query: str,
        schemas: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        query_lower = query.lower()
        detected_categories: Set[str] = set()

        for category, keywords in TOOL_CATEGORY_KEYWORDS.items():
            if any(kw in query_lower for kw in keywords):
                detected_categories.add(category)

        selected = []
        for schema in schemas:
            name = schema.get("name", "")
            if name in self._always_include:
                selected.append(schema)
                continue
            tool_cats = self._tool_categories.get(name, set())
            if tool_cats & detected_categories:
                selected.append(schema)

        # If no category match, fall back to all tools up to max
        if not selected:
            selected = schemas[:self._max_tools]

        return selected[:self._max_tools]
```

## Solution 4: Schema Injection Optimizer

```python
import json
from typing import Any, Dict, List


class SchemaInjectionOptimizer:
    """
    Combines query-relevant tool selection with schema compression
    to produce the minimal schema injection for each prompt.
    """

    def __init__(
        self,
        selector: QueryRelevantToolSelector,
        compressor: ToolSchemaCompressor,
        token_budget: int = 4000,
    ):
        self._selector = selector
        self._compressor = compressor
        self._budget = token_budget

    def optimize(
        self,
        query: str,
        all_schemas: List[Dict[str, Any]],
    ) -> dict:
        selected = self._selector.select(query, all_schemas)
        compressed = self._compressor.compress_all(selected)

        # Fit within token budget
        total_tokens = 0
        fitting = []
        for schema in compressed:
            tokens = self._compressor.token_estimate(schema)
            if total_tokens + tokens > self._budget:
                break
            fitting.append(schema)
            total_tokens += tokens

        original_tokens = sum(
            self._compressor.token_estimate(s)
            for s in all_schemas
        )

        return {
            "schemas": fitting,
            "selected_count": len(selected),
            "fitting_count": len(fitting),
            "total_available": len(all_schemas),
            "injected_tokens_est": total_tokens,
            "original_tokens_est": original_tokens,
            "tokens_saved_est": original_tokens - total_tokens,
            "reduction_pct": round(
                (original_tokens - total_tokens) / max(original_tokens, 1) * 100, 1
            ),
        }
```

## Solution 5: Schema Compression Quality Checker

```python
import json
from typing import Any, Dict, List


REQUIRED_SCHEMA_FIELDS = {"name", "description"}
REQUIRED_PARAM_FIELDS = {"type"}


class SchemaCompressionQualityChecker:
    """
    Verifies that compressed schemas retain all fields necessary for
    the model to call the tool correctly. Flags schemas where
    compression has removed critical information.
    """

    def check(self, original: Dict[str, Any], compressed: Dict[str, Any]) -> dict:
        issues = []
        # Top-level required fields
        for field in REQUIRED_SCHEMA_FIELDS:
            if field in original and field not in compressed:
                issues.append(f"missing required field: {field}")

        # Required parameters must be present
        orig_required = set(
            original.get("parameters", {}).get("required", [])
        )
        comp_props = set(
            compressed.get("parameters", {}).get("properties", {}).keys()
        )
        missing_required = orig_required - comp_props
        if missing_required:
            issues.append(f"missing required params: {missing_required}")

        return {
            "tool_name": original.get("name", ""),
            "valid": len(issues) == 0,
            "issues": issues,
        }

    def check_all(
        self,
        originals: List[Dict[str, Any]],
        compressed: List[Dict[str, Any]],
    ) -> List[dict]:
        return [
            self.check(orig, comp)
            for orig, comp in zip(originals, compressed)
        ]
```

## Solution 6: Schema Compression Dashboard

```python
import time
from typing import List


class SchemaCompressionDashboard:
    """
    Tracks per-query schema injection efficiency and
    surfaces tools that contribute disproportionate token cost.
    """

    def __init__(self):
        self._records: List[dict] = []

    def record(self, optimizer_result: dict) -> None:
        self._records.append({**optimizer_result, "ts": time.time()})

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "queries": 0}
        total_saved = sum(r.get("tokens_saved_est", 0) for r in recent)
        avg_reduction = sum(r.get("reduction_pct", 0) for r in recent) / len(recent)
        return {
            "window_seconds": window_seconds,
            "queries": len(recent),
            "total_tokens_saved_est": total_saved,
            "avg_reduction_pct": round(avg_reduction, 1),
            "avg_injected_tokens": round(
                sum(r.get("injected_tokens_est", 0) for r in recent) / len(recent), 0
            ),
        }
```

## Comparison

| Approach | Description Truncation | Field Stripping | Query-Relevant Selection | Budget Enforcement | Quality Check |
|---|---|---|---|---|---|
| ToolSchemaCompressor | Yes | Yes (examples, deprecated) | No | No | No |
| QueryRelevantToolSelector | No | No | Yes (category match) | No | No |
| SchemaInjectionOptimizer | Via compressor | Via compressor | Via selector | Yes (token budget) | No |
| SchemaCompressionQualityChecker | No | No | No | No | Yes |
| SchemaCompressionDashboard | No | No | No | No | Yes |

**Best for production**: Start with `strip_description_to_chars=80` and `strip_examples=True` — these two changes alone typically reduce schema token cost by 50% with no measurable impact on tool call accuracy. Use `QueryRelevantToolSelector` with `always_include` set to 2-3 general-purpose tools (like `search` and `calculator`) that should always be available regardless of query category. Run `SchemaCompressionQualityChecker.check_all()` in CI to verify that compression never removes required parameter definitions — a compressed schema that omits a required field will cause the model to produce invalid tool calls.
