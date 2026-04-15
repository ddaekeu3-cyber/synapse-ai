---
layout: solution
title: "Agent Regenerates Unchanged Content on Every Call"
category: token-cost
description: "The agent rewrites entire documents, reports, or code files on every turn even when only a small section changed. Output tokens for unchanged content are billed at full price every time."
tags: [token-cost, regeneration, differential, incremental, caching, document-editing, efficiency]
---

## Symptom

A user asks the agent to fix a typo in a 2,000-word report. The agent reads the full document, generates all 2,000 words again with the one-word fix, and returns the complete text. You pay for 2,000 output tokens when 1 would have sufficed. This pattern repeats for every "small change" request: add a sentence, rename a variable, fix formatting — always full regeneration.

## Root Cause

The agent treats every editing request as a generation task. There is no concept of "return only the diff" or "return only the changed section." The model defaults to producing the complete artifact because that is what language models do — they complete sequences. Without explicit instructions to produce a patch, edit instruction, or section-only output, full regeneration is the path of least resistance.

## Fix

---

### Option 1: Structured Edit Instructions Instead of Full Regeneration

Ask the model to return `{"action": "replace", "old": "...", "new": "..."}` edit instructions. Apply them locally.

```python
import json
import re
import anthropic

client = anthropic.Anthropic()

EDIT_TOOL = {
    "name": "apply_edits",
    "description": "Return a list of targeted edits to apply to the document. Do NOT return the full document.",
    "input_schema": {
        "type": "object",
        "properties": {
            "edits": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["replace", "insert_after", "insert_before", "delete"],
                        },
                        "target": {
                            "type": "string",
                            "description": "Exact text to find in the document (for replace/delete/insert_after/insert_before)",
                        },
                        "replacement": {
                            "type": "string",
                            "description": "New text (for replace and insert actions). Empty string for delete.",
                        },
                        "reason": {"type": "string"},
                    },
                    "required": ["action", "target"],
                },
            },
            "summary": {"type": "string", "description": "One-line summary of all changes made"},
        },
        "required": ["edits", "summary"],
    },
}


def apply_edits(document: str, edits: list[dict]) -> tuple[str, int]:
    """Apply edit instructions to document. Returns (new_document, tokens_saved_estimate)."""
    result = document
    applied = 0

    for edit in edits:
        action = edit["action"]
        target = edit.get("target", "")
        replacement = edit.get("replacement", "")

        if action == "replace" and target in result:
            result = result.replace(target, replacement, 1)
            applied += 1
        elif action == "insert_after" and target in result:
            result = result.replace(target, target + replacement, 1)
            applied += 1
        elif action == "insert_before" and target in result:
            result = result.replace(target, replacement + target, 1)
            applied += 1
        elif action == "delete" and target in result:
            result = result.replace(target, "", 1)
            applied += 1
        else:
            print(f"  [Edit skipped] action={action}, target not found: {target[:40]!r}")

    # Estimate tokens saved: full regen would cost len(document)/4 tokens
    # Edit instruction costs only the edit objects (~50 tokens each)
    full_regen_tokens = len(document) // 4
    edit_tokens = applied * 50
    saved = full_regen_tokens - edit_tokens

    return result, saved


def edit_document(document: str, instruction: str) -> tuple[str, str]:
    """
    Edit a document using targeted edit instructions.
    Returns (updated_document, summary).
    """
    # Count chars to decide strategy
    doc_len = len(document)
    if doc_len < 200:
        # Short doc: full regen is fine
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": f"Apply this edit to the document:\n\nInstruction: {instruction}\n\nDocument:\n{document}",
            }],
        )
        return response.content[0].text, "Full rewrite (short document)"

    # Long doc: use edit instructions
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        tools=[EDIT_TOOL],
        tool_choice={"type": "any"},
        messages=[{
            "role": "user",
            "content": (
                f"Apply this edit to the document using targeted edit instructions.\n"
                f"IMPORTANT: Return ONLY edit instructions, not the full document.\n\n"
                f"Instruction: {instruction}\n\n"
                f"Document ({doc_len} chars):\n{document}"
            ),
        }],
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == "apply_edits":
            edits = block.input["edits"]
            summary = block.input["summary"]
            updated, saved = apply_edits(document, edits)
            print(f"  [Edit mode] Applied {len(edits)} edits. ~{saved} tokens saved vs full regen.")
            return updated, summary

    # Fallback: return original if no edits
    return document, "No changes"


# Example: 800-word document with tiny fix
DOCUMENT = """
Executive Summary

The quarterly performance report for Q1 2025 demonstrates strong growth across all business units.
Revenue increased by 23% year-over-year, driven primarily by expansion in the European market segment.
Customer acquisition costs decreased by 15% due to improved targeting in digital channels.

Key Findings

1. Revenue Performance: Total revenue reached $4.2 billion, exceeding projections by 8%.
   The APAC region showed the strongest growth at 31%, while North America grew 19%.

2. Customer Metrics: Monthly active users grew to 12.4 million, up from 9.8 million.
   Customer lifetime value increased by $340 on average across all segments.

3. Operational Efficiency: Operating margins improved to 28.3% from 24.1% in Q4 2024.
   Headcount remained flat while output per employee increased 18%.

4. Product Development: Three major features were launched in Q1, with user adoption
   rates exceeding 40% within the first 30 days for each feature.

Recommendations

Based on these findings, we recommend continued investment in the European expansion strategy,
with a particular focus on the German and French markets. Additionally, the successful
customer acquisition improvements should be replicated in the APAC region.
""".strip()

# Make a small change — only fix "APAC" to "Asia-Pacific" in two places
updated, summary = edit_document(
    DOCUMENT,
    "Replace 'APAC' with 'Asia-Pacific' throughout the document"
)
print(f"Summary: {summary}")
print(f"Changed: {'Asia-Pacific' in updated}")
```

