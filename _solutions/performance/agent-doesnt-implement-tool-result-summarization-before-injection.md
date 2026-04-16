---
title: "Agent Doesn't Implement Tool Result Summarization Before Injection"
description: "Agents that inject raw tool results verbatim bloat the context window with redundant data, JSON boilerplate, and formatting noise — summarizing or extracting key fields before injection cuts context usage by 60-80% with no quality loss."
difficulty: intermediate
category: performance
tags: [performance, context-window, summarization, tool-results, cost-optimization, tokens]
---

# Agent Doesn't Implement Tool Result Summarization Before Injection

## Problem

A database query returns 200 rows of JSON. A web search returns full HTML snippets. An API response includes pagination metadata, rate limit headers, and nested objects the LLM will never use. Injecting these raw results verbatim uses 3,000 tokens where 300 would suffice, accelerates context compression, and degrades quality by burying the signal in noise.

**Symptoms:**
- Tool results dominate the token count in long conversations
- Context window fills after 5-6 tool calls when it could support 30+
- LLM ignores data buried deep in long tool result blocks
- Identical fields repeated across array items waste hundreds of tokens
- Search results include full article text when only title and URL are needed

---

## Solution 1: Field Extraction — Keep Only Needed Keys

Strip tool results to only the fields the LLM actually uses before injecting into context.

```python
import asyncio
import json
from typing import Any, Optional
import anthropic


def extract_fields(data: Any, keep: list[str]) -> Any:
    """
    Recursively extract only specified keys from dicts/lists.
    Reduces a full API response to the essential fields.
    """
    if isinstance(data, dict):
        return {k: extract_fields(v, keep) for k, v in data.items() if k in keep}
    if isinstance(data, list):
        return [extract_fields(item, keep) for item in data]
    return data


# Define which fields matter per tool
FIELD_POLICIES: dict[str, list[str]] = {
    "web_search":     ["title", "snippet", "url", "published_date"],
    "database_query": ["id", "name", "status", "updated_at", "value"],
    "github_search":  ["full_name", "description", "stargazers_count", "language", "html_url"],
    "news_feed":      ["headline", "summary", "source", "published_at"],
    "user_lookup":    ["user_id", "name", "email", "plan", "created_at"],
}


def slim_tool_result(tool_name: str, raw_result: Any) -> tuple[Any, int, int]:
    """
    Returns (slimmed_result, original_tokens, slimmed_tokens).
    """
    original_json = json.dumps(raw_result)
    original_tokens = len(original_json) // 4  # Rough estimate

    fields = FIELD_POLICIES.get(tool_name)
    if fields:
        slimmed = extract_fields(raw_result, fields)
    else:
        slimmed = raw_result  # No policy — pass through

    slimmed_json = json.dumps(slimmed)
    slimmed_tokens = len(slimmed_json) // 4
    return slimmed, original_tokens, slimmed_tokens


class FieldExtractionAgent:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)

    async def answer_with_tool(
        self,
        question: str,
        tool_name: str,
        raw_tool_result: Any,
    ) -> str:
        slimmed, orig_tokens, slim_tokens = slim_tool_result(tool_name, raw_tool_result)
        print(
            f"[slim] {tool_name}: {orig_tokens} → {slim_tokens} tokens "
            f"({100 - 100*slim_tokens//max(orig_tokens,1)}% reduction)"
        )

        context = json.dumps(slimmed, indent=2)[:3000]
        response = await self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": f"{question}\n\nRelevant data:\n{context}"
            }],
        )
        return response.content[0].text


async def demo():
    # Simulate a bloated web search result
    raw_search = [
        {
            "title": "OpenAI launches GPT-5",
            "snippet": "OpenAI has announced...",
            "url": "https://example.com/1",
            "published_date": "2026-04-01",
            "cache_url": "https://cache.example.com/...",
            "safe_search_rating": "medium",
            "html_content": "<html>..." * 500,  # 500x repeated — huge
            "language": "en",
            "position": 1,
            "tracking_pixel": "https://t.example.com/px?id=abc",
        }
        for _ in range(10)
    ]

    agent = FieldExtractionAgent(api_key="sk-...")
    reply = await agent.answer_with_tool(
        "What's the latest news about AI models?",
        "web_search",
        raw_search,
    )
    print(reply[:100])

# asyncio.run(demo())
```

---

## Solution 2: Array Truncation with Item Count Cap

Cap the number of items in list results; include a summary count so the LLM knows data was truncated.

