---
layout: solution
title: "Agent Sends Entire File When Only the Diff Is Needed — Context Bloat"
category: token-cost
description: "Agent reads a 10,000-line file, sends all of it to the model to make a 3-line change. 99.97% of the context is irrelevant. Next iteration reads the file again and sends all 10,000 lines again. Token cost scales with file size, not change size."
tags: [token-cost, diff, context, file, chunking, rag, selective-context, large-files]
---

# Agent Sends Entire File When Only the Diff Is Needed — Context Bloat

## Symptom
- Agent spends 80,000 tokens on file context to make a 2-line fix
- Same 5,000-line file sent to the model 4 times in a single task (read, analyze, edit, verify)
- Token cost per task scales linearly with codebase size — unacceptable for large repos
- Agent includes entire log file (50,000 lines) to answer "what was the last error?"
- Model hits context window limit because the file is larger than the context window
- API cost is dominated by input tokens from large files, not by actual reasoning

## Root Cause
Agents that use full-file reads send all content regardless of relevance. A 10,000-line file to fix one function wastes 9,990 lines of context. Every re-read multiplies the cost. Large file contents also push other important context (system prompt, conversation history) out of the context window. The fix is to send only what the model actually needs: the relevant section, a diff, a summary, or a search result.

## Fix

### Option 1: Send only the relevant section, not the whole file

```python
import re
from pathlib import Path
from dataclasses import dataclass

@dataclass
class FileSection:
    file_path: str
    start_line: int
    end_line: int
    content: str
    total_lines: int

def extract_relevant_section(
    file_path: str,
    target_line: int = None,
    target_function: str = None,
    target_class: str = None,
    context_lines: int = 20
) -> FileSection:
    """
    Extract the relevant section of a file instead of sending the whole thing.
    Returns target + N lines of context above and below.
    """
    lines = Path(file_path).read_text(encoding="utf-8").splitlines()
    total = len(lines)

    if target_function:
        # Find function definition
        for i, line in enumerate(lines):
            if re.match(rf'^\s*(?:async\s+)?def\s+{re.escape(target_function)}\s*\(', line):
                target_line = i + 1
                break

    if target_class:
        for i, line in enumerate(lines):
            if re.match(rf'^\s*class\s+{re.escape(target_class)}\s*[:(]', line):
                target_line = i + 1
                break

    if target_line is None:
        raise ValueError("Specify target_line, target_function, or target_class")

    # Extract section with context
    start = max(0, target_line - context_lines - 1)
    end = min(total, target_line + context_lines)

    # Extend to include the full function/class if needed
    # Find the end of the block by tracking indentation
    if end < total:
        target_indent = len(lines[target_line - 1]) - len(lines[target_line - 1].lstrip())
        for i in range(target_line, min(total, target_line + 200)):
            line = lines[i]
            if line.strip() and len(line) - len(line.lstrip()) <= target_indent and i > target_line:
                end = min(i + 5, total)
                break

    section_lines = [f"{i+1:4d} | {line}" for i, line in enumerate(lines[start:end], start=start)]
    content = f"# {file_path} (lines {start+1}-{end} of {total})\n" + "\n".join(section_lines)

    return FileSection(file_path, start+1, end, content, total)

# Usage — 50 lines instead of 10,000:
section = extract_relevant_section(
    "src/agent/processor.py",
    target_function="process_message",
    context_lines=15
)

print(f"Sending {section.end - section.start_line + 1} lines instead of {section.total_lines}")
# → "Sending 47 lines instead of 8,432"
# Token savings: ~8,385 lines × ~4 tokens = ~33,540 tokens saved
```

### Option 2: Diff-based editing — send only the change, not the file

