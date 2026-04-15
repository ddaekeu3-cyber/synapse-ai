---
layout: solution
title: "Agent Doesn't Use Binary Serialization for Large Payloads"
category: performance
description: "Agent serializes large tool results and inter-agent messages as verbose JSON strings — a 10MB dataset becomes 15MB of UTF-8 JSON. Switching to binary serialization (MessagePack, Protocol Buffers, or compression) reduces payload size by 60-80% and speeds up processing."
tags: [performance, serialization, messagepack, compression, throughput]
---

## Symptom

An agent fetches a large dataset from a tool and stores it in the conversation:

```python
tool_result = json.dumps(large_dataset)  # 10MB DataFrame → 15MB JSON string
# Tool result sent back to model context: 15,000,000 chars ≈ 3,750,000 tokens
```

Network transfer, JSON parsing, and context consumption all scale with payload size. A dataset that could be 3MB in binary becomes 15MB of verbose JSON — 5x overhead.

## Root Cause

Python's `json.dumps()` is the default serialization path — it produces human-readable but verbose output with no compression:

```python
import anthropic
import json

client = anthropic.Anthropic(api_key="sk-live-...")

# Anti-pattern: large data serialized as uncompressed JSON
def fetch_dataset(table: str) -> str:
    data = load_large_table(table)  # Returns list of 50,000 dicts
    return json.dumps(data)  # ← 15MB of JSON in tool result
```

---

## Fix

### Option 1 — Compress JSON with zlib + base64 for transport

Compress the JSON payload before encoding it as a string tool result. Decompress when reading.

```python
import anthropic
import json
import zlib
import base64

client = anthropic.Anthropic(api_key="sk-live-...")


def compress_payload(data: dict | list) -> str:
    """Compress data as zlib-compressed, base64-encoded JSON."""
    json_bytes = json.dumps(data, separators=(',', ':')).encode('utf-8')
    compressed = zlib.compress(json_bytes, level=6)
    encoded = base64.b64encode(compressed).decode('ascii')
    return f"COMPRESSED:{encoded}"


def decompress_payload(text: str) -> dict | list:
    """Decompress a compressed tool result."""
    if not text.startswith("COMPRESSED:"):
        return json.loads(text)
    encoded = text[len("COMPRESSED:"):]
    compressed = base64.b64decode(encoded)
    json_bytes = zlib.decompress(compressed)
    return json.loads(json_bytes)


def tool_fetch_large_data(n_rows: int = 10_000) -> str:
    """Simulated large data tool — returns compressed result."""
    data = [
        {"id": i, "name": f"item_{i}", "value": i * 3.14, "tags": ["a", "b", "c"]}
        for i in range(n_rows)
    ]
    raw_json = json.dumps(data, separators=(',', ':'))
    compressed_result = compress_payload(data)

    raw_size = len(raw_json)
    compressed_size = len(compressed_result)
    print(f"[compress] Raw: {raw_size:,} chars → Compressed: {compressed_size:,} chars "
          f"({100 * compressed_size / raw_size:.1f}% of original)")

    return compressed_result


def process_tool_result(compressed: str) -> int:
    """Decompress and process a tool result."""
    data = decompress_payload(compressed)
    return len(data)


# Demonstrate size reduction
result = tool_fetch_large_data(10_000)
count = process_tool_result(result)
print(f"Recovered {count:,} rows from compressed result")

# Expected Token Savings: 60-80% size reduction on structured data → 3x fewer tokens in tool results
# Environment: agents fetching large datasets, logs, or API responses; data pipeline agents
```

---

### Option 2 — MessagePack serialization for inter-agent communication

Use MessagePack for binary-efficient serialization between agent components. 30-50% smaller than JSON with faster encode/decode.