**Expected Token Savings**: Edit instruction for a 2,000-token document costs ~100 output tokens instead of 2,000. **95% reduction** for single-location edits.
**Environment**: Tool use required. Works best for documents >500 characters.

---

### Option 2: Section-Based Editing — Only Return the Changed Section

Split the document into sections. Ask the model to return only the modified section.

```python
import re
import anthropic

client = anthropic.Anthropic()


def split_into_sections(document: str) -> list[tuple[str, str]]:
    """
    Split document into (heading, content) pairs.
    Returns list of (section_id, section_text).
    """
    # Match markdown headings
    pattern = r"(#{1,3}\s+[^\n]+)"
    parts = re.split(pattern, document)

    sections = []
    if parts[0].strip():
        sections.append(("__preamble__", parts[0]))

    i = 1
    while i < len(parts) - 1:
        heading = parts[i].strip()
        content = parts[i + 1] if i + 1 < len(parts) else ""
        section_id = re.sub(r"[^a-z0-9_]", "_", heading.lower().lstrip("#").strip())
        sections.append((section_id, heading + "\n" + content))
        i += 2

    return sections


def reassemble(sections: list[tuple[str, str]]) -> str:
    return "".join(content for _, content in sections)


def targeted_section_edit(document: str, instruction: str) -> tuple[str, int]:
    """
    Edit only the relevant section(s) of a document.
    Returns (updated_document, sections_edited).
    """
    sections = split_into_sections(document)
    section_map = dict(sections)
    section_preview = "\n".join(
        f"[{sid}]: {content[:60].strip()!r}..."
        for sid, content in sections
    )

    # Step 1: Identify which section(s) to edit
    identify_response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": (
                f"Given this edit instruction, which section IDs need to change?\n\n"
                f"Instruction: {instruction}\n\n"
                f"Sections:\n{section_preview}\n\n"
                f"Return a JSON array of section IDs to edit, e.g. [\"section_id_1\"]"
            ),
        }],
    )

    raw = identify_response.content[0].text
    try:
        start, end = raw.find("["), raw.rfind("]") + 1
        target_ids = json.loads(raw[start:end])
    except Exception:
        target_ids = [sections[0][0]] if sections else []

    import json

    # Step 2: Edit only the identified sections
    edited_sections = list(sections)
    sections_edited = 0

    for i, (sid, content) in enumerate(edited_sections):
        if sid in target_ids:
            edit_response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=len(content) // 3 + 200,  # budget proportional to section size
                messages=[{
                    "role": "user",
                    "content": (
                        f"Apply this edit to ONLY this section. Return only the updated section text.\n\n"
                        f"Instruction: {instruction}\n\n"
                        f"Section:\n{content}"
                    ),
                }],
            )
            edited_sections[i] = (sid, edit_response.content[0].text)
            sections_edited += 1
            print(f"  [Section edited] {sid} ({len(content)} → {len(edit_response.content[0].text)} chars)")

    if sections_edited == 0:
        print("  [No sections identified for editing]")

    return reassemble(edited_sections), sections_edited


DOCUMENT = """# Introduction

This document describes the deployment procedure for the production system.
All engineers must follow these steps carefully.

## Prerequisites

Before beginning, ensure you have:
- SSH access to production servers
- AWS CLI configured with appropriate permissions
- Docker installed and running locally

## Deployment Steps

1. Pull the latest image from ECR
2. Run database migrations
3. Deploy to staging environment
4. Run smoke tests
5. Deploy to production with blue-green strategy

## Rollback Procedure

If deployment fails, execute the rollback script immediately.
Contact the on-call engineer if rollback does not resolve the issue.
"""

import json

updated, count = targeted_section_edit(
    DOCUMENT,
    "Add 'kubectl configured for EKS cluster' to the Prerequisites section"
)
print(f"\nEdited {count} section(s)")
print(updated)
```

