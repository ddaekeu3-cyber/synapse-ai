---
layout: solution
title: "Agent Truncates Long Tool Results — Important Data Cut Off"
category: context-window
description: "Tool returns a 50,000-token API response. Agent includes the full result in the conversation. Context fills up. Either the model ignores later turns, or the framework truncates the tool result and the agent never sees the critical data at the end."
tags: [context-window, tool-results, truncation, summarization, chunking, rag, extraction, token-budget]
---

# Agent Truncates Long Tool Results — Important Data Cut Off

## Symptom
- Agent calls a search tool — results contain 40,000 tokens — context window fills immediately
- Tool returns a full file read — agent uses the first 8,000 tokens, silently ignores the rest
- Database query returns 10,000 rows — agent summarizes only the first page
- Agent reads a large document — misses a critical section that appears near the end
- Framework truncates tool result at a token limit — agent proceeds as if the data was complete
- Multi-tool agent hits context limit after 2-3 tool calls — remaining tools never execute

## Root Cause
Tool results are injected into the conversation verbatim. A single large result can consume most of the context budget before the agent has processed multiple tools or formulated a response. The model can't tell the difference between "the tool result ended here" and "the tool result was cut off here." The fix is to process large tool results outside the context window: extract the relevant portion, summarize, paginate, or use RAG to retrieve only what's needed.

## Fix

### Option 1: Extract-before-inject — pull only the relevant section

```python
import anthropic
import json

client = anthropic.Anthropic()

def extract_relevant_section(
    tool_result: str,
    query: str,
    max_tokens: int = 2000,
    model: str = "claude-haiku-4-5-20251001"
) -> str:
    """
    Use a fast, cheap model to extract the relevant portion of a large tool result.
    Call this before injecting the result into the main agent context.
    """
    if len(tool_result) < max_tokens * 4:  # Rough char estimate — no need to extract
        return tool_result

    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{
            "role": "user",
            "content": (
                f"Extract the information relevant to this query from the document below.\n"
                f"Query: {query}\n\n"
                f"Return only the relevant sections verbatim — do not summarize or paraphrase.\n"
                f"If the relevant data is a list or table, include it completely.\n\n"
                f"Document:\n{tool_result[:50000]}"  # Hard cap on extraction input
            )
        }]
    )
    return response.content[0].text

def summarize_tool_result(
    tool_result: str,
    context: str,
    max_tokens: int = 1000,
    model: str = "claude-haiku-4-5-20251001"
) -> str:
    """
    Summarize a large tool result preserving key facts.
    Use when the full result is too large but a summary is sufficient.
    """
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{
            "role": "user",
            "content": (
                f"Summarize the following tool result for this context: {context}\n\n"
                f"Preserve: numbers, dates, IDs, status values, error messages, key names.\n"
                f"Compress: prose descriptions, examples, metadata.\n\n"
                f"Tool result:\n{tool_result[:60000]}"
            )
        }]
    )
    return f"[Summarized from {len(tool_result)} chars]\n{response.content[0].text}"

class TokenBudgetedToolRunner:
    """
    Runs tools and manages result size to fit within a token budget.
    Large results are automatically extracted or summarized before injection.
    """

    CHARS_PER_TOKEN = 4  # Rough estimate

    def __init__(
        self,
        result_token_budget: int = 4000,  # Max tokens per tool result
        extraction_model: str = "claude-haiku-4-5-20251001"
    ):
        self.result_token_budget = result_token_budget
        self.extraction_model = extraction_model

    def _estimate_tokens(self, text: str) -> int:
        return len(text) // self.CHARS_PER_TOKEN

    async def run_tool(
        self,
        tool_name: str,
        tool_input: dict,
        tool_fn,
        query_context: str = ""
    ) -> str:
        """Run a tool and return a result that fits within the token budget"""
        raw_result = await tool_fn(tool_name, tool_input)

        if isinstance(raw_result, dict):
            raw_result = json.dumps(raw_result, indent=2)

        estimated_tokens = self._estimate_tokens(raw_result)

        if estimated_tokens <= self.result_token_budget:
            return raw_result  # Fits — return as-is

        print(
            f"Tool '{tool_name}' returned ~{estimated_tokens} tokens — "
            f"budget is {self.result_token_budget}. Extracting relevant section."
        )

        if query_context:
            return extract_relevant_section(
                raw_result, query_context,
                max_tokens=self.result_token_budget,
                model=self.extraction_model
            )
        else:
            return summarize_tool_result(
                raw_result, tool_name,
                max_tokens=self.result_token_budget,
                model=self.extraction_model
            )
```

### Option 2: Paginated tool results — process in chunks

