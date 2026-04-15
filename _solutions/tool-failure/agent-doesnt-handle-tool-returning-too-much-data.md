---
layout: solution
title: "Agent Doesn't Handle Tool Returning Too Much Data"
category: tool-failure
description: "Agent passes full tool results directly into context — database queries returning 10,000 rows, file reads returning entire codebases, search results returning full HTML pages — causing context overflow, truncation, and degraded reasoning quality."
tags: [tool-failure, context-window, truncation, pagination, summarization, token-cost]
---

## Symptom

The agent calls a database query tool and receives 50,000 tokens of raw results. It stuffs all of them into the next message, causing a 400 context-too-long error. Alternatively, the SDK silently truncates the results and the agent reasons about an incomplete dataset, producing wrong conclusions. A web search tool returns full HTML pages instead of relevant snippets. A file read tool returns 3,000 lines of code when the agent only needed 20 lines around a specific function.

## Root Cause

Tool implementations are often written to return complete data ("give me everything, the caller can filter"). Agent wrappers pass tool results directly to the next LLM call without checking size. The Anthropic API has a 200K-token context limit, and tool results count toward it. When tool results are large, they crowd out conversation history, system prompts, and previous reasoning, degrading the model's ability to maintain context about the original task.

## Fix

### Option 1 — Truncate tool results with a size guard before injecting into context

```python
import json
import anthropic

client = anthropic.Anthropic()

MAX_TOOL_RESULT_TOKENS = 2000   # conservative limit per tool result

def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)

def truncate_tool_result(result: str, max_tokens: int = MAX_TOOL_RESULT_TOKENS,
                          note_template: str = "\n\n[TRUNCATED: {kept}/{total} tokens shown. Use pagination to fetch more.]") -> str:
    """Truncate tool result and append a clear truncation notice."""
    if estimate_tokens(result) <= max_tokens:
        return result
    max_chars = max_tokens * 4
    truncated = result[:max_chars]
    kept      = estimate_tokens(truncated)
    total     = estimate_tokens(result)
    note      = note_template.format(kept=kept, total=total)
    return truncated + note

TOOLS = [
    {
        "name": "query_database",
        "description": "Run a SQL query. Results are automatically truncated to 2000 tokens.",
        "input_schema": {
            "type": "object",
            "required": ["sql"],
            "properties": {"sql": {"type": "string"}},
        },
    },
    {
        "name": "read_file",
        "description": "Read a file. Results are truncated if very large.",
        "input_schema": {
            "type": "object",
            "required": ["path"],
            "properties": {
                "path":       {"type": "string"},
                "start_line": {"type": "integer", "description": "First line to read (1-indexed)"},
                "num_lines":  {"type": "integer", "description": "Number of lines to read"},
            },
        },
    },
]

def simulate_query_database(sql: str) -> str:
    """Simulate a database returning many rows."""
    rows = [f"row_{i}: user_{i}@example.com, status=active, created=2024-0{(i%9)+1}-01" for i in range(500)]
    return "\n".join(rows)

def simulate_read_file(path: str, start_line: int = 1, num_lines: int | None = None) -> str:
    """Simulate reading a large file with optional line range."""
    import os
    all_lines = [f"{i+1}: def function_{i}(): pass  # auto-generated" for i in range(1000)]
    if num_lines:
        selected = all_lines[start_line-1:start_line-1+num_lines]
    else:
        selected = all_lines
    return "\n".join(selected)

def handle_tool(name: str, inputs: dict) -> str:
    if name == "query_database":
        raw = simulate_query_database(inputs["sql"])
    elif name == "read_file":
        raw = simulate_read_file(
            inputs["path"],
            inputs.get("start_line", 1),
            inputs.get("num_lines"),
        )
    else:
        raw = json.dumps({"error": f"unknown tool: {name}"})

    truncated = truncate_tool_result(raw)
    orig_tok  = estimate_tokens(raw)
    kept_tok  = estimate_tokens(truncated)
    print(f"  [{name}] {orig_tok} → {kept_tok} tokens (truncated={orig_tok > MAX_TOOL_RESULT_TOKENS})")
    return truncated

def run_agent(question: str) -> str:
    messages = [{"role": "user", "content": question}]
    for _ in range(6):
        r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=TOOLS,
            messages=messages,
        )
        if r.stop_reason == "end_turn":
            return next((b.text for b in r.content if b.type == "text"), "")
        messages.append({"role": "assistant", "content": r.content})
        results = []
        for b in r.content:
            if b.type == "tool_use":
                result = handle_tool(b.name, b.input)
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": result})
        messages.append({"role": "user", "content": results})
    return "max steps reached"

print(f"A: {run_agent('How many users are in the database?')[:200]}")
```

