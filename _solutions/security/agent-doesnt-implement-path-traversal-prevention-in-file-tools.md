---
title: "Agent Doesn't Implement Path Traversal Prevention in File Tools"
description: "File-reading and file-writing tools that accept user-controlled paths are vulnerable to directory traversal attacks (../../../etc/passwd), allowing agents to access or overwrite arbitrary files on the host system."
difficulty: intermediate
category: security
tags: [path-traversal, file-tools, sandbox, security, injection, lfi, directory]
---

## Problem

An agent's `read_file` tool accepts a filename from user input or LLM output and passes it directly to `open()`. A malicious user (or a prompt-injected LLM) can escape the intended working directory and read sensitive system files.

```python
# Broken: no path validation — vulnerable to directory traversal
async def read_file(path: str) -> str:
    with open(path, "r") as f:  # path = "../../etc/passwd" → reads /etc/passwd
        return f.read()

# LLM output: read_file("../../../etc/shadow") → exposes credential hashes
```

---

## Solution 1: Canonical Path Allowlist with os.path.realpath

```python
import os
from pathlib import Path

# Define the single root all file operations must stay within
WORKSPACE_ROOT = Path("/app/workspace").resolve()

def safe_path(user_path: str, workspace: Path = WORKSPACE_ROOT) -> Path:
    """
    Resolve to absolute path and verify it stays within workspace.
    Raises ValueError on traversal attempt.
    """
    # Resolve symlinks and .. components to canonical path
    target = (workspace / user_path).resolve()

    # Verify the canonical path starts with workspace root
    try:
        target.relative_to(workspace)
    except ValueError:
        raise ValueError(
            f"Path traversal denied: '{user_path}' resolves outside workspace "
            f"({target} not under {workspace})"
        )
    return target

async def safe_read_file(user_path: str) -> str:
    """File read tool with path traversal protection."""
    target = safe_path(user_path)
    if not target.exists():
        raise FileNotFoundError(f"File not found: {user_path}")
    if not target.is_file():
        raise ValueError(f"Not a file: {user_path}")
    # Additional limit: don't read files > 10MB
    size = target.stat().st_size
    if size > 10 * 1024 * 1024:
        raise ValueError(f"File too large ({size} bytes): {user_path}")
    return target.read_text(encoding="utf-8", errors="replace")

async def safe_write_file(user_path: str, content: str) -> str:
    """File write tool with path traversal protection."""
    target = safe_path(user_path)
    # Ensure parent directory exists within workspace
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"Written {len(content)} bytes to {user_path}"

async def safe_list_directory(user_path: str) -> list[str]:
    """Directory listing with path traversal protection."""
    target = safe_path(user_path)
    if not target.is_dir():
        raise ValueError(f"Not a directory: {user_path}")
    return [str(p.relative_to(WORKSPACE_ROOT)) for p in target.iterdir()]

# Demo
def test_path_validation():
    assert safe_path("notes/task.txt")           # OK
    assert safe_path("./notes/../notes/task.txt") # OK (resolves within workspace)
    try:
        safe_path("../../etc/passwd")
        assert False, "Should have raised"
    except ValueError as e:
        print(f"Blocked: {e}")
    try:
        safe_path("/etc/passwd")
        assert False, "Should have raised"
    except ValueError as e:
        print(f"Blocked: {e}")
```

---

## Solution 2: Filename Allowlist with Extension and Name Validation

