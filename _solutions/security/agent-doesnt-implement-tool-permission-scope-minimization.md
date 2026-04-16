---
title: "Agent Doesn't Implement Tool Permission Scope Minimization"
description: "Agents that request broad permission scopes for tools at registration time grant those permissions for the lifetime of the session: a tool registered with 'read all files' permission is available with that scope even when the current task only needs to read one specific directory. Implement tool permission scope minimization that narrows each tool's effective permissions to the minimum required for the current task context."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-tool-permission-scope-minimization
tags: [least-privilege, permission-scoping, tool-permissions, dynamic-scope, blast-radius, zero-trust]
symptoms:
  - "File tool granted 'read /' permission when the task only requires reading '/tmp/reports'"
  - "Database tool has write access during a read-only query task"
  - "API tool credentials grant admin scope when user-level scope is sufficient"
  - "No mechanism to narrow permissions dynamically based on the current task"
  - "Prompt injection exploits broad tool permissions to access files outside the task scope"
---

## Why This Happens

Tools are typically registered once with their maximum capability (full read access, write access, admin API scope). The agent then uses those capabilities for all tasks, including tasks that only need a fraction of the permission. The principle of least privilege requires that permissions be the minimum necessary for the specific operation being performed. Dynamic scope minimization derives a narrower permission set from the current task context — the directories involved, the operations requested, the user's authorization level — and passes that narrowed scope when the tool is invoked, rather than using the maximum registered scope.

## Solution 1: Tool Permission Scope

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional, Set


class PermissionLevel(str, Enum):
    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"
    ADMIN = "admin"


@dataclass
class ToolPermissionScope:
    """
    Describes the permissions a tool is granted for one invocation.
    More restrictive than the tool's maximum registered capability.
    """
    tool_name: str
    allowed_operations: Set[PermissionLevel]
    resource_constraints: Dict[str, Any] = field(default_factory=dict)
    # e.g. {"path_prefix": "/tmp/reports", "max_bytes": 1048576}
    expires_at: Optional[float] = None
    issued_for_task: str = ""
    rationale: str = ""

    def allows(self, operation: PermissionLevel) -> bool:
        return operation in self.allowed_operations

    def is_expired(self) -> bool:
        import time
        return self.expires_at is not None and time.time() > self.expires_at
```

## Solution 2: Task Context Scope Deriver

```python
import re
import time
from typing import Any, Dict, List, Optional


class TaskContextScopeDeriver:
    """
    Derives a minimized ToolPermissionScope for a tool given the
    current task description and any explicit resource hints.
    """

    READ_SIGNALS = re.compile(
        r"\b(read|list|get|fetch|search|view|show|display|find|look up)\b", re.IGNORECASE
    )
    WRITE_SIGNALS = re.compile(
        r"\b(write|create|update|delete|modify|save|store|insert|remove)\b", re.IGNORECASE
    )
    EXEC_SIGNALS = re.compile(
        r"\b(run|execute|call|invoke|start|launch|trigger)\b", re.IGNORECASE
    )

    def derive(
        self,
        tool_name: str,
        task_description: str,
        resource_hints: Optional[Dict[str, Any]] = None,
        scope_ttl_seconds: float = 300.0,
    ) -> ToolPermissionScope:
        allowed: set = set()

        if self.READ_SIGNALS.search(task_description):
            allowed.add(PermissionLevel.READ)
        if self.WRITE_SIGNALS.search(task_description):
            allowed.add(PermissionLevel.WRITE)
        if self.EXEC_SIGNALS.search(task_description):
            allowed.add(PermissionLevel.EXECUTE)

        # Default to read-only if no signals detected
        if not allowed:
            allowed.add(PermissionLevel.READ)

        constraints = resource_hints or {}

        return ToolPermissionScope(
            tool_name=tool_name,
            allowed_operations=allowed,
            resource_constraints=constraints,
            expires_at=time.time() + scope_ttl_seconds,
            issued_for_task=task_description[:100],
            rationale=f"Derived from task signals: {[p.value for p in allowed]}",
        )
```

## Solution 3: Scope-Constrained Tool Wrapper

```python
import time
from typing import Any, Callable, Dict, Optional


class ScopeViolationError(Exception):
    def __init__(self, tool_name: str, operation: str, reason: str):
        super().__init__(f"Scope violation for '{tool_name}' ({operation}): {reason}")
        self.tool_name = tool_name
        self.operation = operation
        self.reason = reason


class ScopeConstrainedToolWrapper:
    """
    Wraps a tool function and enforces its ToolPermissionScope on every call.
    Validates that the requested operation and resource constraints are satisfied.
    """

    def __init__(
        self,
        tool_fn: Callable,
        scope_enforcer: "ToolScopeEnforcer",
    ):
        self._fn = tool_fn
        self._enforcer = scope_enforcer

    async def __call__(self, scope: ToolPermissionScope, **kwargs: Any) -> Any:
        self._enforcer.enforce(scope, kwargs)
        return await self._fn(**kwargs)


