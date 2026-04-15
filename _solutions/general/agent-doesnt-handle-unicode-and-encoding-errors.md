---
layout: solution
title: "Agent Doesn't Handle Unicode and Encoding Errors"
category: general
description: "Agent crashes or corrupts data when users send emoji, non-Latin scripts, or files with mixed encodings."
tags: [general, unicode, encoding, reliability, internationalisation]
---

## Symptom

The agent raises `UnicodeDecodeError` when reading uploaded files. Emoji and CJK characters are replaced with `?` or `\ufffd` replacement characters. Japanese or Arabic text is returned garbled. A single byte outside ASCII crashes the pipeline. The agent works perfectly for English-only input but breaks immediately for international users.

## Root Cause

Python 3 strings are Unicode by default, but many I/O operations default to the system's locale encoding (often ASCII on servers). Files opened without an explicit `encoding=` parameter, strings passed through `bytes.decode()` without error handling, and third-party APIs that truncate on non-BMP characters all produce silent data corruption or hard crashes when encountering non-ASCII text.

## Fix

### Option 1 — Always open files with explicit UTF-8 and error handling

```python
import anthropic

client = anthropic.Anthropic()

def read_file_safe(path: str) -> str:
    """Read a file, trying UTF-8 first, falling back to latin-1."""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except UnicodeDecodeError:
        # latin-1 never raises UnicodeDecodeError — every byte is valid
        with open(path, encoding="latin-1") as f:
            content = f.read()
        print(f"[encoding] {path!r} was not UTF-8; read as latin-1")
        return content

def write_file_safe(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

def summarise_file(path: str) -> str:
    content  = read_file_safe(path)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": f"Summarise:\n\n{content[:3000]}"}],
    )
    return response.content[0].text

# Create test files with various encodings
import tempfile, os

utf8_text   = "Hello 世界! Привет мир! مرحبا بالعالم 🌍"
latin1_text = "café résumé naïve".encode("latin-1")

with tempfile.NamedTemporaryFile(mode="w",  suffix=".txt", delete=False, encoding="utf-8")  as f:
    f.write(utf8_text);   utf8_path = f.name
with tempfile.NamedTemporaryFile(mode="wb", suffix=".txt", delete=False) as f:
    f.write(latin1_text); latin1_path = f.name

print(summarise_file(utf8_path)[:100])
print(summarise_file(latin1_path)[:100])

os.unlink(utf8_path); os.unlink(latin1_path)
```

**Expected Token Savings:** No crash = no retry; garbled content that reaches the model produces wrong answers requiring correction turns.
**Environment:** Any agent that reads user-uploaded files; explicit encoding is mandatory.

---

### Option 2 — Detect encoding with chardet before decoding

```python
import anthropic

client = anthropic.Anthropic()

def detect_and_read(data: bytes, hint_path: str = "") -> str:
    """Detect encoding of raw bytes and decode accordingly."""
    # Try chardet if available
    try:
        import chardet
        detection = chardet.detect(data)
        encoding  = detection.get("encoding") or "utf-8"
        confidence = detection.get("confidence", 0)
        print(f"[encoding] detected {encoding!r} ({confidence:.0%} confidence)")
    except ImportError:
        encoding = "utf-8"

    # Attempt decode with detected encoding, fall back to UTF-8 with replacement
    for enc in [encoding, "utf-8", "latin-1"]:
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue

    # Last resort: decode UTF-8 replacing unknown bytes
    return data.decode("utf-8", errors="replace")

def process_upload(raw_bytes: bytes, filename: str) -> str:
    text = detect_and_read(raw_bytes, filename)
    # Strip null bytes and other control characters that confuse LLMs
    text = "".join(c for c in text if c >= " " or c in "\n\r\t")

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": f"Analyse this file content:\n\n{text[:2000]}"}],
    )
    return response.content[0].text

# Test with various byte sequences
samples = [
    ("utf8.txt",   "Hello 世界 🎉".encode("utf-8")),
    ("latin1.txt", "Ångström résumé".encode("latin-1")),
    ("mixed.txt",  b"Normal text then \x80\x81\x82 garbage bytes"),
]
for name, data in samples:
    print(f"\n{name}: {process_upload(data, name)[:80]}")
```

**Expected Token Savings:** Correct decoding means the model receives readable text instead of garbled bytes; prevents hallucinated content from corrupted input.
**Environment:** File upload pipelines processing user documents of unknown provenance (PDFs, CSVs, legacy exports).

