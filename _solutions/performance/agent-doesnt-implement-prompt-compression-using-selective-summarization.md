---
title: "Agent Doesn't Implement Prompt Compression Using Selective Summarization"
description: "Agents that inject full tool schemas, verbose system prompts, and unabridged conversation history into every LLM call waste tokens on content that is largely redundant with what the model already knows. Implement prompt compression that selectively summarizes verbose sections, strips redundant tool descriptions, and prunes boilerplate — reducing input token counts by 30–60% with minimal impact on response quality."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-prompt-compression-using-selective-summarization
tags: [prompt-compression, token-reduction, selective-summarization, context-optimization, boilerplate-pruning, input-tokens]
symptoms:
  - "70% of input tokens are system prompt and tool schemas that rarely change per turn"
  - "Full tool schemas for 30 tools are injected even when only 2 are relevant"
  - "Verbose error messages from previous tool calls inflate context with low-value content"
  - "No measurement of what fraction of input tokens are actually used by the model"
  - "Input token cost is the dominant cost driver despite short user queries"
---

## Why This Happens

LLM prompts accumulate static content: a system prompt written for the worst case, full schemas for all registered tools, and uncompressed tool results. Most of this content is redundant with the model's training or irrelevant to the current query. Selective compression identifies sections with high redundancy or low query relevance and replaces them with summaries, strips boilerplate, and removes irrelevant tool schemas — reducing token counts without removing information the model actually needs for the current turn.

## Solution 1: Prompt Section Model

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class PromptSectionKind(str, Enum):
    SYSTEM_INSTRUCTIONS = "system_instructions"
    TOOL_SCHEMA = "tool_schema"
    CONVERSATION_HISTORY = "conversation_history"
    TOOL_RESULT = "tool_result"
    USER_MESSAGE = "user_message"
    CONTEXT_INJECTION = "context_injection"