```python
import anthropic
import json
import base64
import time

client = anthropic.Anthropic(api_key="sk-live-...")

try:
    import msgpack
    HAS_MSGPACK = True
except ImportError:
    HAS_MSGPACK = False
    print("Install: pip install msgpack")


def msgpack_encode(data) -> str:
    """Encode data as MessagePack, base64-encoded for string transport."""
    if not HAS_MSGPACK:
        return json.dumps(data)
    packed = msgpack.packb(data, use_bin_type=True)
    return f"MSGPACK:{base64.b64encode(packed).decode('ascii')}"


def msgpack_decode(text: str):
    """Decode a MessagePack-encoded string."""
    if not text.startswith("MSGPACK:") or not HAS_MSGPACK:
        return json.loads(text)
    encoded = text[len("MSGPACK:"):]
    packed = base64.b64decode(encoded)
    return msgpack.unpackb(packed, raw=False)


def benchmark_serialization(n_rows: int = 5_000) -> None:
    """Compare JSON vs MessagePack on a realistic dataset."""
    dataset = [
        {
            "user_id": i,
            "username": f"user_{i:05d}",
            "score": round(i * 0.73, 4),
            "active": i % 3 != 0,
            "tags": ["premium" if i % 10 == 0 else "standard"],
            "metadata": {"region": "us-east", "tier": i % 5}
        }
        for i in range(n_rows)
    ]

    # JSON
    t0 = time.monotonic()
    json_result = json.dumps(dataset, separators=(',', ':'))
    json_time = time.monotonic() - t0

    # MessagePack (if available)
    if HAS_MSGPACK:
        t0 = time.monotonic()
        mp_result = msgpack_encode(dataset)
        mp_time = time.monotonic() - t0
        mp_size = len(mp_result)
    else:
        mp_result = json_result
        mp_time = json_time
        mp_size = len(json_result)

    print(f"Dataset: {n_rows:,} rows")
    print(f"JSON:     {len(json_result):>10,} chars | {json_time*1000:.1f}ms")
    print(f"MsgPack:  {mp_size:>10,} chars | {mp_time*1000:.1f}ms")
    if HAS_MSGPACK:
        print(f"Reduction: {100 * (1 - mp_size/len(json_result)):.1f}%")

    # Verify round-trip
    recovered = msgpack_decode(mp_result)
    print(f"Round-trip OK: {len(recovered) == n_rows}")


benchmark_serialization(5_000)

# Expected Token Savings: 30-50% payload reduction → fewer tokens in tool results + faster processing
# Environment: high-throughput inter-agent pipelines; agents exchanging large structured datasets
```

---

### Option 3 — Summary-first: return metadata only, fetch details on demand

Instead of serializing entire datasets, return a compact summary with an ID. Let the model request specific subsets on demand.

