---
layout: solution
title: "Agent doesn't paginate large datasets"
category: performance
description: "Agent loads entire datasets into memory in a single request, causing OOM crashes, context overflow, and multi-second stalls on large collections."
tags: [performance, pagination, memory, scalability, database]
---

## Symptom

The agent calls `list_all_records()` or runs `SELECT * FROM orders` with no `LIMIT`, receives tens of thousands of rows, attempts to stuff them all into the context window, and either crashes with an OOM error, hits the context token limit, or takes 30+ seconds to process. Smaller datasets work fine, so the bug hides until production scale.

```
Agent fetches: SELECT * FROM events;   → 847,293 rows, 420 MB JSON
Context used:  1,847,000 tokens        → API returns 400 context-too-long
Memory used:   2.1 GB RSS              → OOM kill
```

## Root Cause

The agent was designed and tested on small datasets. No `LIMIT`/`OFFSET` clause, no cursor-based pagination, and no streaming were added because they were not needed at development scale. The tool or API returns all results in one shot without a built-in page size constraint.

## Fix

Always fetch data in bounded pages. Use `LIMIT`/`OFFSET`, cursor-based pagination, or async generator patterns to process records incrementally without loading the full dataset into memory or context.

---

### Option 1 — LIMIT/OFFSET pagination with per-page LLM analysis

```python
import anthropic
import json

client = anthropic.Anthropic()

# Simulated large dataset
ALL_RECORDS = [
    {"id": i, "user_id": i % 100, "event": f"click_{i % 5}", "ts": 1700000000 + i * 60}
    for i in range(10_000)
]

PAGE_SIZE = 50   # records per LLM call

def fetch_page(offset: int, limit: int) -> list[dict]:
    """Simulate a paginated database query."""
    return ALL_RECORDS[offset:offset + limit]

def analyze_page(page: list[dict], page_num: int, total_pages: int) -> str:
    """Send one page to the LLM for incremental analysis."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{
            "role": "user",
            "content": (
                f"Page {page_num}/{total_pages} of event data ({len(page)} records).\n"
                f"Count events by type and report the top event. Data:\n{json.dumps(page[:10])}..."
                f"\n(showing first 10 of {len(page)} records)"
            ),
        }],
    )
    return response.content[0].text.strip()

def paginated_analysis(total_limit: int = 200) -> str:
    """Analyze up to total_limit records in PAGE_SIZE batches."""
    total_pages = -(-total_limit // PAGE_SIZE)  # ceiling division
    summaries = []

    for page_num in range(1, total_pages + 1):
        offset = (page_num - 1) * PAGE_SIZE
        page = fetch_page(offset, PAGE_SIZE)
        if not page:
            break

        print(f"[PAGE {page_num}/{total_pages}] offset={offset} size={len(page)}")
        summary = analyze_page(page, page_num, total_pages)
        summaries.append(f"Page {page_num}: {summary}")

    # Final synthesis
    if len(summaries) > 1:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{
                "role": "user",
                "content": "Synthesize these page summaries into one overall finding:\n\n" +
                           "\n".join(summaries),
            }],
        )
        return response.content[0].text.strip()

    return summaries[0] if summaries else "No data"

result = paginated_analysis(total_limit=200)
print(f"\nFinal analysis:\n{result}")
```

**Expected Token Savings:** 95%+ memory reduction vs. loading all records; each page call uses a fixed token budget; total cost scales linearly with pages rather than quadratically with dataset size.

**Environment:** Any database-backed agent; set `PAGE_SIZE` to the largest batch that fits comfortably in `max_tokens` for the analysis step.

---

### Option 2 — Cursor-based pagination for stable ordering

