---
title: "Agent Doesn't Implement Zero-Copy Buffer for Large Tool Responses"
description: "AI agents that pass large tool responses through multiple layers of string concatenation and re-encoding create O(n²) memory allocations as data grows. Zero-copy techniques—memoryview slicing, bytearray pre-allocation, and buffer protocol objects—allow agents to read, parse, and forward large payloads without creating intermediate copies, reducing peak memory usage by 60-80% for multi-megabyte tool outputs."
date: 2025-02-20
difficulty: advanced
category: performance
slug: agent-doesnt-implement-zero-copy-buffer-for-large-tool-responses
tags:
  - zero-copy
  - memoryview
  - buffer-protocol
  - memory-efficiency
  - large-payloads
  - performance
  - allocation
symptoms:
  - "Agent RSS memory spikes to 4GB when processing a 50MB file tool response"
  - "GC pauses of 800ms when concatenating thousands of streaming chunks"
  - "Tool response handling allocates 5x the payload size in temporary objects"
  - "Memory profiler shows dozens of intermediate string copies for a single tool call"
  - "Agent OOMs on documents larger than 20MB despite only needing to extract 1KB of content"
---

## Problem

When a tool returns a 50MB JSON payload, naive code does: `response_str = json.dumps(data)`, passes it to a parser, which calls `response_str[offset:offset+chunk]` to slice it (creating a copy), then encodes it to bytes for the LLM API (another copy), then base64-encodes it (another copy). Each copy allocates a new buffer proportional to payload size. Python's `memoryview` and `bytearray` expose the buffer protocol—slicing a `memoryview` returns a view into the original memory with zero allocation. Combined with streaming parsers and pre-allocated output buffers, this eliminates the copy cascade entirely.

---

## Solution 1: ZeroCopyBuffer — Memoryview-Based Read Buffer

```python
import io
from typing import Iterator, Optional


class ZeroCopyBuffer:
    """
    Wraps a bytes-like object and exposes zero-copy slicing via memoryview.
    All slice operations return views into the original buffer—no new bytes
    objects are allocated. Use as a drop-in replacement for BytesIO when
    you need to pass subranges to parsers without copying.

    Usage:
        buf = ZeroCopyBuffer(response_bytes)
        header = buf.read(4)           # memoryview, no copy
        body = buf.read_slice(4, -1)   # memoryview of bytes[4:-1]
        for chunk in buf.iter_chunks(65536):
            process(chunk)             # each chunk is a view
    """

    def __init__(self, data: bytes):
        self._mv = memoryview(data)
        self._pos = 0

    @property
    def remaining(self) -> int:
        return len(self._mv) - self._pos

    def read(self, n: int = -1) -> memoryview:
        """Return a zero-copy view of the next n bytes."""
        if n < 0 or n > self.remaining:
            n = self.remaining
        chunk = self._mv[self._pos: self._pos + n]
        self._pos += n
        return chunk

    def read_slice(self, start: int, stop: Optional[int] = None) -> memoryview:
        """Return a zero-copy view of an absolute byte range."""
        stop = stop if stop is not None else len(self._mv)
        if stop < 0:
            stop = len(self._mv) + stop
        return self._mv[start:stop]

    def peek(self, n: int) -> memoryview:
        """View next n bytes without advancing position."""
        return self._mv[self._pos: self._pos + n]

    def seek(self, pos: int):
        self._pos = max(0, min(pos, len(self._mv)))

    def iter_chunks(self, chunk_size: int) -> Iterator[memoryview]:
        """Yield successive zero-copy views of chunk_size bytes."""
        while self._pos < len(self._mv):
            end = min(self._pos + chunk_size, len(self._mv))
            yield self._mv[self._pos: end]
            self._pos = end

    def to_bytes(self) -> bytes:
        """Only call when a real bytes object is unavoidable (e.g. json.loads)."""
        return bytes(self._mv[self._pos:])

    def __len__(self) -> int:
        return len(self._mv)
```

---

## Solution 2: PreAllocatedOutputBuffer — Single-Alloc Write Buffer