```python
import anthropic
import json
import uuid

client = anthropic.Anthropic(api_key="sk-live-...")

# Simulated data store (in production: actual database or object storage)
DATA_STORE: dict[str, list[dict]] = {}


def store_dataset(data: list[dict]) -> str:
    """Store dataset; return a reference ID."""
    dataset_id = str(uuid.uuid4())[:8]
    DATA_STORE[dataset_id] = data
    return dataset_id


def fetch_summary(dataset_id: str) -> str:
    """Return compact summary — not the full data."""
    data = DATA_STORE.get(dataset_id, [])
    if not data:
        return json.dumps({"error": f"Dataset {dataset_id} not found"})

    # Compact summary: schema, counts, sample
    cols = list(data[0].keys()) if data else []
    numeric_cols = [k for k, v in (data[0] or {}).items() if isinstance(v, (int, float))]

    summary = {
        "dataset_id": dataset_id,
        "rows": len(data),
        "columns": cols,
        "sample": data[:3],  # First 3 rows only
    }

    # Add basic stats for numeric columns
    for col in numeric_cols[:3]:  # Max 3 numeric summaries
        values = [row[col] for row in data if isinstance(row.get(col), (int, float))]
        if values:
            summary[f"stats_{col}"] = {
                "min": min(values), "max": max(values),
                "avg": round(sum(values) / len(values), 2)
            }

    return json.dumps(summary)


def fetch_slice(dataset_id: str, offset: int, limit: int) -> str:
    """Return a specific slice of the dataset."""
    data = DATA_STORE.get(dataset_id, [])
    slice_data = data[offset:offset + limit]
    return json.dumps({
        "dataset_id": dataset_id,
        "offset": offset,
        "limit": limit,
        "rows": len(slice_data),
        "data": slice_data
    })


tools = [
    {
        "name": "get_dataset_summary",
        "description": "Get a compact summary of a dataset (rows, schema, sample). Use this first before fetching full data.",
        "input_schema": {
            "type": "object",
            "properties": {"dataset_id": {"type": "string"}},
            "required": ["dataset_id"]
        }
    },
    {
        "name": "get_dataset_slice",
        "description": "Fetch a specific slice of rows from a dataset. Use offset/limit to get only what you need.",
        "input_schema": {
            "type": "object",
            "properties": {
                "dataset_id": {"type": "string"},
                "offset": {"type": "integer", "default": 0},
                "limit": {"type": "integer", "default": 100}
            },
            "required": ["dataset_id"]
        }
    }
]

# Seed test data
big_dataset = [{"id": i, "value": i * 2.5, "label": f"item_{i}"} for i in range(50_000)]
ds_id = store_dataset(big_dataset)
print(f"Dataset stored: {ds_id} ({len(big_dataset):,} rows)")

# Show size comparison
full_json = json.dumps(big_dataset)
summary_json = fetch_summary(ds_id)
print(f"Full data:    {len(full_json):>10,} chars")
print(f"Summary:      {len(summary_json):>10,} chars ({100*len(summary_json)/len(full_json):.1f}%)")

# Expected Token Savings: summary is <1% the size of full data; model requests only needed slices
# Environment: data analysis agents; report generation; agents querying large result sets
```

---

### Option 4 — Streaming + chunked processing: never materialize the full payload

Process large data in chunks rather than loading and serializing it all at once.

```python
import anthropic
import json

client = anthropic.Anthropic(api_key="sk-live-...")

CHUNK_SIZE = 500  # Process 500 rows at a time


def stream_process_dataset(
    dataset_generator,
    process_fn,
    chunk_size: int = CHUNK_SIZE
) -> dict:
    """
    Process a large dataset in chunks without ever holding all data in memory
    or serializing it as a single string.
    Returns aggregated statistics, not raw data.
    """
    total_rows = 0
    aggregated = {}

    chunk: list[dict] = []
    for row in dataset_generator:
        chunk.append(row)
        if len(chunk) >= chunk_size:
            result = process_fn(chunk, aggregated)
            aggregated = result
            total_rows += len(chunk)
            chunk = []

    # Process remaining
    if chunk:
        result = process_fn(chunk, aggregated)
        aggregated = result
        total_rows += len(chunk)

    return {"total_rows": total_rows, "aggregated": aggregated}


def example_aggregator(chunk: list[dict], current: dict) -> dict:
    """Compute running totals/counts from each chunk."""
    total = current.get("total_value", 0.0)
    count = current.get("count_negative", 0)
    for row in chunk:
        val = row.get("value", 0)
        total += val
        if val < 0:
            count += 1
    return {"total_value": round(total, 4), "count_negative": count}


def large_data_generator(n: int):
    """Simulate a streaming data source."""
    import random
    for i in range(n):
        yield {"id": i, "value": random.uniform(-100, 100), "category": i % 5}


# Process 100,000 rows without ever materializing the full dataset as a string
N_ROWS = 100_000
result = stream_process_dataset(large_data_generator(N_ROWS), example_aggregator)
print(f"Processed {result['total_rows']:,} rows")
print(f"Aggregated result: {json.dumps(result['aggregated'])}")
print(f"Tool result size: {len(json.dumps(result))} chars (vs ~{N_ROWS * 50 // 1000}KB for full data)")

# Expected Token Savings: tool results contain aggregates (~100 chars) not raw data (~5MB)
# Environment: data pipeline agents; ETL agents; agents computing statistics over large datasets
```