```python
import anthropic
import json

client = anthropic.Anthropic()

# Simulated database with cursor support
RECORDS = sorted(
    [{"id": i, "score": i * 7 % 100, "label": f"item_{i}"} for i in range(5_000)],
    key=lambda r: r["id"],
)

def fetch_after_cursor(cursor_id: int | None, limit: int) -> tuple[list[dict], int | None]:
    """
    Cursor-based fetch: returns (page, next_cursor).
    next_cursor is None when there are no more pages.
    """
    start = 0
    if cursor_id is not None:
        # Find position after the cursor ID
        ids = [r["id"] for r in RECORDS]
        try:
            start = ids.index(cursor_id) + 1
        except ValueError:
            start = 0

    page = RECORDS[start:start + limit]
    next_cursor = page[-1]["id"] if len(page) == limit else None
    return page, next_cursor

def score_aggregator(pages_processed: int, running_total: float, page: list[dict]) -> float:
    """Aggregate score without keeping all records in memory."""
    return running_total + sum(r["score"] for r in page)

PAGE_SIZE = 100
cursor: int | None = None
pages = 0
total_score = 0.0

print("Processing with cursor pagination:")
while True:
    page, next_cursor = fetch_after_cursor(cursor, PAGE_SIZE)
    if not page:
        break

    pages += 1
    total_score = score_aggregator(pages, total_score, page)
    print(f"  Page {pages:3d}: cursor={cursor} → next={next_cursor} | records={len(page)}")

    if next_cursor is None:
        break
    cursor = next_cursor

# Final LLM call with only the aggregated statistics (not raw data)
response = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=128,
    messages=[{
        "role": "user",
        "content": (
            f"Dataset summary: {pages} pages × {PAGE_SIZE} records, "
            f"total score={total_score:.0f}, avg score={total_score/(pages*PAGE_SIZE):.2f}. "
            "Write a one-sentence interpretation."
        ),
    }],
)
print(f"\nPages processed: {pages}")
print(f"LLM interpretation: {response.content[0].text.strip()}")
```

**Expected Token Savings:** Cursor pagination is stable under concurrent inserts (unlike OFFSET which can skip or duplicate rows); the LLM only sees aggregated statistics, not raw records.

**Environment:** APIs and databases that support keyset/cursor pagination (`id > :last_id ORDER BY id LIMIT N`); preferred over OFFSET for large datasets.

---

### Option 3 — Async generator with bounded context budget

```python
import anthropic
import asyncio
import json

async_client = anthropic.AsyncAnthropic()

# Simulated async data source (e.g., paginated REST API)
async def async_data_source(total: int = 2000, page_size: int = 100):
    """Async generator that yields one page at a time."""
    offset = 0
    while offset < total:
        # Simulate I/O: fetch page from API
        await asyncio.sleep(0.01)
        page = [
            {"id": offset + i, "value": (offset + i) ** 2 % 997}
            for i in range(min(page_size, total - offset))
        ]
        yield page
        offset += page_size

async def incremental_llm_summary(
    running_summary: str,
    new_page: list[dict],
    page_num: int,
) -> str:
    """Update a rolling summary with each new page."""
    resp = await async_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=80,
        messages=[{
            "role": "user",
            "content": (
                f"Current summary: {running_summary or 'none yet'}\n"
                f"New page {page_num} ({len(new_page)} records, "
                f"values range {min(r['value'] for r in new_page)}–{max(r['value'] for r in new_page)}). "
                "Update summary in ≤20 words."
            ),
        }],
    )
    return resp.content[0].text.strip()

async def process_large_dataset() -> str:
    summary = ""
    page_num = 0

    async for page in async_data_source(total=500, page_size=50):
        page_num += 1
        summary = await incremental_llm_summary(summary, page, page_num)
        print(f"[Page {page_num:3d}] summary: {summary[:80]}")

    return summary

result = asyncio.run(process_large_dataset())
print(f"\nFinal summary: {result}")
```

**Expected Token Savings:** Rolling summary keeps context at O(summary_length) regardless of dataset size; each page contributes ~80 tokens to maintain the summary vs. thousands if raw data were accumulated.

**Environment:** Async agents consuming streaming APIs, Kafka topics, or async database cursors; async generator pattern also works for real-time data.

---

### Option 4 — Map-reduce: parallel page processing then aggregate

