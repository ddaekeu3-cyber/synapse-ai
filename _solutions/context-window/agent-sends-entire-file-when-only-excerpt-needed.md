---
layout: solution
title: "Agent Sends Entire File When Only an Excerpt Is Needed"
category: context-window
description: "Agent reads a 50,000-token codebase or document and sends it all to the LLM when the answer requires only a 200-line function or a 3-paragraph section."
tags: [context-window, token-cost, retrieval, chunking, performance, efficiency]
---

## Symptom

The agent is asked "What does the `parse_config` function do?" and sends all 4,000 lines of `config.py` to the model. Or a user asks about page 3 of a 200-page PDF and the agent sends the full document. Token costs are 10-50× higher than necessary, response latency increases proportionally, and for very large files the context window overflows entirely, causing an API error.

## Root Cause

The simplest implementation of a file-reading tool uses `open(path).read()` and injects the entire content into the prompt. This works for small files but is never necessary for large ones — the model only needs the relevant section. Without a chunking or retrieval layer, the agent has no mechanism to extract and send only the portion relevant to the query.

## Fix

### Option 1 — Keyword search to extract relevant lines before sending

```python
import re
import anthropic

client = anthropic.Anthropic()

def extract_relevant_lines(
    content: str,
    query: str,
    context_lines: int = 20,
) -> str:
    """
    Find lines most relevant to the query and return them with surrounding context.
    Falls back to first N lines if nothing matches.
    """
    lines   = content.splitlines()
    keywords = set(re.findall(r'\b\w{4,}\b', query.lower()))
    scored: list[tuple[int, int]] = []   # (score, line_index)

    for i, line in enumerate(lines):
        line_lower = line.lower()
        score = sum(1 for kw in keywords if kw in line_lower)
        if score > 0:
            scored.append((score, i))

    if not scored:
        # No keyword match — return a useful head excerpt
        return "\n".join(lines[:50]) + f"\n[... {max(0, len(lines)-50)} more lines not shown ...]"

    # Expand top matches with surrounding context
    scored.sort(key=lambda x: -x[0])
    chosen_lines: set[int] = set()
    for _, center in scored[:3]:   # top 3 matching lines
        for j in range(max(0, center - context_lines), min(len(lines), center + context_lines)):
            chosen_lines.add(j)

    excerpt_lines = [lines[i] for i in sorted(chosen_lines)]
    excerpt = "\n".join(excerpt_lines)
    omitted = len(lines) - len(chosen_lines)
    if omitted > 0:
        excerpt += f"\n\n[... {omitted} non-matching lines omitted ...]"
    return excerpt

def ask_about_file(file_content: str, question: str) -> str:
    excerpt = extract_relevant_lines(file_content, question, context_lines=15)
    print(f"  [excerpt] sending {len(excerpt.splitlines())} lines of {len(file_content.splitlines())} total")

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"File excerpt (relevant to your question):\n```\n{excerpt}\n```\n\nQuestion: {question}",
        }],
    )
    return response.content[0].text

# Simulate a large file
sample_code = "\n".join([
    "import os",
    "import json",
    "",
    "DEFAULT_CONFIG = {'debug': False, 'timeout': 30}",
    "",
    "def parse_config(path: str) -> dict:",
    "    '''Load and validate config from JSON file.'''",
    "    with open(path) as f:",
    "        data = json.load(f)",
    "    return {**DEFAULT_CONFIG, **data}",
    "",
    "def save_config(config: dict, path: str) -> None:",
    "    with open(path, 'w') as f:",
    "        json.dump(config, f, indent=2)",
    "",
] + [f"# padding line {i}" for i in range(500)])   # 500 lines of padding

reply = ask_about_file(sample_code, "What does parse_config do?")
print(f"Answer: {reply[:200]}")
```

**Expected Token Savings:** Sending 30 relevant lines instead of 500 total = 94% token reduction; savings scale linearly with file size.
**Environment:** All agents that read source files, logs, or documents to answer targeted questions.