```python
import re
from pathlib import Path

# Only allow specific file extensions in the workspace
ALLOWED_EXTENSIONS = frozenset({
    ".txt", ".md", ".json", ".yaml", ".yml", ".csv",
    ".py", ".js", ".ts", ".html", ".css", ".toml",
})

# Block filenames that could be misused even within workspace
BLOCKED_FILENAME_PATTERNS = [
    re.compile(r"^\."),           # hidden files (.env, .ssh, .aws)
    re.compile(r"\.exe$", re.I),  # executables
    re.compile(r"\.sh$", re.I),   # shell scripts
    re.compile(r"\.\.", ),        # any component with double-dot
]

# Block specific sensitive filenames regardless of location
BLOCKED_NAMES = frozenset({
    ".env", ".env.local", ".env.production",
    "credentials", "credentials.json",
    "secrets.yaml", "secrets.yml",
    "id_rsa", "id_ed25519", "private.key",
})

def validate_filename(path_str: str) -> Path:
    """
    Validate a filename before use.
    Raises ValueError with specific reason on rejection.
    """
    p = Path(path_str)
    name = p.name
    suffix = p.suffix.lower()

    # Block traversal components
    parts = Path(path_str).parts
    for part in parts:
        if part == "..":
            raise ValueError(f"Directory traversal component '..' not allowed")
        if part.startswith("/"):
            raise ValueError(f"Absolute paths not allowed")

    # Block known sensitive filenames
    if name in BLOCKED_NAMES:
        raise ValueError(f"Blocked filename: {name}")

    # Block by pattern
    for pattern in BLOCKED_FILENAME_PATTERNS:
        if pattern.search(name):
            raise ValueError(f"Filename pattern blocked: {name}")

    # Require allowed extension
    if suffix not in ALLOWED_EXTENSIONS:
        raise ValueError(
            f"Extension '{suffix}' not allowed. "
            f"Allowed: {sorted(ALLOWED_EXTENSIONS)}"
        )

    return p

def safe_path_with_validation(user_path: str,
                               workspace: Path = Path("/app/workspace")) -> Path:
    """Combined: extension/name validation + canonical path check."""
    validate_filename(user_path)
    target = (workspace / user_path).resolve()
    try:
        target.relative_to(workspace.resolve())
    except ValueError:
        raise ValueError(f"Path escapes workspace: {user_path}")
    return target

# Tests
def run_validation_tests():
    cases = [
        ("notes/task.txt", True),
        ("../etc/passwd", False),          # traversal
        (".env", False),                   # blocked name
        ("script.sh", False),              # blocked extension
        ("data.csv", True),
        ("/absolute/path.txt", False),     # absolute path
        ("reports/../../../etc/hosts", False),  # traversal via ..
    ]
    for path, should_pass in cases:
        try:
            validate_filename(path)
            passed = True
        except ValueError:
            passed = False
        status = "OK" if passed == should_pass else "FAIL"
        print(f"[{status}] {path}: {'allowed' if passed else 'blocked'}")
```

---

## Solution 3: Chroot-Style Sandbox Using os.chroot (Linux)

```python
import asyncio
import os
import subprocess
from pathlib import Path

class ChrootFileSandbox:
    """
    Run file operations inside a chroot jail.
    Requires root or CAP_SYS_CHROOT capability.
    For production, use a container or seccomp instead.
    """

    def __init__(self, sandbox_root: str = "/var/agent-sandbox"):
        self._root = Path(sandbox_root)

    def setup(self):
        """Initialize the sandbox directory."""
        self._root.mkdir(parents=True, exist_ok=True)
        (self._root / "workspace").mkdir(exist_ok=True)

    def read_file_sandboxed(self, relative_path: str) -> str:
        """Read a file using a subprocess inside the chroot."""
        # Validate path before entering chroot
        clean = Path(relative_path)
        if ".." in clean.parts or clean.is_absolute():
            raise ValueError(f"Invalid path: {relative_path}")

        # Execute inside chroot
        result = subprocess.run(
            ["chroot", str(self._root), "cat", f"/workspace/{relative_path}"],
            capture_output=True, text=True, timeout=5.0
        )
        if result.returncode != 0:
            raise FileNotFoundError(f"File not found or read error: {relative_path}")
        return result.stdout

class ProcessIsolatedFileTools:
    """
    Alternative to chroot: run file operations as a separate unprivileged
    subprocess that cannot access the parent's file namespace.
    """

    def __init__(self, workspace: str):
        self._workspace = workspace

    async def read_file(self, path: str) -> str:
        # Validate path first
        target = safe_path(path, Path(self._workspace))

        # Run in subprocess to limit blast radius
        proc = await asyncio.create_subprocess_exec(
            "python3", "-c",
            f"import sys; print(open({repr(str(target))}).read())",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        if proc.returncode != 0:
            raise IOError(f"Read failed: {stderr.decode()[:200]}")
        return stdout.decode()

def safe_path(user_path: str, workspace: "Path") -> "Path":
    from pathlib import Path
    target = (workspace / user_path).resolve()
    target.relative_to(workspace.resolve())
    return target
```

---

## Solution 4: Symlink Attack Prevention

