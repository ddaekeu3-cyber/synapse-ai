---
title: "Agent Doesn't Implement Response Compression for Large Payloads"
description: "AI agents return large JSON or text payloads uncompressed, wasting bandwidth and inflating latency for downstream consumers."
category: performance
difficulty: intermediate
tags: [compression, gzip, brotli, bandwidth, latency, fastapi, streaming]
---

# Agent Doesn't Implement Response Compression for Large Payloads

## Problem

AI agents that return tool results, conversation history, or embedding arrays over HTTP send raw bytes with no compression. A 2 MB JSON response compresses to ~120 KB with gzip — a 16× reduction. Without compression, downstream services pay in bandwidth costs and added round-trip latency, especially over mobile or cross-region links.

## Solution 1: GZip Middleware for FastAPI (Zero-Code Change)

Add GZip middleware to your ASGI app; all responses above the minimum size are compressed automatically.

```python
from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware

app = FastAPI()

# Compress any response body ≥ 1 KB
app.add_middleware(GZipMiddleware, minimum_size=1024)

@app.post("/agent/run")
async def run_agent(prompt: str) -> dict:
    result = await execute_agent(prompt)
    # Response is gzip-compressed automatically when client sends Accept-Encoding: gzip
    return {"result": result, "tokens_used": 1234}
```

**When to use**: Existing FastAPI apps. One-line fix, zero handler changes needed.

---

## Solution 2: Manual GZip + Content-Encoding for Raw ASGI / aiohttp

Compress manually when middleware is not available or you need fine-grained control.

```python
import gzip
import json
from aiohttp import web

async def agent_response_handler(request: web.Request) -> web.Response:
    accepts_gzip = "gzip" in request.headers.get("Accept-Encoding", "")

    payload = await build_agent_payload(request)
    body = json.dumps(payload).encode()

    if accepts_gzip and len(body) > 1024:
        compressed = gzip.compress(body, compresslevel=6)
        return web.Response(
            body=compressed,
            content_type="application/json",
            headers={
                "Content-Encoding": "gzip",
                "Vary": "Accept-Encoding",
                "X-Uncompressed-Length": str(len(body)),
            },
        )

    return web.Response(body=body, content_type="application/json")
```

**When to use**: aiohttp, raw ASGI, or when you need per-route compression thresholds.

---

## Solution 3: Brotli Compression with gzip Fallback

Brotli achieves 20–26% better compression than gzip. Prefer brotli, fall back to gzip.

```python
import brotli
import gzip
import json
from fastapi import Request, Response

def compress_payload(body: bytes, accept_encoding: str) -> tuple[bytes, str | None]:
    """Return (compressed_bytes, encoding_name) or (body, None)."""
    if len(body) < 1024:
        return body, None
    if "br" in accept_encoding:
        return brotli.compress(body, quality=4), "br"
    if "gzip" in accept_encoding:
        return gzip.compress(body, compresslevel=6), "gzip"
    return body, None

async def compressed_json_response(request: Request, data: dict) -> Response:
    body = json.dumps(data, separators=(",", ":")).encode()
    accept = request.headers.get("Accept-Encoding", "")
    compressed, encoding = compress_payload(body, accept)

    headers = {"Content-Type": "application/json", "Vary": "Accept-Encoding"}
    if encoding:
        headers["Content-Encoding"] = encoding

    return Response(content=compressed, headers=headers)
```

**When to use**: Clients that support brotli (all modern browsers, recent HTTP libs). Install `brotli` via pip.

---

## Solution 4: Streaming Compressed Response for Large Tool Results

Stream compressed chunks to achieve low time-to-first-byte even for multi-MB payloads.

```python
import gzip
import json
import asyncio
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

app = FastAPI()

async def stream_gzip_chunks(data_generator, chunk_size: int = 65536):
    """Compress data chunks with a shared GzipFile and yield as they're ready."""
    import io
    buf = io.BytesIO()
    gz = gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6)
    try:
        async for chunk in data_generator:
            raw = json.dumps(chunk).encode() + b"\n"
            gz.write(raw)
            gz.flush()
            compressed = buf.getvalue()
            if compressed:
                buf.seek(0)
                buf.truncate()
                yield compressed
    finally:
        gz.close()
        remainder = buf.getvalue()
        if remainder:
            yield remainder

@app.post("/agent/stream-results")
async def stream_results(request: Request):
    accepts_gzip = "gzip" in request.headers.get("Accept-Encoding", "")
    items = large_tool_result_generator()  # async generator

    if not accepts_gzip:
        return StreamingResponse(
            (json.dumps(item).encode() + b"\n" async for item in items),
            media_type="application/x-ndjson",
        )

    return StreamingResponse(
        stream_gzip_chunks(items),
        media_type="application/x-ndjson",
        headers={"Content-Encoding": "gzip", "Vary": "Accept-Encoding"},
    )

async def large_tool_result_generator():
    for i in range(1000):
        yield {"index": i, "data": "x" * 500}
        await asyncio.sleep(0)
```