**Expected Token Savings:** Truncating a 50,000-token database result to 2,000 tokens saves 48,000 input tokens per tool call — at $3/MTok, that's $0.144 per call; for 1,000 daily calls, truncation saves $144/day.
**Environment:** All agents with data-returning tools; result truncation is the minimum viable protection against context overflow from large tool outputs.

---

### Option 2 — Paginated tool interface: agent fetches data in pages

```python
import json
import anthropic

client = anthropic.Anthropic()

# Simulated large dataset
_DB = [{"id": i, "email": f"user{i}@example.com", "status": "active" if i % 3 else "inactive"}
       for i in range(1000)]

TOOLS = [
    {
        "name": "query_users",
        "description": "Query users with optional filters. Returns paginated results — always check 'has_more' and use 'page' to fetch subsequent pages.",
        "input_schema": {
            "type": "object",
            "properties": {
                "status":  {"type": "string", "enum": ["active", "inactive", "all"], "description": "Filter by status"},
                "page":    {"type": "integer", "description": "Page number (1-indexed)", "default": 1},
                "per_page":{"type": "integer", "description": "Results per page (max 20)", "default": 10},
            },
        },
    },
]

def query_users(status: str = "all", page: int = 1, per_page: int = 10) -> dict:
    per_page = min(per_page, 20)   # enforce max page size
    filtered = [u for u in _DB if status == "all" or u["status"] == status]
    total    = len(filtered)
    start    = (page - 1) * per_page
    end      = start + per_page
    items    = filtered[start:end]
    return {
        "items":    items,
        "page":     page,
        "per_page": per_page,
        "total":    total,
        "has_more": end < total,
        "pages":    (total + per_page - 1) // per_page,
    }

def handle_tool(name: str, inputs: dict) -> str:
    if name == "query_users":
        result = query_users(**{k: v for k, v in inputs.items()})
        print(f"  [query_users] page={result['page']} items={len(result['items'])} total={result['total']} has_more={result['has_more']}")
        return json.dumps(result)
    return json.dumps({"error": "unknown tool"})

def run_agent(question: str) -> str:
    messages = [{"role": "user", "content": question}]
    for _ in range(12):
        r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=TOOLS,
            messages=messages,
        )
        if r.stop_reason == "end_turn":
            return next((b.text for b in r.content if b.type == "text"), "")
        messages.append({"role": "assistant", "content": r.content})
        results = []
        for b in r.content:
            if b.type == "tool_use":
                result = handle_tool(b.name, b.input)
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": result})
        messages.append({"role": "user", "content": results})
    return "max steps reached"

questions = [
    "How many inactive users are there?",
    "Get the first 5 active users.",
]
for q in questions:
    print(f"Q: {q}")
    print(f"A: {run_agent(q)[:200]}\n")
```

**Expected Token Savings:** Pagination caps each tool result at per_page × row_size tokens; the agent fetches only as many pages as needed to answer the question — for "how many inactive users?", it may only need page 1 plus the total count, never fetching all 1,000 rows.
**Environment:** Agents querying databases, APIs, or any data source with unbounded result sets; pagination is the architectural solution that prevents large results by design.

---

### Option 3 — Summarise tool results before injecting into context

