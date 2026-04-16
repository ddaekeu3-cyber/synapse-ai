---
title: "Agent Doesn't Implement Tool Call Allowlist Enforcement at Runtime"
description: "Agents that expose a dynamic tool registry without runtime allowlist enforcement allow prompt injection to invoke any registered tool — including internal administrative tools, debug endpoints, or high-privilege operations that were registered for internal use but never intended to be LLM-callable. Implement a runtime allowlist that explicitly declares which tools the LLM is permitted to call, blocks all others at the dispatch layer, and alerts on attempts to invoke non-allowlisted tools."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-tool-call-allowlist-enforcement-at-runtime
tags: [allowlist, tool-registry, runtime-enforcement, prompt-injection, tool-access-control, dispatch-security]
symptoms:
  - "LLM can invoke any tool in the registry including internal admin tools"
  - "Prompt injection causes agent to call debug or privileged tools not intended for LLM use"
  - "Tool list sent to LLM includes all registered tools with no access control filtering"
  - "No distinction between tools available for LLM invocation vs internal-only tools"
  - "New tools registered by developers automatically become LLM-callable without review"
---

## Why This Happens

Tool registries are designed for extensibility: any module can register a tool and it becomes available. This works well for the application layer but creates a security gap when the LLM can invoke tools directly — the registry lacks a concept of "LLM-callable vs internal-only." Prompt injection exploits this: an instruction embedded in retrieved content can name any registered tool and the dispatch layer will execute it. Runtime allowlists close this gap by maintaining an explicit, separately-managed set of tool names that are approved for LLM invocation, distinct from the full set of registered tools.

## Solution 1: Tool Allowlist

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, FrozenSet, Optional, Set


class AllowlistSource(str, Enum):
    STATIC = "static"         # hardcoded in configuration
    DYNAMIC = "dynamic"       # loaded from config file or database
    ROLE_BASED = "role_based" # per-session role determines allowed tools


@dataclass
class ToolAllowlistConfig:
    allowed_tool_names: FrozenSet[str]
    source: AllowlistSource = AllowlistSource.STATIC
    description: str = ""
    version: str = "1.0"
    # Optional per-tool metadata
    tool_metadata: Dict[str, Dict] = field(default_factory=dict)

    def is_allowed(self, tool_name: str) -> bool:
        return tool_name in self.allowed_tool_names

    def size(self) -> int:
        return len(self.allowed_tool_names)
```

## Solution 2: Runtime Allowlist Enforcer

```python
import time
from typing import Any, Dict, List, Optional, Set


