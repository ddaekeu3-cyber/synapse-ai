---
title: "Agent Doesn't Implement Tool Capability Sandboxing"
description: "Agents that expose all tools to all requests regardless of context grant every conversation the same maximum capability level — a user asking for a weather report has access to the same file system tools as an admin performing a database migration. Implement tool capability sandboxing that assigns each request a capability tier, restricts which tools are accessible per tier, and enforces this at dispatch time so that compromised or confused agents cannot invoke tools beyond their granted scope."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-tool-capability-sandboxing
tags: [sandboxing, capability-model, tool-access-control, least-privilege, tier-based-access, tool-restriction]
symptoms:
  - "A prompt injection attack causes the agent to call file deletion tools it should never access"
  - "All users have access to admin-level tools regardless of their authentication level"
  - "No mechanism to restrict tool access based on the sensitivity of the current conversation"
  - "Tool registry exposes every registered tool to the LLM in every context"
  - "Confused deputy attack: agent acting on behalf of a low-privilege user invokes a high-privilege tool"
---

## Why This Happens

Tool registries typically expose all registered tools in every context. The LLM sees the full tool list and can invoke any of them. This violates the principle of least privilege: a tool that deletes database records should not be invocable by a request handling a user's casual query. Capability sandboxing enforces a boundary between what the LLM sees and what tools physically exist. Each request is assigned a capability tier; the sandbox presents only the tools permitted for that tier to the LLM; dispatch enforces the same restriction so that even if the model generates a call to a restricted tool, it is rejected before execution.

## Solution 1: Capability Tier

```python
from dataclasses import dataclass, field
from enum import IntEnum
from typing import FrozenSet, Set


class CapabilityLevel(IntEnum):
    PUBLIC = 0        # unauthenticated, read-only, no side effects
    USER = 1          # authenticated user, limited writes
    POWER_USER = 2    # verified user, broader read/write
    OPERATOR = 3      # internal operator, most tools
    ADMIN = 4         # full access including destructive operations


@dataclass(frozen=True)
class CapabilityTier:
    level: CapabilityLevel
    allowed_tool_names: FrozenSet[str]
    denied_tool_names: FrozenSet[str] = field(default_factory=frozenset)
    max_tool_calls_per_turn: int = 10
    description: str = ""

    def permits(self, tool_name: str) -> bool:
        if tool_name in self.denied_tool_names:
            return False
        if self.allowed_tool_names:
            return tool_name in self.allowed_tool_names
        return True   # empty allowed = all permitted (use explicit deny only)
```

## Solution 2: Capability Tier Registry

```python
from typing import Dict, Optional


class CapabilityTierRegistry:
    """
    Stores named capability tiers and resolves the appropriate tier
    for a request based on authentication level and context.
    """

    def __init__(self):
        self._tiers: Dict[str, CapabilityTier] = {}

    def register(self, name: str, tier: CapabilityTier) -> None:
        self._tiers[name] = tier

    def get(self, name: str) -> Optional[CapabilityTier]:
        return self._tiers.get(name)

    def resolve_for_user(
        self,
        user_role: str,
        context_tags: set = None,
    ) -> CapabilityTier:
        """Map user role to a capability tier."""
        tags = context_tags or set()
        # Admin context overrides
        if "admin_session" in tags and "admin" in self._tiers:
            return self._tiers["admin"]
        tier = self._tiers.get(user_role) or self._tiers.get("public")
        if tier is None:
            # Safest default: no tools permitted
            return CapabilityTier(
                level=CapabilityLevel.PUBLIC,
                allowed_tool_names=frozenset(),
                description="default-deny fallback",
            )
        return tier


def build_default_tier_registry(all_tools: set) -> CapabilityTierRegistry:
    registry = CapabilityTierRegistry()

    registry.register("public", CapabilityTier(
        level=CapabilityLevel.PUBLIC,
        allowed_tool_names=frozenset({"search", "weather", "calculator", "time"}),
        max_tool_calls_per_turn=3,
        description="Unauthenticated users: read-only, low-risk tools only",
    ))

    registry.register("user", CapabilityTier(
        level=CapabilityLevel.USER,
        allowed_tool_names=frozenset({
            "search", "weather", "calculator", "time",
            "read_file", "list_directory", "send_email",
        }),
        denied_tool_names=frozenset({"delete_file", "drop_table", "exec_shell"}),
        max_tool_calls_per_turn=10,
        description="Authenticated users",
    ))

    registry.register("admin", CapabilityTier(
        level=CapabilityLevel.ADMIN,
        allowed_tool_names=frozenset(all_tools),
        max_tool_calls_per_turn=50,
        description="Admin: full tool access",
    ))

    return registry
```

## Solution 3: Sandboxed Tool Registry View

```python
from typing import Any, Dict, List, Optional


class SandboxedToolRegistryView:
    """
    Presents a filtered view of the tool registry to the LLM.
    Only tools permitted by the capability tier appear in the
    tool list sent to the model — the model cannot even see
    tools outside its sandbox.
    """

    def __init__(
        self,
        full_registry: Dict[str, Any],   # tool_name -> tool definition
        tier: CapabilityTier,
    ):
        self._full = full_registry
        self._tier = tier

    def visible_tools(self) -> Dict[str, Any]:
        """Returns only tool definitions the tier permits."""
        return {
            name: defn
            for name, defn in self._full.items()
            if self._tier.permits(name)
        }

    def tool_names(self) -> List[str]:
        return list(self.visible_tools().keys())

    def is_visible(self, tool_name: str) -> bool:
        return self._tier.permits(tool_name) and tool_name in self._full
```

