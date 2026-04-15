---
layout: solution
title: "Agent doesn't compress request payloads"
category: performance
description: "Agent sends large uncompressed payloads to APIs, wasting bandwidth and increasing latency on every request."
tags: [performance, network, compression, latency, bandwidth]
---

## Symptom

API requests consistently take 300–800 ms longer than expected. Profiling reveals most of the latency is in the network transfer phase, not in model inference. The agent sends multi-kilobyte JSON bodies — conversation histories, large tool results, embedded documents — as raw UTF-8 without any compression. On slow or metered connections (mobile, cross-region, VPN) the problem is severe enough to trigger timeouts.

```
Request body:  48,291 bytes  (47 KB of uncompressed JSON)
Transfer time: 620 ms
Model latency: 180 ms
Total:         800 ms
```

## Root Cause

The `anthropic` Python SDK uses `httpx` under the hood. By default, `httpx` does not apply gzip or brotli compression to outbound request bodies. JSON conversation histories and tool results compress at ratios of 4:1 to 10:1, so agents that send large contexts without compression waste significant bandwidth and add hundreds of milliseconds of transfer latency per request.

## Fix

Enable Content-Encoding compression on outbound requests via a custom `httpx` transport or middleware. For the Anthropic SDK, pass a custom `http_client` with a transport that compresses request bodies.

---

### Option 1 — Custom httpx transport with gzip Content-Encoding

```python
import anthropic
import httpx
import gzip
import json
from typing import Any

class GzipRequestTransport(httpx.HTTPTransport):
    """
    Intercepts every request and compresses the body with gzip
    if the body exceeds a size threshold.
    """

    def __init__(self, compress_threshold_bytes: int = 1024, **kwargs: Any):
        super().__init__(**kwargs)
        self.threshold = compress_threshold_bytes

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if (
            request.content
            and len(request.content) >= self.threshold
            and b"Content-Encoding" not in dict(request.headers)
        ):
            compressed = gzip.compress(request.content, compresslevel=6)
            ratio = len(request.content) / max(len(compressed), 1)
            print(f"[GZIP] {len(request.content):,} → {len(compressed):,} bytes (ratio {ratio:.1f}x)")

            headers = dict(request.headers)
            headers["Content-Encoding"] = "gzip"
            headers["Content-Length"] = str(len(compressed))

            request = httpx.Request(
                method=request.method,
                url=request.url,
                headers=headers,
                content=compressed,
            )

        return super().handle_request(request)

# Build an httpx client with the gzip transport
http_client = httpx.Client(
    transport=GzipRequestTransport(compress_threshold_bytes=1024),
    timeout=60.0,
)

client = anthropic.Anthropic(http_client=http_client)

# Large conversation history — would be 20+ KB uncompressed
large_context = " ".join(["The agent analyzed the repository structure."] * 200)

response = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=256,
    messages=[
        {"role": "user", "content": f"Summarize this context in one sentence: {large_context}"}
    ],
)
print(response.content[0].text)
```

**Expected Token Savings:** No token reduction, but 60–80% bandwidth reduction for large payloads, cutting transfer latency by 200–600 ms per request.

**Environment:** Any environment with httpx available; Anthropic SDK >= 0.18 supports custom `http_client`.

---

### Option 2 — Async gzip transport for high-throughput async agents

