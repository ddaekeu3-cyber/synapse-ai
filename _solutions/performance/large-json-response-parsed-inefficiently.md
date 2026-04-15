---
layout: solution
title: "Agent Parses Large JSON Response Inefficiently — High Memory and Latency"
category: performance
description: "Agent receives a 50MB JSON API response and parses the entire thing into memory. Causes OOM errors or 10+ second pauses. Only 5% of the data is actually needed."
tags: [json, parsing, memory, performance, streaming, large-response]
---

# Agent Parses Large JSON Response Inefficiently — High Memory and Latency

## Symptom
- `MemoryError` or `OOM killed` when processing large API responses
- Agent hangs for 10–30 seconds while parsing JSON
- Fetching a 50MB response to extract 3 fields
- Agent passes entire API response into LLM context (huge token cost)
- `json.loads()` on a 100MB string causes container crash

## Root Cause
Full JSON parsing loads the entire document into memory as a Python dict/list. For large responses, this is wasteful when only a subset of data is needed. Passing raw large JSON to an LLM compounds the problem with massive token costs.

## Fix

### Option 1: Extract only needed fields before passing to agent

```python
import httpx, jmespath

# BAD — load 50MB response into LLM context
response = httpx.get("https://api.example.com/large-dataset")
data = response.json()
agent.complete(f"Here is the data: {data}")  # Passes entire 50MB to LLM

# GOOD — extract only what's needed
response = httpx.get("https://api.example.com/large-dataset")
data = response.json()

# Extract specific fields with jmespath
relevant_data = jmespath.search(
    "items[?status=='active'].{id: id, name: name, score: metrics.score}",
    data
)
agent.complete(f"Active items with scores: {relevant_data}")  # Passes only relevant subset
```

### Option 2: Streaming JSON parse for very large files

```python
import ijson  # pip install ijson — streaming JSON parser

def extract_from_large_json(file_path: str, target_key: str) -> list:
    """Parse large JSON file without loading it all into memory"""
    results = []
    with open(file_path, "rb") as f:
        # Stream parse — only load one item at a time
        for item in ijson.items(f, f"{target_key}.item"):
            results.append(item)
    return results

# For HTTP streaming:
def stream_parse_api_response(url: str) -> list:
    import httpx
    results = []
    with httpx.stream("GET", url) as response:
        for item in ijson.items(response.iter_bytes(), "data.item"):
            results.append(item)
    return results
```

### Option 3: Paginate instead of fetching everything

```python
async def paginate_api(base_url: str, page_size: int = 100) -> list:
    """Fetch data page by page instead of all at once"""
    results = []
    page = 1

    async with httpx.AsyncClient() as client:
        while True:
            response = await client.get(
                base_url,
                params={"page": page, "per_page": page_size},
                timeout=30.0
            )
            data = response.json()
            items = data.get("items", [])

            if not items:
                break

            results.extend(items)
            page += 1

            # Stop if we have enough data
            if len(results) >= 1000:
                print(f"Fetched 1000 items, stopping pagination")
                break

    return results
```

### Option 4: Use API query parameters to filter server-side

```python
# BAD — fetch everything, filter locally
all_users = httpx.get("https://api.example.com/users").json()
active_premium = [u for u in all_users if u["status"] == "active" and u["plan"] == "premium"]

# GOOD — filter on the server, receive only what you need
active_premium = httpx.get(
    "https://api.example.com/users",
    params={
        "status": "active",
        "plan": "premium",
        "fields": "id,name,email",  # Only return needed fields
        "limit": 100
    }
).json()
```

### Option 5: Summarize large responses before passing to LLM

```python
def summarize_json_for_llm(data: dict | list, max_items: int = 10) -> str:
    """Create a compact summary of large JSON for LLM consumption"""
    if isinstance(data, list):
        total = len(data)
        sample = data[:max_items]
        schema = {k: type(v).__name__ for k, v in sample[0].items()} if sample else {}
        return (
            f"List of {total} items. Schema: {schema}\n"
            f"First {min(max_items, total)} items: {sample}"
        )
    elif isinstance(data, dict):
        # Show keys and value types
        summary = {k: (type(v).__name__, str(v)[:50] if not isinstance(v, (dict, list)) else f"{type(v).__name__}[{len(v)}]")
                   for k, v in data.items()}
        return f"Object with {len(data)} keys: {summary}"
    return str(data)[:1000]

# Usage
large_response = httpx.get("https://api.example.com/dataset").json()
compact_summary = summarize_json_for_llm(large_response)
agent.complete(f"API returned: {compact_summary}\nWhat is the total count?")
```

### Option 6: JSONPath / jq for server-side filtering

```bash
# Use jq to pre-process large JSON files before passing to agent
# Extract only active users' names and emails:
cat large_response.json | jq '[.items[] | select(.status == "active") | {name, email}]'

# Count by category:
cat large_response.json | jq '[.items[].category] | group_by(.) | map({category: .[0], count: length})'
```

```python
import subprocess, json

def jq_filter(data: str, query: str) -> list:
    """Apply jq filter to JSON string"""
    result = subprocess.run(
        ["jq", query],
        input=data.encode(),
        capture_output=True
    )
    return json.loads(result.stdout)

filtered = jq_filter(large_json_string, '.items[] | select(.score > 90)')
```

## Memory Usage by Approach

| Approach | Memory for 100MB JSON | Latency |
|---|---|---|
| `json.loads()` full parse | ~500MB (5× expansion) | 3–10s |
| `ijson` streaming | ~1MB | 5–15s (streaming) |
| Paginate (100 items/page) | ~1MB | Per-page latency |
| Server-side filter | Proportional to result | Fast |
| jq pre-processing | ~1MB | 1–3s |

## Expected Token Savings
Passing 50MB JSON to LLM: 12+ million tokens (impossible)
Summarizing to 500 tokens: 99.99% reduction

## Environment
- Agents fetching data from APIs with large payloads; most critical for analytics/reporting agents
- Source: direct experience with data-fetching agents hitting memory limits