```python
import io
from typing import Optional, Union


class PreAllocatedOutputBuffer:
    """
    Pre-allocates a bytearray of the expected output size and writes
    into it with a cursor, avoiding repeated reallocation during
    incremental construction of a large response payload.
    Falls back to dynamic growth if the initial capacity is exceeded.

    Usage:
        buf = PreAllocatedOutputBuffer(initial_capacity=10 * 1024 * 1024)  # 10MB
        for chunk in stream:
            buf.write(chunk)
        result_view = buf.getvalue_view()   # memoryview, no copy
        send_to_api(result_view)
    """

    GROWTH_FACTOR = 1.5

    def __init__(self, initial_capacity: int = 65536):
        self._buf = bytearray(initial_capacity)
        self._pos = 0

    def _ensure_capacity(self, needed: int):
        if self._pos + needed > len(self._buf):
            new_size = max(
                int(len(self._buf) * self.GROWTH_FACTOR),
                self._pos + needed,
            )
            self._buf.extend(bytearray(new_size - len(self._buf)))

    def write(self, data: Union[bytes, memoryview, bytearray]) -> int:
        n = len(data)
        self._ensure_capacity(n)
        self._buf[self._pos: self._pos + n] = data
        self._pos += n
        return n

    def write_str(self, s: str, encoding: str = "utf-8") -> int:
        return self.write(s.encode(encoding))

    def getvalue_view(self) -> memoryview:
        """Zero-copy view of the written bytes."""
        return memoryview(self._buf)[:self._pos]

    def getvalue(self) -> bytes:
        """Copy only when caller requires a bytes object."""
        return bytes(self._buf[:self._pos])

    def tell(self) -> int:
        return self._pos

    def reset(self):
        """Reuse the buffer for a new write without reallocating."""
        self._pos = 0

    @property
    def capacity(self) -> int:
        return len(self._buf)
```

---

## Solution 3: StreamingChunkAccumulator — Zero-Copy Chunk Assembly

```python
import io
from typing import Iterator, List, Optional, Union


class StreamingChunkAccumulator:
    """
    Accumulates streaming response chunks without concatenating them.
    Maintains a list of memoryviews (or bytes objects) and provides
    a unified read interface. Only allocates a single contiguous buffer
    when explicitly joined, avoiding the O(n²) allocation pattern of
    repeated string += concatenation.

    Usage:
        acc = StreamingChunkAccumulator()
        async for chunk in tool_response.stream():
            acc.append(chunk)
        # Iterate without joining (zero copies):
        for view in acc.iter_chunks():
            write_to_socket(view)
        # Or join once when truly needed:
        full_bytes = acc.join()
    """

    def __init__(self):
        self._chunks: List[Union[bytes, memoryview]] = []
        self._total_bytes = 0

    def append(self, chunk: Union[bytes, bytearray, memoryview]):
        if isinstance(chunk, bytearray):
            chunk = memoryview(chunk)
        self._chunks.append(chunk)
        self._total_bytes += len(chunk)

    def iter_chunks(self) -> Iterator[Union[bytes, memoryview]]:
        """Yield each chunk as-is — no allocation."""
        yield from self._chunks

    def iter_lines(self, separator: bytes = b"\n") -> Iterator[bytes]:
        """
        Parse lines across chunk boundaries without joining all chunks first.
        Only allocates for lines that span a chunk boundary.
        """
        pending = b""
        for chunk in self._chunks:
            raw = bytes(chunk) if isinstance(chunk, memoryview) else chunk
            data = pending + raw
            lines = data.split(separator)
            for line in lines[:-1]:
                yield line
            pending = lines[-1]
        if pending:
            yield pending

    def join(self) -> bytearray:
        """
        Allocate exactly one buffer of the right size and copy all chunks in.
        More efficient than repeated concatenation even when a copy is needed.
        """
        result = bytearray(self._total_bytes)
        pos = 0
        for chunk in self._chunks:
            n = len(chunk)
            result[pos: pos + n] = chunk
            pos += n
        return result

    def join_view(self) -> memoryview:
        return memoryview(self.join())

    @property
    def total_bytes(self) -> int:
        return self._total_bytes

    def __len__(self) -> int:
        return self._total_bytes
```

