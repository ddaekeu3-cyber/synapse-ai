---
layout: solution
title: "Agent Crashes on Unicode, Emoji, or Non-ASCII Input"
category: general
description: "User sends a message with emoji or non-Latin characters. Agent crashes with UnicodeDecodeError, UnicodeEncodeError, or replaces characters with '???'. Database insert fails with 'incorrect string value'. File output is garbled."
tags: [unicode, encoding, emoji, utf-8, chardet, crash, non-ascii, internationalization]
---

# Agent Crashes on Unicode, Emoji, or Non-ASCII Input

## Symptom
- `UnicodeDecodeError: 'ascii' codec can't decode byte 0xf0 in position 3`
- `UnicodeEncodeError: 'latin-1' codec can't encode character '\U0001f600'`
- Emoji in user input replaced with `?` or `\ufffd` in the output
- MySQL/Postgres error: `Incorrect string value: '\xF0\x9F\x98\x80'` (emoji not stored)
- JSON serialization fails: `json.dumps()` on string with surrogate characters
- Log file shows `\u0000` or corrupted bytes where emoji should be

## Root Cause
Python 2 legacy code, `open()` without `encoding="utf-8"`, databases configured for `latin1` instead of `utf8mb4`, or `json.dumps()` without `ensure_ascii=False` all cause encoding failures. Emoji require 4-byte UTF-8 sequences (U+1F000 and above). MySQL's `utf8` charset only supports 3-byte sequences — emoji need `utf8mb4`. Python's default `ascii` codec rejects anything above U+007F.

## Fix

### Option 1: Always open files with explicit UTF-8 encoding

```python
import io

# WRONG — uses platform default encoding (often not UTF-8 on Windows)
with open("output.txt", "w") as f:
    f.write("Hello 🌍")  # May crash or garble

# RIGHT — explicit UTF-8 always
with open("output.txt", "w", encoding="utf-8") as f:
    f.write("Hello 🌍")

# RIGHT — binary mode with explicit encode/decode
with open("output.txt", "wb") as f:
    f.write("Hello 🌍".encode("utf-8"))

# Set default encoding for all file operations in your process:
import sys
# Check current default:
print(sys.getdefaultencoding())  # Should be 'utf-8'
# If not utf-8, set PYTHONIOENCODING=utf-8 environment variable

# Read files defensively:
def read_file_safe(path: str) -> str:
    """Read file with UTF-8, falling back to latin-1 for legacy files"""
    try:
        return open(path, encoding="utf-8").read()
    except UnicodeDecodeError:
        print(f"Warning: {path} is not UTF-8, trying latin-1")
        return open(path, encoding="latin-1").read()
```

### Option 2: Sanitize user input for encoding issues

```python
import unicodedata
import re

def normalize_unicode(text: str, mode: str = "NFC") -> str:
    """
    Normalize Unicode to a consistent form.
    NFC: composed form (preferred for storage)
    NFKC: compatibility decomposition (useful for search/comparison)
    """
    return unicodedata.normalize(mode, text)

def sanitize_text_input(text: str, allow_emoji: bool = True) -> str:
    """
    Sanitize text for safe processing and storage.
    """
    if not isinstance(text, str):
        text = str(text)

    # Normalize Unicode
    text = normalize_unicode(text)

    # Remove null bytes (cause issues in C-based libraries and databases)
    text = text.replace("\x00", "")

    # Remove or replace surrogates (broken Unicode from bad decoding)
    text = text.encode("utf-8", errors="surrogatepass").decode("utf-8", errors="replace")

    if not allow_emoji:
        # Remove emoji (characters outside BMP + emoji blocks)
        text = re.sub(
            r'[\U0001F000-\U0001FFFF\U0002F000-\U0002FFFF]',
            '',
            text
        )

    return text.strip()

# Usage:
user_message = sanitize_text_input(raw_user_input)
```

### Option 3: JSON with proper Unicode handling

```python
import json

text_with_emoji = "Hello 🌍! Привет! 日本語"

# WRONG — escapes all non-ASCII to \uXXXX sequences
json.dumps({"text": text_with_emoji})
# → '{"text": "Hello \\ud83c\\udf0d! \\u041f\\u0440\\u0438..."}'

# RIGHT — keep Unicode as-is (smaller, human-readable)
json.dumps({"text": text_with_emoji}, ensure_ascii=False)
# → '{"text": "Hello 🌍! Привет! 日本語"}'

# For API responses, always use ensure_ascii=False:
def safe_json_dumps(obj, **kwargs) -> str:
    return json.dumps(obj, ensure_ascii=False, **kwargs)

def safe_json_loads(text: str) -> dict:
    """Parse JSON, handling BOM and encoding issues"""
    # Remove BOM if present
    text = text.lstrip("\ufeff")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        # Try re-encoding as UTF-8 if parsing fails
        if isinstance(text, bytes):
            text = text.decode("utf-8", errors="replace")
        return json.loads(text.encode("utf-8").decode("utf-8"))
```