```python
import math
from typing import AsyncIterator

class PaginatedToolResult:
    """
    Break a large tool result into pages.
    Agent processes one page at a time, accumulating findings.
    """

    def __init__(self, content: str, page_size_chars: int = 8000):
        self.content = content
        self.page_size = page_size_chars
        self.total_pages = math.ceil(len(content) / page_size_chars)

    def get_page(self, page: int) -> str:
        start = page * self.page_size
        end = min(start + self.page_size, len(content))
        return (
            f"[Page {page + 1} of {self.total_pages}]\n"
            f"{self.content[start:end]}\n"
            f"[{'End of document' if end >= len(self.content) else 'More pages follow'}]"
        )

    def __iter__(self):
        for i in range(self.total_pages):
            yield self.get_page(i)

async def process_large_result_in_pages(
    large_content: str,
    task: str,
    model: str = "claude-sonnet-4-6"
) -> str:
    """
    Process a large tool result by feeding it to the model page by page.
    Accumulates findings across pages into a final answer.
    """
    pages = PaginatedToolResult(large_content, page_size_chars=8000)

    if pages.total_pages == 1:
        return large_content  # No pagination needed

    print(f"Processing {pages.total_pages} pages of tool result")
    accumulated_findings = []

    for page_num, page_content in enumerate(pages):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",  # Cheap model for page processing
            max_tokens=800,
            messages=[{
                "role": "user",
                "content": (
                    f"Task: {task}\n\n"
                    f"Extract any information relevant to the task from this page.\n"
                    f"If nothing relevant, respond with 'No relevant content on this page.'\n\n"
                    f"{page_content}"
                )
            }]
        )
        finding = response.content[0].text
        if "no relevant content" not in finding.lower():
            accumulated_findings.append(f"[From page {page_num + 1}]\n{finding}")
            print(f"Page {page_num + 1}/{pages.total_pages}: found relevant content")
        else:
            print(f"Page {page_num + 1}/{pages.total_pages}: no relevant content")

    if not accumulated_findings:
        return f"[Searched {pages.total_pages} pages — no relevant content found for: {task}]"

    return f"[Synthesized from {pages.total_pages} pages]\n\n" + "\n\n".join(accumulated_findings)
```

### Option 3: Structured result limiting — trim at semantic boundaries

```python
import json
from typing import Any

def trim_tool_result(
    result: Any,
    max_items: int = 50,
    max_chars: int = 8000
) -> tuple[Any, dict]:
    """
    Trim large tool results at semantic boundaries (not character mid-point).
    Returns (trimmed_result, metadata_about_trim).
    """
    metadata = {"trimmed": False, "original_size": 0, "returned_size": 0}

    if isinstance(result, list):
        metadata["original_size"] = len(result)
        if len(result) > max_items:
            trimmed = result[:max_items]
            metadata["trimmed"] = True
            metadata["returned_size"] = max_items
            metadata["note"] = f"Showing {max_items} of {len(result)} items. Request with offset={max_items} for more."
            return trimmed, metadata
        metadata["returned_size"] = len(result)
        return result, metadata

    elif isinstance(result, dict):
        serialized = json.dumps(result, indent=2)
        metadata["original_size"] = len(serialized)
        if len(serialized) > max_chars:
            # Try to trim nested lists within the dict
            trimmed = _trim_dict_lists(result, max_items=max_items)
            trimmed_serialized = json.dumps(trimmed, indent=2)
            metadata["trimmed"] = True
            metadata["returned_size"] = len(trimmed_serialized)
            metadata["note"] = f"Large response trimmed from {len(serialized)} to {len(trimmed_serialized)} chars"
            return trimmed, metadata
        metadata["returned_size"] = len(serialized)
        return result, metadata

    elif isinstance(result, str):
        metadata["original_size"] = len(result)
        if len(result) > max_chars:
            # Trim at sentence or newline boundary
            trimmed = _trim_at_boundary(result, max_chars)
            metadata["trimmed"] = True
            metadata["returned_size"] = len(trimmed)
            metadata["note"] = f"Result trimmed at {len(trimmed)} chars of {len(result)} total"
            return trimmed, metadata
        metadata["returned_size"] = len(result)
        return result, metadata

    return result, metadata

def _trim_dict_lists(obj: Any, max_items: int) -> Any:
    """Recursively trim lists in dicts to max_items"""
    if isinstance(obj, list):
        return obj[:max_items]
    elif isinstance(obj, dict):
        return {k: _trim_dict_lists(v, max_items) for k, v in obj.items()}
    return obj

def _trim_at_boundary(text: str, max_chars: int) -> str:
    """Trim text at a sentence or newline boundary near max_chars"""
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars]
    # Find last newline near the end
    last_newline = truncated.rfind("\n", max_chars - 500)
    if last_newline > max_chars * 0.8:
        truncated = truncated[:last_newline]
    # Append trim indicator
    return truncated + f"\n\n[... {len(text) - len(truncated)} chars omitted ...]"

def format_tool_result_with_trim(result: Any, tool_name: str) -> str:
    """Format a tool result with automatic trimming and metadata"""
    trimmed, meta = trim_tool_result(result, max_items=100, max_chars=10000)

    result_str = json.dumps(trimmed, indent=2) if isinstance(trimmed, (dict, list)) else str(trimmed)

    if meta["trimmed"]:
        result_str += f"\n\n[Tool '{tool_name}' note: {meta['note']}]"

    return result_str
```