```python
import json
import anthropic

client = anthropic.Anthropic()

SUMMARISE_SYSTEM = """Summarise the following tool result into at most 200 words.
Focus on the information most relevant to answering the question.
Preserve all numbers, counts, and key facts.
Do not include raw data rows — summarise patterns and key findings."""

def summarise_if_large(tool_name: str, result: str, question: str,
                        threshold_tokens: int = 1000) -> str:
    """Summarise large tool results before they enter the main context."""
    if len(result) // 4 <= threshold_tokens:
        return result

    print(f"  [summarise] {tool_name} result too large ({len(result)//4} tok) — summarising")
    prompt = f"Question being answered: {question}\n\nTool result:\n{result[:8000]}"   # cap raw input to summariser
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=SUMMARISE_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    summary = r.content[0].text.strip()
    print(f"  [summarise] {len(result)//4} → {len(summary)//4} tokens")
    return f"[SUMMARY of {tool_name} result]\n{summary}"

TOOLS = [
    {
        "name": "web_search",
        "description": "Search the web and return results.",
        "input_schema": {"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}}},
    },
]

def simulate_web_search(query: str) -> str:
    # Simulate a large HTML response
    return "\n".join([
        f"<html><body><h1>Results for: {query}</h1>",
        *[f"<p>Result {i}: Lorem ipsum dolor sit amet " * 20 + f"keyword_{i}</p>" for i in range(50)],
        "</body></html>",
    ])

_current_question = ""

def run_agent(question: str) -> str:
    global _current_question
    _current_question = question
    messages = [{"role": "user", "content": question}]

    for _ in range(6):
        r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=TOOLS,
            messages=messages,
        )
        if r.stop_reason == "end_turn":
            return next((b.text for b in r.content if b.type == "text"), "")
        messages.append({"role": "assistant", "content": r.content})
        results = []
        for b in r.content:
            if b.type == "tool_use":
                raw    = simulate_web_search(b.input.get("query", ""))
                result = summarise_if_large(b.name, raw, _current_question)
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": result})
        messages.append({"role": "user", "content": results})
    return "max steps reached"

print(f"A: {run_agent('What are the key facts about Python 3.12?')[:300]}")
```

**Expected Token Savings:** Summarising a 5,000-token web search result to 200 tokens saves 4,800 tokens — an LLM summarisation call costs ~300 tokens but produces a result 24× smaller; net saving is ~4,500 tokens per large tool result.
**Environment:** Agents processing web search results, document reads, or any tool returning unstructured prose; LLM summarisation is ideal when the structure of raw data is too complex for simple truncation.

---

### Option 4 — Relevant excerpt extractor: return only task-relevant sections

```python
import re
import json
import anthropic

client = anthropic.Anthropic()

EXTRACT_SYSTEM = """Extract only the sections of the document that are directly relevant to the question.
Return exact quotes from the document — do not paraphrase.
Include at most 500 words of relevant content.
If nothing is relevant, return "No relevant content found." """

def extract_relevant(document: str, question: str, max_raw_chars: int = 20_000) -> str:
    """Extract only the relevant parts of a large document."""
    # First: cheap heuristic — find paragraphs containing query keywords
    keywords = set(question.lower().split()) - {"what", "how", "is", "the", "a", "an", "of", "in", "for"}
    paragraphs = re.split(r"\n{2,}", document)
    relevant_paragraphs = [
        p for p in paragraphs
        if any(kw in p.lower() for kw in keywords)
    ]

    if relevant_paragraphs:
        candidate = "\n\n".join(relevant_paragraphs[:10])
        if len(candidate) // 4 < 500:
            return candidate   # already small enough

    # Fallback: LLM extraction from a capped excerpt
    excerpt = document[:max_raw_chars]
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=EXTRACT_SYSTEM,
        messages=[{"role": "user", "content": f"Question: {question}\n\nDocument:\n{excerpt}"}],
    )
    return r.content[0].text.strip()

TOOLS = [
    {
        "name": "read_file",
        "description": "Read a file. Large files are automatically filtered to relevant sections.",
        "input_schema": {"type": "object", "required": ["path"], "properties": {"path": {"type": "string"}}},
    },
]

def simulate_read_large_file(path: str) -> str:
    sections = []
    sections.append("# Configuration Guide\n\nThis document describes configuration options.")
    sections.append("\n\n## Database Settings\n\nhost: localhost\nport: 5432\nname: mydb\npool_size: 10")
    sections.append("\n\n## " + "\n\n## ".join([f"Section {i}\n\nContent {i}: " + "x " * 200 for i in range(20)]))
    sections.append("\n\n## Logging\n\nlevel: INFO\nformat: json\noutput: /var/log/app.log")
    return "".join(sections)

_question = ""

def run_agent(question: str) -> str:
    global _question
    _question = question
    messages  = [{"role": "user", "content": question}]

    for _ in range(6):
        r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=TOOLS,
            messages=messages,
        )
        if r.stop_reason == "end_turn":
            return next((b.text for b in r.content if b.type == "text"), "")
        messages.append({"role": "assistant", "content": r.content})
        results = []
        for b in r.content:
            if b.type == "tool_use":
                raw     = simulate_read_large_file(b.input.get("path", ""))
                excerpt = extract_relevant(raw, _question)
                print(f"  [extract] {len(raw)//4} → {len(excerpt)//4} tokens")
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": excerpt})
        messages.append({"role": "user", "content": results})
    return "max steps reached"

print(f"A: {run_agent('What is the database port configured in the config file?')[:200]}")
```