## Solution 4: Sandboxed Tool Dispatcher

```python
import time
from typing import Any, Callable, Dict, Optional


class SandboxedToolDispatcher:
    """
    Enforces capability tier restrictions at dispatch time.
    Even if the LLM generates a call to a restricted tool (e.g., via
    prompt injection), the dispatcher rejects it before execution.
    """

    def __init__(
        self,
        tool_executors: Dict[str, Callable],
        tier: CapabilityTier,
    ):
        self._executors = tool_executors
        self._tier = tier
        self._calls_this_turn = 0
        self._violations = 0
        self._allowed_calls = 0

    def reset_turn(self) -> None:
        """Call at the start of each LLM turn."""
        self._calls_this_turn = 0

    async def dispatch(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
    ) -> dict:
        # Check call count limit
        if self._calls_this_turn >= self._tier.max_tool_calls_per_turn:
            self._violations += 1
            return {
                "success": False,
                "error": "tool_call_limit_exceeded",
                "message": (
                    f"Maximum {self._tier.max_tool_calls_per_turn} tool calls "
                    f"per turn reached for capability tier {self._tier.level.name}"
                ),
            }

        # Check capability permission
        if not self._tier.permits(tool_name):
            self._violations += 1
            return {
                "success": False,
                "error": "capability_violation",
                "tool_name": tool_name,
                "message": (
                    f"Tool '{tool_name}' is not permitted at capability level "
                    f"{self._tier.level.name}"
                ),
            }

        # Check tool exists
        executor = self._executors.get(tool_name)
        if executor is None:
            return {
                "success": False,
                "error": "tool_not_found",
                "tool_name": tool_name,
            }

        self._calls_this_turn += 1
        self._allowed_calls += 1

        try:
            result = await executor(**arguments)
            return {"success": True, "result": result}
        except Exception as exc:
            return {"success": False, "error": "execution_error", "message": str(exc)}

    def stats(self) -> dict:
        return {
            "capability_level": self._tier.level.name,
            "allowed_calls": self._allowed_calls,
            "violations": self._violations,
            "calls_this_turn": self._calls_this_turn,
        }
```

## Solution 5: Capability Violation Auditor

```python
import json
import time
from pathlib import Path
from threading import Lock


class CapabilityViolationAuditor:
    """
    Logs capability violation attempts for security analysis.
    High violation rates from a session indicate prompt injection or abuse.
    """

    def __init__(self, log_path: str = "/tmp/capability_violations.jsonl"):
        self._path = Path(log_path)
        self._lock = Lock()
        self._total = 0

    def log(
        self,
        session_id: str,
        user_id: str,
        tool_name: str,
        tier_level: str,
        reason: str,
    ) -> None:
        record = {
            "ts": time.time(),
            "session_id": session_id,
            "user_id": user_id,
            "tool_name": tool_name,
            "tier_level": tier_level,
            "reason": reason,
        }
        with self._lock:
            with self._path.open("a") as f:
                f.write(json.dumps(record) + "\n")
            self._total += 1

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        records = []
        if not self._path.exists():
            return {"window_seconds": window_seconds, "violations": 0}
        with self._lock:
            for line in self._path.read_text().splitlines():
                try:
                    r = json.loads(line)
                    if r.get("ts", 0) >= cutoff:
                        records.append(r)
                except json.JSONDecodeError:
                    continue
        by_tool: dict = {}
        for r in records:
            by_tool[r["tool_name"]] = by_tool.get(r["tool_name"], 0) + 1
        return {
            "window_seconds": window_seconds,
            "violations": len(records),
            "by_tool": by_tool,
            "unique_sessions": len({r["session_id"] for r in records}),
        }
```

## Solution 6: Sandbox Status Dashboard

```python
import time


class CapabilitySandboxDashboard:
    """
    Combines dispatcher stats and violation audit into a single
    operational view for security monitoring.
    """

    def __init__(
        self,
        dispatcher: SandboxedToolDispatcher,
        auditor: CapabilityViolationAuditor,
        registry_view: SandboxedToolRegistryView,
    ):
        self._dispatcher = dispatcher
        self._auditor = auditor
        self._view = registry_view

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "dispatcher": self._dispatcher.stats(),
            "visible_tools": self._view.tool_names(),
            "violations_1h": self._auditor.summary(3600.0),
        }
```

## Comparison

| Approach | Tier-Based Filtering | LLM View Filtering | Dispatch Enforcement | Violation Logging | Dashboard |
|---|---|---|---|---|---|
| CapabilityTier | Yes (allow/deny) | No | No | No | No |
| CapabilityTierRegistry | Yes (role mapping) | No | No | No | No |
| SandboxedToolRegistryView | No | Yes (LLM sees only permitted) | No | No | No |
| SandboxedToolDispatcher | No | No | Yes (enforces at call time) | No | Stats |
| CapabilityViolationAuditor | No | No | No | Yes (JSONL) | No |
| CapabilitySandboxDashboard | No | No | No | No | Yes |

**Best for production**: Apply both layers — filter the LLM's tool list via `SandboxedToolRegistryView` (so the model never generates calls to restricted tools) AND enforce at dispatch via `SandboxedToolDispatcher` (so prompt injection cannot invoke restricted tools even if it somehow gets a tool name into the call). Set `max_tool_calls_per_turn` conservatively — a legitimate user query rarely needs more than 5 tool calls per turn; anything above 10 is likely an adversarial loop. Monitor `by_tool` in the violation audit: a restricted tool that appears frequently in violations is a target of prompt injection campaigns and may warrant additional input filtering or removal from the tool registry entirely.
