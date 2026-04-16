---
title: "Agent Doesn't Implement Tool Result Compression Before Context Injection"
description: "Agents that inject raw tool results verbatim into the LLM context include structural overhead that consumes tokens without adding information: JSON keys repeated for every record, HTML tags surrounding article text, XML namespaces prefixing every element, and CSV headers repeated in multi-page results. Implement tool result compression that strips structural overhead, extracts the information-dense content, and encodes it in a compact format before context injection — reducing token consumption without losing the data the model needs."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-tool-result-compression-before-context-injection
tags: [result-compression, structural-overhead, json-compaction, html-stripping, token-reduction, context-packing]
symptoms:
  - "JSON tool results inject key names for every record when only values are needed"
  - "Web scraper results include full HTML markup around the target content"
  - "CSV data includes repeated headers across paginated results"
  - "XML responses with deeply nested namespace prefixes consume 40% overhead tokens"
  - "No distinction between structural tokens (keys, tags) and content tokens (values)"
---

## Why This Happens

Tools return data in formats designed for machine consumption, not LLM context efficiency. A JSON array of 50 objects with 8 fields each repeats all 8 field names 50 times. HTML wraps article text in dozens of structural tags. CSV paginated results include header rows on every page. These structural tokens — keys, tags, separators — are necessary for programmatic parsing but redundant when the LLM only needs the content values. Compression strips the structure and encodes the content in a more compact representation: a JSON array becomes a key-prefixed table, HTML becomes stripped plain text, CSV becomes a compact column-aligned block. The model receives the same information in fewer tokens.

## Solution 1: Compression Policy

```python
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class CompressionFormat(str, Enum):
    JSON_TABLE = "json_table"        # JSON array -> compact key:value table
    HTML_STRIP = "html_strip"        # HTML -> plain text
    XML_EXTRACT = "xml_extract"      # XML -> extracted values
    CSV_COMPACT = "csv_compact"      # CSV -> no repeated headers
    MARKDOWN_STRIP = "markdown_strip"  # Markdown -> plain text
    PASSTHROUGH = "passthrough"       # no compression


@dataclass
class CompressionPolicy:
    format: CompressionFormat
    max_items: Optional[int] = None     # for list formats: max records to include
    key_allowlist: Optional[list] = None  # for JSON: only include these keys
    preserve_structure: bool = False     # keep minimal structure (e.g., indented vs flat)
    tokens_per_char: float = 0.25
```

## Solution 2: JSON Table Compressor

```python
import json
from typing import Any, Dict, List, Optional


class JSONTableCompressor:
    """
    Compresses a JSON array of objects into a compact table format.
    Instead of repeating key names per record, emits a header row
    followed by value-only rows.
    """

    def compress(
        self,
        data: Any,
        policy: CompressionPolicy,
    ) -> str:
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError:
                return data

        if isinstance(data, dict):
            data = [data]

        if not isinstance(data, list) or not data:
            return json.dumps(data, ensure_ascii=False)

        # Limit records
        records = data[:policy.max_items] if policy.max_items else data
        if not isinstance(records[0], dict):
            return "\n".join(str(r) for r in records)

        # Determine columns
        all_keys = list(records[0].keys())
        if policy.key_allowlist:
            keys = [k for k in all_keys if k in policy.key_allowlist]
        else:
            keys = all_keys

        lines = [" | ".join(keys)]
        lines.append("-" * len(lines[0]))
        for record in records:
            row = " | ".join(str(record.get(k, "")) for k in keys)
            lines.append(row)

        if len(data) > len(records):
            lines.append(f"[{len(data) - len(records)} more records omitted]")

        return "\n".join(lines)
```

## Solution 3: HTML Strip Compressor

```python
import re
from typing import Optional


class HTMLStripCompressor:
    """
    Strips HTML markup and extracts readable text content.
    Removes scripts, styles, navigation, and structural tags.
    Collapses whitespace and preserves paragraph breaks.
    """

    REMOVE_TAGS = re.compile(
        r"<(script|style|nav|header|footer|aside|noscript)[^>]*>.*?</\1>",
        re.DOTALL | re.IGNORECASE,
    )
    TAG_PATTERN = re.compile(r"<[^>]+>")
    WHITESPACE = re.compile(r"[ \t]{2,}")
    NEWLINES = re.compile(r"\n{3,}")
    HTML_ENTITIES = {
        "&amp;": "&", "&lt;": "<", "&gt;": ">",
        "&quot;": '"', "&nbsp;": " ", "&#39;": "'",
    }

    def compress(self, html: str, policy: CompressionPolicy) -> str:
        # Remove structural noise sections
        text = self.REMOVE_TAGS.sub("", html)
        # Replace block elements with newlines before stripping
        text = re.sub(r"</(p|div|li|h[1-6]|br|tr)>", "\n", text, flags=re.IGNORECASE)
        # Strip remaining tags
        text = self.TAG_PATTERN.sub("", text)
        # Decode HTML entities
        for entity, char in self.HTML_ENTITIES.items():
            text = text.replace(entity, char)
        # Normalize whitespace
        text = self.WHITESPACE.sub(" ", text)
        text = self.NEWLINES.sub("\n\n", text)
        text = text.strip()

        if policy.max_items:
            # Treat max_items as max lines for HTML
            lines = text.split("\n")
            if len(lines) > policy.max_items:
                text = "\n".join(lines[:policy.max_items])
                text += f"\n[{len(lines) - policy.max_items} lines omitted]"

        return text
```