```python
import os
from pathlib import Path

def safe_path_nosymlink(user_path: str,
                         workspace: Path = Path("/app/workspace")) -> Path:
    """
    Prevent symlink attacks: a symlink inside the workspace pointing
    to /etc/passwd would pass the realpath check because realpath
    follows symlinks. This version checks each path component.
    """
    workspace = workspace.resolve()
    parts = Path(user_path).parts

    current = workspace
    for part in parts:
        if part == "..":
            raise ValueError(f"Directory traversal '..' not allowed")
        if part == ".":
            continue
        current = current / part
        # Check this component exists and is NOT a symlink
        if current.exists() and current.is_symlink():
            real = current.resolve()
            try:
                real.relative_to(workspace)
            except ValueError:
                raise ValueError(
                    f"Symlink escape detected: '{current}' → '{real}' "
                    f"is outside workspace"
                )

    # Final canonical path check
    if current.exists():
        resolved = current.resolve()
        try:
            resolved.relative_to(workspace)
        except ValueError:
            raise ValueError(f"Path escape after resolution: {current}")

    return current

def safe_open(user_path: str, mode: str = "r",
              workspace: str = "/app/workspace"):
    """
    Drop-in replacement for open() with path traversal and symlink protection.
    """
    if any(c in mode for c in ["w", "a", "x"]) and "r" not in mode:
        # Write mode: extra caution
        if ".." in Path(user_path).parts:
            raise ValueError("Directory traversal in write path")

    target = safe_path_nosymlink(user_path, Path(workspace))
    return open(target, mode)

# Additional: prevent TOCTOU (Time Of Check Time Of Use)
import fcntl

def atomic_safe_open(user_path: str, workspace: str = "/app/workspace"):
    """
    Open a file and hold the file descriptor to prevent TOCTOU attacks.
    The fd stays open while we check its real path.
    """
    target = safe_path_nosymlink(user_path, Path(workspace))
    fd = os.open(str(target), os.O_RDONLY | os.O_NOFOLLOW)
    try:
        # Verify via fd that the opened file is within workspace
        fd_path = Path(f"/proc/self/fd/{fd}").resolve()
        try:
            fd_path.relative_to(Path(workspace).resolve())
        except ValueError:
            raise ValueError(f"TOCTOU: opened file outside workspace: {fd_path}")
        return os.fdopen(fd, "r")
    except Exception:
        os.close(fd)
        raise
```

---

## Solution 5: Tool Definition with Schema-Enforced Path Constraints

```python
import json
import re
from anthropic import Anthropic

# Define the file tool with schema-level path constraints
FILE_TOOL_DEFINITION = {
    "name": "read_file",
    "description": "Read a file from the workspace. Only files within the workspace directory are accessible.",
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Relative path within workspace (e.g., 'data/report.txt'). No absolute paths or .. components.",
                "pattern": "^[a-zA-Z0-9][a-zA-Z0-9._/\\-]*$",  # allowlist chars
                "maxLength": 256,
            }
        },
        "required": ["path"],
        "additionalProperties": False,
    }
}

def validate_tool_path_input(tool_input: dict) -> str:
    """
    Validate path from tool_use input before execution.
    JSON schema pattern is advisory — always validate server-side too.
    """
    path = tool_input.get("path", "")

    # Reject empty
    if not path:
        raise ValueError("Path cannot be empty")

    # Reject absolute paths
    if path.startswith("/") or path.startswith("\\"):
        raise ValueError(f"Absolute paths not allowed: {path}")

    # Reject traversal
    if ".." in path.split("/"):
        raise ValueError(f"Directory traversal not allowed: {path}")

    # Reject null bytes (some OS file operations accept them)
    if "\x00" in path:
        raise ValueError("Null bytes not allowed in path")

    # Allowlist: only safe characters
    if not re.match(r"^[\w/.\-]+$", path):
        raise ValueError(f"Invalid characters in path: {path}")

    return path

async def handle_tool_use(tool_name: str, tool_input: dict) -> str:
    """Process tool calls from the LLM with path validation."""
    if tool_name == "read_file":
        path = validate_tool_path_input(tool_input)
        return await safe_read_file(path)
    raise ValueError(f"Unknown tool: {tool_name}")

async def safe_read_file(path: str) -> str:
    from pathlib import Path
    workspace = Path("/app/workspace")
    target = (workspace / path).resolve()
    target.relative_to(workspace.resolve())
    return target.read_text()
```