```python
import difflib
from pathlib import Path

def generate_diff(original: str, modified: str, filename: str = "file") -> str:
    """
    Generate a unified diff between original and modified content.
    Send the diff (small) instead of the full file (large).
    """
    original_lines = original.splitlines(keepends=True)
    modified_lines = modified.splitlines(keepends=True)

    diff = list(difflib.unified_diff(
        original_lines,
        modified_lines,
        fromfile=f"a/{filename}",
        tofile=f"b/{filename}",
        n=3  # 3 lines of context
    ))

    return "".join(diff)

def apply_diff_to_file(file_path: str, diff_text: str) -> str:
    """
    Apply a unified diff to a file.
    Returns the modified content.
    """
    import subprocess
    import tempfile

    with tempfile.NamedTemporaryFile(mode='w', suffix='.patch', delete=False) as f:
        f.write(diff_text)
        patch_path = f.name

    try:
        result = subprocess.run(
            ["patch", "--dry-run", file_path, patch_path],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            raise ValueError(f"Patch would fail: {result.stderr}")

        subprocess.run(["patch", file_path, patch_path], check=True)
        return Path(file_path).read_text()
    finally:
        Path(patch_path).unlink(missing_ok=True)

def summarize_large_file_changes(
    original_path: str,
    changes: list[dict]
) -> str:
    """
    Build a context message showing ONLY what changed.
    changes: [{"line": 42, "old": "...", "new": "..."}, ...]
    """
    lines = Path(original_path).read_text().splitlines()
    total = len(lines)

    context_parts = [f"Modifying {original_path} ({total} lines total):"]

    for change in changes:
        line_num = change["line"]
        start = max(0, line_num - 4)
        end = min(total, line_num + 4)

        context = "\n".join(
            f"  {'>>>' if i+1 == line_num else '   '} {i+1:4d}: {lines[i]}"
            for i in range(start, end)
        )
        context_parts.append(
            f"\nChange at line {line_num}:\n{context}\n"
            f"Replace with: {change['new']}"
        )

    return "\n".join(context_parts)
```

### Option 3: Semantic search — retrieve only relevant chunks

```python
import anthropic
from dataclasses import dataclass

@dataclass
class FileChunk:
    file_path: str
    start_line: int
    end_line: int
    content: str
    chunk_type: str  # "function", "class", "block", "imports"

def chunk_python_file(file_path: str) -> list[FileChunk]:
    """
    Split Python file into semantic chunks (functions, classes, imports).
    Each chunk can be retrieved independently.
    """
    import ast
    source = Path(file_path).read_text()
    lines = source.splitlines()

    try:
        tree = ast.parse(source)
    except SyntaxError:
        # Fall back to fixed-size chunking
        chunk_size = 50
        return [
            FileChunk(
                file_path=file_path,
                start_line=i+1,
                end_line=min(i+chunk_size, len(lines)),
                content="\n".join(lines[i:i+chunk_size]),
                chunk_type="block"
            )
            for i in range(0, len(lines), chunk_size)
        ]

    chunks = []

    # Find import block
    import_lines = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
    if import_lines:
        first_import = min(n.lineno for n in import_lines)
        last_import = max(n.lineno for n in import_lines)
        chunks.append(FileChunk(
            file_path=file_path,
            start_line=first_import,
            end_line=last_import,
            content="\n".join(lines[first_import-1:last_import]),
            chunk_type="imports"
        ))

    # Functions and classes
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            end_line = node.end_lineno
            chunks.append(FileChunk(
                file_path=file_path,
                start_line=node.lineno,
                end_line=end_line,
                content="\n".join(lines[node.lineno-1:end_line]),
                chunk_type="class" if isinstance(node, ast.ClassDef) else "function"
            ))

    return chunks

def find_relevant_chunks(
    chunks: list[FileChunk],
    query: str,
    max_chunks: int = 3
) -> list[FileChunk]:
    """
    Find the most relevant chunks for a query using keyword matching.
    For production: use embeddings + vector search.
    """
    query_terms = set(query.lower().split())

    def relevance_score(chunk: FileChunk) -> float:
        content_lower = chunk.content.lower()
        score = sum(1 for term in query_terms if term in content_lower)
        # Boost score for type matches
        if any(t in query.lower() for t in ["function", "def", "method"]) and chunk.chunk_type == "function":
            score += 2
        if any(t in query.lower() for t in ["class", "model", "entity"]) and chunk.chunk_type == "class":
            score += 2
        return score

    scored = sorted(chunks, key=relevance_score, reverse=True)
    relevant = [c for c in scored[:max_chunks] if relevance_score(c) > 0]

    total_lines = sum(c.end_line - c.start_line for c in relevant)
    print(f"Retrieved {len(relevant)} chunks ({total_lines} lines) from {len(chunks)} total chunks")
    return relevant

# Usage:
chunks = chunk_python_file("src/agent/processor.py")
relevant = find_relevant_chunks(chunks, "process_message function error handling")

context = "\n\n".join(
    f"# {c.file_path}:{c.start_line}-{c.end_line} ({c.chunk_type})\n{c.content}"
    for c in relevant
)
# Send context (150 lines) instead of full file (8,000 lines)
```