**Expected Token Savings:** Keyword-based extraction returns only relevant paragraphs — for a question about "database port" in a 5,000-line config guide, extraction returns 3 paragraphs instead of 5,000 lines, saving ~4,900 tokens; the LLM extraction fallback costs ~300 tokens and handles complex relevance.
**Environment:** Document-reading agents processing long files (configs, docs, code); relevant excerpt extraction is more precise than truncation and more efficient than full summarisation.

---

### Option 5 — Structured tool output: enforce compact schemas instead of raw data

```python
import json
import anthropic

client = anthropic.Anthropic()

# Tools that return structured summaries, not raw data
TOOLS = [
    {
        "name": "get_user_stats",
        "description": "Get aggregate statistics about users — NOT raw user records.",
        "input_schema": {
            "type": "object",
            "properties": {
                "group_by": {"type": "string", "enum": ["status", "plan", "country"], "description": "Grouping dimension"},
            },
        },
    },
    {
        "name": "get_recent_errors",
        "description": "Get a summary of recent errors — returns top N error types with counts, not full log lines.",
        "input_schema": {
            "type": "object",
            "properties": {
                "hours":   {"type": "integer", "description": "Look-back window in hours", "default": 24},
                "top_n":  {"type": "integer", "description": "Return top N error types", "default": 5},
            },
        },
    },
]

def get_user_stats(group_by: str = "status") -> dict:
    """Return aggregate statistics, not raw records."""
    data = {
        "status":  {"active": 8432, "inactive": 1205, "pending": 341},
        "plan":    {"free": 6200, "pro": 2800, "enterprise": 978},
        "country": {"US": 4100, "UK": 1800, "DE": 900, "FR": 750, "other": 2428},
    }
    return {"group_by": group_by, "breakdown": data.get(group_by, {}), "total": 9978}

def get_recent_errors(hours: int = 24, top_n: int = 5) -> dict:
    """Return summarised error data, not raw log lines."""
    errors = [
        {"type": "TimeoutError",      "count": 1842, "pct": 45.2},
        {"type": "AuthError",         "count":  921, "pct": 22.6},
        {"type": "ValidationError",   "count":  614, "pct": 15.1},
        {"type": "RateLimitError",    "count":  307, "pct":  7.5},
        {"type": "DatabaseError",     "count":  153, "pct":  3.8},
    ]
    return {"window_hours": hours, "top_errors": errors[:top_n], "total_errors": 4071}

def handle_tool(name: str, inputs: dict) -> str:
    if name == "get_user_stats":
        result = get_user_stats(inputs.get("group_by", "status"))
    elif name == "get_recent_errors":
        result = get_recent_errors(inputs.get("hours", 24), inputs.get("top_n", 5))
    else:
        result = {"error": "unknown tool"}
    payload = json.dumps(result)
    print(f"  [{name}] {len(payload)//4} tokens")
    return payload

def run_agent(question: str) -> str:
    messages = [{"role": "user", "content": question}]
    for _ in range(6):
        r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=TOOLS,
            messages=messages,
        )
        if r.stop_reason == "end_turn":
            return next((b.text for b in r.content if b.type == "text"), "")
        messages.append({"role": "assistant", "content": r.content})
        results = []
        for b in r.content:
            if b.type == "tool_use":
                result = handle_tool(b.name, b.input)
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": result})
        messages.append({"role": "user", "content": results})
    return "max steps reached"

questions = [
    "How many active users do we have?",
    "What are the most common errors in the last 24 hours?",
]
for q in questions:
    print(f"Q: {q}")
    print(f"A: {run_agent(q)[:200]}\n")
```