@dataclass
class PromptSection:
    kind: PromptSectionKind
    content: str
    name: str = ""
    token_count: Optional[int] = None
    compressible: bool = True
    relevance_score: float = 1.0    # 0.0–1.0; lower = more compressible

    def estimated_tokens(self) -> int:
        if self.token_count is not None:
            return self.token_count
        return max(1, len(self.content) // 4)
```

## Solution 2: Boilerplate Stripper

```python
import re
from typing import List


class BoilerplateStripper:
    """
    Removes known boilerplate patterns from prompt sections:
    redundant capability declarations, excessive formatting instructions,
    repeated safety caveats, and verbose XML-style wrappers.
    """

    BOILERPLATE_PATTERNS = [
        (re.compile(r"You are Claude, an AI assistant made by Anthropic\.\s*", re.IGNORECASE), ""),
        (re.compile(r"Always be helpful, harmless, and honest\.\s*", re.IGNORECASE), ""),
        (re.compile(r"</?(?:context|document|result|output)>\s*", re.IGNORECASE), ""),
        (re.compile(r"\n{4,}", re.MULTILINE), "\n\n"),
        (re.compile(r"[ \t]{3,}", re.MULTILINE), " "),
    ]

    def strip(self, content: str) -> str:
        result = content
        for pattern, replacement in self.BOILERPLATE_PATTERNS:
            result = pattern.sub(replacement, result)
        return result.strip()

    def tokens_saved(self, original: str, stripped: str) -> int:
        return max(0, len(original) // 4 - len(stripped) // 4)
```

## Solution 3: Tool Schema Compressor

```python
import re
from typing import Any, Dict, List


class ToolSchemaCompressor:
    """
    Reduces tool schema verbosity by truncating long descriptions,
    removing default values from non-required params, and
    condensing parameter documentation.
    """

    def __init__(
        self,
        max_description_chars: int = 150,
        max_param_description_chars: int = 80,
        include_optional_params: bool = True,
    ):
        self._max_desc = max_description_chars
        self._max_param = max_param_description_chars
        self._include_optional = include_optional_params

    def compress(self, schema: Dict[str, Any]) -> Dict[str, Any]:
        compressed = dict(schema)

        # Truncate top-level description
        if "description" in compressed:
            desc = compressed["description"]
            if len(desc) > self._max_desc:
                compressed["description"] = desc[:self._max_desc] + "…"

        # Compress parameters
        if "input_schema" in compressed and "properties" in compressed["input_schema"]:
            props = compressed["input_schema"]["properties"]
            required = set(compressed["input_schema"].get("required", []))
            new_props = {}
            for param_name, param_schema in props.items():
                if not self._include_optional and param_name not in required:
                    continue
                new_param = {"type": param_schema.get("type", "string")}
                if param_name in required:
                    new_param["required"] = True
                if "description" in param_schema:
                    d = param_schema["description"]
                    new_param["description"] = d[:self._max_param] + "…" if len(d) > self._max_param else d
                new_props[param_name] = new_param
            compressed["input_schema"] = {
                "type": "object",
                "properties": new_props,
                "required": list(required),
            }

        return compressed
```

## Solution 4: Relevance-Based Section Filter

```python
import re
from typing import List


class RelevanceSectionFilter:
    """
    Scores prompt sections by relevance to the current user query
    and drops low-relevance sections (e.g., tool schemas for unrelated tools).
    """

    def __init__(self, min_relevance_to_include: float = 0.30):
        self._min_relevance = min_relevance_to_include

    def score_section(self, section: PromptSection, query: str) -> float:
        if not section.compressible:
            return 1.0
        if section.kind == PromptSectionKind.USER_MESSAGE:
            return 1.0

        query_words = set(re.sub(r"[^a-z0-9 ]", "", query.lower()).split())
        content_words = set(re.sub(r"[^a-z0-9 ]", "", section.content.lower()).split())

        if not query_words:
            return 0.5

        overlap = len(query_words & content_words) / len(query_words)
        return min(1.0, overlap * 2)

    def filter_sections(
        self,
        sections: List[PromptSection],
        query: str,
    ) -> List[PromptSection]:
        result = []
        for section in sections:
            score = self.score_section(section, query)
            section.relevance_score = score
            if score >= self._min_relevance or not section.compressible:
                result.append(section)
        return result
```

## Solution 5: Prompt Compression Pipeline

```python
from typing import Any, Dict, List, Optional


class PromptCompressionPipeline:
    """
    Applies boilerplate stripping, tool schema compression,
    and relevance filtering to reduce total input token count.
    """

    def __init__(
        self,
        boilerplate_stripper: BoilerplateStripper,
        schema_compressor: ToolSchemaCompressor,
        relevance_filter: RelevanceSectionFilter,
    ):
        self._stripper = boilerplate_stripper
        self._schema = schema_compressor
        self._filter = relevance_filter

    def compress(
        self,
        sections: List[PromptSection],
        tool_schemas: List[Dict[str, Any]],
        query: str,
    ) -> dict:
        original_tokens = sum(s.estimated_tokens() for s in sections)

        # Strip boilerplate from compressible sections
        for section in sections:
            if section.compressible and section.kind != PromptSectionKind.TOOL_SCHEMA:
                original_content = section.content
                section.content = self._stripper.strip(section.content)
                section.token_count = None

        # Compress tool schemas
        compressed_schemas = [self._schema.compress(s) for s in tool_schemas]

        # Filter by relevance
        filtered_sections = self._filter.filter_sections(sections, query)

        final_tokens = sum(s.estimated_tokens() for s in filtered_sections)
        tokens_saved = original_tokens - final_tokens

        return {
            "sections": filtered_sections,
            "compressed_schemas": compressed_schemas,
            "original_tokens_est": original_tokens,
            "final_tokens_est": final_tokens,
            "tokens_saved_est": tokens_saved,
            "compression_ratio": round(final_tokens / max(original_tokens, 1), 4),
            "sections_filtered": len(sections) - len(filtered_sections),
        }
```

## Solution 6: Compression Savings Monitor

```python
import time
from typing import List


class CompressionSavingsMonitor:
    """Tracks compression savings over time for cost and quality analysis."""

    def __init__(self):
        self._events: List[dict] = []

    def record(self, result: dict, session_id: str = "") -> None:
        self._events.append({
            "ts": time.time(),
            "session_id": session_id,
            "tokens_saved": result.get("tokens_saved_est", 0),
            "compression_ratio": result.get("compression_ratio", 1.0),
            "sections_filtered": result.get("sections_filtered", 0),
        })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [e for e in self._events if e["ts"] >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "compressions": 0}
        total_saved = sum(e["tokens_saved"] for e in recent)
        avg_ratio = sum(e["compression_ratio"] for e in recent) / len(recent)
        return {
            "window_seconds": window_seconds,
            "compressions": len(recent),
            "total_tokens_saved": total_saved,
            "avg_compression_ratio": round(avg_ratio, 4),
            "savings_pct": round((1 - avg_ratio) * 100, 1),
        }
```

## Comparison

| Approach | Boilerplate Removal | Schema Compression | Relevance Filtering | Token Measurement | Savings Monitoring |
|---|---|---|---|---|---|
| BoilerplateStripper | Yes | No | No | Yes (delta) | No |
| ToolSchemaCompressor | No | Yes | No | No | No |
| RelevanceSectionFilter | No | No | Yes (score) | No | No |
| PromptCompressionPipeline | Via stripper | Via compressor | Via filter | Yes | No |
| CompressionSavingsMonitor | No | No | No | No | Yes |

**Best for production**: Apply compression on every call — even a 20% token reduction compounds into significant cost savings at scale. Tune `min_relevance_to_include=0.30` carefully: too aggressive filtering removes tool schemas the model unexpectedly needs, degrading quality; start at 0.20 and raise until quality drops. Use `ToolSchemaCompressor` to reduce tool schema tokens by 50–70% — descriptions are the largest contributor and models respond equally well to 100-char descriptions as to 500-char ones for most tools. Monitor `avg_compression_ratio` in `CompressionSavingsMonitor`: ratio above 0.85 (less than 15% savings) means the prompt is already lean and further compression efforts have diminishing returns.
