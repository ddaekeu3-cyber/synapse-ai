---
title: "Agent Doesn't Implement Graceful Degradation When Tool Registry Is Unavailable"
description: "Agents that depend on a central tool registry to discover and dispatch tools fail completely when the registry is unreachable — returning hard errors instead of using cached tool definitions or falling back to a minimal safe capability set. Implement graceful degradation that serves stale registry data under a TTL, falls back to a hardcoded essential tool subset, and clearly communicates reduced capability to the user."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-graceful-degradation-when-tool-registry-is-unavailable
tags: [graceful-degradation, tool-registry, fallback-capability, stale-cache, capability-reduction, fault-tolerance]
symptoms:
  - "Agent returns 500 errors when tool registry service is down for maintenance"
  - "All tool calls fail during registry outage even for tools that were working moments ago"
  - "No cached tool definitions available after registry becomes unreachable"
  - "Users receive unhelpful 'service unavailable' errors when only the registry is down"
  - "No distinction between 'tool not available' and 'registry temporarily unreachable'"
---

## Why This Happens

Tool registries are infrastructure dependencies. When deployed as a separate service, they have their own availability SLA which is never 100%. An agent that calls the registry on every tool dispatch — to check permissions, resolve tool metadata, or verify the tool exists — inherits that dependency's downtime. Graceful degradation requires a multi-tier fallback: first, a hot in-memory cache of recently-fetched tool definitions; second, a warm disk/Redis cache with a longer TTL; and third, a hardcoded minimal tool set that covers essential operations. The agent should communicate degraded mode to users but continue serving rather than failing completely.

## Solution 1: Tool Definition Cache

```python
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: Dict[str, Any]
    version: str = "1.0"
    essential: bool = False     # True = included in fallback minimal set
    fetched_at: float = field(default_factory=time.time)


class ToolDefinitionCache:
    """
    Two-tier cache: hot (in-memory, short TTL) and warm (longer TTL).
    Falls back through tiers when registry is unreachable.
    """

    def __init__(
        self,
        hot_ttl_seconds: float = 300.0,
        warm_ttl_seconds: float = 3600.0,
    ):
        self._hot: Dict[str, ToolDefinition] = {}
        self._warm: Dict[str, ToolDefinition] = {}
        self._hot_ttl = hot_ttl_seconds
        self._warm_ttl = warm_ttl_seconds

    def store(self, tool: ToolDefinition) -> None:
        self._hot[tool.name] = tool
        self._warm[tool.name] = tool

    def get_hot(self, name: str) -> Optional[ToolDefinition]:
        tool = self._hot.get(name)
        if tool and time.time() - tool.fetched_at <= self._hot_ttl:
            return tool
        self._hot.pop(name, None)
        return None

    def get_warm(self, name: str) -> Optional[ToolDefinition]:
        tool = self._warm.get(name)
        if tool and time.time() - tool.fetched_at <= self._warm_ttl:
            return tool
        return None

    def get(self, name: str) -> Optional[ToolDefinition]:
        return self.get_hot(name) or self.get_warm(name)

    def all_warm(self) -> List[ToolDefinition]:
        cutoff = time.time() - self._warm_ttl
        return [t for t in self._warm.values() if t.fetched_at >= cutoff]

    def essential_tools(self) -> List[ToolDefinition]:
        return [t for t in self._warm.values() if t.essential]
```

## Solution 2: Registry Availability Monitor