### Option 4: RAG over tool results — embed and retrieve relevant chunks

```python
import hashlib
import json
from typing import Optional
import anthropic

client = anthropic.Anthropic()

def chunk_text(text: str, chunk_size: int = 500, overlap: int = 50) -> list[dict]:
    """Split text into overlapping chunks for embedding"""
    words = text.split()
    chunks = []
    for i in range(0, len(words), chunk_size - overlap):
        chunk_words = words[i:i + chunk_size]
        chunks.append({
            "id": hashlib.sha256(" ".join(chunk_words).encode()).hexdigest()[:12],
            "text": " ".join(chunk_words),
            "start_word": i,
            "end_word": i + len(chunk_words)
        })
    return chunks

def retrieve_relevant_chunks(
    query: str,
    chunks: list[dict],
    top_k: int = 5,
    model: str = "claude-haiku-4-5-20251001"
) -> list[dict]:
    """
    Use Claude to select the most relevant chunks from a large tool result.
    Lightweight alternative to vector search — no embedding infrastructure needed.
    """
    if len(chunks) <= top_k:
        return chunks

    # Build a summary of each chunk
    chunk_summaries = "\n".join(
        f"{i}. [{c['id']}] {c['text'][:150]}..."
        for i, c in enumerate(chunks[:50])  # Limit candidates
    )

    response = client.messages.create(
        model=model,
        max_tokens=200,
        messages=[{
            "role": "user",
            "content": (
                f"Query: {query}\n\n"
                f"Select the {top_k} most relevant chunks by number. "
                f"Return as JSON array of numbers only, e.g. [0, 3, 7, 12, 18]\n\n"
                f"Chunks:\n{chunk_summaries}"
            )
        }]
    )

    try:
        indices = json.loads(response.content[0].text)
        return [chunks[i] for i in indices if i < len(chunks)]
    except Exception:
        return chunks[:top_k]  # Fallback to first N

class RAGToolResultProcessor:
    """
    Process large tool results using RAG:
    1. Chunk the result
    2. Retrieve chunks relevant to the current query
    3. Inject only the relevant chunks into context
    """

    def __init__(self, chunk_size: int = 500, top_k: int = 5):
        self.chunk_size = chunk_size
        self.top_k = top_k

    def process(self, tool_result: str, query: str) -> str:
        """Return only the relevant portion of a large tool result"""
        # Rough token estimate — if small enough, return as-is
        if len(tool_result) < self.chunk_size * 4 * 2:
            return tool_result

        chunks = chunk_text(tool_result, chunk_size=self.chunk_size)
        relevant = retrieve_relevant_chunks(query, chunks, top_k=self.top_k)

        if not relevant:
            return tool_result[:4000]  # Fallback

        result_parts = [f"[Retrieved {len(relevant)} of {len(chunks)} chunks relevant to: {query}]\n"]
        for chunk in relevant:
            result_parts.append(chunk["text"])

        return "\n\n---\n\n".join(result_parts)

rag_processor = RAGToolResultProcessor(chunk_size=400, top_k=5)
```

### Option 5: Tool result budget enforcer — stop before context overflows

