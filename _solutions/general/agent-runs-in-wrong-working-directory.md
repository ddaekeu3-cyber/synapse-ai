---
layout: solution
title: "Agent Runs Commands in Wrong Directory — File Operations Fail or Affect Wrong Files"
category: general
description: "Agent executes shell commands or file operations from an unexpected working directory. Writes to wrong paths, reads empty results, or modifies files in unexpected locations."
tags: [working-directory, cwd, file-paths, bash, shell]
---

# Agent Runs Commands in Wrong Directory — File Operations Fail or Affect Wrong Files

## Symptom
- Agent runs `ls` and gets unexpected results (different files than expected)
- `cat config.yaml` returns "No such file" but file exists in the project
- Agent writes output to wrong directory
- Git commands affect wrong repository
- `pwd` returns `/` or `/tmp` instead of the project directory
- Problem: agent changes directory during task but doesn't restore it

## Root Cause
Shell sessions don't persist `cd` state between separate command executions in many agent frameworks. Or the agent's working directory is set to the container/process root, not the project directory. Sometimes the agent `cd`s into a subdirectory mid-task and subsequent commands run there.

## Fix

### Option 1: Use absolute paths instead of relative

```python
# BAD — relative path depends on cwd
subprocess.run(["cat", "config.yaml"])

# GOOD — absolute path works regardless of cwd
import os
PROJECT_ROOT = "/workspace/my-project"
subprocess.run(["cat", os.path.join(PROJECT_ROOT, "config.yaml")])
```

In agent prompts:
```
"Always use absolute paths for file operations.
The project root is /workspace. Reference files as /workspace/path/to/file,
not relative paths like ./path/to/file."
```

### Option 2: Set and verify cwd at task start

```python
import os, subprocess

PROJECT_ROOT = os.environ.get("PROJECT_ROOT", "/workspace")

def run_in_project(command, **kwargs):
    """Always run commands from the project root"""
    return subprocess.run(command, cwd=PROJECT_ROOT, **kwargs)

# Verify on startup
def verify_cwd():
    current = os.getcwd()
    if current != PROJECT_ROOT:
        print(f"Warning: cwd is {current}, expected {PROJECT_ROOT}")
        os.chdir(PROJECT_ROOT)
```

### Option 3: Include cwd check in every command block

```bash
# Add pwd to verify cwd before important commands
pwd && ls
pwd && git status
pwd && cat config.yaml
```

For agent prompt:
```
"Before any file operation, run `pwd` to verify your working directory.
If it's not the expected project root, run `cd /workspace/project` first."
```

### Option 4: Docker — set WORKDIR explicitly

```dockerfile
FROM node:20
WORKDIR /workspace/agent   # This becomes the default cwd for the agent process
COPY . .
CMD ["node", "agent.js"]
```

```yaml
# docker-compose.yml
services:
  agent:
    working_dir: /workspace/agent  # Also set here for override
```

### Option 5: Detect and fix wrong cwd in system prompt

```
System prompt:
"Your working directory is /workspace/project.
If any file operation returns 'No such file' and the file should exist,
first check your working directory with `pwd`.
If not in /workspace/project, run `cd /workspace/project` before retrying."
```

## Diagnosis

```bash
# Run at start of any debugging session
pwd                    # What directory is the agent actually in?
ls -la                 # What files are visible?
echo $PROJECT_ROOT     # Is the env var set?
git rev-parse --show-toplevel 2>/dev/null  # What's the git root?
```

## Expected Token Savings
Debugging wrong-directory failures across multiple file operations: ~8,000 tokens
Absolute paths from the start: prevents all cwd-related failures

## Environment
- Any agent running shell commands or file operations
- Higher risk: Docker containers, subprocess-based tools
- Source: direct experience, extremely common in containerized agent deployments