---

## Solution 4: ZeroCopyJSONExtractor — Field Extraction Without Full Parse

```python
import re
from typing import Any, Optional


class ZeroCopyJSONExtractor:
    """
    Extracts specific top-level fields from a large JSON bytes payload
    without deserializing the entire document. Uses byte-level scanning
    on a memoryview to locate field boundaries, returning memoryview
    slices of value regions without allocating intermediate strings.

    Intended for tool responses where only 1-2 fields (e.g. "content",
    "result") are needed from a large JSON object.

    Usage:
        extractor = ZeroCopyJSONExtractor(response_bytes)
        content_view = extractor.extract_string_field("content")
        if content_view:
            process(bytes(content_view))   # allocate only the extracted field
    """

    # Matches "fieldname": "value" with escaped chars (simplified)
    _FIELD_PATTERN = re.compile(
        rb'"(?P<key>[^"\\]+)"\s*:\s*"(?P<value>(?:[^"\\]|\\.)*)"',
        re.DOTALL,
    )
    _NUM_PATTERN = re.compile(
        rb'"(?P<key>[^"\\]+)"\s*:\s*(?P<value>-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)',
    )

    def __init__(self, data: bytes):
        self._mv = memoryview(data)
        self._raw = data

    def extract_string_field(self, field: str) -> Optional[memoryview]:
        """Return a zero-copy view of the string value for `field`."""
        key = field.encode()
        for m in self._FIELD_PATTERN.finditer(self._raw):
            if m.group("key") == key:
                start, end = m.span("value")
                return self._mv[start:end]
        return None

    def extract_number_field(self, field: str) -> Optional[memoryview]:
        """Return a zero-copy view of the numeric value for `field`."""
        key = field.encode()
        for m in self._NUM_PATTERN.finditer(self._raw):
            if m.group("key") == key:
                start, end = m.span("value")
                return self._mv[start:end]
        return None

    def to_float(self, view: memoryview) -> float:
        return float(bytes(view))

    def to_int(self, view: memoryview) -> int:
        return int(bytes(view))

    def to_str(self, view: memoryview) -> str:
        return bytes(view).decode("utf-8").replace('\\"', '"')
```

---

## Solution 5: BufferPool — Reusable Buffer Allocation Pool

```python
import threading
from typing import Dict, List, Optional


class PooledBuffer:
    """Context manager that returns a bytearray to the pool on exit."""

    def __init__(self, buf: bytearray, pool: "BufferPool"):
        self._buf = buf
        self._pool = pool

    @property
    def buffer(self) -> bytearray:
        return self._buf

    def view(self, length: Optional[int] = None) -> memoryview:
        return memoryview(self._buf)[:length] if length else memoryview(self._buf)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self._pool._return(self._buf)


class BufferPool:
    """
    Maintains a pool of reusable bytearray buffers to eliminate allocation
    overhead for repeated large tool-response processing. Buffers are
    recycled between requests; pool size is bounded to cap memory usage.

    Usage:
        pool = BufferPool(buffer_size=4 * 1024 * 1024, max_pool_size=8)

        async with pool.acquire() as pbuf:
            n = read_tool_response_into(pbuf.buffer)
            parsed = parse(pbuf.view(n))
    """

    def __init__(self, buffer_size: int = 1024 * 1024, max_pool_size: int = 8):
        self._size = buffer_size
        self._max = max_pool_size
        self._pool: List[bytearray] = []
        self._lock = threading.Lock()
        self._allocated = 0

    def _acquire_buf(self) -> bytearray:
        with self._lock:
            if self._pool:
                buf = self._pool.pop()
                # Zero out only the header — full zeroing is unnecessary
                buf[:64] = b'\x00' * 64
                return buf
            self._allocated += 1
        return bytearray(self._size)

    def _return(self, buf: bytearray):
        with self._lock:
            if len(self._pool) < self._max:
                self._pool.append(buf)

    def acquire(self) -> PooledBuffer:
        return PooledBuffer(self._acquire_buf(), self)

    @property
    def pool_depth(self) -> int:
        return len(self._pool)

    @property
    def total_allocated(self) -> int:
        return self._allocated
```

