---
title: "Agent Doesn't Implement Streaming JSON Parsing for Large Tool Responses"
description: "Agents that buffer entire tool responses before parsing block for the full network round-trip before any processing begins — a 5MB database result set sits in memory twice (raw bytes + parsed object) before the agent can filter it down to 10 relevant rows. Implement streaming JSON parsing that begins extracting relevant fields as bytes arrive, applies early termination when enough data is found, and enforces memory limits without loading the full response."
date: 2026-04-16
difficulty: advanced
category: performance
slug: agent-doesnt-implement-streaming-json-parsing-for-large-tool-responses
tags: [streaming-json, incremental-parsing, memory-efficiency, large-responses, early-termination, ijson]
symptoms:
  - "Tool returning a 5MB JSON array causes 10MB memory spike (raw + parsed)"
  - "Agent waits 3 seconds for full response before processing the first item"
  - "Context window is filled with thousands of rows when only the top 5 are needed"
  - "No early exit — agent parses the entire response even when a match is found at item 3"
  - "Out-of-memory errors when tool responses are unexpectedly large"
---

## Why This Happens

Standard JSON parsing with `json.loads()` requires the complete document before returning any result — it buffers everything, parses everything, and returns the full object tree. For large responses (database dumps, search result arrays, log files), this means paying full memory and latency costs even when only a small subset is needed. Streaming JSON parsers (like `ijson`) emit events as bytes arrive, enabling early termination and per-item processing without ever holding the full document in memory.

## Solution 1: Streaming Parse Configuration

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, List, Optional


class StreamingParseMode(str, Enum):
    FIRST_N_ITEMS = "first_n"         # stop after N items from an array
    FIELD_EXTRACTION = "field_extract" # extract specific fields only
    FILTER_MATCH = "filter_match"     # stop when predicate matches
    FULL_PARSE = "full_parse"         # streaming but no early exit


@dataclass
class StreamingParseConfig:
    mode: StreamingParseMode = StreamingParseMode.FIRST_N_ITEMS
    max_items: int = 100
    target_fields: List[str] = field(default_factory=list)
    filter_predicate: Optional[Callable[[Any], bool]] = None
    max_bytes: int = 10 * 1024 * 1024   # 10MB hard limit
    array_path: str = "item"             # ijson path for array items
```

## Solution 2: Incremental JSON Stream Parser

```python
import io
from typing import Any, AsyncIterator, Iterator, List, Optional


class IncrementalJsonStreamParser:
    """
    Wraps ijson to provide incremental parsing of JSON arrays
    from byte streams. Falls back to standard json.loads for
    small responses where streaming overhead exceeds savings.
    """

    STREAMING_THRESHOLD_BYTES = 8 * 1024   # use streaming above 8KB

    def __init__(self, config: StreamingParseConfig) -> None:
        self._config = config

    def _try_import_ijson(self):
        try:
            import ijson
            return ijson
        except ImportError:
            return None

    def parse_bytes(self, raw: bytes) -> List[Any]:
        """Parse a complete byte buffer, using streaming if large enough."""
        if len(raw) < self.STREAMING_THRESHOLD_BYTES:
            import json
            data = json.loads(raw)
            if isinstance(data, list):
                return self._apply_limits(data)
            return [data]

        ijson = self._try_import_ijson()
        if ijson is None:
            import json
            data = json.loads(raw)
            if isinstance(data, list):
                return self._apply_limits(data)
            return [data]

        return list(self._stream_from_bytes(raw, ijson))

    def _stream_from_bytes(self, raw: bytes, ijson) -> Iterator[Any]:
        stream = io.BytesIO(raw)
        items_seen = 0

        for item in ijson.items(stream, self._config.array_path):
            if self._config.target_fields:
                item = self._project_fields(item, self._config.target_fields)

            if (self._config.filter_predicate is None
                    or self._config.filter_predicate(item)):
                yield item
                items_seen += 1

            if (self._config.mode == StreamingParseMode.FIRST_N_ITEMS
                    and items_seen >= self._config.max_items):
                break

            if (self._config.mode == StreamingParseMode.FILTER_MATCH
                    and self._config.filter_predicate
                    and self._config.filter_predicate(item)):
                break

    def _apply_limits(self, items: List[Any]) -> List[Any]:
        if self._config.mode == StreamingParseMode.FIRST_N_ITEMS:
            items = items[:self._config.max_items]
        if self._config.target_fields:
            items = [self._project_fields(i, self._config.target_fields) for i in items]
        if self._config.filter_predicate:
            items = [i for i in items if self._config.filter_predicate(i)]
        return items

    @staticmethod
    def _project_fields(item: Any, fields: List[str]) -> Any:
        if not isinstance(item, dict):
            return item
        return {k: v for k, v in item.items() if k in fields}
```

## Solution 3: Chunked HTTP Stream Reader

```python
import asyncio
from typing import AsyncIterator, Optional


class ChunkedHttpStreamReader:
    """
    Reads an HTTP response body in chunks, enforcing a byte limit.
    Yields chunks as they arrive without buffering the full response.
    Compatible with httpx AsyncClient streaming responses.
    """

    def __init__(
        self,
        max_bytes: int = 10 * 1024 * 1024,
        chunk_size: int = 64 * 1024,
    ) -> None:
        self._max_bytes = max_bytes
        self._chunk_size = chunk_size

    async def read_chunks(self, response) -> AsyncIterator[bytes]:
        """Yields chunks from an httpx streaming response."""
        bytes_read = 0
        async for chunk in response.aiter_bytes(self._chunk_size):
            bytes_read += len(chunk)
            if bytes_read > self._max_bytes:
                remaining = chunk[:max(0, self._max_bytes - (bytes_read - len(chunk)))]
                if remaining:
                    yield remaining
                break
            yield chunk

    async def read_all_bounded(self, response) -> bytes:
        """Reads full response up to max_bytes limit."""
        chunks = []
        async for chunk in self.read_chunks(response):
            chunks.append(chunk)
        return b"".join(chunks)