class RuntimeAllowlistEnforcer:
    """
    Enforces a tool call allowlist at dispatch time.
    Filters the tool list sent to the LLM so that only allowlisted
    tools appear as options, and blocks any call to a non-allowlisted tool
    even if the LLM somehow generates one.
    """

    def __init__(
        self,
        config: ToolAllowlistConfig,
        audit_logger: Optional["AllowlistViolationLogger"] = None,
    ):
        self._config = config
        self._audit = audit_logger
        self._blocked_calls = 0
        self._total_calls = 0

    def filter_tool_definitions(
        self, all_tools: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Filters the tool definition list before sending to the LLM.
        Only allowlisted tools appear in the LLM's context.
        """
        return [
            t for t in all_tools
            if self._config.is_allowed(t.get("name", ""))
        ]

    def check_call(
        self, tool_name: str, session_id: str = ""
    ) -> tuple[bool, str]:
        """
        Returns (allowed, reason). Call this before executing any tool.
        """
        self._total_calls += 1

        if self._config.is_allowed(tool_name):
            return True, ""

        self._blocked_calls += 1
        reason = (
            f"Tool '{tool_name}' is not on the runtime allowlist "
            f"({self._config.size()} tools permitted)"
        )
        if self._audit:
            self._audit.record(
                tool_name=tool_name,
                session_id=session_id,
                reason=reason,
            )
        return False, reason

    def stats(self) -> dict:
        return {
            "total_calls": self._total_calls,
            "blocked_calls": self._blocked_calls,
            "block_rate_pct": round(
                self._blocked_calls / max(self._total_calls, 1) * 100, 2
            ),
            "allowlist_size": self._config.size(),
        }
```

## Solution 3: Allowlist-Gated Tool Dispatcher

```python
import asyncio
from typing import Any, Callable, Dict, Optional


class AllowlistGatedToolDispatcher:
    """
    Wraps tool execution with allowlist enforcement.
    Raises AllowlistViolationError for non-allowlisted tool calls
    before the tool function is ever invoked.
    """

    def __init__(self, enforcer: RuntimeAllowlistEnforcer):
        self._enforcer = enforcer

    async def dispatch(
        self,
        tool_name: str,
        args: Dict[str, Any],
        tool_fn: Callable,
        session_id: str = "",
    ) -> Any:
        allowed, reason = self._enforcer.check_call(tool_name, session_id)
        if not allowed:
            raise AllowlistViolationError(
                tool_name=tool_name,
                reason=reason,
                session_id=session_id,
            )
        return await tool_fn(**args)


class AllowlistViolationError(Exception):
    def __init__(self, tool_name: str, reason: str, session_id: str = ""):
        super().__init__(
            f"Allowlist violation: {reason}"
        )
        self.tool_name = tool_name
        self.reason = reason
        self.session_id = session_id
```

## Solution 4: Dynamic Allowlist Loader

```python
import json
import time
from pathlib import Path
from threading import Lock
from typing import Optional


class DynamicAllowlistLoader:
    """
    Loads the tool allowlist from a configuration file.
    Watches for changes and hot-reloads without agent restart.
    Useful for ops teams that need to add/remove tool access without deployment.
    """

    def __init__(
        self,
        config_path: str,
        reload_interval_seconds: float = 60.0,
    ):
        self._path = Path(config_path)
        self._interval = reload_interval_seconds
        self._lock = Lock()
        self._current: Optional[ToolAllowlistConfig] = None
        self._last_loaded: float = 0.0
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text())
            allowed = frozenset(data.get("allowed_tools", []))
            self._current = ToolAllowlistConfig(
                allowed_tool_names=allowed,
                source=AllowlistSource.DYNAMIC,
                version=data.get("version", "1.0"),
                description=data.get("description", ""),
            )
            self._last_loaded = time.time()
        except (json.JSONDecodeError, OSError):
            pass  # keep existing config on error

    def get(self) -> Optional[ToolAllowlistConfig]:
        with self._lock:
            if time.time() - self._last_loaded > self._interval:
                self._load()
            return self._current

    def force_reload(self) -> Optional[ToolAllowlistConfig]:
        with self._lock:
            self._load()
            return self._current
```

## Solution 5: Allowlist Violation Logger

```python
import time
from typing import List


class AllowlistViolationLogger:
    """
    Records every blocked tool call attempt with tool name,
    session ID, and timestamp for security audit and threat detection.
    """

    def __init__(self, max_records: int = 10000):
        self._max = max_records
        self._records: List[dict] = []

    def record(self, tool_name: str, session_id: str, reason: str) -> None:
        if len(self._records) >= self._max:
            self._records.pop(0)
        self._records.append({
            "ts": time.time(),
            "tool_name": tool_name,
            "session_id": session_id,
            "reason": reason,
        })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        by_tool: dict = {}
        by_session: dict = {}
        for r in recent:
            by_tool[r["tool_name"]] = by_tool.get(r["tool_name"], 0) + 1
            by_session[r["session_id"]] = by_session.get(r["session_id"], 0) + 1
        return {
            "window_seconds": window_seconds,
            "total_violations": len(recent),
            "unique_blocked_tools": len(by_tool),
            "top_blocked_tools": sorted(
                by_tool.items(), key=lambda x: x[1], reverse=True
            )[:10],
            "unique_violating_sessions": len(by_session),
        }
```

## Solution 6: Allowlist Enforcement Dashboard

```python
import time


class AllowlistEnforcementDashboard:
    """
    Combines enforcer statistics, violation log summaries,
    and current allowlist configuration into a security view.
    """

    def __init__(
        self,
        enforcer: RuntimeAllowlistEnforcer,
        logger: AllowlistViolationLogger,
    ):
        self._enforcer = enforcer
        self._logger = logger

    def render(self) -> dict:
        config = self._enforcer._config
        return {
            "generated_at": time.time(),
            "allowlist": {
                "size": config.size(),
                "source": config.source.value,
                "version": config.version,
                "tools": sorted(config.allowed_tool_names),
            },
            "enforcement_stats": self._enforcer.stats(),
            "violations_1h": self._logger.summary(3600.0),
            "violations_24h": self._logger.summary(86400.0),
        }
```

## Comparison

| Approach | LLM Tool List Filtering | Call-Time Blocking | Dynamic Reload | Violation Audit | Dashboard |
|---|---|---|---|---|---|
| RuntimeAllowlistEnforcer | Yes | Yes | No | Via logger | No |
| AllowlistGatedToolDispatcher | No | Via enforcer | No | Via enforcer | No |
| DynamicAllowlistLoader | No | No | Yes | No | No |
| AllowlistViolationLogger | No | No | No | Yes | No |
| AllowlistEnforcementDashboard | No | No | No | No | Yes |

**Best for production**: Apply the allowlist at two points — when building the tool list sent to the LLM (`filter_tool_definitions`) and when dispatching a tool call (`check_call`) — the first prevents the LLM from knowing non-allowlisted tools exist, the second catches the rare case where a stale tool list or prompt injection produces a call to an unlisted tool. Default all newly registered tools to non-allowlisted: require an explicit allowlist addition (code change or config update) to make a tool LLM-callable, rather than automatically exposing everything. Monitor `unique_blocked_tools` in `AllowlistViolationLogger`: if a tool that was never advertised to the LLM appears in violation logs, it means prompt injection is actively discovering and attempting internal tools — investigate what content is in context for those sessions.