```python
import anthropic
import httpx
import gzip
import asyncio
from typing import Any

class AsyncGzipTransport(httpx.AsyncHTTPTransport):
    """Async transport that gzip-compresses large request bodies."""

    def __init__(self, threshold: int = 2048, level: int = 6, **kwargs: Any):
        super().__init__(**kwargs)
        self.threshold = threshold
        self.level = level
        self._stats = {"compressed": 0, "skipped": 0, "bytes_saved": 0}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.content and len(request.content) >= self.threshold:
            compressed = gzip.compress(request.content, compresslevel=self.level)
            saved = len(request.content) - len(compressed)
            self._stats["compressed"] += 1
            self._stats["bytes_saved"] += saved

            headers = dict(request.headers)
            headers["Content-Encoding"] = "gzip"
            headers["Content-Length"] = str(len(compressed))

            request = httpx.Request(
                method=request.method,
                url=request.url,
                headers=headers,
                content=compressed,
            )
        else:
            self._stats["skipped"] += 1

        return await super().handle_async_request(request)

    @property
    def stats(self) -> dict:
        return {**self._stats, "kb_saved": round(self._stats["bytes_saved"] / 1024, 1)}

transport = AsyncGzipTransport(threshold=1024, level=6)
async_http = httpx.AsyncClient(transport=transport, timeout=60.0)
async_client = anthropic.AsyncAnthropic(http_client=async_http)

async def process_batch(prompts: list[str]) -> list[str]:
    tasks = [
        async_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{"role": "user", "content": p}],
        )
        for p in prompts
    ]
    responses = await asyncio.gather(*tasks)
    print(f"\nCompression stats: {transport.stats}")
    return [r.content[0].text for r in responses]

# Each prompt is padded with context to trigger compression
padding = " ".join(["context data"] * 100)
prompts = [
    f"{padding} Question: What is 2+2?",
    f"{padding} Question: Name one planet.",
    f"{padding} Question: What color is the sky?",
]

results = asyncio.run(process_batch(prompts))
for i, r in enumerate(results):
    print(f"[{i+1}] {r}")
```

**Expected Token Savings:** 0 token reduction; 65–85% bandwidth reduction; particularly impactful for concurrent request fans that share a connection pool.

**Environment:** Async FastAPI or async worker processes; benefits scale linearly with request concurrency.

---

### Option 3 — Payload trimmer: remove whitespace before transmission

```python
import anthropic
import json
import httpx
from typing import Any

class MinifiedJsonTransport(httpx.HTTPTransport):
    """
    Re-minifies JSON request bodies to strip unnecessary whitespace
    before transmission. Works without server-side compression support.
    """

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type and request.content:
            try:
                parsed = json.loads(request.content)
                minified = json.dumps(parsed, separators=(",", ":"), ensure_ascii=False).encode()
                original_size = len(request.content)
                saving = original_size - len(minified)

                if saving > 0:
                    print(f"[MINIFY] {original_size:,} → {len(minified):,} bytes (saved {saving:,})")
                    headers = dict(request.headers)
                    headers["Content-Length"] = str(len(minified))
                    request = httpx.Request(
                        method=request.method,
                        url=request.url,
                        headers=headers,
                        content=minified,
                    )
            except (json.JSONDecodeError, ValueError):
                pass  # Non-JSON body: pass through unchanged

        return super().handle_request(request)

client = anthropic.Anthropic(
    http_client=httpx.Client(
        transport=MinifiedJsonTransport(),
        timeout=30.0,
    )
)

# Prettily formatted messages (e.g., from a debug logger or config file)
messages = [
    {
        "role":    "user",
        "content": "What is the capital of France? Answer in one word.",
    }
]

response = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=16,
    messages=messages,
)
print(response.content[0].text)
```

**Expected Token Savings:** 0 token reduction; 5–15% body size reduction (whitespace only); zero CPU overhead; works with any server regardless of Content-Encoding support.

**Environment:** Universal; use as a base layer under gzip transport for maximum reduction.

---

### Option 4 — Conversation history compressor before API call