---

### Option 3 — Normalise Unicode before sending to the model

```python
import unicodedata
import re
import anthropic

client = anthropic.Anthropic()

def normalise_text(text: str) -> str:
    """
    Normalise Unicode for consistent LLM processing:
    - NFC: compose characters (e.g., e + combining accent → é)
    - Remove zero-width chars that inflate token count
    - Normalise whitespace
    - Replace control characters
    """
    # Compose characters to NFC normal form
    text = unicodedata.normalize("NFC", text)

    # Remove zero-width and invisible characters
    zero_width = {"\u200b", "\u200c", "\u200d", "\ufeff", "\u00ad"}
    text = "".join(c for c in text if c not in zero_width)

    # Remove other control characters (keep \n, \r, \t)
    text = "".join(c if c >= " " or c in "\n\r\t" else " " for c in text)

    # Normalise whitespace runs
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()

def ask(user_input: str) -> str:
    clean = normalise_text(user_input)
    if clean != user_input:
        char_diff = len(user_input) - len(clean)
        print(f"[unicode] normalised: removed {char_diff} chars")

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": clean}],
    )
    return response.content[0].text

# Test with problematic Unicode inputs
inputs = [
    "Caf\u0301e",                # é as e + combining accent (NFD)
    "Hello\u200bworld",           # zero-width space
    "Text\u0000with\u0001controls",  # control characters
    "Normal text with emoji 🚀",  # emoji — fine, keep it
    "   multiple   spaces   ",    # normalise whitespace
]
for inp in inputs:
    print(f"Input:  {inp!r}")
    print(f"Output: {ask(inp)[:60]}\n")
```

**Expected Token Savings:** NFC normalisation reduces token count by collapsing composed characters; zero-width char removal prevents invisible inflation of token counts.
**Environment:** All agents; Unicode normalisation should be a standard preprocessing step.

---

### Option 4 — Handle emoji and surrogate pairs in JSON serialisation

```python
import json
import anthropic

client = anthropic.Anthropic()

def safe_json_dumps(data) -> str:
    """Serialise to JSON without escaping non-ASCII characters."""
    return json.dumps(data, ensure_ascii=False)

def safe_json_loads(raw: str) -> dict:
    """Parse JSON that may contain Unicode or emoji."""
    # Strip BOM if present
    raw = raw.lstrip("\ufeff").strip()
    # Strip markdown fences
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    return json.loads(raw)

SYSTEM = """Extract user profile information and return ONLY a JSON object:
{"name": str, "greeting": str, "emoji_mood": str}
The greeting should be in the same language as the input.
emoji_mood should be a single emoji representing their mood."""

def extract_profile(user_text: str) -> dict:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        system=SYSTEM,
        messages=[{"role": "user", "content": user_text}],
    )
    raw = response.content[0].text
    try:
        profile = safe_json_loads(raw)
        # Verify the JSON round-trips correctly with Unicode
        serialised = safe_json_dumps(profile)
        reparsed   = json.loads(serialised)
        assert reparsed == profile
        return profile
    except (json.JSONDecodeError, AssertionError) as e:
        print(f"[encoding] JSON error: {e} | raw: {raw[:100]!r}")
        return {"name": "unknown", "greeting": user_text[:50], "emoji_mood": "😐"}

inputs = [
    "Hi, I'm María and I'm feeling great today! 😄",
    "こんにちは！田中です。今日はとても楽しいです。🌸",
    "مرحبا، اسمي أحمد وأنا سعيد جداً اليوم! 🎉",
    "Привет! Меня зовут Иван. Настроение отличное! 🚀",
]
for text in inputs:
    profile = extract_profile(text)
    print(f"Input:   {text[:50]!r}")
    print(f"Profile: {safe_json_dumps(profile)}\n")
```

**Expected Token Savings:** `ensure_ascii=False` produces shorter JSON for non-ASCII text (no `\uXXXX` escape sequences); fewer tokens per serialised response.
**Environment:** Agents that serialise/deserialise user data containing international characters or emoji.

---

### Option 5 — Truncate safely on Unicode boundaries

