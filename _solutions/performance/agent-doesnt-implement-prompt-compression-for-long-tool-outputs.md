---
title: "Agent Doesn't Implement Prompt Compression for Long Tool Outputs"
description: "Agents that inject full tool outputs into the context verbatim — complete database result sets, entire web pages, full API responses — fill the context window with content the LLM will largely ignore. Implement prompt compression that extracts the information-dense portions of tool outputs, removes boilerplate and redundant content, and compresses large results to their essential facts before context injection."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-prompt-compression-for-long-tool-outputs
tags: [prompt-compression, context-efficiency, tool-output-filtering, boilerplate-removal, information-density, token-reduction]
symptoms:
  - "Full HTML pages injected into context when only a few paragraphs are relevant"
  - "Database result sets with 200 rows injected when the LLM needs only 5-10"
  - "API responses include metadata headers and pagination fields that consume context"
  - "Context window fills with tool output boilerplate before meaningful content is injected"
  - "No measurement of compression ratio or tokens saved by output filtering"
---

## Why This Happens

Tools return what the underlying data source provides — and data sources are not designed with LLM context budgets in mind. A web scraper returns full HTML; a database driver returns all columns including internal IDs, timestamps, and audit fields; an API returns envelope metadata around the actual payload. Without a compression step, the agent injects this raw content, wasting tokens on content the LLM treats as noise. Compression should be tool-specific: HTML pages need different extraction logic than JSON API responses or database result sets.

## Solution 1: Compression Strategy

```python
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, List, Optional


class CompressionStrategy(str, Enum):
    TRUNCATE = "truncate"           # hard character/token limit
    EXTRACT_FIELDS = "extract_fields"  # keep only specified JSON fields
    STRIP_HTML = "strip_html"       # remove HTML, keep text
    SUMMARIZE = "summarize"         # use LLM to summarize (expensive)
    DEDUPLICATE = "deduplicate"     # remove duplicate lines/entries
    TOP_N_ROWS = "top_n_rows"       # keep first N rows of tabular data
    REGEX_FILTER = "regex_filter"   # keep lines matching a pattern


@dataclass
class CompressionConfig:
    strategy: CompressionStrategy
    max_chars: int = 2000
    fields_to_keep: List[str] = None
    fields_to_drop: List[str] = None
    top_n: int = 10
    regex_pattern: str = ""
    preserve_structure: bool = True

    def __post_init__(self):
        if self.fields_to_keep is None:
            self.fields_to_keep = []
        if self.fields_to_drop is None:
            self.fields_to_drop = []
```

## Solution 2: Tool Output Compressor

