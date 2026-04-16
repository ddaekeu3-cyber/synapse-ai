---
title: "Agent Doesn't Implement Path Traversal Prevention in File Tool Arguments"
description: "Agents that pass user-influenced paths to file system tools without validation are vulnerable to path traversal attacks: a crafted argument like '../../etc/passwd' or '../secrets/.env' escapes the intended working directory and reads or writes sensitive files. Implement path traversal prevention that resolves all file paths to their canonical form and rejects any path that escapes a configured safe root directory."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-path-traversal-prevention-in-file-tool-arguments
tags: [path-traversal, directory-traversal, file-tool-security, sandbox-enforcement, canonical-path, file-access-control]
symptoms:
  - "File read tool called with '../../etc/passwd' via prompt injection"
  - "User-supplied filename passed directly to os.path.join without validation"
  - "Symlinks in the working directory allow escape to parent directories"
  - "No check that the resolved canonical path remains within the intended root"
  - "Write tool can overwrite arbitrary files if given a traversal path"
---

## Why This Happens

`os.path.join` and similar functions do not validate that the resulting path stays within a safe root — they simply concatenate. A path like `os.path.join('/safe/root', '../../etc/passwd')` resolves to `/etc/passwd`. Symlinks add another vector: a symlink inside the safe root can point to a parent directory, and following it escapes the root. Prevention requires resolving the full canonical path (following symlinks) and verifying that it starts with the safe root prefix before any file operation is performed.

## Solution 1: Path Safety Policy

```python
from dataclasses import dataclass, field
from typing import FrozenSet, Set


@dataclass
class PathSafetyPolicy:
    safe_root: str                         # all file ops must stay under this directory
    allowed_extensions: FrozenSet[str] = field(
        default_factory=lambda: frozenset()
    )  # empty = all extensions allowed
    blocked_extensions: FrozenSet[str] = field(
        default_factory=lambda: frozenset({
            ".env", ".pem", ".key", ".pfx", ".p12", ".crt", ".cer",
        })
    )
    allow_symlinks: bool = False           # False = resolve and re-check
    max_path_depth: int = 10              # max directory depth relative to root
```

## Solution 2: Path Traversal Validator

```python
import os
from dataclasses import dataclass
from typing import Optional


@dataclass
class PathValidationResult:
    safe: bool
    canonical_path: Optional[str]
    rejection_reason: Optional[str]
    original_path: str


class PathTraversalValidator:
    """
    Validates file paths against a safe root policy.
    Resolves symlinks and canonical form before comparison.
    """

    def __init__(self, policy: PathSafetyPolicy):
        self._policy = policy
        self._safe_root = os.path.realpath(os.path.abspath(policy.safe_root))

    def validate(self, path: str) -> PathValidationResult:
        original = path

        # Resolve to canonical absolute path
        if not os.path.isabs(path):
            path = os.path.join(self._safe_root, path)

        try:
            if self._policy.allow_symlinks:
                canonical = os.path.abspath(path)
            else:
                # realpath follows symlinks — catches symlink escapes
                canonical = os.path.realpath(path)
        except (OSError, ValueError) as e:
            return PathValidationResult(
                safe=False,
                canonical_path=None,
                rejection_reason=f"path_resolution_error: {e}",
                original_path=original,
            )

        # Check root containment
        safe_root_with_sep = self._safe_root.rstrip(os.sep) + os.sep
        if not (canonical == self._safe_root or canonical.startswith(safe_root_with_sep)):
            return PathValidationResult(
                safe=False,
                canonical_path=canonical,
                rejection_reason=f"path_escapes_root: '{canonical}' is outside '{self._safe_root}'",
                original_path=original,
            )

        # Check depth
        rel = os.path.relpath(canonical, self._safe_root)
        depth = len(rel.split(os.sep))
        if depth > self._policy.max_path_depth:
            return PathValidationResult(
                safe=False,
                canonical_path=canonical,
                rejection_reason=f"path_too_deep: depth {depth} exceeds limit {self._policy.max_path_depth}",
                original_path=original,
            )

        # Check extension
        _, ext = os.path.splitext(canonical)
        ext_lower = ext.lower()
        if ext_lower in self._policy.blocked_extensions:
            return PathValidationResult(
                safe=False,
                canonical_path=canonical,
                rejection_reason=f"blocked_extension: '{ext_lower}'",
                original_path=original,
            )
        if self._policy.allowed_extensions and ext_lower not in self._policy.allowed_extensions:
            return PathValidationResult(
                safe=False,
                canonical_path=canonical,
                rejection_reason=f"extension_not_allowed: '{ext_lower}'",
                original_path=original,
            )

        return PathValidationResult(
            safe=True,
            canonical_path=canonical,
            rejection_reason=None,
            original_path=original,
        )
```