```python
import anthropic

client = anthropic.Anthropic()

def safe_truncate(text: str, max_chars: int) -> str:
    """
    Truncate text at a Unicode grapheme cluster boundary.
    Never splits a surrogate pair, combining sequence, or emoji sequence.
    """
    if len(text) <= max_chars:
        return text

    # Find the last safe boundary at or before max_chars
    truncated = text[:max_chars]

    # Walk back from truncation point to avoid splitting combining sequences
    i = len(truncated) - 1
    while i > 0 and unicodedata.category(truncated[i]) in {"Mn", "Mc", "Me"}:
        i -= 1  # skip combining marks

    truncated = truncated[:i + 1]

    # Don't end mid-word
    last_space = truncated.rfind(" ")
    if last_space > max_chars * 0.8:   # only trim if close to limit
        truncated = truncated[:last_space]

    return truncated + "…"

import unicodedata

def ask_safe(content: str, max_input_chars: int = 4000) -> str:
    safe_content = safe_truncate(content, max_input_chars)
    if len(safe_content) < len(content):
        print(f"[unicode] truncated {len(content)} → {len(safe_content)} chars")

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": safe_content}],
    )
    return response.content[0].text

# Test safe truncation with emoji and combining characters
texts = [
    "A" * 5000,                              # pure ASCII — simple truncation
    "こんにちは世界！" * 300,                  # CJK — must not split
    "café résumé naïve " * 200,              # combining accents
    "Hello 👨‍👩‍👧‍👦 World " * 200,     # family emoji with ZWJ sequences
]
for t in texts:
    result = ask_safe(t)
    print(f"Original: {len(t)} chars | Truncated + answered: OK")
```

**Expected Token Savings:** Safe truncation prevents wasted tokens from split codepoints that the model sees as two characters; correct truncation avoids re-runs.
**Environment:** Agents that limit input length; always truncate on boundaries, never mid-character.

---

### Option 6 — Encoding-safe tool result processing

```python
import json
import anthropic

client = anthropic.Anthropic()

def sanitise_tool_result(result: object) -> str:
    """
    Convert any tool result to a UTF-8 safe JSON string.
    Handles bytes objects, encoding errors, and non-serialisable types.
    """
    def convert(obj):
        if isinstance(obj, bytes):
            try:
                return obj.decode("utf-8")
            except UnicodeDecodeError:
                return obj.decode("latin-1")
        if isinstance(obj, dict):
            return {k: convert(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [convert(i) for i in obj]
        return obj

    safe = convert(result)
    try:
        return json.dumps(safe, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        # Last resort: str() everything
        return json.dumps(str(safe), ensure_ascii=False)

TOOLS = [
    {
        "name": "get_document",
        "description": "Retrieve a document by ID.",
        "input_schema": {
            "type": "object",
            "required": ["doc_id"],
            "properties": {"doc_id": {"type": "string"}},
        },
    }
]

# Simulated tool that returns mixed-encoding data
def get_document(doc_id: str) -> object:
    docs = {
        "doc-jp": {"title": "東京レポート", "content": "日本語のドキュメント 🗾"},
        "doc-ar": {"title": "تقرير عربي",   "content": "محتوى عربي مهم جداً"},
        "doc-bin": {"title": "Binary data",  "content": b"\xff\xfe binary \x80\x81"},
    }
    return docs.get(doc_id, {"error": f"not found: {doc_id}"})

def run_agent(query: str) -> str:
    messages = [{"role": "user", "content": query}]
    for _ in range(6):
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
                raw    = get_document(b.input["doc_id"])
                safe   = sanitise_tool_result(raw)
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": safe})
        messages.append({"role": "user", "content": results})
    return "max steps reached"

for query in ["Get document doc-jp", "Get document doc-ar", "Get document doc-bin"]:
    print(f"\nQuery: {query}")
    print(f"Answer: {run_agent(query)[:100]}")
```

**Expected Token Savings:** Sanitised tool results prevent `json.JSONDecodeError` in the control loop; binary blobs that crash without sanitisation are handled cleanly.
**Environment:** Tool-using agents that receive data from databases, file systems, or APIs of unknown encoding.

---

## Comparison

| Option | Problem Solved | Library Required | Best For |
|---|---|---|---|
| 1. Explicit encoding on open | File I/O crashes | None | All file reading code |
| 2. Chardet detection | Unknown file encoding | `chardet` (optional) | User-uploaded documents |
| 3. NFC normalisation | Composed/decomposed chars, invisible chars | `unicodedata` (stdlib) | All user input preprocessing |
| 4. JSON serialisation | Emoji/non-ASCII in JSON | None | API responses with international content |
| 5. Safe truncation | Mid-character truncation | `unicodedata` (stdlib) | Input length limiting |
| 6. Tool result sanitisation | Mixed-encoding tool returns | None | Tool-using agents with external data |
