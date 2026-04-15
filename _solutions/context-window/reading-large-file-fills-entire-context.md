---
layout: solution
title: "Reading a Large File Fills the Entire Context Window — No Room for Reasoning"
category: context-window
description: "Agent reads a large file (log, codebase, CSV) and the contents consume the entire context. Agent has no tokens left for analysis, tool calls, or output. Task fails or truncates."
tags: [context-window, file-read, large-file, truncation, chunking]
---

# Reading a Large File Fills the Entire Context Window — No Room for Reasoning

## Symptom
- Agent reads a log file, code file, or data file
- Response is truncated or agent stops mid-analysis
- API returns 400 context_length_exceeded after reading the file
- Agent reads file, then can't call any more tools (context full)
- File is 100KB+ and model max input is 200K tokens (but 100K of those are the file)

## Root Cause
A 100KB text file is approximately 25,000 tokens. Reading it fully leaves only 175K tokens for the system prompt, conversation history, tool results, and output. Large log files, compiled outputs, or data exports can easily exceed this. Reading the whole file is almost never necessary.

## Fix

### Option 1: Read only relevant sections

```python
def read_file_sections(path, section="head", lines=100):
    """Read only the part of the file that's needed"""
    with open(path) as f:
        all_lines = f.readlines()

    if section == "head":
        return "".join(all_lines[:lines])
    elif section == "tail":
        return "".join(all_lines[-lines:])
    elif section == "middle":
        mid = len(all_lines) // 2
        return "".join(all_lines[mid:mid+lines])

# Instead of reading 50,000 lines:
# Read first 100 lines to understand structure
# Read last 100 lines to see recent events
```

### Option 2: Search before reading

```python
import subprocess

def search_file(path, pattern, context_lines=5):
    """Get only lines matching a pattern — much smaller than full file"""
    result = subprocess.run(
        ["grep", "-n", "-C", str(context_lines), pattern, path],
        capture_output=True, text=True
    )
    return result.stdout  # Only matching sections, not the entire file
```

For agent prompts:
```
"Before reading any file:
1. Check the file size: `wc -l <file>` and `wc -c <file>`
2. If file is >1000 lines, use grep to find relevant sections rather than reading the whole file
3. Only read the full file if it's <500 lines"
```

### Option 3: Chunk and summarize

```python
async def analyze_large_file(path, agent, chunk_size=2000):
    """Process file in chunks, summarizing each"""
    with open(path) as f:
        lines = f.readlines()

    summaries = []
    for i in range(0, len(lines), chunk_size):
        chunk = "".join(lines[i:i+chunk_size])
        summary = await agent.complete(
            f"Summarize this section of the log file in 3 bullet points, "
            f"highlighting any errors or anomalies:\n\n{chunk}"
        )
        summaries.append(f"Lines {i}-{i+chunk_size}: {summary}")

    # Final analysis on summaries (much smaller than original)
    return await agent.complete(
        f"Based on these section summaries, what's the main issue?\n\n"
        + "\n".join(summaries)
    )
```

### Option 4: Structured extraction instead of raw read

```python
import re

def extract_errors_from_log(path, max_errors=50):
    """Extract only error lines instead of full log"""
    errors = []
    with open(path) as f:
        for line_num, line in enumerate(f, 1):
            if re.search(r'\b(ERROR|FATAL|CRITICAL|Exception|Traceback)\b', line):
                errors.append(f"Line {line_num}: {line.rstrip()}")
                if len(errors) >= max_errors:
                    errors.append(f"... ({sum(1 for _ in open(path)) - max_errors} more errors)")
                    break
    return "\n".join(errors)
```

### Option 5: Instruction to agent for file handling

```
System prompt:
"File reading rules:
- Always check file size before reading: `wc -c <file>`
- For files >50KB: use grep/search tools, not full read
- For files >1MB: only extract specific sections (first/last N lines, or search)
- Never read the full content of log files, database dumps, or build artifacts
- If you need to analyze a large file, summarize sections progressively"
```

## File Size → Token Estimate

| File size | Approx tokens | Strategy |
|-----------|--------------|---------|
| <10KB | <2,500 | Read fully |
| 10-50KB | 2,500-12,500 | Read with caution |
| 50-200KB | 12,500-50,000 | Read relevant sections only |
| >200KB | >50,000 | Search/extract only |

## Expected Token Savings
Reading 100KB log file fully: ~25,000 tokens input (often useless)
Grep for errors: ~500 tokens — 98% savings

## Environment
- Any agent with file reading capability
- Common offenders: log files, CSV exports, compiled JS/CSS, database dumps
- Source: direct experience
