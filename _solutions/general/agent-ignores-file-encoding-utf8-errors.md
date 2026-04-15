---
layout: solution
title: "Agent Fails on Non-UTF-8 Files — UnicodeDecodeError Reading Source Files"
category: general
description: "Agent reads a file and crashes with UnicodeDecodeError. File uses latin-1, cp1252, or other encoding. Common with legacy codebases, Windows files, or CSV exports from Excel."
tags: [encoding, unicode, utf-8, csv, file-reading, UnicodeDecodeError]
---

# Agent Fails on Non-UTF-8 Files — UnicodeDecodeError Reading Source Files

## Symptom
- `UnicodeDecodeError: 'utf-8' codec can't decode byte 0x92 in position 45`
- Agent crashes reading CSV files exported from Excel
- Legacy codebase files open fine in editors but fail in agent
- Works on some files, fails on others that look identical
- Error mentions specific bytes like `0x92`, `0xe9`, `0xa0`

## Root Cause
Python 3's default file encoding is UTF-8 (or the system locale). Files saved by Windows applications (Excel, Notepad, older IDEs) often use `cp1252` (Windows-1252) or `latin-1`. Files from Asian systems may use `gbk`, `shift-jis`, or `euc-kr`. Opening these with `open(path)` defaults to UTF-8 and fails.

## Fix

### Option 1: Detect encoding with chardet/charset-normalizer

```python
# pip install chardet
import chardet

def read_file_autodetect(path: str) -> str:
    """Read file with automatic encoding detection"""
    raw = open(path, "rb").read()
    detected = chardet.detect(raw)
    encoding = detected.get("encoding") or "utf-8"
    confidence = detected.get("confidence", 0)

    print(f"Detected encoding: {encoding} (confidence: {confidence:.0%})")

    try:
        return raw.decode(encoding)
    except (UnicodeDecodeError, LookupError):
        # Fallback to latin-1 (accepts any byte value)
        print(f"Decoding with {encoding} failed, falling back to latin-1")
        return raw.decode("latin-1")
```

```python
# pip install charset-normalizer (more accurate, no C dependency)
from charset_normalizer import from_bytes

def read_file_normalized(path: str) -> str:
    raw = open(path, "rb").read()
    result = from_bytes(raw).best()
    return str(result)
```

### Option 2: Try encodings in priority order

```python
ENCODING_PRIORITY = [
    "utf-8",
    "utf-8-sig",   # UTF-8 with BOM (common from Excel)
    "cp1252",      # Windows Western European
    "latin-1",     # ISO 8859-1 (accepts all byte values)
    "gbk",         # Chinese Simplified
    "shift-jis",   # Japanese
    "euc-kr",      # Korean
]

def read_file_tolerant(path: str) -> tuple[str, str]:
    """Returns (content, encoding_used)"""
    raw = open(path, "rb").read()

    for encoding in ENCODING_PRIORITY:
        try:
            return raw.decode(encoding), encoding
        except (UnicodeDecodeError, LookupError):
            continue

    # Last resort: replace undecodable bytes
    return raw.decode("utf-8", errors="replace"), "utf-8 (with replacements)"

content, enc = read_file_tolerant("legacy_file.csv")
print(f"Read {len(content)} chars using {enc}")
```

### Option 3: Safe file reader with errors parameter

```python
def read_file_safe(path: str, encoding: str = "utf-8") -> str:
    """Read file, replacing undecodable characters instead of crashing"""
    with open(path, encoding=encoding, errors="replace") as f:
        content = f.read()

    # Check if replacement characters appeared (indicates wrong encoding)
    if "\ufffd" in content:
        replacement_count = content.count("\ufffd")
        print(f"Warning: {replacement_count} undecodable characters replaced with \ufffd")
        print("Try detecting encoding with: chardet.detect(open(path, 'rb').read())")

    return content
```

### Option 4: Handle CSV files specifically

```python
import csv, chardet

def read_csv_autodetect(path: str) -> list[dict]:
    """Read CSV with automatic encoding detection"""
    raw = open(path, "rb").read()
    encoding = chardet.detect(raw).get("encoding", "utf-8")

    with open(path, encoding=encoding, newline="", errors="replace") as f:
        reader = csv.DictReader(f)
        return list(reader)

# For pandas
import pandas as pd

def read_csv_pandas(path: str) -> pd.DataFrame:
    """Read CSV trying multiple encodings"""
    for encoding in ["utf-8", "utf-8-sig", "cp1252", "latin-1"]:
        try:
            return pd.read_csv(path, encoding=encoding)
        except UnicodeDecodeError:
            continue
    # Final fallback
    return pd.read_csv(path, encoding="latin-1", errors="replace")
```

### Option 5: Convert file to UTF-8 before processing

```bash
# Convert file to UTF-8 using iconv
iconv -f cp1252 -t utf-8 input.csv > output_utf8.csv

# Detect encoding first
file -i legacy_file.txt       # Linux: shows charset
enca legacy_file.txt          # More detailed detection

# Python one-liner to convert
python3 -c "
import chardet
raw = open('input.csv', 'rb').read()
enc = chardet.detect(raw)['encoding']
open('output.csv', 'w', encoding='utf-8').write(raw.decode(enc))
print(f'Converted from {enc} to UTF-8')
"
```

### Option 6: System prompt guidance for agent

```
System prompt:
"When reading files:
1. Always open files in binary mode first to detect encoding
2. Use chardet.detect() if the file may not be UTF-8
3. Never assume all files are UTF-8 — especially CSV, legacy code files, and Windows files
4. If you encounter UnicodeDecodeError, try cp1252 or latin-1 before giving up
5. latin-1 accepts any single byte — use as absolute fallback (content may have garbled chars)"
```

## Encoding by File Source

| Source | Likely encoding |
|---|---|
| Modern web/API | UTF-8 |
| Excel CSV export (Western) | cp1252 / UTF-8 with BOM |
| Windows Notepad (old) | cp1252 |
| Linux files | UTF-8 |
| macOS files | UTF-8 |
| Japanese text files | shift-jis or euc-jp |
| Chinese text files | gbk or big5 |
| Old Python 2 source | latin-1 or ascii |
| SQL dumps (MySQL) | latin-1 or utf-8 |

## Expected Token Savings
Debugging encoding errors + retrying: ~3,000 tokens
Auto-detect encoding upfront: 0 wasted

## Environment
- Any agent reading files from mixed sources; most common with CSV ingestion
- Source: direct experience with legacy codebases and Excel exports
