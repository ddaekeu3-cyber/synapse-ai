---
layout: solution
title: "Agent Edits the Wrong File — Confused by Similar File Names or Paths"
category: general
description: "Agent edits config.json instead of config.yaml, modifies the test file instead of the source file, or overwrites the wrong environment's configuration. Silent wrong-file edits."
tags: [file-editing, wrong-file, similar-names, confirmation]
---

# Agent Edits the Wrong File — Confused by Similar File Names or Paths

## Symptom
- Agent confirms it edited `config.yaml` but actually modified `config.json`
- Agent edits `tests/utils.py` when asked to edit `utils.py`
- Agent modifies `.env.production` when `.env` was requested
- No error — agent reports success, but wrong file was changed
- Discovery only happens on deployment or test failure

## Root Cause
When multiple files have similar names, the model may pick the wrong one from context, especially if it doesn't use search tools to verify the path first. The model generates a plausible path based on the request rather than resolving the actual file.

## Fix

### Option 1: Always verify the file path before editing

```
System prompt:
"Before editing any file:
1. Run: find . -name '<filename>' to see all matching files
2. Confirm the exact path with the user if multiple matches exist
3. Read the first 5 lines of the target file to confirm it's correct
4. Only then make the edit

Never assume a file path — verify it exists before modifying."
```

### Option 2: Require full path specification

```
User instruction pattern:
# WEAK — ambiguous
"Edit config.yaml to add the timeout setting"

# STRONG — unambiguous
"Edit /workspace/project/openclaw.config.yaml (not any other config file)
to add: timeout_ms: 30000 under the providers.anthropic section"
```

### Option 3: Pre-edit confirmation check

```python
async def safe_edit_file(path, old_content, new_content, agent):
    """Verify file content before editing"""
    actual_content = open(path).read()

    if old_content not in actual_content:
        similar_files = find_similar_files(os.path.basename(path))
        return await agent.complete(
            f"The content I expected to find in {path} is not there. "
            f"Similar files: {similar_files}. Which file did you mean?"
        )

    # Content verified — safe to edit
    return actual_content.replace(old_content, new_content)
```

### Option 4: List similar files in system prompt context

```python
import glob, os

def get_file_context(target_filename):
    """Find all files with similar names for disambiguation"""
    base = os.path.splitext(target_filename)[0]
    similar = glob.glob(f"**/*{base}*", recursive=True)
    return similar

# Include in agent context
context = f"""
Files matching 'config':
{chr(10).join(get_file_context('config'))}

When editing a config file, specify which one exactly.
"""
```

### Option 5: Rename convention to avoid ambiguity

When designing agent-accessible codebases:
```
# BAD — agent-unfriendly naming
config.yaml      # Which config?
config.json
config.example.yaml

# GOOD — agent-friendly naming
openclaw.config.yaml
agent.config.yaml
app.config.example.yaml
```

## Recovery

When the wrong file was edited:
1. `git diff` to see what actually changed
2. `git checkout <wrong-file>` to revert
3. Re-run with explicit absolute path
4. Add the ambiguous filename to your disambiguation instruction

## Expected Token Savings
Debugging deployment failure caused by wrong file edited: ~10,000 tokens
Pre-edit path verification: ~200 tokens per edit

## Environment
- Any code-editing agent
- Higher risk: projects with many similar config file names
- Source: direct experience