```python
import asyncio
import json
from typing import Any, Optional
import anthropic


def truncate_list_result(
    data: Any,
    max_items: int = 10,
    summary_key: str = "_summary",
) -> Any:
    """
    If data is a list, keep first max_items and append a summary.
    Nested lists are also truncated recursively.
    """
    if isinstance(data, list):
        total = len(data)
        truncated = [truncate_list_result(item, max_items) for item in data[:max_items]]
        if total > max_items:
            truncated.append({
                summary_key: f"... {total - max_items} more items not shown (total: {total})"
            })
        return truncated
    if isinstance(data, dict):
        return {k: truncate_list_result(v, max_items) for k, v in data.items()}
    return data


TOOL_ITEM_CAPS: dict[str, int] = {
    "web_search":     5,
    "database_query": 10,
    "list_files":     20,
    "search_users":   8,
    "get_commits":    15,
}


class TruncatingAgent:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)

    async def run(
        self,
        question: str,
        tool_name: str,
        raw_result: Any,
    ) -> str:
        cap = TOOL_ITEM_CAPS.get(tool_name, 10)
        slimmed = truncate_list_result(raw_result, max_items=cap)

        orig_size = len(json.dumps(raw_result)) // 4
        slim_size = len(json.dumps(slimmed)) // 4
        print(f"[truncate] {tool_name}: {orig_size} → {slim_size} tokens (cap={cap})")

        response = await self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": f"{question}\n\nData:\n{json.dumps(slimmed, indent=2)[:2000]}"
            }],
        )
        return response.content[0].text


async def demo():
    agent = TruncatingAgent(api_key="sk-...")

    # 500 database rows — truncated to 10
    raw_rows = [{"id": i, "name": f"User {i}", "email": f"u{i}@example.com",
                 "created_at": "2026-01-01", "last_login": "2026-04-01",
                 "metadata": {"key": "value" * 20}} for i in range(500)]

    reply = await agent.run("How many users are there?", "database_query", raw_rows)
    print(reply[:100])

# asyncio.run(demo())
```

---

## Solution 3: LLM-Assisted Summarization for Complex Results

Use a cheap, fast model call to summarize complex tool results before injecting them into the main context.

```python
import asyncio
import json
from typing import Any
import anthropic


async def llm_summarize_tool_result(
    client: anthropic.AsyncAnthropic,
    tool_name: str,
    raw_result: Any,
    question_context: str,
    max_summary_tokens: int = 200,
) -> str:
    """Use claude-haiku to summarize a large tool result into a dense digest."""
    raw_json = json.dumps(raw_result)[:8000]  # Cap input to summarizer

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",  # Cheap summarizer
        max_tokens=max_summary_tokens,
        system=(
            "You are a data summarizer. Extract only the facts directly relevant "
            "to answering the user's question. Be extremely concise. No preamble."
        ),
        messages=[{
            "role": "user",
            "content": (
                f"Question context: {question_context}\n\n"
                f"Tool: {tool_name}\n"
                f"Raw result:\n{raw_json}\n\n"
                "Summarize the key facts in 2-4 sentences."
            ),
        }],
    )
    return response.content[0].text


class SummarizingAgent:
    def __init__(self, api_key: str, summary_threshold_tokens: int = 500):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.threshold = summary_threshold_tokens

    async def process_tool_result(
        self,
        tool_name: str,
        raw_result: Any,
        question: str,
    ) -> str:
        raw_tokens = len(json.dumps(raw_result)) // 4

        if raw_tokens > self.threshold:
            print(f"[summarize] {tool_name}: {raw_tokens} tokens → summarizing with Haiku")
            summary = await llm_summarize_tool_result(
                self.client, tool_name, raw_result, question
            )
            return f"[Summarized {tool_name} result ({raw_tokens} tokens → {len(summary)//4}t)]: {summary}"
        else:
            return json.dumps(raw_result)

    async def answer(self, question: str, tool_name: str, raw_result: Any) -> str:
        context = await self.process_tool_result(tool_name, raw_result, question)

        response = await self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": f"{question}\n\nContext:\n{context}"
            }],
        )
        return response.content[0].text


async def demo():
    agent = SummarizingAgent(api_key="sk-...", summary_threshold_tokens=200)

    # Large result that will be summarized
    large_result = {
        "analytics": [
            {"date": f"2026-04-{i:02d}", "sessions": i * 100, "revenue": i * 50,
             "bounce_rate": 0.4 + i * 0.01, "page_views": i * 300,
             "avg_session_duration": 120 + i * 5}
            for i in range(1, 31)  # 30 days of data
        ]
    }

    reply = await agent.answer(
        "What was the revenue trend last month?",
        "analytics",
        large_result,
    )
    print(reply[:150])

# asyncio.run(demo())
```

---

## Solution 4: Structured Digest Format

Instead of raw JSON, convert tool results into a compact markdown digest format that conveys the same information with fewer tokens.