### Option 4: Cache file content — avoid re-reading same file

```python
import hashlib
import time
from functools import lru_cache
from pathlib import Path

class FileCache:
    """
    Cache file reads with content-based invalidation.
    Same file read 4 times in one task = 1 actual read + 3 cache hits.
    """

    def __init__(self, max_size_bytes: int = 10 * 1024 * 1024):  # 10MB cache
        self._cache: dict[str, dict] = {}
        self._max_size = max_size_bytes
        self._current_size = 0
        self._hits = 0
        self._misses = 0

    def read(self, file_path: str) -> str:
        path = Path(file_path)
        mtime = path.stat().st_mtime
        key = str(path.resolve())

        if key in self._cache and self._cache[key]["mtime"] == mtime:
            self._hits += 1
            return self._cache[key]["content"]

        # Cache miss or file changed
        content = path.read_text(encoding="utf-8")
        self._cache[key] = {"content": content, "mtime": mtime, "size": len(content)}
        self._misses += 1

        print(f"File cache: {'HIT' if self._hits else 'MISS'} — {file_path}")
        return content

    def read_lines(self, file_path: str, start: int, end: int) -> str:
        """Read specific line range — uses cache for the full file"""
        content = self.read(file_path)
        lines = content.splitlines()
        selected = lines[start-1:end]
        return "\n".join(f"{i+start}: {line}" for i, line in enumerate(selected))

    @property
    def stats(self) -> dict:
        total = self._hits + self._misses
        return {
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": f"{self._hits/total*100:.0f}%" if total > 0 else "0%",
            "cached_files": len(self._cache)
        }

file_cache = FileCache()

# Agent reads same file 4 times — only 1 actual disk read:
content1 = file_cache.read("src/agent/processor.py")  # Miss — reads from disk
content2 = file_cache.read("src/agent/processor.py")  # Hit — from cache
content3 = file_cache.read("src/agent/processor.py")  # Hit — from cache
content4 = file_cache.read("src/agent/processor.py")  # Hit — from cache

print(file_cache.stats)  # → hits: 3, misses: 1, hit_rate: 75%
```

### Option 5: Tail / grep — extract specific patterns from large files

```python
from pathlib import Path
import re

def tail_file(file_path: str, lines: int = 100) -> str:
    """Get last N lines of a file — for logs, don't send entire log"""
    content = Path(file_path).read_text(encoding="utf-8", errors="replace")
    all_lines = content.splitlines()
    tail = all_lines[-lines:]
    total = len(all_lines)
    header = f"# {file_path} — last {min(lines, total)} of {total} lines\n"
    return header + "\n".join(f"{total-len(tail)+i+1}: {line}" for i, line in enumerate(tail))

def grep_file(file_path: str, pattern: str, context_lines: int = 5) -> str:
    """Find pattern in file, return matches with context — like grep -C"""
    lines = Path(file_path).read_text(encoding="utf-8", errors="replace").splitlines()
    matches = []
    shown = set()

    for i, line in enumerate(lines):
        if re.search(pattern, line, re.IGNORECASE):
            start = max(0, i - context_lines)
            end = min(len(lines), i + context_lines + 1)
            for j in range(start, end):
                if j not in shown:
                    marker = ">>>" if j == i else "   "
                    matches.append(f"{marker} {j+1:4d}: {lines[j]}")
                    shown.add(j)
            matches.append("---")

    if not matches:
        return f"Pattern '{pattern}' not found in {file_path}"

    header = f"# {file_path} — matches for '{pattern}' ({len(shown)} lines shown of {len(lines)} total)\n"
    return header + "\n".join(matches)

# For log analysis — don't send 1M line log:
recent_errors = grep_file("/var/log/agent.log", pattern="ERROR|CRITICAL", context_lines=3)
last_100 = tail_file("/var/log/agent.log", lines=100)

# For code search — don't send whole codebase:
usages = grep_file("src/agent/processor.py", pattern=r"def process_message", context_lines=10)
```