```python
import json
import re
from typing import Any, List, Optional


class ToolOutputCompressor:
    """
    Applies compression strategies to tool outputs before context injection.
    Returns both the compressed output and compression statistics.
    """

    def compress(
        self,
        output: Any,
        config: CompressionConfig,
    ) -> tuple:
        """Returns (compressed_output, original_chars, compressed_chars)."""
        original_str = self._to_string(output)
        original_chars = len(original_str)

        if config.strategy == CompressionStrategy.TRUNCATE:
            result = self._truncate(original_str, config.max_chars)

        elif config.strategy == CompressionStrategy.EXTRACT_FIELDS:
            result = self._extract_fields(output, config.fields_to_keep, config.fields_to_drop)

        elif config.strategy == CompressionStrategy.STRIP_HTML:
            result = self._strip_html(original_str)
            if len(result) > config.max_chars:
                result = self._truncate(result, config.max_chars)

        elif config.strategy == CompressionStrategy.TOP_N_ROWS:
            result = self._top_n_rows(output, config.top_n)

        elif config.strategy == CompressionStrategy.DEDUPLICATE:
            result = self._deduplicate_lines(original_str)
            if len(result) > config.max_chars:
                result = self._truncate(result, config.max_chars)

        elif config.strategy == CompressionStrategy.REGEX_FILTER:
            result = self._regex_filter(original_str, config.regex_pattern)
            if len(result) > config.max_chars:
                result = self._truncate(result, config.max_chars)

        else:
            result = self._truncate(original_str, config.max_chars)

        compressed_str = self._to_string(result)
        return compressed_str, original_chars, len(compressed_str)

    def _to_string(self, obj: Any) -> str:
        if isinstance(obj, str):
            return obj
        try:
            return json.dumps(obj, ensure_ascii=False)
        except Exception:
            return str(obj)

    def _truncate(self, text: str, max_chars: int) -> str:
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + f"\n... [truncated {len(text) - max_chars} chars]"

    def _extract_fields(self, obj: Any, keep: List[str], drop: List[str]) -> Any:
        if isinstance(obj, dict):
            if keep:
                return {k: v for k, v in obj.items() if k in keep}
            if drop:
                return {k: v for k, v in obj.items() if k not in drop}
            return obj
        if isinstance(obj, list):
            return [self._extract_fields(item, keep, drop) for item in obj[:50]]
        return obj

    def _strip_html(self, html: str) -> str:
        # Remove script and style blocks
        html = re.sub(r"<(script|style)[^>]*>.*?</(script|style)>", "", html, flags=re.DOTALL | re.IGNORECASE)
        # Remove HTML tags
        text = re.sub(r"<[^>]+>", " ", html)
        # Normalize whitespace
        text = re.sub(r"\s{3,}", "\n\n", text)
        return text.strip()

    def _top_n_rows(self, obj: Any, n: int) -> Any:
        if isinstance(obj, list):
            total = len(obj)
            truncated = obj[:n]
            if total > n:
                if isinstance(truncated, list):
                    return truncated + [f"... and {total - n} more rows"]
            return truncated
        if isinstance(obj, str):
            lines = obj.splitlines()
            if len(lines) > n:
                return "\n".join(lines[:n]) + f"\n... and {len(lines) - n} more lines"
            return obj
        return obj

    def _deduplicate_lines(self, text: str) -> str:
        seen = set()
        result = []
        for line in text.splitlines():
            stripped = line.strip()
            if stripped and stripped not in seen:
                seen.add(stripped)
                result.append(line)
        return "\n".join(result)

    def _regex_filter(self, text: str, pattern: str) -> str:
        if not pattern:
            return text
        try:
            compiled = re.compile(pattern, re.IGNORECASE)
            matching = [line for line in text.splitlines() if compiled.search(line)]
            return "\n".join(matching) if matching else text
        except re.error:
            return text
```

## Solution 3: Per-Tool Compression Registry

```python
from threading import Lock
from typing import Dict, Optional


class ToolCompressionRegistry:
    """
    Maps tool names to their compression configurations.
    """

    DEFAULT_CONFIGS = {
        "web_search": CompressionConfig(
            strategy=CompressionStrategy.STRIP_HTML,
            max_chars=3000,
        ),
        "database_query": CompressionConfig(
            strategy=CompressionStrategy.TOP_N_ROWS,
            top_n=20,
            fields_to_drop=["id", "created_at", "updated_at", "_id", "__v"],
        ),
        "api_call": CompressionConfig(
            strategy=CompressionStrategy.EXTRACT_FIELDS,
            max_chars=2000,
        ),
        "file_read": CompressionConfig(
            strategy=CompressionStrategy.TRUNCATE,
            max_chars=4000,
        ),
    }

    def __init__(self):
        self._configs: Dict[str, CompressionConfig] = dict(self.DEFAULT_CONFIGS)
        self._lock = Lock()

    def register(self, tool_name: str, config: CompressionConfig) -> None:
        with self._lock:
            self._configs[tool_name] = config

    def get(self, tool_name: str) -> Optional[CompressionConfig]:
        with self._lock:
            return self._configs.get(tool_name)
```

## Solution 4: Compressing Tool Result Injector