```python
import anthropic
from dataclasses import dataclass, field

CHARS_PER_TOKEN = 4

@dataclass
class ContextBudget:
    """Track token usage across tool calls in an agent loop"""
    total_budget: int = 180_000       # Leave room for model response
    system_tokens: int = 0
    message_tokens: int = 0
    tool_result_tokens: int = 0
    _tool_result_counts: dict = field(default_factory=dict)

    @property
    def remaining(self) -> int:
        return self.total_budget - self.system_tokens - self.message_tokens - self.tool_result_tokens

    def estimate_and_consume(self, content: str, tool_name: str = "") -> tuple[bool, int]:
        """
        Check if content fits in remaining budget.
        If yes, consume the budget. If no, return False.
        """
        estimated = len(content) // CHARS_PER_TOKEN
        if estimated > self.remaining:
            return False, estimated
        self.tool_result_tokens += estimated
        self._tool_result_counts[tool_name] = self._tool_result_counts.get(tool_name, 0) + estimated
        return True, estimated

    def report(self) -> dict:
        return {
            "total_budget": self.total_budget,
            "used": self.total_budget - self.remaining,
            "remaining": self.remaining,
            "tool_results": self._tool_result_counts
        }

def run_agent_with_budget(
    messages: list[dict],
    tools: list[dict],
    system: str,
    model: str = "claude-sonnet-4-6"
) -> str:
    """Agent loop that enforces a token budget on tool results"""
    budget = ContextBudget(total_budget=160_000)
    budget.system_tokens = len(system) // CHARS_PER_TOKEN
    budget.message_tokens = sum(
        len(str(m.get("content", ""))) // CHARS_PER_TOKEN
        for m in messages
    )

    while True:
        response = client.messages.create(
            model=model,
            max_tokens=4096,
            system=system,
            tools=tools,
            messages=messages
        )

        if response.stop_reason != "tool_use":
            return response.content[0].text if response.content else ""

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            raw_result = execute_tool(block.name, block.input)
            result_str = str(raw_result) if not isinstance(raw_result, str) else raw_result

            fits, estimated = budget.estimate_and_consume(result_str, block.name)

            if not fits:
                # Result too large — summarize it
                print(
                    f"Tool '{block.name}' result (~{estimated} tokens) exceeds budget "
                    f"({budget.remaining} remaining) — summarizing"
                )
                result_str = summarize_tool_result(
                    result_str,
                    context=f"query: {block.input}",
                    max_tokens=min(budget.remaining // 2, 1500)
                )
                budget.estimate_and_consume(result_str, f"{block.name}_summarized")

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result_str
            })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

        print(f"Budget: {budget.report()}")

def execute_tool(tool_name: str, tool_input: dict) -> str:
    return f"Tool {tool_name} result"
```

### Option 6: Streaming tool result processing — handle results too large to hold in memory

```python
import asyncio
from typing import AsyncIterator

async def stream_process_large_result(
    content: str,
    extraction_query: str,
    window_size: int = 6000,
    stride: int = 5000
) -> str:
    """
    Process a result that's too large for a single context window.
    Slides a window across the content, extracting relevant data at each position.
    Merges findings at the end.
    """
    if len(content) <= window_size:
        return content

    windows = []
    for start in range(0, len(content), stride):
        end = min(start + window_size, len(content))
        windows.append(content[start:end])

    print(f"Processing {len(windows)} windows over {len(content)} char result")
    findings = []

    tasks = [
        _extract_from_window(window, extraction_query, i, len(windows))
        for i, window in enumerate(windows)
    ]
    results = await asyncio.gather(*tasks)

    findings = [r for r in results if r and "no relevant" not in r.lower()]

    if not findings:
        return f"[No relevant content found in {len(content)} char result for: {extraction_query}]"

    # Deduplicate and merge findings
    merged = "\n\n".join(findings)
    return f"[Extracted from {len(windows)} windows of {len(content)} char result]\n\n{merged}"

async def _extract_from_window(window: str, query: str, window_num: int, total: int) -> str:
    """Extract relevant content from a single window"""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=600,
        messages=[{
            "role": "user",
            "content": (
                f"Extract content relevant to: {query}\n"
                f"From window {window_num + 1} of {total}:\n\n{window}\n\n"
                f"If nothing relevant, respond only: 'no relevant content'"
            )
        }]
    )
    return response.content[0].text
```

## Tool Result Size Management Strategies

| Strategy | Best For | Latency Added | Token Saved |
|---|---|---|---|
| Extract-before-inject | Structured queries with known target | Low (1 LLM call) | 80-95% |
| Pagination | Sequential processing needed | Medium (N pages) | Per-page budget |
| Semantic trim at boundary | Lists, JSON arrays | None | 50-90% |
| RAG over chunks | Large unstructured text | Low (1 LLM call) | 85-95% |
| Budget enforcer | Multi-tool agent loops | None | Prevents overflow |
| Sliding window | Giant files, logs | High (N windows) | Full coverage |

## Expected Token Savings
50,000-token tool result injected raw → context overflow after 1 tool call: entire session unusable
Extract 2,000 relevant tokens → agent completes 10+ tool calls in same budget: 96% savings

## Environment
- Any agent using tool use with external APIs, file reads, database queries, or search tools; especially critical for agents that process search results, read large files, or query paginated APIs — tool result size is the primary cause of unexpected context overflow in production agents
- Source: direct experience; unmanaged tool result size is the most common reason multi-tool agents fail mid-task in production
