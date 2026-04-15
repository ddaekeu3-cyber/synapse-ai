---
layout: solution
title: "Agent Doesn't Sanitize Output Before Writing to Storage"
category: general
description: "Raw model output is written directly to databases, files, or downstream APIs without validation — leading to SQL injection via model-generated queries, XSS in stored content, oversized records, or binary garbage corrupting structured data."
tags: [general, security, sanitization, storage, reliability]
---

## Symptom

The agent generates text and writes it directly to a database column, file, or API endpoint. The model occasionally outputs markdown fences around code, hallucinated control characters, excessively long strings that overflow column widths, or content that contains SQL-like fragments. A downstream renderer executes script tags embedded in model output. A file write fails because the model generated null bytes. A database insert is truncated silently, corrupting a JSON blob.

## Root Cause

Model output is user-controlled data from the storage system's perspective. Just as you would never write `INSERT INTO notes VALUES (user_input)` without parameterization and validation, you should never write `INSERT INTO notes VALUES (model_output)` without sanitization. The model can generate any text, including content that is structurally valid but semantically dangerous for the target storage system.

## Fix

### Option 1 — Strip markdown fences and normalize whitespace before storage

```python
import anthropic
import re

client = anthropic.Anthropic()

def sanitize_text_for_storage(text: str, max_length: int = 10_000) -> str:
    """Remove markdown artifacts and normalize for plain-text storage."""
    # Strip code fences (```python ... ``` or ``` ... ```)
    text = re.sub(r"```[\w]*\n?", "", text)
    # Strip inline code backticks
    text = re.sub(r"`([^`]+)`", r"\1", text)
    # Remove null bytes and other control characters (keep \n \t)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    # Normalize multiple blank lines to at most two
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Strip leading/trailing whitespace
    text = text.strip()
    # Enforce max length (truncate at word boundary)
    if len(text) > max_length:
        text = text[:max_length].rsplit(" ", 1)[0] + "…"
    return text

def generate_and_store(prompt: str) -> str:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = resp.content[0].text
    clean = sanitize_text_for_storage(raw, max_length=2000)
    print(f"[sanitize] {len(raw)} → {len(clean)} chars")
    # Safe to write to DB or file
    return clean

result = generate_and_store("Write a Python function to sort a list.")
print(result[:200])
```

**Expected Token Savings:** No direct savings, but prevents costly incident response when malformed data corrupts production records.
**Environment:** Agents writing plain text to databases, CMS platforms, or files; any pipeline where markdown-aware storage is not the target.

---

### Option 2 — HTML sanitization to prevent XSS in stored content

```python
import anthropic
import html
import re

# pip install bleach
try:
    import bleach
    BLEACH_AVAILABLE = True
except ImportError:
    BLEACH_AVAILABLE = False

client = anthropic.Anthropic()

ALLOWED_TAGS = ["p", "ul", "ol", "li", "strong", "em", "code", "pre", "blockquote"]
ALLOWED_ATTRS: dict = {}

def sanitize_html_output(text: str) -> str:
    """Sanitize model-generated HTML before storing in a web-rendered context."""
    if BLEACH_AVAILABLE:
        # Strip disallowed tags; escape attributes
        clean = bleach.clean(
            text,
            tags=ALLOWED_TAGS,
            attributes=ALLOWED_ATTRS,
            strip=True,
        )
    else:
        # Fallback: escape everything
        clean = html.escape(text)

    # Remove null bytes
    clean = clean.replace("\x00", "")
    # Enforce max length
    if len(clean) > 50_000:
        clean = clean[:50_000] + "…"
    return clean

def agent_write_html(user_request: str) -> str:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system="Respond with simple HTML using only basic tags (p, ul, ol, li, strong, em, code).",
        messages=[{"role": "user", "content": user_request}],
    )
    raw = resp.content[0].text
    sanitized = sanitize_html_output(raw)

    # Example: safe to store in a web CMS column now
    print(f"[html] raw={len(raw)} chars → sanitized={len(sanitized)} chars")
    return sanitized