```python
import asyncio
import json
from typing import Any
import anthropic


def to_markdown_digest(tool_name: str, data: Any, max_items: int = 5) -> str:
    """Convert tool result to compact markdown instead of raw JSON."""
    lines = [f"**{tool_name} results:**"]

    if isinstance(data, list):
        total = len(data)
        for i, item in enumerate(data[:max_items]):
            if isinstance(item, dict):
                # Key=value pairs on one line
                kv = " | ".join(f"{k}: {str(v)[:50]}" for k, v in item.items()
                                if v is not None and str(v).strip())
                lines.append(f"- {kv}")
            else:
                lines.append(f"- {str(item)[:100]}")
        if total > max_items:
            lines.append(f"*... and {total - max_items} more items*")

    elif isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, list):
                lines.append(f"- **{k}**: {len(v)} items")
            elif isinstance(v, dict):
                lines.append(f"- **{k}**: {json.dumps(v)[:80]}")
            else:
                lines.append(f"- **{k}**: {str(v)[:100]}")

    else:
        lines.append(str(data)[:500])

    return "\n".join(lines)


def compare_formats(tool_name: str, data: Any) -> dict:
    raw_json = json.dumps(data)
    markdown = to_markdown_digest(tool_name, data)
    return {
        "json_tokens": len(raw_json) // 4,
        "markdown_tokens": len(markdown) // 4,
        "reduction_pct": 100 - 100 * len(markdown) // max(len(raw_json), 1),
    }


class DigestAgent:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)

    async def answer(
        self,
        question: str,
        tool_name: str,
        raw_result: Any,
        use_digest: bool = True,
    ) -> str:
        if use_digest:
            context = to_markdown_digest(tool_name, raw_result)
            stats = compare_formats(tool_name, raw_result)
            print(f"[digest] {stats['json_tokens']} → {stats['markdown_tokens']} tokens "
                  f"({stats['reduction_pct']}% reduction)")
        else:
            context = json.dumps(raw_result)[:3000]

        response = await self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": f"{question}\n\n{context}"
            }],
        )
        return response.content[0].text


async def demo():
    agent = DigestAgent(api_key="sk-...")

    # 50 search results
    search_results = [
        {
            "title": f"Article {i}: AI Advances in {2020 + i}",
            "url": f"https://news.example.com/ai-{i}",
            "snippet": "Researchers have found that " + "AI is improving " * 10,
            "published_date": f"2026-{(i % 12) + 1:02d}-01",
            "source": "TechNews",
            "relevance_score": 0.95 - i * 0.01,
            "cached_html": "<html>" + "content " * 200 + "</html>",
        }
        for i in range(50)
    ]

    reply = await agent.answer("What are the latest AI developments?", "web_search", search_results)
    print(reply[:150])

# asyncio.run(demo())
```

---

## Solution 5: Incremental Result Injection with Token Budget Tracking

Inject tool results one page at a time, tracking cumulative token usage and stopping when the budget is 80% consumed.

```python
import asyncio
import json
from dataclasses import dataclass
from typing import Any, Iterator
import anthropic


def paginate(data: Any, page_size: int = 20) -> Iterator[Any]:
    """Yield chunks of a list result."""
    if isinstance(data, list):
        for i in range(0, len(data), page_size):
            yield data[i:i + page_size]
    else:
        yield data


@dataclass
class InjectionBudget:
    max_tokens: int = 50_000
    used: int = 0

    @property
    def remaining(self) -> int:
        return max(0, self.max_tokens - self.used)

    @property
    def fraction_used(self) -> float:
        return self.used / self.max_tokens

    def consume(self, tokens: int) -> None:
        self.used += tokens

    def can_inject(self, tokens: int, threshold: float = 0.80) -> bool:
        return self.fraction_used < threshold and (self.used + tokens) <= self.max_tokens


class IncrementalInjectionAgent:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)

    async def answer_with_large_result(
        self,
        question: str,
        tool_name: str,
        raw_result: Any,
        budget: InjectionBudget,
        page_size: int = 20,
    ) -> str:
        injected_pages = []
        pages_injected = 0
        pages_skipped = 0

        for page in paginate(raw_result, page_size):
            page_json = json.dumps(page)
            page_tokens = len(page_json) // 4

            if budget.can_inject(page_tokens):
                injected_pages.append(page_json)
                budget.consume(page_tokens)
                pages_injected += 1
            else:
                pages_skipped += 1

        print(
            f"[incremental] {tool_name}: {pages_injected} pages injected, "
            f"{pages_skipped} skipped. Budget: {budget.fraction_used:.0%}"
        )

        context = f"[{tool_name}]\n" + "\n".join(injected_pages[:2000])
        if pages_skipped > 0:
            context += f"\n[Note: {pages_skipped} additional pages omitted due to token budget]"

        response = await self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=512,
            messages=[{"role": "user", "content": f"{question}\n\n{context[:3000]}"}],
        )
        return response.content[0].text


async def demo():
    agent = IncrementalInjectionAgent(api_key="sk-...")
    budget = InjectionBudget(max_tokens=5000)  # Tight budget

    large_data = [{"id": i, "data": "x" * 50} for i in range(1000)]
    reply = await agent.answer_with_large_result(
        "Summarize the data", "database_query", large_data, budget
    )
    print(reply[:100])

# asyncio.run(demo())
```