---

### Option 2 — Semantic chunking with sliding window retrieval

```python
import anthropic

client = anthropic.Anthropic()

def chunk_text(text: str, chunk_size: int = 200, overlap: int = 20) -> list[dict]:
    """Split text into overlapping chunks with metadata."""
    lines  = text.splitlines()
    chunks = []
    i      = 0
    while i < len(lines):
        end   = min(i + chunk_size, len(lines))
        chunk = "\n".join(lines[i:end])
        chunks.append({
            "id":         len(chunks),
            "start_line": i + 1,
            "end_line":   end,
            "text":       chunk,
        })
        i += chunk_size - overlap
    return chunks

def rank_chunks(chunks: list[dict], query: str, top_k: int = 2) -> list[dict]:
    """Simple keyword ranking — replace with embedding similarity in production."""
    import re
    keywords = set(re.findall(r'\b\w{4,}\b', query.lower()))
    scored = []
    for chunk in chunks:
        score = sum(chunk["text"].lower().count(kw) for kw in keywords)
        scored.append((score, chunk))
    scored.sort(key=lambda x: -x[0])
    return [c for _, c in scored[:top_k]]

def ask_large_document(document: str, question: str) -> str:
    chunks = chunk_text(document, chunk_size=100, overlap=10)
    relevant = rank_chunks(chunks, question, top_k=2)

    context_parts = []
    for c in relevant:
        context_parts.append(
            f"[Lines {c['start_line']}-{c['end_line']}]\n{c['text']}"
        )
    context = "\n\n---\n\n".join(context_parts)

    total_lines   = len(document.splitlines())
    excerpt_lines = sum(c["end_line"] - c["start_line"] for c in relevant)
    print(f"  [chunks] {len(chunks)} chunks, sending {len(relevant)} ({excerpt_lines}/{total_lines} lines)")

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": (
                f"Relevant sections from the document:\n\n{context}\n\n"
                f"Question: {question}\n\n"
                f"Answer based only on the sections provided."
            ),
        }],
    )
    return response.content[0].text

# Simulate a large technical document
document = "\n".join([
    "# System Architecture",
    "The system uses a microservices design with three core services.",
    "",
    "## Authentication Service",
    "Handles JWT token issuance and validation. Tokens expire after 1 hour.",
    "Refresh tokens are stored in Redis with a 30-day TTL.",
    "",
    "## API Gateway",
    "Routes requests to backend services. Rate limit: 100 req/min per user.",
    "Uses circuit breaker pattern for downstream service failures.",
    "",
    "## Data Service",
    "PostgreSQL primary with read replicas. Connection pool: 20 per instance.",
    "All writes go through the primary; reads are load-balanced across replicas.",
    "",
] + [f"Additional detail paragraph {i}. " * 5 for i in range(100)])

reply = ask_large_document(document, "What is the rate limit for the API gateway?")
print(f"Answer: {reply[:200]}")
```

**Expected Token Savings:** Chunked retrieval sends 2 chunks (200 lines) instead of the full document (600+ lines) = 67% savings; savings improve with larger documents.
**Environment:** Document Q&A agents, codebase search, and any agent that must answer targeted questions about large text artifacts.

---

### Option 3 — Function/section extractor for structured files

