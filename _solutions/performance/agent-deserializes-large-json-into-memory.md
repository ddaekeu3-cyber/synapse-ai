---
layout: solution
title: "Agent Deserializes Entire Large JSON Response Into Memory — OOM Crash"
category: performance
description: "Agent calls an API that returns a 500MB JSON response. Agent does json.loads(response.text) — loads entire document into RAM. Process OOMs and crashes. Or takes 60 seconds to parse before processing a single record."
tags: [performance, json, memory, oom, streaming, ijson, large-response, parsing]
---

# Agent Deserializes Entire Large JSON Response Into Memory — OOM Crash

## Symptom
- `MemoryError` or OOM kill when processing large API responses
- `json.loads()` takes 60+ seconds for a 100MB response
- Agent process memory usage spikes to 5GB for a 500MB JSON file
- Entire response must download before any processing begins
- Container is killed by OOM killer mid-task with no useful error message

## Root Cause
`json.loads()` and `response.json()` parse the entire document into memory before returning. A 500MB JSON file expands to 2-5GB in Python object memory. This works for small responses but fails catastrophically for large ones. The fix is streaming JSON parsing — process records as they arrive without holding the full document in memory.

## Fix

### Option 1: Streaming JSON parsing with ijson

```python
import ijson
import httpx

def stream_json_array(url: str, record_callback) -> int:
    """
    Stream a JSON array from a URL, processing one record at a time.
    Memory usage stays constant regardless of response size.
    """
    count = 0
    with httpx.stream("GET", url, timeout=300) as response:
        response.raise_for_status()
        # ijson parses incrementally from the byte stream
        parser = ijson.items(response.iter_bytes(chunk_size=65536), "item")
        for record in parser:
            record_callback(record)
            count += 1
    return count

# Process 1M records in constant memory:
def process_record(record: dict):
    insert_to_db(record)

count = stream_json_array("https://api.example.com/large-dataset", process_record)
print(f"Processed {count} records")

# Memory: ~5MB constant vs ~5GB peak for json.loads()
```

### Option 2: ijson for large local files

```python
import ijson

def process_large_json_file(
    filepath: str,
    array_prefix: str = "item",
    batch_size: int = 1000
) -> int:
    """
    Process a large JSON file record by record with constant memory.
    array_prefix: JSON path to the array (e.g., "results.item" for {"results": [...]})
    """
    total = 0
    batch = []

    with open(filepath, "rb") as f:
        parser = ijson.items(f, array_prefix)
        for record in parser:
            batch.append(record)
            if len(batch) >= batch_size:
                process_batch(batch)
                total += len(batch)
                batch.clear()
                print(f"Processed {total} records...")

    # Final partial batch
    if batch:
        process_batch(batch)
        total += len(batch)

    return total

# For nested JSON: {"data": {"users": [...]}}
count = process_large_json_file("big_export.json", array_prefix="data.users.item")
```

### Option 3: Request paginated results instead of one giant response

```python
import httpx
import asyncio

async def fetch_all_paginated(
    base_url: str,
    page_size: int = 100,
    max_pages: int = None,
    params: dict = None
) -> list:
    """
    Fetch all results via pagination instead of one massive response.
    Each page is small — process and discard before fetching the next.
    """
    all_results = []
    page = 1
    params = params or {}

    async with httpx.AsyncClient() as client:
        while True:
            response = await client.get(
                base_url,
                params={**params, "page": page, "per_page": page_size},
                timeout=30
            )
            data = response.json()
            results = data.get("results", data.get("items", data.get("data", [])))

            if not results:
                break

            all_results.extend(results)
            print(f"Page {page}: {len(results)} records (total: {len(all_results)})")

            # Check for "no more pages" signals
            if len(results) < page_size:
                break
            if max_pages and page >= max_pages:
                break

            page += 1

    return all_results

# Better: process each page immediately, don't accumulate all_results in memory
async def process_all_paginated(base_url: str, processor, page_size: int = 100):
    page = 1
    async with httpx.AsyncClient() as client:
        while True:
            response = await client.get(
                base_url, params={"page": page, "per_page": page_size}
            )
            data = response.json()
            items = data.get("items", [])
            if not items:
                break

            for item in items:
                await processor(item)  # Process and discard
            page += 1
```