## Solution 3: Path Traversal Error

```python
class PathTraversalError(Exception):
    def __init__(self, result: PathValidationResult):
        super().__init__(
            f"path traversal blocked for '{result.original_path}': {result.rejection_reason}"
        )
        self.result = result
```

## Solution 4: Safe File Tool Wrapper

```python
import time
from typing import Any, Callable, Optional


class SafeFileToolWrapper:
    """
    Wraps file tool calls with path traversal validation.
    Raises PathTraversalError before the file operation is attempted.
    """

    def __init__(
        self,
        validator: PathTraversalValidator,
        audit_fn: Optional[Callable[[dict], None]] = None,
    ):
        self._validator = validator
        self._audit = audit_fn or (lambda _: None)
        self._blocked_count = 0

    def safe_path(self, path: str) -> str:
        """
        Returns the canonical safe path or raises PathTraversalError.
        Use this to get a validated path before passing to file operations.
        """
        result = self._validator.validate(path)
        self._audit({
            "ts": time.time(),
            "original_path": path,
            "canonical_path": result.canonical_path,
            "safe": result.safe,
            "rejection_reason": result.rejection_reason,
        })
        if not result.safe:
            self._blocked_count += 1
            raise PathTraversalError(result)
        return result.canonical_path

    async def execute(
        self,
        path: str,
        file_fn: Callable[[str], Any],
    ) -> Any:
        safe = self.safe_path(path)
        return await file_fn(safe)

    def stats(self) -> dict:
        return {"blocked_attempts": self._blocked_count}
```

## Solution 5: Multi-Root Path Validator

```python
from typing import List


class MultiRootPathValidator:
    """
    Supports multiple safe root directories (e.g., /data/uploads and /data/exports)
    with per-root policies. A path is safe if it is valid under any registered root.
    """

    def __init__(self, validators: List[PathTraversalValidator]):
        self._validators = validators

    def validate(self, path: str) -> PathValidationResult:
        last_result: Optional[PathValidationResult] = None
        for validator in self._validators:
            result = validator.validate(path)
            if result.safe:
                return result
            last_result = result
        return last_result or PathValidationResult(
            safe=False,
            canonical_path=None,
            rejection_reason="no_matching_root",
            original_path=path,
        )
```

## Solution 6: Path Traversal Audit Log

```python
import time
from collections import deque
from threading import Lock
from typing import Deque


class PathTraversalAuditLog:
    """
    Records blocked path traversal attempts for security investigation.
    """

    def __init__(self, max_records: int = 10_000):
        self._records: Deque[dict] = deque(maxlen=max_records)
        self._lock = Lock()

    def record(self, audit_event: dict) -> None:
        if audit_event.get("safe", True):
            return
        with self._lock:
            self._records.append({**audit_event, "logged_at": time.time()})

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [r for r in self._records if r.get("logged_at", 0) >= cutoff]
        if not recent:
            return {"blocked_attempts": 0}

        reasons: dict = {}
        for r in recent:
            reason = (r.get("rejection_reason") or "unknown").split(":")[0]
            reasons[reason] = reasons.get(reason, 0) + 1

        return {
            "window_seconds": window_seconds,
            "blocked_attempts": len(recent),
            "by_rejection_reason": reasons,
            "sample_blocked_paths": [
                r.get("original_path", "") for r in recent[:5]
            ],
        }
```

## Comparison

| Approach | Canonical Resolution | Root Containment | Extension Check | Symlink Safety | Audit |
|---|---|---|---|---|---|
| PathTraversalValidator | Yes (realpath) | Yes | Yes | Via realpath | No |
| SafeFileToolWrapper | Via validator | Via validator | Via validator | Via validator | Yes |
| MultiRootPathValidator | Via validators | Via validators | Via validators | Via validators | No |
| PathTraversalAuditLog | No | No | No | No | Yes |

**Best for production**: Use `os.path.realpath()` not `os.path.abspath()` — realpath follows symlinks and resolves the true canonical path, while abspath only collapses `..` segments syntactically without following symlinks. Always validate against the canonical root after resolution, not before. Block `.env`, `.pem`, `.key` by default — these extensions in a file tool context almost always indicate credential exfiltration. Wire `PathTraversalAuditLog` to your security SIEM; a burst of traversal attempts from a single session within 60 seconds indicates active exploitation, not accidental misconfiguration.