**Expected Token Savings**: 4-section document editing 1 section: ~75% output token reduction. Scales with document length.
**Environment**: Two LLM calls (identify + edit). Total cost still far below full regen for long documents.

---

### Option 3: Differential Output with Unified Diff Format

Ask the model to return a unified diff. Apply it with Python's `difflib`. Zero full-document output tokens.

```python
import difflib
import anthropic

client = anthropic.Anthropic()

DIFF_SYSTEM = """You are a code and document editor. When asked to make changes, return ONLY a unified diff in this exact format:

--- original
+++ modified
@@ -LINE,COUNT +LINE,COUNT @@
 context line
-removed line
+added line
 context line

Rules:
- Include 2 lines of context around each change
- Use exact line numbers
- Return ONLY the diff, no explanation
- If no changes needed, return: NO_CHANGES"""


def parse_and_apply_diff(original: str, diff_text: str) -> str | None:
    """Apply a unified diff to the original document."""
    if diff_text.strip() == "NO_CHANGES":
        return original

    original_lines = original.splitlines(keepends=True)
    diff_lines = diff_text.splitlines(keepends=True)

    try:
        result = list(difflib.restore(diff_lines, 2))
        return "".join(result)
    except Exception:
        # Fallback: use difflib's SequenceMatcher to apply changes
        return None


def diff_based_edit(document: str, instruction: str) -> str:
    """Edit document using diff output. Much cheaper than full regeneration."""
    doc_lines = len(document.splitlines())

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,  # Diffs are compact; rarely need more than this
        system=DIFF_SYSTEM,
        messages=[{
            "role": "user",
            "content": (
                f"Apply this change: {instruction}\n\n"
                f"Document ({doc_lines} lines):\n{document}"
            ),
        }],
    )

    diff_output = response.content[0].text.strip()
    output_tokens = response.usage.output_tokens

    if diff_output == "NO_CHANGES":
        print(f"  [No changes needed] {output_tokens} output tokens")
        return document

    print(f"  [Diff output] {output_tokens} output tokens (vs ~{doc_lines*1.2:.0f} for full regen)")

    # Try to apply diff
    updated = parse_and_apply_diff(document, diff_output)
    if updated:
        return updated

    # If diff application fails, fall back to asking for the specific change only
    print("  [Diff application failed, requesting minimal edit]")
    fallback = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": (
                f"Return ONLY the exact line(s) to change for this edit:\n"
                f"Instruction: {instruction}\n\n"
                f"Current document:\n{document}\n\n"
                f"Format: OLD_TEXT|||NEW_TEXT"
            ),
        }],
    )

    raw = fallback.content[0].text.strip()
    if "|||" in raw:
        old, new = raw.split("|||", 1)
        return document.replace(old.strip(), new.strip(), 1)

    return document


DOCUMENT = """\
# API Reference

## Authentication

All requests require an Authorization header with a Bearer token.
Example: Authorization: Bearer YOUR_TOKEN_HERE

## Endpoints

### GET /users
Returns a list of all users.
Parameters: page (int), limit (int, max 100)

### POST /users
Creates a new user.
Required fields: name, email, role

### DELETE /users/{id}
Deletes a user by ID.
Requires admin role.
"""

result = diff_based_edit(DOCUMENT, "Change max limit from 100 to 500 in GET /users")
print(result)
```

