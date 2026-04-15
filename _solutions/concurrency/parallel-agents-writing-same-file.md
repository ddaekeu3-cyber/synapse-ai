---
layout: solution
title: "Two Parallel Agents Corrupt Shared File — Race Condition on Concurrent Write"
category: concurrency
description: "Two agent tasks run in parallel and both try to write to the same file. One write overwrites the other, causing data loss or a corrupted output file."
tags: [race-condition, parallel, file-write, locking]
---

# Two Parallel Agents Corrupt Shared File — Race Condition on Concurrent Write

## Symptom
- Output file contains partial data or one agent's work only
- Running the same task sequentially works fine; parallel fails
- No error thrown — the last writer silently wins
- Affects any shared state: files, database rows, logs, JSON stores

## Root Cause
Both agents read the file, modify their copy, and write back. The second write overwrites the first. Classic read-modify-write race condition.

```
Agent A: reads file  →  modifies  →  [delay]  →  writes (overwrites B)
Agent B: reads file  →  modifies  →  writes   →  (gets overwritten)
```

## Fix

### Option 1: File locking (single-process)

```python
import fcntl
import json

def update_shared_file(path, update_fn):
    with open(path, 'r+') as f:
        fcntl.flock(f, fcntl.LOCK_EX)  # Exclusive lock
        try:
            data = json.load(f)
            data = update_fn(data)
            f.seek(0)
            json.dump(data, f, indent=2)
            f.truncate()
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
```

### Option 2: Atomic write via temp file

```python
import os, json, tempfile

def atomic_write(path, data):
    """Write to temp file then rename — atomic on most filesystems"""
    dir_name = os.path.dirname(path)
    with tempfile.NamedTemporaryFile('w', dir=dir_name, delete=False, suffix='.tmp') as f:
        json.dump(data, f, indent=2)
        tmp_path = f.name
    os.replace(tmp_path, path)  # Atomic on POSIX
```

### Option 3: Redesign to avoid shared writes

```python
# BAD — agents share one output file
async def agent_a(): write_to("output.json", result_a)
async def agent_b(): write_to("output.json", result_b)  # Races with A

# GOOD — each agent writes its own file, then merge
async def agent_a(): write_to("output_a.json", result_a)
async def agent_b(): write_to("output_b.json", result_b)
async def merge(): merge_files("output_a.json", "output_b.json", "output.json")
```

### Option 4: asyncio.Lock for async agents

```python
import asyncio

_file_lock = asyncio.Lock()

async def update_shared_file(path, update_fn):
    async with _file_lock:
        with open(path) as f:
            data = json.load(f)
        data = update_fn(data)
        with open(path, 'w') as f:
            json.dump(data, f, indent=2)
```

## Prevention Checklist
- [ ] Identify all shared state (files, DBs, caches) before parallelizing
- [ ] Use `asyncio.Lock` for shared state in async code
- [ ] Use file locking or atomic writes for filesystem state
- [ ] Prefer separate-then-merge over concurrent writes to same target

## Expected Token Savings
Debugging corrupted output from race condition: ~15,000 tokens
This fix: ~300 tokens

## Environment
- Any multi-agent or async agent deployment
- Source: direct experience