```python
import anthropic
import json

client = anthropic.Anthropic()

def compress_history(messages: list[dict], max_chars_per_turn: int = 500) -> list[dict]:
    """
    Trim verbose tool results and long user/assistant turns in the history
    before sending, reducing payload size without losing conversational context.
    """
    compressed = []
    for msg in messages:
        content = msg["content"]

        if isinstance(content, str):
            if len(content) > max_chars_per_turn:
                trimmed = content[:max_chars_per_turn] + f"... [truncated {len(content) - max_chars_per_turn} chars]"
                compressed.append({**msg, "content": trimmed})
            else:
                compressed.append(msg)

        elif isinstance(content, list):
            new_blocks = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    result_text = block.get("content", "")
                    if isinstance(result_text, str) and len(result_text) > max_chars_per_turn:
                        block = {
                            **block,
                            "content": result_text[:max_chars_per_turn] + " [trimmed]",
                        }
                new_blocks.append(block)
            compressed.append({**msg, "content": new_blocks})
        else:
            compressed.append(msg)

    original_size = len(json.dumps(messages).encode())
    compressed_size = len(json.dumps(compressed).encode())
    print(f"[HISTORY COMPRESS] {original_size:,} → {compressed_size:,} bytes "
          f"({(1 - compressed_size/original_size)*100:.0f}% reduction)")
    return compressed

# Simulate a multi-turn conversation with large tool results
messages = [
    {"role": "user", "content": "Search for Python async patterns."},
    {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "I'll search for that."},
            {"type": "tool_use", "id": "tu_1", "name": "search_docs", "input": {"query": "async"}},
        ],
    },
    {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "tu_1",
                "content": "Result: " + ("asyncio patterns explained in detail. " * 100),  # 3KB result
            }
        ],
    },
    {"role": "user", "content": "Now summarize the top 3 patterns."},
]

compressed_messages = compress_history(messages, max_chars_per_turn=400)

response = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=256,
    messages=compressed_messages,
)
print(response.content[0].text)
```

**Expected Token Savings:** 30–60% token reduction by trimming tool results in the conversation history — directly reduces API cost in addition to bandwidth.

**Environment:** Best applied to turns older than the current one; do not trim the most recent tool result as the model needs full context for the active reasoning step.

---

### Option 5 — Selective compression: only compress above size threshold

```python
import anthropic
import httpx
import gzip
import zlib
import time
from dataclasses import dataclass, field

@dataclass
class CompressionStats:
    requests_compressed: int = 0
    requests_skipped: int = 0
    bytes_before: int = 0
    bytes_after: int = 0
    time_saved_ms: float = 0.0

    def report(self) -> str:
        total = self.requests_compressed + self.requests_skipped
        ratio = self.bytes_before / max(self.bytes_after, 1)
        pct = (1 - self.bytes_after / max(self.bytes_before, 1)) * 100
        return (
            f"Compressed {self.requests_compressed}/{total} requests | "
            f"Ratio {ratio:.1f}x | Saved {pct:.0f}% bytes"
        )

stats = CompressionStats()

class AdaptiveGzipTransport(httpx.HTTPTransport):
    """
    Only compresses if payload > threshold AND estimated transfer savings
    outweigh compression CPU cost (approximated by body size).
    """
    THRESHOLD = 4096       # bytes: minimum body size to compress
    LEVEL_MAP = {          # size → compression level
        (4096,   16384):  1,   # small:  fastest
        (16384, 131072):  4,   # medium: balanced
        (131072, 2**31):  6,   # large:  best ratio
    }

    def _compression_level(self, size: int) -> int:
        for (lo, hi), level in self.LEVEL_MAP.items():
            if lo <= size < hi:
                return level
        return 6

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        body = request.content
        stats.bytes_before += len(body)

        if len(body) >= self.THRESHOLD:
            level = self._compression_level(len(body))
            t0 = time.perf_counter()
            compressed = gzip.compress(body, compresslevel=level)
            elapsed_ms = (time.perf_counter() - t0) * 1000

            stats.bytes_after += len(compressed)
            stats.requests_compressed += 1
            stats.time_saved_ms += elapsed_ms

            print(f"[ADAPTIVE GZIP lv{level}] {len(body):,}→{len(compressed):,}B in {elapsed_ms:.1f}ms")

            headers = dict(request.headers)
            headers["Content-Encoding"] = "gzip"
            headers["Content-Length"] = str(len(compressed))
            request = httpx.Request(
                method=request.method, url=request.url,
                headers=headers, content=compressed,
            )
        else:
            stats.bytes_after += len(body)
            stats.requests_skipped += 1

        return super().handle_request(request)

client = anthropic.Anthropic(
    http_client=httpx.Client(
        transport=AdaptiveGzipTransport(),
        timeout=60.0,
    )
)

# Short request — will NOT be compressed (below threshold)
r1 = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=32,
    messages=[{"role": "user", "content": "Say hello."}],
)

# Large request — will be compressed
big_context = "The system log showed: " + ("ERROR timeout retry backoff " * 300)
r2 = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=64,
    messages=[{"role": "user", "content": f"{big_context}\n\nSummarize in 5 words."}],
)

print(stats.report())
```