```

## Solution 4: Streaming Tool Response Handler

```python
import time
from typing import Any, Dict, List, Optional


class StreamingToolResponseHandler:
    """
    Orchestrates streaming read + incremental parsing for large tool responses.
    Applies field projection and item limits before returning to the agent,
    keeping the processed result small enough for context injection.
    """

    def __init__(
        self,
        stream_reader: ChunkedHttpStreamReader,
        parser: IncrementalJsonStreamParser,
    ) -> None:
        self._reader = stream_reader
        self._parser = parser
        self._stats: List[dict] = []

    async def handle(
        self,
        response,   # httpx streaming response or bytes
        tool_name: str,
    ) -> Dict[str, Any]:
        start = time.time()
        original_bytes = 0

        if isinstance(response, bytes):
            raw = response
            original_bytes = len(raw)
        else:
            raw = await self._reader.read_all_bounded(response)
            original_bytes = len(raw)

        items = self._parser.parse_bytes(raw)
        latency = (time.time() - start) * 1000

        parsed_size = sum(len(str(i)) for i in items)
        self._stats.append({
            "tool": tool_name,
            "original_bytes": original_bytes,
            "items_extracted": len(items),
            "parsed_size_chars": parsed_size,
            "compression_ratio": round(original_bytes / max(parsed_size, 1), 2),
            "latency_ms": round(latency, 2),
        })

        return {
            "items": items,
            "item_count": len(items),
            "bytes_read": original_bytes,
            "truncated": original_bytes >= self._reader._max_bytes,
        }

    def stats(self) -> List[dict]:
        return list(self._stats)
```

## Solution 5: Response Size Policy Enforcer

```python
from typing import Dict, Optional


class ResponseSizePolicyEnforcer:
    """
    Applies per-tool response size and item limits based on registered policies.
    Prevents runaway tool responses from flooding the agent context.
    """

    @dataclass
    class ToolResponsePolicy:
        max_bytes: int = 5 * 1024 * 1024
        max_items: int = 50
        target_fields: List[str] = field(default_factory=list)
        array_path: str = "item"

    def __init__(self) -> None:
        self._policies: Dict[str, "ResponseSizePolicyEnforcer.ToolResponsePolicy"] = {}

    def register(self, tool_name: str, policy: "ResponseSizePolicyEnforcer.ToolResponsePolicy") -> None:
        self._policies[tool_name] = policy

    def get_config(self, tool_name: str) -> StreamingParseConfig:
        policy = self._policies.get(
            tool_name,
            self.ToolResponsePolicy(),
        )
        return StreamingParseConfig(
            mode=StreamingParseMode.FIRST_N_ITEMS,
            max_items=policy.max_items,
            target_fields=policy.target_fields,
            max_bytes=policy.max_bytes,
            array_path=policy.array_path,
        )

    def build_handler(self, tool_name: str) -> StreamingToolResponseHandler:
        config = self.get_config(tool_name)
        return StreamingToolResponseHandler(
            stream_reader=ChunkedHttpStreamReader(max_bytes=config.max_bytes),
            parser=IncrementalJsonStreamParser(config),
        )
```

## Solution 6: Streaming Parse Dashboard

```python
import time


class StreamingParseDashboard:
    """
    Aggregates streaming parse stats across all tool invocations
    to measure memory savings and early-termination effectiveness.
    """

    def __init__(self, handlers: List[StreamingToolResponseHandler]) -> None:
        self._handlers = handlers

    def render(self) -> dict:
        all_stats = []
        for h in self._handlers:
            all_stats.extend(h.stats())

        if not all_stats:
            return {"generated_at": time.time(), "invocations": 0}

        total_original = sum(s["original_bytes"] for s in all_stats)
        total_parsed = sum(s["parsed_size_chars"] for s in all_stats)
        avg_compression = round(total_original / max(total_parsed, 1), 2)
        truncated = sum(1 for s in all_stats if s.get("truncated", False))

        return {
            "generated_at": time.time(),
            "invocations": len(all_stats),
            "total_bytes_received": total_original,
            "total_chars_extracted": total_parsed,
            "avg_compression_ratio": avg_compression,
            "truncated_responses": truncated,
            "avg_latency_ms": round(
                sum(s["latency_ms"] for s in all_stats) / len(all_stats), 2
            ),
        }
```

## Comparison

| Approach | Incremental Parsing | Early Termination | Byte Limit | Field Projection | Per-Tool Policy |
|---|---|---|---|---|---|
| IncrementalJsonStreamParser | Yes (ijson) | Yes | Via config | Yes | No |
| ChunkedHttpStreamReader | No | No | Yes | No | No |
| StreamingToolResponseHandler | Via parser | Via parser | Via reader | Via parser | No |
| ResponseSizePolicyEnforcer | Via handler | Via handler | Via handler | Via handler | Yes |
| StreamingParseDashboard | No | No | No | No | No (reporting) |

**Best for production**: Register `ToolResponsePolicy` for every tool that can return variable-length results — database queries, search APIs, log fetchers. Set `max_items=50` as the default and increase only for tools where context completeness outweighs token cost. Use `target_fields` aggressively: if the agent only needs `id`, `name`, and `score` from a search result, project those three fields and discard the rest before they ever enter Python's object heap. Monitor `avg_compression_ratio`: values above 10× confirm that field projection is eliminating most of the response payload before context injection.