```python
import anthropic
import asyncio
import json

async_client = anthropic.AsyncAnthropic()

RECORDS = [
    {"id": i, "category": f"cat_{i%5}", "amount": i * 3 % 200}
    for i in range(1000)
]

PAGE_SIZE = 100
MAX_CONCURRENT_PAGES = 5   # process N pages in parallel

async def analyze_page_async(page: list[dict], page_id: int) -> dict:
    """Map step: extract stats from one page."""
    by_cat = {}
    for r in page:
        cat = r["category"]
        by_cat[cat] = by_cat.get(cat, 0) + r["amount"]
    return {"page_id": page_id, "by_category": by_cat, "count": len(page)}

async def reduce_results(page_results: list[dict]) -> str:
    """Reduce step: aggregate all page stats and get final LLM insight."""
    merged: dict[str, int] = {}
    total_count = 0
    for result in page_results:
        total_count += result["count"]
        for cat, amt in result["by_category"].items():
            merged[cat] = merged.get(cat, 0) + amt

    resp = await async_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{
            "role": "user",
            "content": (
                f"Total records: {total_count}. "
                f"Amount by category: {json.dumps(merged)}. "
                "Which category dominates and by what percentage?"
            ),
        }],
    )
    return resp.content[0].text.strip()

async def map_reduce_dataset() -> str:
    # Split into pages
    pages = [RECORDS[i:i+PAGE_SIZE] for i in range(0, len(RECORDS), PAGE_SIZE)]
    print(f"[MAP-REDUCE] {len(RECORDS)} records → {len(pages)} pages × {PAGE_SIZE}")

    # Process pages in parallel batches
    all_results = []
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_PAGES)

    async def bounded_analyze(page: list[dict], pid: int) -> dict:
        async with semaphore:
            result = await analyze_page_async(page, pid)
            print(f"  [MAP page {pid+1:3d}] categories={list(result['by_category'].keys())}")
            return result

    tasks = [bounded_analyze(page, i) for i, page in enumerate(pages)]
    all_results = await asyncio.gather(*tasks)

    # Reduce
    print(f"[REDUCE] aggregating {len(all_results)} page results")
    return await reduce_results(all_results)

result = asyncio.run(map_reduce_dataset())
print(f"\nFinal insight: {result}")
```

**Expected Token Savings:** Map step requires zero LLM calls (pure aggregation); only one reduce call with compact statistics; parallel page processing minimizes wall-clock time.

**Environment:** Analytics agents processing large datasets where pure aggregation (sum, count, max) can be done in Python before the LLM insight call.

---

### Option 5 — Streaming tool with page-aware tool schema

```python
import anthropic
import json

client = anthropic.Anthropic()

# Simulated paginated data API
DATA = [{"id": i, "status": ["active","inactive","pending"][i%3], "value": i*11%100} for i in range(300)]

def list_records(page: int, page_size: int, status_filter: str | None = None) -> dict:
    """Paginated tool that always bounds its response."""
    filtered = [r for r in DATA if status_filter is None or r["status"] == status_filter]
    total = len(filtered)
    start = (page - 1) * page_size
    items = filtered[start:start + page_size]
    return {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total_items": total,
        "total_pages": -(-total // page_size),  # ceiling
        "has_more": (page * page_size) < total,
    }

TOOLS = [
    {
        "name": "list_records",
        "description": (
            "List records with pagination. Always specify page and page_size. "
            "Check has_more to know if more pages exist. Max page_size: 50."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "page":          {"type": "integer", "description": "Page number (1-indexed)"},
                "page_size":     {"type": "integer", "description": "Records per page (max 50)"},
                "status_filter": {"type": "string",  "description": "Filter by status: active/inactive/pending"},
            },
            "required": ["page", "page_size"],
        },
    },
]

SYSTEM = (
    "You have access to a paginated list_records tool. "
    "Process all pages by checking has_more and incrementing page until it is false. "
    "Summarize findings after processing all pages."
)

def run_pagination_agent(question: str) -> str:
    messages = [{"role": "user", "content": question}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            break

        results = []
        for b in response.content:
            if b.type != "tool_use":
                continue
            page_result = list_records(
                page=b.input.get("page", 1),
                page_size=min(b.input.get("page_size", 20), 50),
                status_filter=b.input.get("status_filter"),
            )
            print(f"  [TOOL] page={page_result['page']}/{page_result['total_pages']} "
                  f"has_more={page_result['has_more']} items={len(page_result['items'])}")
            results.append({
                "type": "tool_result",
                "tool_use_id": b.id,
                "content": json.dumps({
                    **page_result,
                    "items": page_result["items"][:10],  # show first 10 only
                    "_note": f"Showing 10 of {len(page_result['items'])} items",
                }),
            })

        messages.append({"role": "user", "content": results})

    return next(b.text for b in response.content if hasattr(b, "text"))

print(run_pagination_agent("Count how many records have status 'active'. Use pagination."))
```