```python
import re
import ast
import anthropic

client = anthropic.Anthropic()

def extract_python_function(source: str, function_name: str) -> str | None:
    """Extract a specific function definition from Python source."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # Fallback: regex extraction
        pattern = re.compile(
            rf"((?:^[ \t]*(?:@\w+\n))*^[ \t]*(?:async\s+)?def\s+{re.escape(function_name)}\b.*?)(?=\n(?:class|def|async\s+def|\Z))",
            re.MULTILINE | re.DOTALL,
        )
        match = pattern.search(source)
        return match.group(1) if match else None

    lines = source.splitlines()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == function_name:
                start = node.lineno - 1
                end   = node.end_lineno
                return "\n".join(lines[start:end])
    return None

def extract_markdown_section(content: str, heading: str) -> str | None:
    """Extract a specific section from a Markdown document."""
    pattern = re.compile(
        rf"(^#{1,3}\s+{re.escape(heading)}.*?)(?=\n#{1,3}\s|\Z)",
        re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    match = pattern.search(content)
    return match.group(1).strip() if match else None

def ask_about_function(source: str, function_name: str, question: str) -> str:
    excerpt = extract_python_function(source, function_name)
    if excerpt is None:
        excerpt = source[:500] + "\n[function not found — showing file start]"
    else:
        total = len(source.splitlines())
        shown = len(excerpt.splitlines())
        print(f"  [extract] function '{function_name}': {shown} lines of {total} total")

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"Function source:\n```python\n{excerpt}\n```\n\nQuestion: {question}",
        }],
    )
    return response.content[0].text

sample_source = '''
import json
import hashlib

def load_users(path: str) -> list[dict]:
    """Load all users from JSON storage."""
    with open(path) as f:
        return json.load(f)

def authenticate(username: str, password: str, users: list[dict]) -> bool:
    """Check if username/password combination is valid."""
    hashed = hashlib.sha256(password.encode()).hexdigest()
    return any(
        u["username"] == username and u["password_hash"] == hashed
        for u in users
    )

def create_session(user_id: str) -> str:
    """Create a new session token for an authenticated user."""
    import secrets
    return secrets.token_hex(32)

''' + "\n".join([f"# filler {i}" for i in range(300)])

reply = ask_about_function(sample_source, "authenticate", "What hashing algorithm does this use?")
print(f"Answer: {reply[:200]}")
```

**Expected Token Savings:** AST-based function extraction sends exactly one function (5-50 lines) instead of the whole file; for a 1,000-line file asking about a 20-line function, token savings are ~98%.
**Environment:** Code review agents, documentation generators, and debugging assistants that target specific functions in large codebases.

---

### Option 4 — Page-range extraction for long documents

```python
import anthropic

client = anthropic.Anthropic()

def split_into_pages(text: str, lines_per_page: int = 50) -> list[str]:
    """Split text into logical pages."""
    lines = text.splitlines()
    return [
        "\n".join(lines[i : i + lines_per_page])
        for i in range(0, len(lines), lines_per_page)
    ]

def find_relevant_pages(pages: list[str], query: str, context_pages: int = 1) -> list[tuple[int, str]]:
    """Find pages most relevant to the query and include neighbouring pages."""
    import re
    keywords = set(re.findall(r'\b\w{4,}\b', query.lower()))
    scores = []
    for i, page in enumerate(pages):
        score = sum(page.lower().count(kw) for kw in keywords)
        scores.append((score, i))

    scores.sort(key=lambda x: -x[0])
    best_pages: set[int] = set()
    for _, idx in scores[:2]:   # top 2 matching pages
        for j in range(max(0, idx - context_pages), min(len(pages), idx + context_pages + 1)):
            best_pages.add(j)

    if not best_pages:
        best_pages = {0}   # fallback: first page

    return [(i, pages[i]) for i in sorted(best_pages)]

def ask_about_document(document: str, question: str, lines_per_page: int = 50) -> str:
    pages    = split_into_pages(document, lines_per_page)
    relevant = find_relevant_pages(pages, question)

    context_parts = [
        f"[Page {i+1} of {len(pages)}]\n{content}"
        for i, content in relevant
    ]
    context = "\n\n---\n\n".join(context_parts)

    sent_lines = sum(len(c.splitlines()) for _, c in relevant)
    total_lines = len(document.splitlines())
    print(f"  [pages] {len(pages)} pages, sending {len(relevant)} ({sent_lines}/{total_lines} lines)")

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": (
                f"Relevant document sections:\n\n{context}\n\n"
                f"Answer this question using only the provided sections:\n{question}"
            ),
        }],
    )
    return response.content[0].text

# Simulate a long report
sections = []
for chapter in range(1, 11):
    sections.append(f"# Chapter {chapter}: Topic {chapter}")
    for para in range(10):
        if chapter == 5 and para == 3:
            sections.append("The API rate limit is 1000 requests per hour per API key.")
        else:
            sections.append(f"This is paragraph {para} of chapter {chapter}. " * 4)
    sections.append("")

document = "\n".join(sections)
reply = ask_about_document(document, "What is the API rate limit?")
print(f"Answer: {reply[:200]}")
```

