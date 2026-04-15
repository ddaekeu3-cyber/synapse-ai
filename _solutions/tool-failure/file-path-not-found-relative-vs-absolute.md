---
layout: solution
title: "File Not Found — Agent Confuses Relative and Absolute Paths"
category: tool-failure
description: "Agent uses relative path 'data/input.csv' which resolves to different locations depending on working directory. Works when run from project root, fails from other directories."
tags: [file-path, relative-path, absolute-path, working-directory, tool-failure, FileNotFoundError]
---

# File Not Found — Agent Confuses Relative and Absolute Paths

## Symptom
- `FileNotFoundError: [Errno 2] No such file or directory: 'data/input.csv'`
- Works when agent runs from `/project`, fails when run from `/project/scripts`
- Path works in interactive test, fails in production (different working directory)
- Agent constructs paths like `"../" + filename` which breaks with directory changes
- Docker container has different working directory than local dev

## Root Cause
Relative paths resolve against the current working directory (`os.getcwd()`). When the working directory changes — different terminal location, subprocess, Docker container, or pytest runner — the same relative path points to a different (or nonexistent) location.

## Fix

### Option 1: Always use absolute paths anchored to script location

```python
from pathlib import Path

# WRONG — relative path, breaks outside project root
data_file = Path("data/input.csv")

# RIGHT — absolute path relative to THIS FILE's location
# Works regardless of working directory
THIS_DIR = Path(__file__).parent
PROJECT_ROOT = THIS_DIR.parent  # Go up one level from scripts/ to project/
DATA_DIR = PROJECT_ROOT / "data"

data_file = DATA_DIR / "input.csv"
print(data_file)  # /absolute/path/to/project/data/input.csv — always correct
```

### Option 2: Resolve paths at first use

```python
from pathlib import Path
import os

def resolve_path(path_str: str) -> Path:
    """Convert any path to absolute, resolve symlinks"""
    path = Path(path_str)
    if path.is_absolute():
        return path.resolve()
    # Relative: resolve against CWD and make absolute
    return (Path.cwd() / path).resolve()

def open_file_safe(path_str: str, mode: str = "r"):
    """Open file with path resolution and helpful error message"""
    path = resolve_path(path_str)
    if not path.exists():
        raise FileNotFoundError(
            f"File not found: {path}\n"
            f"Current working directory: {Path.cwd()}\n"
            f"Original path given: {path_str!r}"
        )
    return open(path, mode)
```

### Option 3: Use environment variable for project root

```python
import os
from pathlib import Path

# Set PROJECT_ROOT in your .env or deployment config
PROJECT_ROOT = Path(os.environ.get("PROJECT_ROOT", Path(__file__).parent.parent))

def project_path(*parts: str) -> Path:
    """Get absolute path within project, regardless of CWD"""
    return PROJECT_ROOT.joinpath(*parts)

# Usage
data_file = project_path("data", "input.csv")
config_file = project_path("config", "settings.yaml")
output_dir = project_path("output")
```

### Option 4: Path validation in agent tool

```python
from pathlib import Path
import os

def read_file_tool(path: str) -> str:
    """
    Read a file. Accepts both absolute and relative paths.
    Relative paths are resolved against the project root.
    """
    path_obj = Path(path)

    # Try as-is first
    if path_obj.exists():
        return path_obj.read_text()

    # Try resolving against common locations
    candidates = [
        Path.cwd() / path,
        Path(__file__).parent.parent / path,  # Project root
        Path.home() / path,
    ]

    for candidate in candidates:
        if candidate.exists():
            print(f"Resolved '{path}' to '{candidate}'")
            return candidate.read_text()

    # Nothing found — provide helpful error
    raise FileNotFoundError(
        f"File not found: {path!r}\n"
        f"Searched in:\n" +
        "\n".join(f"  - {c}" for c in [path_obj] + candidates) +
        f"\nCurrent directory: {Path.cwd()}"
    )
```

### Option 5: Normalize all paths in system prompt

```
System prompt:
"File path rules:
1. Always use absolute paths when reading or writing files
2. Never use paths starting with '../' or './'
3. When the user mentions a file, ask for the full absolute path if not provided
4. Use Python's Path(__file__).parent to anchor paths to the script location
5. Before accessing a file, verify it exists with Path(path).exists()
6. Report the full absolute path when you access a file successfully"
```

### Option 6: Path debugging helper

```python
from pathlib import Path
import os

def diagnose_path(path_str: str) -> str:
    """Debug why a path isn't being found"""
    path = Path(path_str)
    report = [
        f"Path given: {path_str!r}",
        f"Is absolute: {path.is_absolute()}",
        f"Current working directory: {Path.cwd()}",
        f"Resolved absolute: {path.resolve()}",
        f"Exists: {path.resolve().exists()}",
    ]

    if not path.is_absolute():
        # Show where relative path resolves
        resolved = (Path.cwd() / path).resolve()
        report.append(f"Resolves to: {resolved}")
        report.append(f"That path exists: {resolved.exists()}")

        # Check parent directories
        for parent in resolved.parents[:3]:
            report.append(f"Parent {parent} exists: {parent.exists()}")

    return "\n".join(report)

# Usage
try:
    open("data/input.csv")
except FileNotFoundError:
    print(diagnose_path("data/input.csv"))
```

## Path Resolution Reference

| Path type | Example | Resolves to |
|---|---|---|
| Absolute | `/home/user/project/data.csv` | Always that exact location |
| Relative to CWD | `data/input.csv` | `{CWD}/data/input.csv` |
| Relative to script | `Path(__file__).parent / "data"` | Directory of current .py file |
| Home-relative | `Path.home() / "data"` | `/home/username/data` |
| Relative to package | `importlib.resources.files(pkg)` | Inside installed package |

## Expected Token Savings
Debugging FileNotFoundError with wrong CWD: ~3,000 tokens
Absolute paths from the start: 0 wasted

## Environment
- Any agent writing or reading files; most common in scripts run from multiple locations
- Source: direct experience; universal issue when code moves between environments