class ToolScopeEnforcer:
    """
    Validates that a tool invocation is within its declared ToolPermissionScope.
    """

    def enforce(
        self,
        scope: ToolPermissionScope,
        call_args: Dict[str, Any],
    ) -> None:
        if scope.is_expired():
            raise ScopeViolationError(
                scope.tool_name, "expired",
                f"Scope expired {time.time() - scope.expires_at:.1f}s ago"
            )

        # Path constraint check
        path_prefix = scope.resource_constraints.get("path_prefix")
        if path_prefix:
            for arg_name in ("path", "file_path", "directory", "filename"):
                path_val = call_args.get(arg_name, "")
                if path_val and not str(path_val).startswith(path_prefix):
                    raise ScopeViolationError(
                        scope.tool_name, "path_constraint",
                        f"Argument '{arg_name}={path_val}' violates path_prefix '{path_prefix}'",
                    )

        # Max bytes constraint
        max_bytes = scope.resource_constraints.get("max_bytes")
        if max_bytes:
            content = call_args.get("content") or call_args.get("data") or ""
            if isinstance(content, (str, bytes)) and len(content) > max_bytes:
                raise ScopeViolationError(
                    scope.tool_name, "size_constraint",
                    f"Content size {len(content)} exceeds max_bytes {max_bytes}",
                )
```

## Solution 4: Per-Task Scope Registry

```python
import time
import uuid
from threading import Lock
from typing import Dict, List, Optional


class PerTaskScopeRegistry:
    """
    Tracks active ToolPermissionScopes per task.
    Allows scopes to be revoked when a task completes.
    """

    def __init__(self):
        self._scopes: Dict[str, List[ToolPermissionScope]] = {}
        self._lock = Lock()

    def register(self, task_id: str, scope: ToolPermissionScope) -> None:
        with self._lock:
            self._scopes.setdefault(task_id, []).append(scope)

    def get_scope(self, task_id: str, tool_name: str) -> Optional[ToolPermissionScope]:
        with self._lock:
            for scope in self._scopes.get(task_id, []):
                if scope.tool_name == tool_name and not scope.is_expired():
                    return scope
        return None

    def revoke_task(self, task_id: str) -> int:
        with self._lock:
            count = len(self._scopes.pop(task_id, []))
        return count

    def prune_expired(self) -> int:
        pruned = 0
        with self._lock:
            for task_id in list(self._scopes.keys()):
                active = [s for s in self._scopes[task_id] if not s.is_expired()]
                pruned += len(self._scopes[task_id]) - len(active)
                if active:
                    self._scopes[task_id] = active
                else:
                    del self._scopes[task_id]
        return pruned
```

## Solution 5: Scope Audit Logger

```python
import time
from threading import Lock
from typing import List


class ScopeAuditLogger:
    """
    Records scope derivations, enforcements, and violations for audit review.
    """

    def __init__(self, max_records: int = 10000):
        self._records: List[dict] = []
        self._lock = Lock()
        self._max = max_records

    def log_derivation(self, scope: ToolPermissionScope) -> None:
        with self._lock:
            self._records.append({
                "ts": time.time(),
                "event": "scope_derived",
                "tool": scope.tool_name,
                "operations": [p.value for p in scope.allowed_operations],
                "constraints": scope.resource_constraints,
                "task": scope.issued_for_task,
            })
            if len(self._records) > self._max:
                self._records.pop(0)

    def log_violation(self, error: ScopeViolationError) -> None:
        with self._lock:
            self._records.append({
                "ts": time.time(),
                "event": "scope_violation",
                "tool": error.tool_name,
                "operation": error.operation,
                "reason": error.reason,
            })
            if len(self._records) > self._max:
                self._records.pop(0)

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [r for r in self._records if r["ts"] >= cutoff]
        violations = [r for r in recent if r["event"] == "scope_violation"]
        return {
            "window_seconds": window_seconds,
            "total_events": len(recent),
            "violations": len(violations),
            "violation_tools": list({r["tool"] for r in violations}),
        }
```

## Solution 6: Scope Minimization Coverage Auditor

```python
from typing import Any, Dict, List


class ScopeMinimizationCoverageAuditor:
    """
    Identifies tool invocations that were called with admin or write permissions
    when the task signals only required read — indicating over-privileged scopes.
    """

    def audit(self, scope_records: List[dict]) -> List[dict]:
        findings = []
        for record in scope_records:
            ops = set(record.get("operations", []))
            task = record.get("task", "").lower()
            if "write" in ops or "admin" in ops:
                read_only_signals = any(
                    kw in task for kw in ("read", "list", "get", "view", "show", "find", "search")
                )
                write_signals = any(
                    kw in task for kw in ("write", "create", "update", "delete", "modify", "save")
                )
                if read_only_signals and not write_signals:
                    findings.append({
                        "tool": record.get("tool"),
                        "operations_granted": list(ops),
                        "task": record.get("task"),
                        "finding": "write/admin scope granted for apparent read-only task",
                    })
        return findings
```

## Comparison

| Approach | Dynamic Scope Derivation | Resource Constraints | Expiry Enforcement | Task-Scoped Registry | Audit Trail |
|---|---|---|---|---|---|
| TaskContextScopeDeriver | Yes (NLP signals) | Via hints | Yes | No | No |
| ToolScopeEnforcer | No | Yes (path, size) | Yes | No | No |
| PerTaskScopeRegistry | No | No | Yes | Yes | No |
| ScopeAuditLogger | No | No | No | No | Yes |
| ScopeMinimizationCoverageAuditor | No | No | No | No | Yes (gap detection) |

**Best for production**: Always pass explicit `resource_hints` when creating scopes for file-system and database tools — `{"path_prefix": "/data/user_123/"}` prevents the tool from accessing any path outside the user's directory even if a prompt injection tries to navigate up. Set scope `expires_at` to task completion time, not session end — a scope should be valid for exactly one task. Run `ScopeMinimizationCoverageAuditor.audit()` on recent scope logs weekly to find systematic over-privileging. Log every `ScopeViolationError` as a security event: violations from within a session indicate prompt injection attempts, not bugs.
