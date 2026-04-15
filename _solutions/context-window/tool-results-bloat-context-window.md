---
layout: solution
title: "Tool Results Are Too Verbose and Bloat the Context Window"
category: context-window
description: "Agent runs a database query returning 10,000 rows. The full result goes into context. Or a web scrape returns 500KB of HTML. Tool results fill the context window and cost thousands of tokens per call."
tags: [tool-results, context, token-cost, truncation, summarization, verbose]
---

# Tool Results Are Too Verbose and Bloat the Context Window

## Symptom
- Database query returns all 10,000 records — full result in context
- Web page scrape includes navigation, ads, scripts — 200KB of noise
- Agent reads an entire 5,000-line log file when looking for one error
- Context window fills up after a few tool calls
- Each turn costs 50,000+ tokens mostly from old tool results

## Root Cause
Tool results are included verbatim in the conversation context. The model processes all tokens every turn — including tool results from 5 turns ago. Without trimming, filtering, or summarizing tool results, the context fills quickly with data that's no longer actively needed.

## Fix

### Option 1: Limit results at the tool level

```python
def search_database(query: str, limit: int = 20) -> dict:
    """
    Search database. Returns max 20 results to prevent context bloat.
    Use 'offset' parameter to paginate if more results needed.
    """
    results = db.execute(query).fetchmany(limit)
    total = db.execute(f"SELECT COUNT(*) FROM ({query})").fetchone()[0]

    return {
        "results": results,
        "returned": len(results),
        "total": total,
        "has_more": total > limit,
        "note": f"Showing {len(results)} of {total} results. Use limit/offset to get more."
    }

def read_log_file(path: str, search: str = None, tail_lines: int = 100) -> dict:
    """
    Read log file. By default returns last 100 lines, not entire file.
    Use search parameter to find specific patterns.
    """
    lines = open(path).readlines()

    if search:
        matching = [l for l in lines if search in l]
        return {
            "query": search,
            "matches": matching[:50],
            "total_matches": len(matching),
            "note": f"Found {len(matching)} lines matching '{search}'"
        }

    # Return tail only
    return {
        "lines": lines[-tail_lines:],
        "total_lines": len(lines),
        "note": f"Showing last {tail_lines} of {len(lines)} lines"
    }
```

### Option 2: Compress tool results before adding to context

```python
MAX_TOOL_RESULT_CHARS = 2000  # ~500 tokens

def compress_tool_result(result: str, tool_name: str) -> str:
    """Trim tool result to fit within token budget"""
    if len(result) <= MAX_TOOL_RESULT_CHARS:
        return result

    half = MAX_TOOL_RESULT_CHARS // 2
    compressed = (
        result[:half] +
        f"\n\n[... {len(result) - MAX_TOOL_RESULT_CHARS} characters truncated "
        f"(tool: {tool_name}, total: {len(result)} chars) ...]\n\n" +
        result[-half:]
    )
    return compressed

def execute_tool_with_compression(tool_name: str, tool_input: dict) -> str:
    result = dispatch_tool(tool_name, tool_input)
    result_str = str(result)
    return compress_tool_result(result_str, tool_name)
```

### Option 3: Summarize old tool results in context

```python
async def compress_old_tool_results(history: list, agent, keep_recent: int = 2) -> list:
    """Summarize tool results older than N turns"""
    tool_result_indices = [
        i for i, msg in enumerate(history)
        if isinstance(msg.get("content"), list) and
        any(block.get("type") == "tool_result" for block in msg.get("content", []))
    ]

    # Keep the most recent N tool results verbatim
    old_indices = tool_result_indices[:-keep_recent] if len(tool_result_indices) > keep_recent else []

    for idx in old_indices:
        content_blocks = history[idx].get("content", [])
        for block in content_blocks:
            if block.get("type") == "tool_result":
                original = str(block.get("content", ""))
                if len(original) > 500:
                    # Summarize this old tool result
                    summary = await agent.complete([{
                        "role": "user",
                        "content": f"Summarize this tool result in 1-2 sentences, keeping key facts:\n{original[:2000]}"
                    }])
                    block["content"] = f"[Summarized: {summary}]"

    return history
```

### Option 4: Extract only relevant parts of tool results

```python
import re
from bs4 import BeautifulSoup

def extract_relevant_content(tool_result: str, tool_name: str, user_query: str) -> str:
    """Extract only query-relevant content from large tool results"""

    if tool_name == "web_scrape":
        # Strip HTML, ads, navigation
        soup = BeautifulSoup(tool_result, "html.parser")
        # Remove script, style, nav elements
        for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        # Keep only paragraphs containing relevant keywords
        keywords = user_query.lower().split()
        relevant_paragraphs = [
            p for p in text.split("\n\n")
            if any(kw in p.lower() for kw in keywords)
        ]
        return "\n\n".join(relevant_paragraphs[:10])  # Max 10 relevant paragraphs

    if tool_name == "read_file" and len(tool_result) > 5000:
        # For large files, find relevant section
        lines = tool_result.split("\n")
        keywords = user_query.lower().split()
        relevant_lines = []
        for i, line in enumerate(lines):
            if any(kw in line.lower() for kw in keywords):
                # Include surrounding context
                start = max(0, i - 2)
                end = min(len(lines), i + 5)
                relevant_lines.extend(lines[start:end])
                relevant_lines.append("...")
        return "\n".join(relevant_lines[:100]) if relevant_lines else tool_result[:2000]

    return tool_result
```

### Option 5: Replace old tool results with references

```python
class ToolResultStore:
    """Store full tool results externally, reference them in context"""

    def __init__(self):
        self._store = {}
        self._counter = 0

    def save(self, result: str, tool_name: str) -> str:
        """Save result, return compact reference"""
        key = f"TOOL_RESULT_{self._counter}"
        self._counter += 1
        self._store[key] = result

        # Return compact summary for context
        preview = result[:200]
        return (
            f"[{key}: {tool_name} result, {len(result)} chars. "
            f"Preview: {preview}... "
            f"Request full result by name if needed]"
        )

    def retrieve(self, key: str) -> str:
        return self._store.get(key, f"Result {key} not found")

store = ToolResultStore()

# In tool execution:
full_result = execute_tool(tool_name, tool_input)
compact_reference = store.save(full_result, tool_name)
# compact_reference goes into context — full result retrievable when needed
```

## Token Usage by Tool Result Size

| Result size | Tokens (approx) | Impact |
|---|---|---|
| 100 chars | 25 | Negligible |
| 1,000 chars | 250 | Fine |
| 10,000 chars | 2,500 | Significant |
| 100,000 chars | 25,000 | Critical |
| 500,000 chars | 125,000 | Over context limit |

## Expected Token Savings
Uncompressed DB result (10K rows): ~50,000 tokens
Limit 20 rows + total count: ~500 tokens (99% reduction)

## Environment
- Any agent calling database queries, web scraping, or file reading tools
- Source: direct experience; tool result bloat is the #1 cause of unexpected context exhaustion