```python
import asyncio
import time
from typing import Optional


class RegistryAvailabilityMonitor:
    """
    Tracks registry reachability with consecutive failure counting.
    Declares registry unavailable after N consecutive failures and
    switches to degraded mode automatically.
    """

    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_threshold: int = 2,
        probe_interval_seconds: float = 30.0,
    ):
        self._failure_threshold = failure_threshold
        self._recovery_threshold = recovery_threshold
        self._probe_interval = probe_interval_seconds
        self._consecutive_failures = 0
        self._consecutive_successes = 0
        self._available = True
        self._degraded_since: Optional[float] = None
        self._last_probe_at: float = 0.0

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._consecutive_successes += 1
        if not self._available and self._consecutive_successes >= self._recovery_threshold:
            self._available = True
            self._degraded_since = None
            self._consecutive_successes = 0

    def record_failure(self) -> None:
        self._consecutive_successes = 0
        self._consecutive_failures += 1
        if self._available and self._consecutive_failures >= self._failure_threshold:
            self._available = False
            self._degraded_since = time.time()

    def is_available(self) -> bool:
        return self._available

    def degraded_duration_seconds(self) -> Optional[float]:
        if self._degraded_since is None:
            return None
        return round(time.time() - self._degraded_since, 1)

    def status(self) -> dict:
        return {
            "available": self._available,
            "consecutive_failures": self._consecutive_failures,
            "degraded_since": self._degraded_since,
            "degraded_duration_seconds": self.degraded_duration_seconds(),
        }
```

## Solution 3: Degraded Mode Tool Resolver

```python
from typing import List, Optional


ESSENTIAL_FALLBACK_TOOLS = [
    ToolDefinition(
        name="answer_directly",
        description="Respond to the user using only the model's knowledge, without external tools.",
        parameters={},
        essential=True,
    ),
    ToolDefinition(
        name="clarify_request",
        description="Ask the user to clarify or rephrase their request.",
        parameters={"question": {"type": "string"}},
        essential=True,
    ),
]


class DegradedModeToolResolver:
    """
    Resolves tool definitions under registry unavailability.
    Priority: hot cache → warm cache → essential fallback set.
    Reports which tier served the resolution.
    """

    def __init__(
        self,
        cache: ToolDefinitionCache,
        monitor: RegistryAvailabilityMonitor,
    ):
        self._cache = cache
        self._monitor = monitor

    def resolve(self, tool_name: str) -> dict:
        if self._monitor.is_available():
            # Normal path — caller should fetch from live registry
            return {"source": "registry", "tool": None, "degraded": False}

        hot = self._cache.get_hot(tool_name)
        if hot:
            return {"source": "hot_cache", "tool": hot, "degraded": True}

        warm = self._cache.get_warm(tool_name)
        if warm:
            return {"source": "warm_cache", "tool": warm, "degraded": True}

        # Fall back to essential tools only
        essential = next(
            (t for t in ESSENTIAL_FALLBACK_TOOLS if t.name == tool_name), None
        )
        if essential:
            return {"source": "essential_fallback", "tool": essential, "degraded": True}

        return {"source": "unavailable", "tool": None, "degraded": True}

    def available_tools_degraded(self) -> List[ToolDefinition]:
        warm = self._cache.all_warm()
        if warm:
            return warm
        return list(ESSENTIAL_FALLBACK_TOOLS)
```

## Solution 4: Capability Announcement Generator

```python
from typing import List


class CapabilityAnnouncementGenerator:
    """
    Produces user-facing messages that explain which capabilities are
    reduced during registry degradation, without exposing infrastructure details.
    """

    def __init__(self, resolver: DegradedModeToolResolver):
        self._resolver = resolver

    def generate(self) -> Optional[str]:
        if not self._resolver._monitor._degraded_since:
            return None

        available = self._resolver.available_tools_degraded()
        tool_names = [t.name for t in available if not t.essential]

        if tool_names:
            tool_list = ", ".join(tool_names[:5])
            suffix = f" and {len(tool_names) - 5} more" if len(tool_names) > 5 else ""
            return (
                f"Some tools are temporarily unavailable due to a service disruption. "
                f"I can still use: {tool_list}{suffix}. "
                f"I'll do my best to help with the available capabilities."
            )
        else:
            return (
                "External tools are temporarily unavailable. "
                "I can answer questions using my built-in knowledge, "
                "but cannot perform live lookups or external actions right now."
            )
```