### Option 4: Database emoji support (MySQL utf8mb4)

```python
import pymysql

# WRONG — MySQL 'utf8' only supports 3-byte UTF-8, not emoji
connection = pymysql.connect(
    host="localhost",
    db="mydb",
    charset="utf8"  # Can't store emoji!
)

# RIGHT — utf8mb4 supports full Unicode including emoji
connection = pymysql.connect(
    host="localhost",
    db="mydb",
    charset="utf8mb4",
    use_unicode=True
)

# Also run in MySQL:
# ALTER DATABASE mydb CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
# ALTER TABLE messages CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

# PostgreSQL (already full UTF-8 — just ensure encoding):
import psycopg2
conn = psycopg2.connect(
    dsn="postgresql://localhost/mydb",
    # PostgreSQL uses UTF-8 by default — no special charset needed
)

# SQLite (UTF-8 by default — no configuration needed)
import sqlite3
conn = sqlite3.connect("data.db")
# → Works with all Unicode including emoji out of the box
```

### Option 5: Detect encoding of incoming bytes

```python
import chardet

def decode_bytes_safely(data: bytes) -> str:
    """
    Decode bytes to string, detecting encoding if unknown.
    Priority: UTF-8 → BOM detection → chardet → latin-1 fallback.
    """
    # 1. Try UTF-8 first (most common)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass

    # 2. Check for BOM (byte order mark)
    if data.startswith(b"\xff\xfe"):
        return data.decode("utf-16-le")
    if data.startswith(b"\xfe\xff"):
        return data.decode("utf-16-be")
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8")

    # 3. Use chardet to detect encoding
    detected = chardet.detect(data)
    if detected["confidence"] > 0.7 and detected["encoding"]:
        try:
            return data.decode(detected["encoding"])
        except (UnicodeDecodeError, LookupError):
            pass

    # 4. Fallback: latin-1 never fails (every byte is valid)
    print(f"Warning: encoding unknown, falling back to latin-1")
    return data.decode("latin-1")

# Reading files from external sources:
with open("unknown_encoding_file.txt", "rb") as f:
    raw_bytes = f.read()
text = decode_bytes_safely(raw_bytes)
```

### Option 6: Log and HTTP response encoding

```python
import logging
import sys

# Ensure stdout/stderr use UTF-8 (critical on Windows)
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Logging handler with explicit UTF-8
handler = logging.FileHandler("agent.log", encoding="utf-8")
handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
logging.getLogger().addHandler(handler)

# FastAPI/Flask response encoding:
from fastapi import FastAPI
from fastapi.responses import JSONResponse

app = FastAPI()

@app.get("/result")
async def get_result() -> JSONResponse:
    data = {"message": "Hello 🌍! こんにちは!"}
    # JSONResponse uses json.dumps with ensure_ascii=False by default in FastAPI
    return JSONResponse(content=data, media_type="application/json; charset=utf-8")

# Requests library — always returns str (UTF-8 decoded):
import requests
response = requests.get("https://api.example.com/data")
response.encoding = "utf-8"  # Force UTF-8 if server doesn't specify
text = response.text  # Always str, decoded correctly
```

## Encoding Error Reference

| Error | Cause | Fix |
|---|---|---|
| `UnicodeDecodeError: 'ascii'` | `open()` without `encoding=` | Add `encoding="utf-8"` |
| `UnicodeEncodeError: 'latin-1'` | Writing non-Latin chars to latin-1 stream | Use `encoding="utf-8"` |
| `Incorrect string value: '\xF0...'` | MySQL with `utf8` charset (not `utf8mb4`) | Change table charset to `utf8mb4` |
| `\ufffd` replacement character | Decoding with wrong codec | Detect encoding with chardet first |
| JSON `\u0000` in string | Null bytes in text | Strip nulls: `text.replace("\x00", "")` |
| Surrogate characters | Broken decode → re-encode | `errors="surrogatepass"` then re-encode |

## Expected Token Savings
Unicode crash mid-task → restart → debug → fix: ~12,000 tokens
Defensive encoding at input boundaries: 0 crashes

## Environment
- Any agent processing user-generated content, multilingual text, or social media data; critical for consumer-facing agents
- Source: direct experience; encoding errors are the most common crash for agents moving from English-only dev to international production