### Option 4: Stream HTTP response and parse chunks

```python
import httpx
import json

def stream_jsonl(url: str) -> iter:
    """
    Stream JSONL (one JSON object per line) — most memory-efficient format.
    Process each line as it arrives.
    """
    with httpx.stream("GET", url, timeout=300) as response:
        response.raise_for_status()
        buffer = ""
        for chunk in response.iter_text():
            buffer += chunk
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()
                if line:
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError as e:
                        print(f"Invalid JSONL line: {e}")

# Process streaming JSONL:
for record in stream_jsonl("https://api.example.com/export.jsonl"):
    process(record)  # Each record processed immediately, then GC'd
```

### Option 5: Limit response size and paginate at the caller

```python
import httpx

MAX_RESPONSE_BYTES = 50 * 1024 * 1024  # 50MB limit

async def safe_api_get(url: str, **kwargs) -> dict:
    """
    Fetch JSON API response with size guard.
    Raises if response exceeds memory safety threshold.
    """
    async with httpx.AsyncClient() as client:
        async with client.stream("GET", url, timeout=60, **kwargs) as response:
            response.raise_for_status()

            # Check Content-Length before downloading
            content_length = int(response.headers.get("content-length", 0))
            if content_length > MAX_RESPONSE_BYTES:
                raise ValueError(
                    f"Response too large: {content_length / 1024**2:.0f}MB. "
                    f"Use pagination or streaming endpoint instead."
                )

            # Read with rolling size check
            chunks = []
            total = 0
            async for chunk in response.aiter_bytes(chunk_size=65536):
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    raise ValueError(
                        f"Response exceeded {MAX_RESPONSE_BYTES / 1024**2:.0f}MB limit. "
                        f"Switch to /export/stream or add pagination."
                    )
                chunks.append(chunk)

            return json.loads(b"".join(chunks))
```

### Option 6: Use jq-style filtering to extract only needed fields

```python
import subprocess
import json

def extract_fields_with_jq(json_file: str, jq_filter: str) -> list:
    """
    Use jq to extract only needed fields from a large JSON file.
    jq streams — doesn't load entire file into Python memory.
    """
    result = subprocess.run(
        ["jq", "-c", jq_filter, json_file],
        capture_output=True,
        text=True,
        timeout=120
    )
    if result.returncode != 0:
        raise RuntimeError(f"jq failed: {result.stderr}")

    # Each line is one JSON object
    return [json.loads(line) for line in result.stdout.strip().split("\n") if line]

# Extract only id and email from a 1GB users array:
# jq filter: ".users[] | {id: .id, email: .email}"
users = extract_fields_with_jq(
    "massive_export.json",
    ".users[] | {id: .id, email: .email}"
)
# → Only 2 fields per user in Python memory — 95% smaller

# Pure Python alternative with ijson field projection:
def extract_fields_streaming(filepath: str, fields: list[str]) -> iter:
    with open(filepath, "rb") as f:
        for item in ijson.items(f, "item"):
            yield {k: item[k] for k in fields if k in item}
```

## Memory Usage by Parsing Strategy

| Strategy | Memory for 1GB JSON | Speed | Best for |
|---|---|---|---|
| `json.loads()` | ~5-8GB | Fast once loaded | Files < 50MB |
| `ijson` streaming | ~10MB constant | Slower throughput | Files > 100MB |
| Pagination | Per-page only | Network overhead | API responses |
| JSONL streaming | ~1MB constant | Fastest | Export pipelines |
| jq + Python | ~100MB | Fast | Field extraction |

## Expected Token Savings
OOM crash → restart → re-run from scratch: ~30,000 tokens + compute time
ijson streaming processes same data in constant memory: 0 crashes

## Environment
- Any agent processing large API exports, data pipeline outputs, or bulk database dumps
- Source: direct experience; JSON memory explosion is the #1 cause of agent OOM crashes in data-heavy workloads
