---
layout: solution
title: "Agent Creates New Files Instead of Editing Existing Ones — Duplicate File Proliferation"
category: general
description: "Agent creates new_utils.py instead of editing utils.py, creates config_updated.yaml instead of editing config.yaml, or duplicates entire modules. Codebase fills with agent-generated duplicates."
tags: [file-editing, duplicate-files, codebase-hygiene, conventions]
---

# Agent Creates New Files Instead of Editing Existing Ones — Duplicate File Proliferation

## Symptom
- Directory fills with `*_new.py`, `*_v2.py`, `*_updated.yaml` files
- Agent creates `auth_fixed.py` when `auth.py` should have been edited
- Agent writes a complete replacement file instead of making surgical edits
- Import references still point to old file — new file is never used
- After 10 sessions, there are multiple versions of the same module

## Root Cause
Without explicit instruction to edit existing files, the model defaults to "create a new version" — a safe approach that avoids risking the original. This is especially common when the agent is uncertain about modifying existing code or when it was trained on examples that create new files.

## Fix

### Option 1: Explicit "edit existing" instruction

```
System prompt:
"File management rules:
- ALWAYS edit existing files rather than creating new ones
- NEVER create files named *_new.*, *_v2.*, *_updated.*, *_fixed.*, *_backup.*
- If a file exists: open it, find the section to change, edit in place
- Only create a new file if it genuinely doesn't exist yet

Bad: 'I'll create auth_fixed.py with the corrected code'
Good: 'I'll edit line 47 of auth.py to fix the null check'"
```

### Option 2: Read-before-write requirement

```
System prompt:
"Before writing any file:
1. Check if a file with that name or purpose already exists
2. If yes: edit the existing file — do not create a new one
3. If no: then create it

Never write a file without first searching for existing files with similar names."
```

### Option 3: Surgical edit instruction

```
System prompt:
"When fixing code, make the minimum change necessary:
- Find the specific function/line that needs changing
- Edit only that part
- Leave all other code unchanged

Do not rewrite entire files. Do not create replacement versions.
Show the diff of what changed, not the complete new file."
```

### Option 4: Detect and prevent duplicate creation

```python
import os, glob

def prevent_duplicate_file(proposed_path, agent_response):
    """Check if agent is about to create a duplicate"""
    DUPLICATE_PATTERNS = ['_new.', '_v2.', '_updated.', '_fixed.', '_backup.', '_copy.']

    filename = os.path.basename(proposed_path)
    if any(p in filename for p in DUPLICATE_PATTERNS):
        # Find the likely original
        base = re.sub(r'_(new|v2|updated|fixed|backup|copy)', '', os.path.splitext(filename)[0])
        originals = glob.glob(f"**/{base}.*", recursive=True)
        if originals:
            return f"Warning: {proposed_path} looks like a duplicate of {originals}. Edit the original instead."
    return None
```

### Option 5: Git-based verification

```bash
# After agent session — check for new files that shouldn't exist
git status | grep "??"  # Untracked files

# Check for suspicious naming patterns
git status | grep -E "_(new|v2|updated|fixed|backup)\."

# If found — check if they should have been edits instead
git diff HEAD -- the_original_file.py
```

## Cleanup Strategy

```python
import os, glob, re

def find_agent_duplicates(directory="."):
    """Find files that look like agent-created duplicates"""
    pattern = re.compile(r'_(new|v2|updated|fixed|backup|copy|2)\.(py|js|ts|yaml|json|md)$')
    duplicates = []

    for path in glob.glob(f"{directory}/**/*", recursive=True):
        if pattern.search(os.path.basename(path)):
            # Check if original exists
            original = pattern.sub(r'.\2', os.path.basename(path))  # Simplified
            duplicates.append({"duplicate": path, "possible_original": original})

    return duplicates
```

## Expected Token Savings
Cleaning up duplicate file proliferation after 10 sessions: ~8,000 tokens
Explicit edit-existing instruction: prevents all duplicates

## Environment
- Any code-editing agent with file creation capability
- Source: direct experience, very common in agent-assisted development