**Expected Token Savings**: Diff for a 50-line document changing 1 line: ~15 output tokens vs ~300 for full regen. **95% reduction**.
**Environment**: Python stdlib `difflib`. No external dependencies.

---

### Option 4: Prompt Caching for Unchanged Document Prefix

Structure the call so the unchanged document is in the cached prefix. Pay 10% for re-reading it on every edit.

```python
import anthropic

client = anthropic.Anthropic()


def cached_document_edit(
    document: str,
    instruction: str,
    model: str = "claude-sonnet-4-6",
) -> tuple[str, dict]:
    """
    Place the document in a cached system block.
    On repeated edits, the document read costs only 10% of normal input price.
    Returns (edit_instructions, usage_stats).
    """
    response = client.messages.create(
        model=model,
        max_tokens=512,
        system=[
            {
                "type": "text",
                "text": "You are a document editor. When asked to edit, return ONLY the changed portion with clear markers like [REPLACE: old text] → [WITH: new text]. Never rewrite the full document.",
            },
            {
                "type": "text",
                "text": f"Document to edit:\n\n{document}",
                "cache_control": {"type": "ephemeral"},  # Cache the document
            },
        ],
        messages=[{
            "role": "user",
            "content": f"Apply this edit: {instruction}",
        }],
    )

    usage = response.usage
    cache_read = getattr(usage, "cache_read_input_tokens", 0)
    cache_created = getattr(usage, "cache_creation_input_tokens", 0)

    stats = {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_tokens": cache_read,
        "cache_created_tokens": cache_created,
        "cache_savings_pct": round(cache_read / max(usage.input_tokens, 1) * 90, 1),
    }

    return response.content[0].text, stats


def apply_replacement_markers(document: str, edit_instructions: str) -> str:
    """Parse [REPLACE: ...] → [WITH: ...] format and apply to document."""
    import re
    pattern = r"\[REPLACE:\s*(.*?)\]\s*→\s*\[WITH:\s*(.*?)\]"
    matches = re.findall(pattern, edit_instructions, re.DOTALL)

    result = document
    for old, new in matches:
        result = result.replace(old.strip(), new.strip(), 1)

    return result


# Large document (would cost many input tokens without caching)
LARGE_DOC = "\n\n".join([
    "# Technical Specification v2.3",
    "## Overview\nThis system processes financial transactions in real-time using event-driven architecture. " * 5,
    "## Data Model\nTransactions are stored in PostgreSQL with the following schema: " * 5,
    "## API Design\nRESTful endpoints follow OpenAPI 3.0 specification. All endpoints require JWT auth. " * 5,
    "## Security\nAll data is encrypted at rest using AES-256. Transit encryption uses TLS 1.3. " * 5,
    "## Performance\nP99 latency target is 50ms. Throughput target is 10,000 TPS. " * 5,
])

# First call: cache miss (document cached for next 5 minutes)
edits1, stats1 = cached_document_edit(LARGE_DOC, "Change version from v2.3 to v2.4 in the title")
print(f"Edit 1 stats: {stats1}")

# Second call: cache hit (document read at 10% cost)
edits2, stats2 = cached_document_edit(LARGE_DOC, "Change P99 target from 50ms to 25ms")
print(f"Edit 2 stats: {stats2}")
print(f"Cache saved ~{stats2['cache_savings_pct']}% on document re-read")
```