**Expected Token Savings:** Page-range extraction for a 500-page document answering a question answered on page 5 sends 3 pages instead of 500 = 99.4% savings.
**Environment:** PDF Q&A agents, legal document review, and any agent working with book-length documents.

---

### Option 5 — Two-pass: table of contents then targeted section fetch

```python
import re
import anthropic

client = anthropic.Anthropic()

def extract_headings(content: str) -> list[dict]:
    """Extract all headings from a Markdown document."""
    headings = []
    for i, line in enumerate(content.splitlines()):
        match = re.match(r'^(#{1,4})\s+(.+)', line)
        if match:
            headings.append({
                "level":   len(match.group(1)),
                "title":   match.group(2).strip(),
                "line":    i,
            })
    return headings

def extract_section(content: str, heading_title: str) -> str:
    """Extract content under a specific heading."""
    lines    = content.splitlines()
    headings = extract_headings(content)

    target = next(
        (h for h in headings if heading_title.lower() in h["title"].lower()),
        None,
    )
    if target is None:
        return content[:300] + "\n[section not found]"

    start = target["line"] + 1
    # Find the next heading at same or higher level
    end   = len(lines)
    for h in headings:
        if h["line"] > target["line"] and h["level"] <= target["level"]:
            end = h["line"]
            break

    return "\n".join(lines[start:end])

def ask_two_pass(document: str, question: str) -> str:
    # Pass 1: send only headings to identify the relevant section
    headings = extract_headings(document)
    toc = "\n".join(f"{'  ' * (h['level']-1)}- {h['title']}" for h in headings)

    toc_response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{
            "role": "user",
            "content": (
                f"Table of contents:\n{toc}\n\n"
                f"Which section title is most likely to answer this question?\n"
                f"Question: {question}\n\n"
                f"Reply with the exact section title only."
            ),
        }],
    )
    section_title = toc_response.content[0].text.strip()
    print(f"  [pass-1] identified section: {section_title!r}")

    # Pass 2: send only the identified section
    section = extract_section(document, section_title)
    total   = len(document.splitlines())
    shown   = len(section.splitlines())
    print(f"  [pass-2] sending {shown}/{total} lines ({shown/total*100:.0f}%)")

    final = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"Section: {section_title}\n\n{section}\n\nQuestion: {question}",
        }],
    )
    return final.content[0].text

document = """
# Product Manual

## Getting Started
Install the application using pip: `pip install myapp`.

## Configuration
Set environment variables before running. Required: API_KEY, DB_URL.

## Rate Limits
Each API key is limited to 500 requests per minute and 50,000 per day.
Exceeding limits returns HTTP 429. Use exponential backoff for retries.

## Troubleshooting
If the application crashes on startup, check that DB_URL is set correctly.
""" + "\n".join([f"### Section {i}\n" + "Details. " * 20 for i in range(30)])

reply = ask_two_pass(document, "What are the rate limits?")
print(f"Answer: {reply[:200]}")
```

**Expected Token Savings:** Two-pass approach: pass 1 costs ~50 tokens (TOC only); pass 2 costs the section (~200 tokens); total ~250 vs ~5,000 for the full document = 95% savings.
**Environment:** Structured documents (manuals, specifications, reports) with clear sections; two-pass works best when the TOC reliably indicates where information lives.

---

### Option 6 — Lazy file reader: request ranges as tool calls