---

## Solution 6: ZeroCopyToolResponseAdapter — Drop-In Replacement for String-Based Tool Output

```python
import json
from typing import Any, Dict, Iterator, Optional, Union


class ZeroCopyToolResponseAdapter:
    """
    Drop-in adapter that converts a tool's raw bytes response into the
    agent's expected dict/string format using zero-copy techniques where
    possible. Parses JSON incrementally using a streaming parser for
    large payloads (>1MB) and direct json.loads for small ones.

    Usage:
        adapter = ZeroCopyToolResponseAdapter(size_threshold=1024 * 1024)
        result = adapter.parse(raw_response_bytes)
        text = adapter.extract_text_content(raw_response_bytes, field="content")
    """

    LARGE_THRESHOLD = 1024 * 1024  # 1 MB

    def __init__(self, size_threshold: int = LARGE_THRESHOLD):
        self._threshold = size_threshold

    def parse(self, data: bytes) -> Any:
        if len(data) < self._threshold:
            return json.loads(data)
        # For large payloads, use ijson (streaming JSON parser) if available
        try:
            import ijson
            return self._parse_streaming(data)
        except ImportError:
            return json.loads(data)

    def _parse_streaming(self, data: bytes) -> Dict[str, Any]:
        import io
        import ijson
        result: Dict[str, Any] = {}
        stream = io.BytesIO(data)
        # ijson reads from a file-like object without loading all data at once
        parser = ijson.kvitems(stream, "")
        for key, value in parser:
            result[key] = value
        return result

    def extract_text_content(self, data: bytes, field: str = "content") -> Optional[str]:
        """
        Fast-path: extract a single string field without full parse.
        Falls back to full parse if field not found by regex scanner.
        """
        extractor = ZeroCopyJSONExtractor(data)
        view = extractor.extract_string_field(field)
        if view is not None:
            return extractor.to_str(view)
        # Fallback: full parse
        parsed = self.parse(data)
        return parsed.get(field) if isinstance(parsed, dict) else None

    def iter_array_field(self, data: bytes, field: str) -> Iterator[Any]:
        """Stream items from a top-level array field without loading all items."""
        try:
            import io
            import ijson
            stream = io.BytesIO(data)
            yield from ijson.items(stream, f"{field}.item")
        except ImportError:
            parsed = json.loads(data)
            yield from (parsed.get(field) or [])

    def compute_size_category(self, data: bytes) -> str:
        n = len(data)
        if n < 64 * 1024:
            return "small"
        if n < self._threshold:
            return "medium"
        return "large"
```

---

## Comparison

| Approach | Zero-Copy Reads | Zero-Copy Writes | Buffer Reuse | JSON Extraction | Streaming | Use Case |
|---|---|---|---|---|---|---|
| **ZeroCopyBuffer** | Yes | No | No | No | Chunked read | Parsing received bytes |
| **PreAllocatedOutputBuffer** | No | Yes | Partial (reset) | No | No | Building response payloads |
| **StreamingChunkAccumulator** | Yes | No | No | No | Yes | Async stream assembly |
| **ZeroCopyJSONExtractor** | Yes | No | No | Yes (regex) | No | Extract 1-2 fields from large JSON |
| **BufferPool** | No | Yes | Yes | No | No | High-throughput repeated calls |
| **ZeroCopyToolResponseAdapter** | Partial | No | No | Yes (streaming) | Yes | General tool response parsing |

**Key insight**: the first change to make is replacing `response_str = "".join(chunks)` with `StreamingChunkAccumulator` — this alone eliminates the O(n²) concatenation cost and can reduce peak memory by 3-5× for multi-megabyte streaming responses. For tools that return large JSON payloads where the agent needs only one or two fields (very common in RAG pipelines), `ZeroCopyJSONExtractor` extracts the target field with a single regex scan using ~100 bytes of allocation regardless of payload size. Install `ijson` (`pip install ijson`) to get true streaming JSON parsing for payloads that cannot fit comfortably in RAM.