result = agent_write_html("List the top 3 benefits of using Python for data science.")
print(result[:300])
```

**Expected Token Savings:** Prevents XSS vulnerabilities in stored content that could compromise all users of a web application — the cost of an incident dwarfs all token savings.
**Environment:** Agents generating content for web CMS platforms, comment systems, or any HTML-rendered storage; chatbots storing conversation history that is later rendered in a browser.

---

### Option 3 — JSON output validation and schema enforcement before storage

```python
import anthropic
import json
from typing import Any

client = anthropic.Anthropic()

RECORD_SCHEMA = {
    "name":     (str,  1,   200),   # (type, min_len, max_len)
    "summary":  (str,  10,  2000),
    "score":    (float, 0.0, 1.0),  # (type, min_val, max_val) for numbers
    "tags":     (list,  0,   20),   # (type, min_items, max_items) for lists
}

def validate_json_record(data: Any, schema: dict) -> tuple[bool, list[str]]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return False, ["Output is not a JSON object"]

    for field, constraints in schema.items():
        if field not in data:
            errors.append(f"Missing required field: {field}")
            continue

        val = data[field]
        expected_type, min_v, max_v = constraints

        if not isinstance(val, expected_type):
            errors.append(f"{field}: expected {expected_type.__name__}, got {type(val).__name__}")
            continue

        if expected_type == str and not (min_v <= len(val) <= max_v):
            errors.append(f"{field}: length {len(val)} out of range [{min_v}, {max_v}]")
        elif expected_type in (int, float) and not (min_v <= val <= max_v):
            errors.append(f"{field}: value {val} out of range [{min_v}, {max_v}]")
        elif expected_type == list and not (min_v <= len(val) <= max_v):
            errors.append(f"{field}: {len(val)} items, expected [{min_v}, {max_v}]")

    return len(errors) == 0, errors

def generate_validated_record(prompt: str) -> dict | None:
    system = (
        'Respond with a JSON object containing: name (string), summary (string, ≥10 chars), '
        'score (float 0.0–1.0), tags (list of strings). Output only the JSON object.'
    )
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = resp.content[0].text.strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"[validate] JSON parse failed: {e}")
        return None

    valid, errors = validate_json_record(data, RECORD_SCHEMA)
    if not valid:
        print(f"[validate] Schema errors: {errors}")
        return None

    print(f"[validate] OK — {list(data.keys())}")
    return data

record = generate_validated_record("Describe a fictional Python library for data analysis.")
if record:
    print(json.dumps(record, indent=2))
```

**Expected Token Savings:** Validation catches structurally invalid records before they corrupt a database; prevents cascade failures in downstream consumers that expect valid schema.
**Environment:** Agents writing structured records to databases; ETL pipelines; any agent producing JSON for downstream processing.

---

### Option 4 — File path and content sanitization before disk writes

```python
import anthropic
import os
import re
import unicodedata

client = anthropic.Anthropic()

OUTPUT_DIR = "/tmp/agent_outputs"
os.makedirs(OUTPUT_DIR, exist_ok=True)

def sanitize_filename(name: str, max_len: int = 100) -> str:
    """Make a model-suggested filename safe for the filesystem."""
    # Normalize unicode (e.g., accented chars)
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    # Replace path separators and dangerous chars
    name = re.sub(r'[/\\:*?"<>|]', "_", name)
    # Replace whitespace with underscores
    name = re.sub(r'\s+', "_", name.strip())
    # Remove leading dots (hidden files) and dashes
    name = re.sub(r'^[.\-]+', "", name)
    # Enforce max length without splitting extension
    stem, _, ext = name.rpartition(".")
    if ext and len(name) > max_len:
        name = stem[:max_len - len(ext) - 1] + "." + ext
    elif len(name) > max_len:
        name = name[:max_len]
    return name or "output"

