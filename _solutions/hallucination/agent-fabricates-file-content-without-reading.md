---
layout: solution
title: "Agent Fabricates File Contents Without Actually Reading the File"
category: hallucination
description: "Agent is asked about a file's contents. Instead of reading it, the agent invents plausible-sounding content based on the filename or context. Generated content looks real but is completely fabricated."
tags: [hallucination, file-reading, fabrication, grounding, tool-use, verification]
---

# Agent Fabricates File Contents Without Actually Reading the File

## Symptom
- Agent describes `config.yaml` contents confidently — none of it matches the actual file
- Agent "quotes" from a README that doesn't say what the agent claims
- Agent generates code that "matches" a file it was shown only the name of
- File contains errors, but agent reports it looks fine (never actually read it)
- Agent says "based on the file you shared" but no file was shared

## Root Cause
LLMs generate plausible content based on patterns in training data. A filename like `database_config.yaml` strongly predicts what content it *likely* contains based on thousands of similar files. The model can generate convincing fake content that matches conventions — but misses the actual specific values in the real file.

## Fix

### Option 1: Force file reading before any file-related question

```
System prompt:
"File access rules:
1. NEVER describe, summarize, or quote file contents without first calling the read_file tool
2. If asked about a file's contents, always read it first — do not predict or assume
3. If you cannot read a file, say 'I need to read this file first. Please share its contents.'
4. Never say 'the file probably contains' or 'it likely has' — read it and report what IS there
5. Treat all file paths as references to content you have not seen until you read them"
```

### Option 2: Tool that forces real file access

```python
import os
from pathlib import Path

def read_file(path: str) -> dict:
    """
    Tool that agents MUST use to read files.
    Returns actual content — no fabrication possible.
    """
    file_path = Path(path).resolve()

    if not file_path.exists():
        return {
            "success": False,
            "error": f"File does not exist: {file_path}",
            "suggestion": "Check the path. Use list_directory() to see available files."
        }

    if not file_path.is_file():
        return {
            "success": False,
            "error": f"Path is not a file: {file_path} (it's a {file_path.stat().st_mode})"
        }

    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
        return {
            "success": True,
            "path": str(file_path),
            "size_bytes": file_path.stat().st_size,
            "lines": content.count("\n"),
            "content": content
        }
    except PermissionError:
        return {"success": False, "error": f"Permission denied: {file_path}"}

# Register as a tool and instruct agent to always call it
TOOLS = [
    {
        "name": "read_file",
        "description": "Read the contents of a file. ALWAYS use this before discussing any file's contents.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute or relative file path"}
            },
            "required": ["path"]
        }
    }
]
```

### Option 3: Verify agent read the file before trusting output

```python
async def verify_file_was_read(agent_response: str, file_path: str, actual_content: str) -> bool:
    """Check if agent's response matches actual file content"""
    # Extract a specific unique value from actual file
    actual_lines = actual_content.strip().split("\n")
    if not actual_lines:
        return True  # Empty file, can't verify

    # Check if agent's response contains a specific line from the actual file
    unique_line = actual_lines[0]  # First line is usually unique
    return unique_line[:30] in agent_response

async def ask_about_file(file_path: str, question: str, agent) -> str:
    actual_content = open(file_path).read()

    # Force-include file content in the prompt
    grounded_prompt = f"""File: {file_path}

ACTUAL CONTENTS:
---
{actual_content}
---

Question: {question}

Answer based ONLY on the actual file contents above."""

    return await agent.complete(grounded_prompt)
```

### Option 4: Require agent to cite specific lines

```
System prompt:
"When discussing file contents:
1. Always cite the specific line number and exact text you are referring to
2. Format: 'Line 15 says: [exact quote]'
3. Never paraphrase — quote directly
4. If you cannot find the specific text in the file you read, say so

This forces you to have actually read the file — fabricated content will fail to cite correctly."
```

### Option 5: Canary values to detect fabrication

```python
import hashlib, random, string

def inject_canary_into_file(file_path: str) -> str:
    """Add a unique random value to a file to detect if agent fabricates content"""
    canary = "CANARY_" + "".join(random.choices(string.ascii_uppercase, k=8))
    with open(file_path, "a") as f:
        f.write(f"\n# {canary}\n")
    return canary

def verify_agent_read_file(agent_response: str, canary: str) -> bool:
    """If agent actually read the file, it should mention the canary"""
    return canary in agent_response

# Usage
canary = inject_canary_into_file("config.yaml")
response = await agent.complete("Describe the contents of config.yaml")

if not verify_agent_read_file(response, canary):
    print(f"WARNING: Agent did not read config.yaml — response may be fabricated")
    print(f"Expected canary '{canary}' in response but it was absent")
```

### Option 6: System prompt with anti-fabrication instruction

```
System prompt:
"Anti-hallucination rules for file operations:

You have a 'read_file' tool. You MUST use it before any statement about file contents.

Forbidden phrases (these indicate you're fabricating, not reading):
- 'The file likely contains...'
- 'Based on the filename...'
- 'Typically, this kind of file...'
- 'The config probably has...'

Required pattern:
1. User asks about file
2. YOU CALL read_file(path)
3. You see actual content
4. You describe actual content

If the tool returns an error: say 'I cannot read this file: [error]'
Never invent what the file says."
```

## Fabrication Risk by File Type

| File type | Fabrication risk | Why |
|---|---|---|
| `config.yaml` / `.env` | Very high | Strong conventions predict content |
| `README.md` | High | Common sections are predictable |
| `requirements.txt` | High | Common packages are predictable |
| `main.py` | Medium | Structure is predictable but specifics vary |
| `data.csv` | Low | Row data is unpredictable |
| Binary files | None | Model can't fabricate binary |

## Expected Token Savings
Debugging consequences of acting on fabricated file content: ~10,000 tokens
Forcing read_file tool call before all file questions: 0 wasted

## Environment
- Any code assistant or file-editing agent
- Source: direct experience; one of the most dangerous hallucination patterns in coding agents