```python
import time
from typing import Any, Optional


class CompressingToolResultInjector:
    """
    Compresses tool outputs using the registered strategy before
    they are injected into the LLM context.
    """

    def __init__(
        self,
        registry: ToolCompressionRegistry,
        compressor: ToolOutputCompressor,
    ):
        self._registry = registry
        self._compressor = compressor
        self._total_original_chars = 0
        self._total_compressed_chars = 0
        self._compressions = 0

    def inject(self, tool_name: str, tool_output: Any) -> tuple:
        """Returns (compressed_output_str, compression_ratio)."""
        config = self._registry.get(tool_name)
        if config is None:
            output_str = str(tool_output) if not isinstance(tool_output, str) else tool_output
            return output_str, 1.0

        compressed, orig_chars, comp_chars = self._compressor.compress(tool_output, config)
        self._total_original_chars += orig_chars
        self._total_compressed_chars += comp_chars
        self._compressions += 1

        ratio = round(comp_chars / max(orig_chars, 1), 4)
        return compressed, ratio

    def savings_summary(self) -> dict:
        savings_pct = round(
            (1 - self._total_compressed_chars / max(self._total_original_chars, 1)) * 100,
            1,
        )
        return {
            "compressions": self._compressions,
            "total_original_chars": self._total_original_chars,
            "total_compressed_chars": self._total_compressed_chars,
            "savings_pct": savings_pct,
        }
```

## Solution 5: Compression Quality Monitor

```python
import time
from typing import List


class CompressionQualityMonitor:
    """
    Tracks compression ratios per tool to detect configurations
    that over-compress (losing information) or under-compress (wasting tokens).
    """

    def __init__(self):
        self._records: List[dict] = []

    def record(
        self,
        tool_name: str,
        original_chars: int,
        compressed_chars: int,
        ratio: float,
    ) -> None:
        self._records.append({
            "ts": time.time(),
            "tool": tool_name,
            "original": original_chars,
            "compressed": compressed_chars,
            "ratio": ratio,
        })

    def per_tool_stats(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        by_tool: dict = {}
        for r in recent:
            t = r["tool"]
            if t not in by_tool:
                by_tool[t] = {"count": 0, "total_ratio": 0.0, "total_saved": 0}
            by_tool[t]["count"] += 1
            by_tool[t]["total_ratio"] += r["ratio"]
            by_tool[t]["total_saved"] += r["original"] - r["compressed"]
        return {
            t: {
                "calls": v["count"],
                "avg_ratio": round(v["total_ratio"] / v["count"], 3),
                "total_chars_saved": v["total_saved"],
            }
            for t, v in by_tool.items()
        }
```

## Solution 6: Prompt Compression Dashboard

```python
import time


class PromptCompressionDashboard:
    """
    Combines injector savings and per-tool quality monitoring.
    """

    def __init__(
        self,
        injector: CompressingToolResultInjector,
        monitor: CompressionQualityMonitor,
    ):
        self._injector = injector
        self._monitor = monitor

    def render(self, window_seconds: float = 3600.0) -> dict:
        return {
            "generated_at": time.time(),
            "overall_savings": self._injector.savings_summary(),
            "per_tool_stats": self._monitor.per_tool_stats(window_seconds),
        }
```

## Comparison

| Approach | Multiple Strategies | Per-Tool Config | Savings Tracking | Quality Monitoring | Dashboard |
|---|---|---|---|---|---|
| ToolOutputCompressor | Yes (6 strategies) | No | No | No | No |
| ToolCompressionRegistry | No | Yes | No | No | No |
| CompressingToolResultInjector | Via compressor | Via registry | Yes | No | No |
| CompressionQualityMonitor | No | No | No | Yes (per-tool) | No |
| PromptCompressionDashboard | No | No | No | No | Yes |

**Best for production**: Apply `STRIP_HTML` to all web scraping tool outputs by default — raw HTML is typically 5-10× larger than the extracted text. Apply `TOP_N_ROWS` with `n=20` to all database queries as a safety floor, then tune down to n=5 for tools where empirical quality monitoring shows avg_ratio < 0.2 (meaning 80%+ compression with no LLM quality loss). Monitor `savings_pct` via `injector.savings_summary()`: below 30% means tools are already returning compact responses and compression adds overhead without benefit; above 80% warrants a quality review to ensure information isn't being lost. Never apply `TRUNCATE` to structured JSON responses — use `EXTRACT_FIELDS` instead so the remaining content stays parseable.