def sanitize_file_content(content: str) -> bytes:
    """Sanitize model output before writing to a text file."""
    # Remove null bytes
    content = content.replace("\x00", "")
    # Remove other non-printable characters except \n \t
    content = re.sub(r"[\x01-\x08\x0b\x0c\x0e-\x1f\x7f]", "", content)
    # Normalize line endings
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    # Enforce max file size (1 MB)
    encoded = content.encode("utf-8", errors="replace")
    if len(encoded) > 1_024_000:
        encoded = encoded[:1_024_000]
    return encoded

def generate_and_write_file(topic: str) -> str:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": f"Write a short technical note about: {topic}"}],
    )
    raw_content = resp.content[0].text

    # Sanitize filename (use topic as base)
    safe_name = sanitize_filename(f"{topic[:50]}.txt")

    # Prevent path traversal: ensure output is within OUTPUT_DIR
    full_path = os.path.realpath(os.path.join(OUTPUT_DIR, safe_name))
    if not full_path.startswith(os.path.realpath(OUTPUT_DIR)):
        raise ValueError(f"Path traversal detected: {full_path}")

    # Sanitize content
    safe_content = sanitize_file_content(raw_content)

    with open(full_path, "wb") as f:
        f.write(safe_content)

    print(f"[file] wrote {len(safe_content)} bytes → {full_path}")
    return full_path

path = generate_and_write_file("asyncio event loop internals")
print("Written to:", path)
```

**Expected Token Savings:** Path traversal prevention and content sanitization prevent security incidents; without these, a model generating `../../../etc/passwd` as a filename could overwrite system files.
**Environment:** Agents that write model output to the filesystem; code generation agents; agents creating reports or documents.

---

### Option 5 — SQL-safe parameterized storage (never interpolate model output)

```python
import anthropic
import sqlite3
import re
import hashlib
from datetime import datetime

client = anthropic.Anthropic()

# Create demo DB
conn = sqlite3.connect(":memory:")
conn.execute("""
    CREATE TABLE notes (
        id        TEXT PRIMARY KEY,
        content   TEXT NOT NULL,
        created   TEXT NOT NULL,
        word_count INTEGER
    )
""")
conn.commit()

def sanitize_for_notes(text: str, max_length: int = 5000) -> str:
    """Content-level sanitization; SQL safety is handled by parameterization."""
    # Remove null bytes and dangerous control characters
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    # Normalize whitespace
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    # Enforce max length
    if len(text) > max_length:
        text = text[:max_length].rsplit(" ", 1)[0] + "…"
    return text

def store_note(content: str) -> str:
    """Store model output safely using parameterized queries — never string formatting."""
    clean = sanitize_for_notes(content)
    note_id = hashlib.sha256(clean.encode()).hexdigest()[:16]
    word_count = len(clean.split())

    # ✅ Parameterized — SQL injection impossible regardless of content
    conn.execute(
        "INSERT OR REPLACE INTO notes (id, content, created, word_count) VALUES (?, ?, ?, ?)",
        (note_id, clean, datetime.utcnow().isoformat(), word_count),
    )
    conn.commit()
    return note_id

def generate_and_store(prompt: str) -> str:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = resp.content[0].text
    note_id = store_note(raw)
    print(f"[db] stored note_id={note_id}")
    return note_id

# Works safely even if model generates SQL-like content
note_id = generate_and_store("Write a note about database security best practices.")
row = conn.execute("SELECT word_count, content FROM notes WHERE id=?", (note_id,)).fetchone()
print(f"[db] {row[0]} words: {row[1][:100]}")
```

**Expected Token Savings:** Parameterized queries prevent SQL injection regardless of model output; combined with content sanitization, the stored data is both safe and clean.
**Environment:** Any agent writing to a relational database; the parameterized query pattern is mandatory regardless of the content source.

---

### Option 6 — Output sanitization pipeline with pluggable validators

```python
import anthropic
import re
from typing import Callable