**Expected Token Savings**: Document cached at 10% re-read cost. For 10 edits on a 2,000-token document: 90% savings on input tokens = ~18,000 tokens saved.
**Environment**: Requires `claude-sonnet-4-6` or `claude-opus-4-6`. Cache TTL is 5 minutes (reset on each call that creates the cache).

---

### Option 5: Change Detection — Skip Regeneration If Unchanged

Before calling the model, detect if the instruction would actually change anything. If not, return early.

```python
import hashlib
import anthropic

client = anthropic.Anthropic()


def hash_content(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


# Cache: instruction_hash → (document_hash, result)
_edit_cache: dict[str, tuple[str, str]] = {}


def needs_regeneration(document: str, instruction: str) -> bool:
    """
    Check if this (document, instruction) pair has already been computed.
    Returns False if we have a cached result.
    """
    key = hash_content(document + instruction)
    return key not in _edit_cache


def get_cached_result(document: str, instruction: str) -> str | None:
    key = hash_content(document + instruction)
    if key in _edit_cache:
        doc_hash, result = _edit_cache[key]
        if doc_hash == hash_content(document):
            print("  [Cache hit] Returning cached edit result")
            return result
    return None


def cache_result(document: str, instruction: str, result: str):
    key = hash_content(document + instruction)
    _edit_cache[key] = (hash_content(document), result)


NOOP_DETECTOR_SYSTEM = """Determine if the following edit instruction would actually change the document.
Reply with exactly one word: CHANGE or NOCHANGE."""


def would_change(document: str, instruction: str) -> bool:
    """Use Haiku to quickly determine if the edit would actually change anything."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=10,
        system=NOOP_DETECTOR_SYSTEM,
        messages=[{
            "role": "user",
            "content": f"Instruction: {instruction}\n\nDocument excerpt: {document[:500]}",
        }],
    )
    result = response.content[0].text.strip().upper()
    return "CHANGE" in result


def smart_edit(document: str, instruction: str) -> str:
    # Check exact cache first
    cached = get_cached_result(document, instruction)
    if cached:
        return cached

    # Quick no-op check (costs ~20 tokens vs ~500 for full edit)
    if not would_change(document, instruction):
        print(f"  [No-op detected] Instruction would not change document")
        return document

    # Full edit
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=len(document) // 3 + 200,
        messages=[{
            "role": "user",
            "content": (
                f"Apply this edit. Return ONLY the changed section with context, not the full document.\n\n"
                f"Instruction: {instruction}\n\nDocument:\n{document}"
            ),
        }],
    )

    # For simplicity, return the model's output (in production, apply as diff)
    result = response.content[0].text
    cache_result(document, instruction, result)
    return result


doc = "The server runs on port 8080. Max connections: 100. Timeout: 30 seconds."

# Same instruction twice → second call is free
print(smart_edit(doc, "Change port from 8080 to 9090"))
print(smart_edit(doc, "Change port from 8080 to 9090"))  # cache hit

# No-op instruction
print(smart_edit(doc, "Change port from 3000 to 4000"))  # would_change = False
```

**Expected Token Savings**: No-op detection saves ~480 output tokens per no-op request (20 tokens for detection vs 500 for edit). Cache hits save 100%.
**Environment**: In-memory cache (per process). Replace with Redis for distributed caching.

---

### Option 6: Incremental Code Editing with Line Range Targeting

For code files, return only the changed function/class/block with line numbers. Apply programmatically.