**Expected Token Savings:** Tool schema enforces `max page_size: 50` so the model cannot accidentally request unbounded data; `has_more` flag guides the agent to process all pages without human intervention.

**Environment:** Any tool-using agent; encoding pagination discipline into the tool schema is more reliable than prompt instructions alone.

---

### Option 6 — Chunked file processing with sliding window

```python
import anthropic
import io

client = anthropic.Anthropic()

CHUNK_SIZE = 2000   # characters per chunk
OVERLAP    = 200    # overlap between chunks to preserve context at boundaries

# Simulate a large file
LARGE_FILE_CONTENT = "\n".join(
    f"[{i:05d}] Log entry: user={i%50} action=request status={'OK' if i%7 else 'ERR'} latency={i%300}ms"
    for i in range(5000)
)

def chunk_text(text: str, chunk_size: int, overlap: int) -> list[str]:
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        start = end - overlap   # backtrack by overlap for continuity
    return chunks

def process_file_in_chunks(content: str) -> str:
    chunks = chunk_text(content, CHUNK_SIZE, OVERLAP)
    print(f"[CHUNKS] {len(content):,} chars → {len(chunks)} chunks × {CHUNK_SIZE} chars")

    error_counts: list[int] = []
    latency_totals: list[float] = []

    for i, chunk in enumerate(chunks):
        err_count = chunk.count("ERR")
        # Count latency values
        import re
        latencies = [int(m) for m in re.findall(r"latency=(\d+)ms", chunk)]
        avg_lat = sum(latencies) / len(latencies) if latencies else 0

        error_counts.append(err_count)
        latency_totals.append(avg_lat)
        print(f"  Chunk {i+1:3d}/{len(chunks)}: errors={err_count} avg_latency={avg_lat:.0f}ms")

    # Single LLM call with aggregate statistics
    total_errors = sum(error_counts)
    avg_latency  = sum(latency_totals) / len(latency_totals)

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{
            "role": "user",
            "content": (
                f"Log file analysis: {len(chunks)} chunks processed. "
                f"Total errors: {total_errors}. "
                f"Average latency: {avg_latency:.0f}ms. "
                "Give a one-sentence health assessment."
            ),
        }],
    )
    return response.content[0].text.strip()

result = process_file_in_chunks(LARGE_FILE_CONTENT)
print(f"\nAssessment: {result}")
```

**Expected Token Savings:** Chunk-based processing keeps each API call to a fixed token budget; overlap prevents losing context at chunk boundaries; the final LLM call receives only aggregated statistics (tens of tokens) instead of the raw file.

**Environment:** Log analysis, document processing, codebase scanning agents; set `CHUNK_SIZE` based on the density of information in the content.

---

## Comparison

| Option | Pagination Style | Memory Use | LLM Calls | Best For |
|--------|----------------|-----------|-----------|---------|
| 1 — LIMIT/OFFSET | SQL offset | O(page) | 1 per page + 1 | Database queries |
| 2 — Cursor-based | Keyset cursor | O(page) | Aggregate only | Large ordered datasets |
| 3 — Async generator | Stream | O(summary) | 1 per page | Streaming data sources |
| 4 — Map-reduce | Parallel pages | O(stats) | Aggregate only | Analytics workloads |
| 5 — Paginated tool | Tool schema | O(page) | 1 per page | Tool-using agents |
| 6 — Sliding window | Text chunks | O(chunk) | Aggregate only | File/document processing |

**Recommended default:** Option 2 (cursor pagination) for database-backed agents — stable under concurrent writes and scales to billions of rows. Use Option 4 (map-reduce) when aggregation can be done in Python before the LLM call.