---

### Option 5 — Delta encoding: only serialize changes since last fetch

If the agent periodically fetches the same resource, only serialize what changed since the last fetch.

```python
import anthropic
import json
import hashlib
import time

client = anthropic.Anthropic(api_key="sk-live-...")

# State cache: resource_id → (last_data_hash, last_data)
_resource_cache: dict[str, tuple[str, dict]] = {}


def compute_hash(data: dict | list) -> str:
    canonical = json.dumps(data, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def compute_delta(old: dict, new: dict) -> dict:
    """Return only fields that changed between old and new dicts."""
    if not isinstance(old, dict) or not isinstance(new, dict):
        return new

    delta = {}
    all_keys = set(old.keys()) | set(new.keys())
    for key in all_keys:
        old_val = old.get(key)
        new_val = new.get(key)
        if new_val != old_val:
            delta[key] = {"old": old_val, "new": new_val}

    return delta


def fetch_with_delta(resource_id: str, current_data: dict) -> str:
    """
    Fetch resource and return delta from previous fetch.
    If first fetch, returns full data. Subsequent fetches return only changes.
    """
    new_hash = compute_hash(current_data)
    full_json = json.dumps(current_data, separators=(',', ':'))

    if resource_id not in _resource_cache:
        _resource_cache[resource_id] = (new_hash, current_data)
        print(f"[delta] First fetch of {resource_id}: {len(full_json)} chars (full)")
        return json.dumps({
            "type": "full",
            "resource_id": resource_id,
            "data": current_data,
            "hash": new_hash
        })

    old_hash, old_data = _resource_cache[resource_id]

    if old_hash == new_hash:
        print(f"[delta] {resource_id}: no change (hash match)")
        return json.dumps({
            "type": "unchanged",
            "resource_id": resource_id,
            "hash": new_hash
        })

    delta = compute_delta(old_data, current_data)
    _resource_cache[resource_id] = (new_hash, current_data)

    delta_json = json.dumps({
        "type": "delta",
        "resource_id": resource_id,
        "changes": delta,
        "hash": new_hash
    })

    print(f"[delta] {resource_id}: full={len(full_json)} chars, delta={len(delta_json)} chars "
          f"({100*len(delta_json)/len(full_json):.1f}%)")
    return delta_json


# Simulate periodic config fetches with small changes
config_v1 = {"host": "db.internal", "port": 5432, "pool_size": 10, "timeout": 30, "ssl": True}
config_v2 = {**config_v1, "pool_size": 20, "timeout": 60}  # Two fields changed
config_v3 = config_v2  # No change

fetch_with_delta("db_config", config_v1)
fetch_with_delta("db_config", config_v2)
fetch_with_delta("db_config", config_v3)

# Expected Token Savings: repeated fetches of stable resources cost ~50 chars instead of full payload
# Environment: agents monitoring config, prices, or status that changes infrequently
```

---

### Option 6 — External object store: reference large payloads by URL or key

Store large tool results in S3, Redis, or a temp file. Return only a reference. The model retrieves specific fields on demand.