```python
import ast
import anthropic

client = anthropic.Anthropic()


def find_function_lines(source: str, function_name: str) -> tuple[int, int] | None:
    """Find the start and end lines of a function in Python source."""
    try:
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == function_name:
                    return node.lineno, node.end_lineno
    except SyntaxError:
        pass
    return None


def replace_function(source: str, function_name: str, new_function: str) -> str:
    """Replace a function in source code with a new implementation."""
    lines = source.splitlines()
    bounds = find_function_lines(source, function_name)
    if not bounds:
        return source

    start, end = bounds
    # Find indentation
    indent = ""
    for ch in lines[start - 1]:
        if ch in (" ", "\t"):
            indent += ch
        else:
            break

    new_lines = [indent + line if i > 0 else line
                 for i, line in enumerate(new_function.strip().splitlines())]

    updated = lines[:start - 1] + new_lines + lines[end:]
    return "\n".join(updated)


def targeted_code_edit(source: str, instruction: str, target_function: str | None = None) -> str:
    """Edit only the relevant function, not the whole file."""
    if target_function:
        bounds = find_function_lines(source, target_function)
        if bounds:
            lines = source.splitlines()
            start, end = bounds
            section = "\n".join(lines[start - 1:end])

            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=len(section) // 3 + 300,
                messages=[{
                    "role": "user",
                    "content": (
                        f"Apply this change to ONLY this function. Return ONLY the updated function.\n\n"
                        f"Instruction: {instruction}\n\n"
                        f"Function to edit:\n```python\n{section}\n```"
                    ),
                }],
            )

            new_fn = response.content[0].text.strip()
            # Strip markdown code fences if present
            if new_fn.startswith("```"):
                lines_out = new_fn.splitlines()
                new_fn = "\n".join(lines_out[1:-1] if lines_out[-1] == "```" else lines_out[1:])

            updated = replace_function(source, target_function, new_fn)
            total_lines = len(source.splitlines())
            fn_lines = end - start + 1
            print(f"  [Targeted edit] Edited {fn_lines}/{total_lines} lines (~{fn_lines/total_lines*100:.0f}% of file)")
            return updated

    # Fallback: full file edit
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=len(source) // 3 + 300,
        messages=[{"role": "user", "content": f"Apply: {instruction}\n\nCode:\n{source}"}],
    )
    return response.content[0].text


SOURCE = '''
import time
from typing import Optional

class DataProcessor:
    def __init__(self, batch_size: int = 100):
        self.batch_size = batch_size
        self.processed = 0

    def process_batch(self, items: list) -> list:
        """Process a batch of items."""
        results = []
        for item in items:
            result = self._transform(item)
            results.append(result)
            self.processed += 1
        return results

    def _transform(self, item: dict) -> dict:
        """Transform a single item."""
        return {
            "id": item["id"],
            "value": item["value"] * 2,
            "timestamp": time.time(),
        }

    def get_stats(self) -> dict:
        """Return processing statistics."""
        return {"processed": self.processed, "batch_size": self.batch_size}
'''.strip()

# Edit only `_transform`, not the entire class
updated = targeted_code_edit(
    SOURCE,
    "Add error handling for missing 'id' or 'value' keys",
    target_function="_transform",
)
print(updated)
```

**Expected Token Savings**: For a 200-line file editing a 10-line function: ~95% output token reduction. AST-based targeting is exact.
**Environment**: Python `ast` module (stdlib). Works for Python source; adapt `find_function_lines` for other languages using regex.

---

| Option | Mechanism | Output Token Reduction | Complexity | Best For |
|--------|---------|----------------------|-----------|---------|
| 1 | Edit instruction tool | ~95% | Low | General documents |
| 2 | Section-based editing | ~75% | Medium | Structured markdown/docs |
| 3 | Unified diff output | ~95% | Medium | Code and line-oriented files |
| 4 | Prompt cache for document | Input: 90% | Low | Repeated edits, same document |
| 5 | No-op detection + cache | Up to 100% | Low | Idempotent edit operations |
| 6 | AST function targeting | ~95% | Medium | Python source code editing |