```python
import json
import anthropic

client = anthropic.Anthropic()

class LazyFileReader:
    """Provides line-range read access as a tool — the model requests only what it needs."""

    def __init__(self, content: str):
        self._lines = content.splitlines()

    def read_lines(self, start: int, end: int) -> str:
        """Read lines [start, end) from the file (1-indexed)."""
        start = max(1, start)
        end   = min(len(self._lines) + 1, end)
        excerpt = "\n".join(self._lines[start-1 : end-1])
        print(f"  [lazy] read lines {start}-{end-1} of {len(self._lines)}")
        return excerpt

    def search(self, keyword: str) -> list[int]:
        """Return line numbers containing keyword."""
        matches = [i+1 for i, line in enumerate(self._lines) if keyword.lower() in line.lower()]
        print(f"  [lazy] search '{keyword}' → {len(matches)} hits: {matches[:5]}")
        return matches[:10]   # return first 10 hits

    @property
    def total_lines(self) -> int:
        return len(self._lines)

def ask_with_lazy_reader(content: str, question: str) -> str:
    reader = LazyFileReader(content)

    TOOLS = [
        {
            "name": "read_lines",
            "description": f"Read a range of lines from the file (total: {reader.total_lines} lines). Use this to read specific sections.",
            "input_schema": {
                "type": "object",
                "required": ["start", "end"],
                "properties": {
                    "start": {"type": "integer", "description": "First line to read (1-indexed)"},
                    "end":   {"type": "integer", "description": "Last line to read (exclusive)"},
                },
            },
        },
        {
            "name": "search_file",
            "description": "Search for a keyword and return matching line numbers.",
            "input_schema": {
                "type": "object",
                "required": ["keyword"],
                "properties": {"keyword": {"type": "string"}},
            },
        },
    ]

    messages = [{
        "role": "user",
        "content": (
            f"The file has {reader.total_lines} lines. "
            f"Use the tools to read only the relevant sections, then answer:\n\n{question}"
        ),
    }]

    for _ in range(8):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=TOOLS,
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if b.type == "text"), "")

        messages.append({"role": "assistant", "content": response.content})
        results = []
        for b in response.content:
            if b.type == "tool_use":
                if b.name == "read_lines":
                    result = reader.read_lines(b.input["start"], b.input["end"])
                elif b.name == "search_file":
                    hits = reader.search(b.input["keyword"])
                    result = json.dumps({"matching_lines": hits})
                else:
                    result = "unknown tool"
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": result})
        messages.append({"role": "user", "content": results})

    return "max steps reached"

large_file = "\n".join([f"line {i}: {'some content' if i != 42 else 'TIMEOUT_SECONDS = 300'}" for i in range(500)])
reply = ask_with_lazy_reader(large_file, "What is the value of TIMEOUT_SECONDS?")
print(f"Answer: {reply[:200]}")
```

**Expected Token Savings:** Lazy reader sends 0 tokens of the file upfront; the model reads only what it needs via tool calls — for a 500-line file where the answer is on line 42, total tokens sent is ~20 lines vs 500 = 96% savings.
**Environment:** Very large files (>10,000 lines) where even the top-K chunk approach is too expensive; tool-based lazy reading is the most token-efficient strategy for huge codebases.

---

## Comparison

| Option | Requires Query Understanding | Handles Code | Handles Prose | Token Efficiency | Best For |
|---|---|---|---|---|---|
| 1. Keyword line extraction | Yes | Yes | Yes | High | Quick implementation — works on any text |
| 2. Sliding window chunks | Partial | Yes | Yes | High | General documents without clear structure |
| 3. AST function extractor | Yes | Yes (Python) | No | Very high | Targeted function-level code questions |
| 4. Page-range extraction | Yes | No | Yes | Very high | Long PDFs and reports |
| 5. Two-pass TOC → section | Yes | No | Yes | Very high | Structured markdown/manual documents |
| 6. Lazy tool-based reader | No (model decides) | Yes | Yes | Maximum | Very large files where structure is unknown |