```python
import anthropic
import json
import uuid
import time
from pathlib import Path

client = anthropic.Anthropic(api_key="sk-live-...")

# Simulated object store (in production: S3, Redis, GCS, etc.)
_object_store: dict[str, bytes] = {}
_object_metadata: dict[str, dict] = {}


def store_large_result(data: dict | list, ttl_seconds: int = 3600) -> dict:
    """Store large data externally; return a compact reference."""
    key = f"obj_{uuid.uuid4().hex[:12]}"
    serialized = json.dumps(data, separators=(',', ':')).encode('utf-8')

    _object_store[key] = serialized
    _object_metadata[key] = {
        "size_bytes": len(serialized),
        "rows": len(data) if isinstance(data, list) else 1,
        "expires_at": time.time() + ttl_seconds,
        "keys": list(data[0].keys()) if isinstance(data, list) and data else []
    }

    return {
        "type": "object_reference",
        "key": key,
        "size_bytes": len(serialized),
        "rows": _object_metadata[key]["rows"],
        "schema": _object_metadata[key]["keys"],
        "expires_in_seconds": ttl_seconds,
        "access": "Use get_object_field(key, field) or get_object_slice(key, offset, limit)"
    }


def get_object_field_stats(key: str, field: str) -> dict:
    """Retrieve statistics for a specific field from stored object."""
    data = json.loads(_object_store.get(key, b"[]"))
    if not isinstance(data, list):
        return {"error": "Not a list"}

    values = [row.get(field) for row in data if isinstance(row.get(field), (int, float))]
    if not values:
        return {"field": field, "type": "non-numeric or missing"}

    return {
        "field": field,
        "count": len(values),
        "min": min(values),
        "max": max(values),
        "avg": round(sum(values) / len(values), 4)
    }


tools = [
    {
        "name": "fetch_large_result",
        "description": "Fetch a large dataset. Returns a reference key — NOT the full data.",
        "input_schema": {"type": "object", "properties": {"source": {"type": "string"}}, "required": ["source"]}
    },
    {
        "name": "get_field_stats",
        "description": "Get statistics for a specific field in a stored object.",
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "field": {"type": "string"}
            },
            "required": ["key", "field"]
        }
    }
]


def handle_tool(name: str, input_data: dict) -> str:
    if name == "fetch_large_result":
        big_data = [{"id": i, "revenue": i * 99.5, "region": f"r{i%5}"} for i in range(100_000)]
        ref = store_large_result(big_data)
        return json.dumps(ref)

    if name == "get_field_stats":
        stats = get_object_field_stats(input_data["key"], input_data["field"])
        return json.dumps(stats)

    return json.dumps({"error": "Unknown tool"})


def run_large_data_agent(question: str) -> str:
    messages = [{"role": "user", "content": question}]
    system = "When you receive an object reference, use get_field_stats to analyse specific fields. Never ask for the full data."

    for _ in range(6):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            system=system,
            tools=tools,
            messages=messages
        )

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            return next(b.text for b in response.content if b.type == "text")

        tool_results = []
        for tu in tool_uses:
            result = handle_tool(tu.name, tu.input)
            print(f"[tool] {tu.name}: {result[:80]}...")
            tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": result})

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})


run_large_data_agent("Analyse the revenue field in the sales dataset")

# Expected Token Savings: reference = ~200 chars vs 5MB dataset; model fetches only what it needs
# Environment: data analysis agents; agents over data warehouses; reporting pipelines
```

---

## Comparison

| Option | Payload Reduction | Requires External Dependency | Complexity | Best For |
|--------|------------------|------------------------------|------------|----------|
| 1 | 60-80% (zlib) | No | Low | General-purpose compression |
| 2 | 30-50% (msgpack) | msgpack library | Low | Structured data, fast encode |
| 3 | 95%+ (summary only) | No | Medium | Large datasets where aggregates suffice |
| 4 | 95%+ (streaming) | No | Medium | ETL/pipeline processing |
| 5 | 90%+ (delta only) | No | Medium | Frequently-polled stable resources |
| 6 | 99%+ (reference) | Object store | Medium | Very large datasets needing on-demand access |

**Recommended starting point:** Option 1 (zlib compression) for immediate wins — wrap the tool result in `compress_payload()` and decompress on read. Zero external dependencies, 2-line change, and 60-80% payload reduction. Upgrade to Option 3 (summary-first) for large datasets where the model rarely needs more than metadata and a sample.