**Expected Token Savings:** Aggregate statistics (50 tokens) replace raw records (50,000 tokens) — a 1,000× reduction; structured tool outputs prevent the large-result problem by design, requiring no post-processing or summarisation.
**Environment:** Analytical agents querying operational data; designing tools to return aggregates instead of raw records is the highest-impact architectural fix — change the tool, not the agent.

---

### Option 6 — Tool result size budget: track and enforce a per-conversation token budget

```python
import json
import anthropic

client = anthropic.Anthropic()

TOOL_RESULT_BUDGET = 8_000   # max total tokens from all tool results per conversation

class BudgetedToolRunner:
    def __init__(self, budget: int = TOOL_RESULT_BUDGET) -> None:
        self._budget  = budget
        self._spent   = 0

    @property
    def remaining(self) -> int:
        return max(0, self._budget - self._spent)

    def run(self, name: str, inputs: dict) -> str:
        if self.remaining == 0:
            return json.dumps({"error": "tool_result_budget_exhausted",
                               "message": "Maximum tool result size reached for this conversation. Summarise what you have and answer from that."})

        raw    = self._execute(name, inputs)
        # Cap result to remaining budget
        max_chars = self.remaining * 4
        if len(raw) > max_chars:
            raw = raw[:max_chars] + f"\n[BUDGET: showing {self.remaining} of {len(raw)//4} available tokens]"
        self._spent += len(raw) // 4
        print(f"  [{name}] budget: {self._spent}/{self._budget} tokens used")
        return raw

    def _execute(self, name: str, inputs: dict) -> str:
        if name == "search_logs":
            return "\n".join([f"2024-01-15 {10+i//60:02d}:{i%60:02d} ERROR {inputs.get('query','?')}: exception_{i}" for i in range(500)])
        if name == "list_files":
            return "\n".join([f"/path/to/file_{i}.py" for i in range(200)])
        return json.dumps({"result": "ok"})

TOOLS = [
    {"name": "search_logs",  "description": "Search application logs.", "input_schema": {"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}}}},
    {"name": "list_files",   "description": "List files in a directory.", "input_schema": {"type": "object", "required": ["path"],  "properties": {"path":  {"type": "string"}}}},
]

def run_agent(question: str) -> str:
    runner   = BudgetedToolRunner(budget=TOOL_RESULT_BUDGET)
    messages = [{"role": "user", "content": question}]

    for _ in range(8):
        r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=TOOLS,
            messages=messages,
        )
        if r.stop_reason == "end_turn":
            return next((b.text for b in r.content if b.type == "text"), "")
        messages.append({"role": "assistant", "content": r.content})
        results = []
        for b in r.content:
            if b.type == "tool_use":
                result = runner.run(b.name, b.input)
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": result})
        messages.append({"role": "user", "content": results})
    return "max steps reached"

print(f"A: {run_agent('Search logs for errors, then list files in /src.')[:300]}")
```

**Expected Token Savings:** Conversation-level budget enforcement prevents the cumulative tool result problem — where 10 moderate tool calls (800 tokens each) together consume 8,000 tokens of context; budget tracking ensures the agent adapts its strategy when results are large rather than silently overflowing.
**Environment:** Multi-tool agents that make several tool calls per conversation; budget tracking is the defensive measure that catches cumulative overflow that per-call truncation alone may miss.

---

## Comparison

| Option | Prevents Overflow | Loses Data | Extra API Calls | Best For |
|---|---|---|---|---|
| 1. Size-guard truncation | Yes | Some (tail) | 0 | All agents — minimum baseline |
| 2. Paginated interface | Yes (by design) | No | Multiple (on demand) | Database / API queries |
| 3. LLM summarisation | Yes | Structured loss | +1 per large result | Unstructured prose results |
| 4. Relevant excerpt | Yes | Irrelevant sections | +1 (fallback) | Document / file reading agents |
| 5. Structured aggregate output | Yes (by design) | Raw data | 0 | Analytical / reporting agents |
| 6. Conversation budget | Yes (cumulative) | Some (budgeted) | 0 | Multi-tool conversation agents |