---

## Solution 6: Schema-Aware JSON Minification

Remove whitespace, shorten field names, and drop null/empty values from tool results before serialization.

```python
import asyncio
import json
from typing import Any, Optional
import anthropic


# Field name abbreviation maps per tool (full_name → short_name)
FIELD_ABBREVIATIONS: dict[str, dict[str, str]] = {
    "database_query": {
        "created_at": "cat",
        "updated_at": "uat",
        "user_id": "uid",
        "session_id": "sid",
        "description": "desc",
        "status": "st",
        "organization_id": "oid",
    },
    "web_search": {
        "published_date": "dt",
        "relevance_score": "score",
        "snippet": "snip",
        "source_name": "src",
    },
}


def minify_result(
    data: Any,
    tool_name: Optional[str] = None,
    drop_nulls: bool = True,
    abbreviate: bool = True,
) -> Any:
    """Strip whitespace, nulls, empty strings; optionally abbreviate field names."""
    abbrev_map = FIELD_ABBREVIATIONS.get(tool_name or "", {}) if abbreviate else {}

    if isinstance(data, dict):
        result = {}
        for k, v in data.items():
            if drop_nulls and (v is None or v == "" or v == [] or v == {}):
                continue
            new_key = abbrev_map.get(k, k)
            result[new_key] = minify_result(v, tool_name, drop_nulls, abbreviate)
        return result

    if isinstance(data, list):
        return [minify_result(item, tool_name, drop_nulls, abbreviate) for item in data]

    if isinstance(data, float):
        return round(data, 4)  # Trim excessive decimal places

    return data


def minify_to_json(data: Any, tool_name: Optional[str] = None) -> str:
    """Minified JSON string: no whitespace, no nulls, abbreviated keys."""
    minified = minify_result(data, tool_name)
    return json.dumps(minified, separators=(",", ":"))  # No spaces in separators


class MinifyingAgent:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)

    async def answer(
        self,
        question: str,
        tool_name: str,
        raw_result: Any,
    ) -> str:
        # Pretty JSON (what agents typically use)
        pretty = json.dumps(raw_result, indent=2)
        # Minified (our approach)
        mini = minify_to_json(raw_result, tool_name)

        pretty_tokens = len(pretty) // 4
        mini_tokens = len(mini) // 4
        print(
            f"[minify] {tool_name}: {pretty_tokens} → {mini_tokens} tokens "
            f"({100 - 100*mini_tokens//max(pretty_tokens,1)}% reduction)"
        )

        response = await self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": f"{question}\n\nData: {mini[:2000]}"
            }],
        )
        return response.content[0].text


async def demo():
    agent = MinifyingAgent(api_key="sk-...")

    raw = [
        {
            "user_id": f"usr_{i}",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-04-16T12:00:00Z",
            "description": None,
            "status": "active",
            "organization_id": "org_abc",
            "metadata": None,
            "tags": [],
            "score": 0.9876543210,
        }
        for i in range(50)
    ]

    reply = await agent.answer("Who are the most active users?", "database_query", raw)
    print(reply[:100])

# asyncio.run(demo())
```

---

## Comparison

| Solution | Mechanism | Token Reduction | Quality Impact | Complexity | Best For |
|---|---|---|---|---|---|
| Field extraction | Keep needed keys only | 50-90% | None | Very Low | Known schema APIs |
| Array truncation | Cap list length | 50-95% | Minor (data loss) | Very Low | Large list results |
| LLM summarization | Haiku-powered digest | 70-95% | Very Low | Medium | Unstructured results |
| Markdown digest | Compact text format | 40-70% | None | Low | Tabular data |
| Incremental injection | Budget-aware paging | Variable | Minor | Medium | Very large results |
| JSON minification | Nulls + abbreviation | 20-50% | None | Low | Any JSON |

**Recommendation:** Apply Solution 1 (field extraction) as a mandatory pre-processing step for every tool with a known schema — it's a config table + one function call that eliminates 50-90% of tool result tokens for free. Add Solution 2 (truncation) for any tool that returns lists. Use Solution 3 (LLM summarization) only when the result is unstructured (HTML, free-form text) and you can afford the extra Haiku call.