## Solution 4: CSV Compact Compressor

```python
import csv
import io
from typing import Optional


class CSVCompactCompressor:
    """
    Compresses CSV data by deduplicating headers across paginated results
    and applying column filtering.
    """

    def compress(self, csv_text: str, policy: CompressionPolicy) -> str:
        reader = csv.DictReader(io.StringIO(csv_text))
        rows = list(reader)

        if not rows:
            return csv_text

        keys = policy.key_allowlist or list(rows[0].keys())
        if policy.max_items:
            rows = rows[:policy.max_items]

        lines = [" | ".join(keys)]
        lines.append("-" * len(lines[0]))
        for row in rows:
            lines.append(" | ".join(str(row.get(k, "")) for k in keys))

        return "\n".join(lines)
```

## Solution 5: Tool Result Compressor Registry

```python
from typing import Any, Dict


class ToolResultCompressorRegistry:
    """
    Maps tool names to compression policies and dispatches
    compression to the appropriate compressor.
    """

    def __init__(self):
        self._policies: Dict[str, CompressionPolicy] = {}
        self._json_compressor = JSONTableCompressor()
        self._html_compressor = HTMLStripCompressor()
        self._csv_compressor = CSVCompactCompressor()
        self._default_policy = CompressionPolicy(format=CompressionFormat.PASSTHROUGH)

    def register(self, tool_name: str, policy: CompressionPolicy) -> None:
        self._policies[tool_name] = policy

    def compress(self, tool_name: str, result: Any) -> dict:
        policy = self._policies.get(tool_name, self._default_policy)
        original = str(result) if not isinstance(result, str) else result
        original_chars = len(original)

        if policy.format == CompressionFormat.JSON_TABLE:
            compressed = self._json_compressor.compress(result, policy)
        elif policy.format == CompressionFormat.HTML_STRIP:
            compressed = self._html_compressor.compress(original, policy)
        elif policy.format == CompressionFormat.CSV_COMPACT:
            compressed = self._csv_compressor.compress(original, policy)
        else:
            compressed = original

        compressed_chars = len(compressed)
        tokens_saved = int((original_chars - compressed_chars) * policy.tokens_per_char)

        return {
            "compressed": compressed,
            "original_chars": original_chars,
            "compressed_chars": compressed_chars,
            "chars_saved": original_chars - compressed_chars,
            "tokens_saved_est": tokens_saved,
            "format_applied": policy.format.value,
        }
```

## Solution 6: Compression Savings Monitor

```python
import time
from threading import Lock
from typing import List


class CompressionSavingsMonitor:
    """
    Tracks token savings from compression across all tool calls.
    Surfaces which tools benefit most from compression.
    """

    def __init__(self):
        self._events: List[dict] = []
        self._lock = Lock()

    def record(self, tool_name: str, result: dict) -> None:
        with self._lock:
            self._events.append({
                "tool_name": tool_name,
                "tokens_saved": result.get("tokens_saved_est", 0),
                "chars_saved": result.get("chars_saved", 0),
                "format": result.get("format_applied", "passthrough"),
                "ts": time.time(),
            })
            if len(self._events) > 50000:
                self._events.pop(0)

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [e for e in self._events if e["ts"] >= cutoff]

        if not recent:
            return {"window_seconds": window_seconds, "compressions": 0}

        from collections import defaultdict
        by_tool: dict = defaultdict(lambda: {"count": 0, "saved": 0})
        for e in recent:
            by_tool[e["tool_name"]]["count"] += 1
            by_tool[e["tool_name"]]["saved"] += e["tokens_saved"]

        total_saved = sum(e["tokens_saved"] for e in recent)

        return {
            "window_seconds": window_seconds,
            "compressions": len(recent),
            "total_tokens_saved": total_saved,
            "by_tool": dict(sorted(
                by_tool.items(),
                key=lambda kv: kv[1]["saved"],
                reverse=True,
            )[:10]),
        }
```

## Comparison

| Approach | JSON Compaction | HTML Stripping | CSV Dedup | Per-Tool Policy | Savings Tracking |
|---|---|---|---|---|---|
| JSONTableCompressor | Yes (table format) | No | No | Via policy | No |
| HTMLStripCompressor | No | Yes (tag removal) | No | Via policy | No |
| CSVCompactCompressor | No | No | Yes | Via policy | No |
| ToolResultCompressorRegistry | Via compressors | Via compressors | Via compressors | Yes | No |
| CompressionSavingsMonitor | No | No | No | No | Yes |

**Best for production**: Apply `CompressionFormat.JSON_TABLE` with `key_allowlist` for database query results — specifying only the columns the model actually needs eliminates both structural overhead and irrelevant fields in a single pass. Use `CompressionFormat.HTML_STRIP` for all web scraping tools by default, since HTML markup is never useful to the model and typically represents 30–60% of raw response size. Monitor `total_tokens_saved` in `CompressionSavingsMonitor`: consistently high values confirm that compression is working, while a drop after a tool update signals that the tool changed its output format and the compression policy needs to be updated.