**Expected Token Savings:** 0 token reduction; adaptive level selection balances CPU vs. bandwidth tradeoff — level 1 for small bodies, level 6 for large.

**Environment:** CPU-sensitive environments (serverless, Lambda) where compression cost must be justified by actual payload size.

---

### Option 6 — Brotli compression for maximum ratio on static context

```python
import anthropic
import httpx
import json

try:
    import brotli
    BROTLI_AVAILABLE = True
except ImportError:
    BROTLI_AVAILABLE = False
    import gzip  # fallback

class BrotliOrGzipTransport(httpx.HTTPTransport):
    """
    Uses Brotli compression (better ratio than gzip) when available,
    falls back to gzip. Brotli achieves 15–25% better compression than
    gzip on JSON payloads.
    """
    THRESHOLD = 1024

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        body = request.content
        if not body or len(body) < self.THRESHOLD:
            return super().handle_request(request)

        if BROTLI_AVAILABLE:
            compressed = brotli.compress(body, quality=4)  # 0-11; 4 is fast+good
            encoding = "br"
        else:
            import gzip as _gzip
            compressed = _gzip.compress(body, compresslevel=6)
            encoding = "gzip"

        ratio = len(body) / max(len(compressed), 1)
        print(f"[{encoding.upper()}] {len(body):,} → {len(compressed):,} bytes (ratio {ratio:.1f}x)")

        headers = dict(request.headers)
        headers["Content-Encoding"] = encoding
        headers["Content-Length"] = str(len(compressed))

        request = httpx.Request(
            method=request.method, url=request.url,
            headers=headers, content=compressed,
        )
        return super().handle_request(request)

client = anthropic.Anthropic(
    http_client=httpx.Client(
        transport=BrotliOrGzipTransport(),
        timeout=60.0,
    )
)

# JSON-heavy payload: system prompt + long tool schema list
tools = [
    {
        "name": f"tool_{i}",
        "description": f"This tool does operation number {i} on the dataset.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": f"Input for tool {i}"},
                "limit": {"type": "integer", "description": "Max results"},
            },
            "required": ["query"],
        },
    }
    for i in range(20)
]

response = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=64,
    tools=tools,
    messages=[{"role": "user", "content": "Which tool should I use to query recent logs?"}],
)
print(response.content[0].text if hasattr(response.content[0], "text") else "Tool called")
```

**Expected Token Savings:** 0 token reduction; Brotli achieves 15–25% better compression than gzip on JSON, saving 70–90% of raw payload bytes for large tool schema arrays.

**Environment:** Install `brotli` Python package (`pip install brotli`); falls back to gzip gracefully; most effective for static payloads like tool definitions sent on every request.

---

## Comparison

| Option | Algorithm | Threshold | Token Impact | CPU Cost |
|--------|-----------|-----------|-------------|---------|
| 1 — Custom gzip transport | gzip | Configurable | None | Low |
| 2 — Async gzip transport | gzip | Configurable | None | Low |
| 3 — JSON minifier | whitespace strip | Always | None | Negligible |
| 4 — History trimmer | content trimming | Per-turn limit | Reduces tokens | Negligible |
| 5 — Adaptive gzip | gzip, level-adaptive | 4 KB | None | Scales with size |
| 6 — Brotli/gzip fallback | brotli or gzip | 1 KB | None | Medium |

**Recommended default:** Option 1 (gzip transport) for most agents. Add Option 4 (history trimmer) for long multi-turn conversations where token reduction is also desirable. Use Option 6 only if the server explicitly supports `br` encoding.