client = anthropic.Anthropic()

# ── sanitizer pipeline ─────────────────────────────────────────────────────────

SanitizerFn = Callable[[str], str]

def remove_null_bytes(text: str) -> str:
    return text.replace("\x00", "")

def remove_control_chars(text: str) -> str:
    return re.sub(r"[\x01-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)

def strip_markdown_fences(text: str) -> str:
    return re.sub(r"```[\w]*\n?", "", text).strip()

def normalize_whitespace(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", text).strip()

def truncate_factory(max_len: int) -> SanitizerFn:
    def _truncate(text: str) -> str:
        if len(text) <= max_len:
            return text
        return text[:max_len].rsplit(" ", 1)[0] + "…"
    return _truncate

def replace_unicode_quotes(text: str) -> str:
    return text.replace("\u201c", '"').replace("\u201d", '"').replace("\u2018", "'").replace("\u2019", "'")

class SanitizationPipeline:
    def __init__(self, *sanitizers: SanitizerFn):
        self._sanitizers = sanitizers

    def run(self, text: str) -> str:
        for fn in self._sanitizers:
            text = fn(text)
        return text

# ── preconfigured pipelines for different storage targets ──────────────────────

DB_TEXT_PIPELINE = SanitizationPipeline(
    remove_null_bytes,
    remove_control_chars,
    normalize_whitespace,
    truncate_factory(10_000),
)

FILE_CONTENT_PIPELINE = SanitizationPipeline(
    remove_null_bytes,
    remove_control_chars,
    normalize_whitespace,
    replace_unicode_quotes,
    truncate_factory(100_000),
)

PLAIN_TEXT_PIPELINE = SanitizationPipeline(
    remove_null_bytes,
    remove_control_chars,
    strip_markdown_fences,
    normalize_whitespace,
    truncate_factory(5_000),
)

def generate_and_sanitize(prompt: str, pipeline: SanitizationPipeline) -> str:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = resp.content[0].text
    clean = pipeline.run(raw)
    print(f"[pipeline] {len(raw)} → {len(clean)} chars")
    return clean

# Use the right pipeline for each storage target
db_content   = generate_and_sanitize("Summarise agile development.", DB_TEXT_PIPELINE)
file_content = generate_and_sanitize("Write a config file comment block.", FILE_CONTENT_PIPELINE)
plain_text   = generate_and_sanitize("Explain REST APIs.", PLAIN_TEXT_PIPELINE)

print("\n[db]   ", db_content[:100])
print("[file] ", file_content[:100])
print("[plain]", plain_text[:100])
```

**Expected Token Savings:** Pluggable pipeline makes sanitization explicit and auditable; wrong pipeline for wrong storage target is caught in code review, not in production.
**Environment:** Multi-destination agents writing to databases, files, and APIs from the same codebase; teams wanting sanitization to be a first-class, reviewable component.

---

## Comparison

| Option | Target Storage | XSS Prevention | Injection Prevention | Length Enforcement | Best For |
|---|---|---|---|---|---|
| 1. Strip markdown + control chars | Plain text DB / files | No | No | Yes | CMS text storage; note-taking agents |
| 2. HTML sanitization (bleach) | Web-rendered DB columns | Yes | No | Yes | Chat history; comment systems; CMS |
| 3. JSON schema validation | Structured DB columns | No | Via schema | Via schema | ETL pipelines; record-based storage |
| 4. Filename + content sanitization | Filesystem | No | Path traversal | Yes (file size) | Code generation; report agents |
| 5. Parameterized queries | Relational databases | No | Yes (SQL) | Via content sanitizer | Any DB-writing agent; mandatory baseline |
| 6. Pluggable pipeline | Any target | Via plugin | Via plugin | Yes | Multi-destination agents; auditable pipelines |