**When to use**: Streaming endpoints where you can't buffer the full response in memory.

---

## Solution 5: Compression-Aware HTTP Client with Decompression

Ensure the agent's outbound HTTP client always requests and transparently decompresses compressed upstream responses.

```python
import aiohttp
import asyncio

async def compressed_api_client():
    """Client that requests gzip/brotli and decompresses automatically."""
    connector = aiohttp.TCPConnector(limit=100)
    # aiohttp auto-decompresses when auto_decompress=True (default)
    async with aiohttp.ClientSession(
        connector=connector,
        headers={"Accept-Encoding": "br, gzip, deflate"},
    ) as session:
        # Fetch large tool-result payload
        async with session.get("https://api.example.com/large-dataset") as resp:
            # aiohttp transparently decompresses
            data = await resp.json()
            print(f"Received {len(str(data))} chars (decompressed)")
            return data

# For httpx (also used widely in agent frameworks)
import httpx

async def httpx_compressed_client():
    async with httpx.AsyncClient(
        headers={"Accept-Encoding": "br, gzip"},
        http2=True,  # HTTP/2 compresses headers too
    ) as client:
        resp = await client.get("https://api.example.com/agent-results")
        # httpx decompresses automatically
        return resp.json()
```

**When to use**: Agent calling external APIs or microservices that support compression.

---

## Solution 6: Selective Field Compression + Compression Ratio Logging

Compress only large fields (e.g., `content`, `embeddings`) within a JSON envelope, and log compression ratios for monitoring.

```python
import gzip
import base64
import json
import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

LARGE_FIELD_THRESHOLD = 512  # bytes

def compress_large_fields(payload: dict, threshold: int = LARGE_FIELD_THRESHOLD) -> dict:
    """Recursively compress string/list fields that exceed threshold."""
    result = {}
    for key, value in payload.items():
        if isinstance(value, dict):
            result[key] = compress_large_fields(value, threshold)
        elif isinstance(value, (str, list)):
            raw = json.dumps(value).encode() if isinstance(value, list) else value.encode()
            if len(raw) > threshold:
                compressed = gzip.compress(raw, compresslevel=6)
                ratio = len(raw) / len(compressed)
                logger.info(
                    "field_compressed",
                    extra={
                        "field": key,
                        "original_bytes": len(raw),
                        "compressed_bytes": len(compressed),
                        "ratio": round(ratio, 2),
                    },
                )
                result[key] = {
                    "__compressed": True,
                    "encoding": "gzip+b64",
                    "data": base64.b64encode(compressed).decode(),
                }
            else:
                result[key] = value
        else:
            result[key] = value
    return result

def decompress_fields(payload: dict) -> dict:
    """Reverse of compress_large_fields."""
    result = {}
    for key, value in payload.items():
        if isinstance(value, dict) and value.get("__compressed"):
            raw = gzip.decompress(base64.b64decode(value["data"]))
            result[key] = json.loads(raw) if raw.startswith(b"[") else raw.decode()
        elif isinstance(value, dict):
            result[key] = decompress_fields(value)
        else:
            result[key] = value
    return result

# Usage
response = {
    "summary": "short text",
    "full_content": "x" * 5000,
    "embeddings": [0.1, 0.2] * 768,
}
compressed_response = compress_large_fields(response)
# Only large fields are compressed; small fields pass through unchanged
restored = decompress_fields(compressed_response)
```

**When to use**: When you need field-level control over compression (e.g., always send summary uncompressed for quick display, compress bulk data).

---

## Comparison

| Solution | Compression | Streaming | Client Change | Granularity | Best For |
|---|---|---|---|---|---|
| GZip Middleware | gzip | No | None | Whole response | FastAPI apps, quick wins |
| Manual gzip/aiohttp | gzip | No | None | Per route | aiohttp, custom thresholds |
| Brotli + gzip fallback | br/gzip | No | None | Per route | Modern clients, best ratio |
| Streaming gzip | gzip | Yes | None | Chunk-level | Large streaming payloads |
| Client-side accept | br/gzip | N/A | Client | Upstream calls | Fetching compressed APIs |
| Field-level compression | gzip | No | Decoder needed | Per field | Mixed small+large fields |

**Rule of thumb**: Always enable GZip middleware as baseline. Add brotli for browser-facing APIs. Use field-level compression when clients can't negotiate encoding.