### Option 6: Token-aware context builder

```python
import anthropic

client = anthropic.Anthropic()

class TokenAwareContextBuilder:
    """
    Build context for model calls while tracking token budget.
    Automatically truncates or summarizes when budget is tight.
    """

    def __init__(self, model: str = "claude-sonnet-4-6", token_budget: int = 50_000):
        self.model = model
        self.token_budget = token_budget
        self._sections: list[dict] = []
        self._used_tokens = 0

    def count_tokens(self, text: str) -> int:
        response = client.beta.messages.count_tokens(
            model=self.model,
            messages=[{"role": "user", "content": text}],
            betas=["token-counting-2024-11-01"]
        )
        return response.input_tokens

    def add_section(self, label: str, content: str, priority: int = 1) -> bool:
        """
        Add a section to context if budget allows.
        Higher priority sections are included first.
        Returns True if added, False if budget exceeded.
        """
        tokens = self.count_tokens(content)

        if self._used_tokens + tokens > self.token_budget:
            remaining = self.token_budget - self._used_tokens
            print(
                f"Context budget: '{label}' needs {tokens} tokens, "
                f"only {remaining} remaining — truncating"
            )
            # Add truncated version
            ratio = remaining / tokens
            truncated = content[:int(len(content) * ratio * 0.9)]
            content = truncated + f"\n... [truncated — {tokens - remaining} tokens omitted]"
            tokens = remaining

        self._sections.append({"label": label, "content": content, "priority": priority})
        self._used_tokens += tokens
        return True

    def build(self) -> str:
        # Sort by priority (highest first), then join
        sorted_sections = sorted(self._sections, key=lambda s: -s["priority"])
        parts = [f"## {s['label']}\n{s['content']}" for s in sorted_sections]
        return "\n\n".join(parts)

# Usage:
builder = TokenAwareContextBuilder(token_budget=30_000)

# Add sections in priority order:
builder.add_section("Task", "Fix the bug in process_message", priority=10)
builder.add_section("Error", grep_file("app.py", "AttributeError"), priority=8)
builder.add_section("Relevant Code", extract_relevant_section("app.py", target_function="process_message").content, priority=7)
builder.add_section("Full File", Path("app.py").read_text(), priority=1)  # Will be truncated/skipped

context = builder.build()
print(f"Context built: {builder._used_tokens}/{builder.token_budget} tokens used")
```

## Context Size vs Token Cost

| Approach | Lines Sent | Tokens Used | Relative Cost |
|---|---|---|---|
| Full file (10k lines) | 10,000 | ~40,000 | 100% |
| Relevant section (50 lines) | 50 | ~200 | 0.5% |
| Diff (change only) | 20 | ~80 | 0.2% |
| Grep result (matches) | 30 | ~120 | 0.3% |
| Tail (last 100 lines) | 100 | ~400 | 1% |
| Cached (re-read) | 0 extra | 0 extra | 0% extra |

## Expected Token Savings
Full 10,000-line file × 4 reads in one task: 160,000 input tokens
Section-based reads × 4: ~800 input tokens — 99.5% reduction

## Environment
- Any agent that reads source code, logs, or data files; critical for code agents, log analysis agents, and document processing agents working with large files
- Source: direct experience; full-file reads are the single largest source of unnecessary token consumption in code-editing agents