## Solution 5: Registry-Aware Tool Dispatcher

```python
import asyncio
from typing import Any, Callable, Optional


class RegistryAwareToolDispatcher:
    """
    Dispatches tool calls with registry fallback.
    Fetches live tool definitions when registry is up;
    uses cache and degraded resolver when it is down.
    """

    def __init__(
        self,
        cache: ToolDefinitionCache,
        monitor: RegistryAvailabilityMonitor,
        resolver: DegradedModeToolResolver,
        registry_fetch_fn: Callable,
    ):
        self._cache = cache
        self._monitor = monitor
        self._resolver = resolver
        self._registry_fetch = registry_fetch_fn
        self._degraded_dispatches = 0
        self._normal_dispatches = 0

    async def get_tool_definition(self, tool_name: str) -> Optional[ToolDefinition]:
        if self._monitor.is_available():
            try:
                defn = await asyncio.wait_for(
                    self._registry_fetch(tool_name), timeout=2.0
                )
                if defn:
                    self._cache.store(defn)
                    self._monitor.record_success()
                    self._normal_dispatches += 1
                    return defn
            except Exception:
                self._monitor.record_failure()

        result = self._resolver.resolve(tool_name)
        if result["tool"]:
            self._degraded_dispatches += 1
            return result["tool"]
        return None

    def stats(self) -> dict:
        return {
            "normal_dispatches": self._normal_dispatches,
            "degraded_dispatches": self._degraded_dispatches,
            "registry_status": self._monitor.status(),
        }
```

## Solution 6: Degradation Dashboard

```python
import time


class ToolRegistryDegradationDashboard:
    """
    Combines registry availability status, cache inventory,
    dispatcher stats, and user-facing capability summary.
    """

    def __init__(
        self,
        monitor: RegistryAvailabilityMonitor,
        cache: ToolDefinitionCache,
        dispatcher: RegistryAwareToolDispatcher,
        announcement_generator: CapabilityAnnouncementGenerator,
    ):
        self._monitor = monitor
        self._cache = cache
        self._dispatcher = dispatcher
        self._announcer = announcement_generator

    def render(self) -> dict:
        warm_tools = self._cache.all_warm()
        return {
            "generated_at": time.time(),
            "registry": self._monitor.status(),
            "cache": {
                "warm_tool_count": len(warm_tools),
                "essential_tool_count": len(self._cache.essential_tools()),
                "warm_tool_names": [t.name for t in warm_tools],
            },
            "dispatcher": self._dispatcher.stats(),
            "user_announcement": self._announcer.generate(),
        }
```

## Comparison

| Approach | Cache Fallback | Degraded Mode | Capability Announcement | Auto-Recovery | Dashboard |
|---|---|---|---|---|---|
| ToolDefinitionCache | Yes (hot+warm) | No | No | No | No |
| RegistryAvailabilityMonitor | No | Yes (threshold) | No | Yes (recovery count) | No |
| DegradedModeToolResolver | Via cache | Yes (3-tier) | No | No | No |
| CapabilityAnnouncementGenerator | No | No | Yes (user-facing) | No | No |
| RegistryAwareToolDispatcher | Via cache | Via resolver | No | Via monitor | No |
| ToolRegistryDegradationDashboard | No | No | No | No | Yes |

**Best for production**: Pre-populate the warm cache during agent startup by fetching all tool definitions eagerly — this ensures that even on the very first registry outage, the warm cache has data. Set `hot_ttl=300s` (5 min) for active sessions and `warm_ttl=3600s` (1 hour) for degraded fallback. Mark 3–5 universally available tools as `essential=True` to guarantee a minimal functional agent even when the cache is empty. Always surface `user_announcement` in the response when `degraded=True` — users who understand why capabilities are reduced are less likely to report bugs or escalate unnecessarily.