---

## Solution 6: Audit Log for All File Tool Invocations

```python
import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

@dataclass
class FileAccessEvent:
    operation: str         # read, write, list, delete
    requested_path: str    # as provided by user/LLM
    resolved_path: str     # canonical resolved path
    allowed: bool
    denial_reason: str | None = None
    file_hash: str | None = None  # SHA-256 of file content (for read ops)
    timestamp: float = field(default_factory=time.time)
    session_id: str = ""
    agent_id: str = ""

class FileToolAuditLog:
    def __init__(self, log_path: str = "/var/log/agent-file-access.jsonl"):
        self._path = Path(log_path)
        self._events: list[FileAccessEvent] = []
        self._denied_count = 0

    def record(self, event: FileAccessEvent):
        self._events.append(event)
        if not event.allowed:
            self._denied_count += 1
            print(f"[FileAudit] DENIED: {event.operation} '{event.requested_path}' "
                  f"— {event.denial_reason}")
        with open(self._path, "a") as f:
            f.write(json.dumps({
                "op": event.operation,
                "req": event.requested_path,
                "resolved": event.resolved_path,
                "allowed": event.allowed,
                "reason": event.denial_reason,
                "ts": event.timestamp,
                "session": event.session_id,
            }) + "\n")

    def suspicious_patterns(self) -> list[dict]:
        """Detect patterns that suggest active traversal attempts."""
        patterns = []
        traversal_attempts = [
            e for e in self._events
            if not e.allowed and "traversal" in (e.denial_reason or "")
        ]
        if len(traversal_attempts) >= 3:
            patterns.append({
                "type": "repeated_traversal_attempts",
                "count": len(traversal_attempts),
                "sessions": list({e.session_id for e in traversal_attempts}),
            })
        return patterns

def make_audited_file_tool(audit: FileToolAuditLog,
                            workspace: Path = Path("/app/workspace"),
                            session_id: str = ""):
    """Wrap safe_read_file with audit logging."""

    async def audited_read(user_path: str) -> str:
        resolved_str = ""
        denial = None
        allowed = False
        content = ""
        file_hash = None

        try:
            from pathlib import Path as P
            target = (workspace / user_path).resolve()
            resolved_str = str(target)
            target.relative_to(workspace.resolve())  # raises if outside
            content = target.read_text()
            file_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
            allowed = True
        except ValueError as e:
            denial = str(e)
        except FileNotFoundError:
            denial = "file_not_found"
            allowed = False

        audit.record(FileAccessEvent(
            operation="read",
            requested_path=user_path,
            resolved_path=resolved_str,
            allowed=allowed,
            denial_reason=denial,
            file_hash=file_hash,
            session_id=session_id,
        ))

        if not allowed:
            raise PermissionError(f"File access denied: {denial}")
        return content

    return audited_read
```

---

## Comparison

| Solution | Traversal Prevention | Symlink Safety | TOCTOU Safe | Schema Enforcement | Audit | Best For |
|---|---|---|---|---|---|---|
| 1. realpath + relative_to | Yes | Partial (realpath follows symlinks) | No | No | No | Minimal, production-safe baseline |
| 2. Extension + name allowlist | Yes | No | No | No | No | Extra defense for extension-sensitive systems |
| 3. chroot / subprocess isolation | Yes | Yes | N/A (separate namespace) | No | No | High-security, Linux deployments |
| 4. No-symlink + TOCTOU | Yes | Yes | Yes (O_NOFOLLOW) | No | No | Full Linux hardening |
| 5. Schema-enforced path | Yes (dual: schema + server) | No | No | Yes | No | LLM tool call hardening |
| 6. Audit log | Yes (via wrapper) | No | No | No | Yes | Compliance, attack detection |

**Key principle**: always use `Path.resolve()` followed by `relative_to(workspace)` as the baseline check — it handles `../`, absolute paths, and most symlink attacks. Add no-symlink traversal (solution 4) if the workspace is accessible to untrusted users who could plant symlinks. Use schema-level path validation (solution 5) to prevent the LLM from ever generating traversal paths, and always validate server-side too since JSON Schema is not a security boundary.
