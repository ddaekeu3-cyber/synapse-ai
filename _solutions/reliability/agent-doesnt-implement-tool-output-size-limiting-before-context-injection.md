---
title: "Agent Doesn't Implement Tool Output Size Limiting Before Context Injection"
description: "Agents that inject raw tool outputs directly into the LLM context without size limiting allow a single large tool result — a full webpage, a database dump, a long API response — to consume the entire context window, crowding out conversation history, system instructions, and results from other tools. Implement tool output size limiting that truncates, summarizes, or chunks large results before they reach the context, preserving the available window for other content."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-tool-output-size-limiting-before-context-injection
tags: [output-size, context-injection, truncation, token-budget, large-responses, context-overflow]
symptoms:
  - "One large tool result fills the context window and truncates earlier conversation"
  - "Database query returning thousands of rows injected wholesale into context"
  - "Webpage fetch returning 50KB of HTML injected without size check"
  - "System prompt and history are displaced by a single oversized tool result"
  - "No per-tool maximum output size enforced before context injection"
---

## Why This Happens

Tool implementations return whatever the API returns — full HTML, complete database result sets, raw JSON blobs. There is no layer between the tool return value and the context assembler that enforces a size limit. The context assembler appends all tool results and only discovers the overflow when the total token count exceeds the window. By then, earlier content has already been dropped or truncated. Size limiting must be applied per-tool-result before injection, with a strategy (truncate, extract key fields, chunk) that preserves as much signal as possible within the budget.

## Solution 1: Output Size Policy

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Optional


class OversizeStrategy(str, Enum):
    TRUNCATE = "truncate"           # keep first N chars/tokens
    TAIL = "tail"                   # keep last N chars (useful for logs)
    EXTRACT = "extract"             # call extractor fn to pull key fields
    SUMMARIZE = "summarize"         # LLM call to compress (expensive)
    CHUNK_FIRST = "chunk_first"     # return first chunk only
    REJECT = "reject"               # block injection entirely


@dataclass
class ToolOutputSizePolicy:
    max_chars: int = 8000
    max_tokens_estimate: int = 2000
    strategy: OversizeStrategy = OversizeStrategy.TRUNCATE
    truncation_suffix: str = "\n...[output truncated]"
    extractor: Optional[Callable[[Any], str]] = None   # for EXTRACT strategy
    tokens_per_char: float = 0.25

    def estimated_tokens(self, text: str) -> int:
        return max(1, int(len(text) * self.tokens_per_char))
```

## Solution 2: Per-Tool Policy Registry

```python
from typing import Dict, Optional


_DEFAULT_POLICY = ToolOutputSizePolicy(
    max_chars=8000,
    strategy=OversizeStrategy.TRUNCATE,
)

_TOOL_POLICIES: Dict[str, ToolOutputSizePolicy] = {
    "web_fetch": ToolOutputSizePolicy(max_chars=6000, strategy=OversizeStrategy.EXTRACT),
    "database_query": ToolOutputSizePolicy(max_chars=4000, strategy=OversizeStrategy.TRUNCATE),
    "file_read": ToolOutputSizePolicy(max_chars=10000, strategy=OversizeStrategy.CHUNK_FIRST),
    "log_fetch": ToolOutputSizePolicy(max_chars=5000, strategy=OversizeStrategy.TAIL),
    "execute_code": ToolOutputSizePolicy(max_chars=3000, strategy=OversizeStrategy.TAIL),
}


class ToolOutputPolicyRegistry:
    def __init__(self, default_policy: Optional[ToolOutputSizePolicy] = None):
        self._default = default_policy or _DEFAULT_POLICY
        self._policies: Dict[str, ToolOutputSizePolicy] = {}

    def register(self, tool_name: str, policy: ToolOutputSizePolicy) -> None:
        self._policies[tool_name] = policy

    def get(self, tool_name: str) -> ToolOutputSizePolicy:
        return self._policies.get(tool_name, self._default)
```

## Solution 3: Output Size Limiter

```python
import json
from typing import Any


class ToolOutputSizeLimiter:
    """
    Applies the configured size limiting strategy to a tool result
    before it is injected into the LLM context.
    """

    def __init__(self, registry: ToolOutputPolicyRegistry):
        self._registry = registry

    def limit(self, tool_name: str, raw_output: Any) -> dict:
        policy = self._registry.get(tool_name)
        text = self._to_text(raw_output)
        original_len = len(text)

        if len(text) <= policy.max_chars:
            return {
                "text": text,
                "truncated": False,
                "original_chars": original_len,
                "final_chars": len(text),
                "strategy": None,
            }

        strategy = policy.strategy

        if strategy == OversizeStrategy.REJECT:
            return {
                "text": f"[Output rejected: {original_len} chars exceeds limit {policy.max_chars}]",
                "truncated": True,
                "original_chars": original_len,
                "final_chars": 0,
                "strategy": strategy.value,
            }

        if strategy == OversizeStrategy.TRUNCATE:
            truncated = text[: policy.max_chars] + policy.truncation_suffix

        elif strategy == OversizeStrategy.TAIL:
            truncated = "...[truncated]\n" + text[-policy.max_chars :]

        elif strategy == OversizeStrategy.CHUNK_FIRST:
            truncated = text[: policy.max_chars] + policy.truncation_suffix

        elif strategy == OversizeStrategy.EXTRACT:
            if policy.extractor:
                try:
                    extracted = policy.extractor(raw_output)
                    truncated = str(extracted)[: policy.max_chars]
                except Exception:
                    truncated = text[: policy.max_chars] + policy.truncation_suffix
            else:
                truncated = text[: policy.max_chars] + policy.truncation_suffix

        else:
            truncated = text[: policy.max_chars] + policy.truncation_suffix

        return {
            "text": truncated,
            "truncated": True,
            "original_chars": original_len,
            "final_chars": len(truncated),
            "strategy": strategy.value,
        }

    @staticmethod
    def _to_text(output: Any) -> str:
        if isinstance(output, str):
            return output
        if isinstance(output, (dict, list)):
            try:
                return json.dumps(output, indent=2)
            except (TypeError, ValueError):
                pass
        return str(output)
```

## Solution 4: Size-Limited Context Injector

```python
import time
from typing import Any, Dict, List


@dataclass
class InjectionRecord:
    tool_name: str
    original_chars: int
    final_chars: int
    truncated: bool
    strategy: Optional[str]
    injected_at: float


class SizeLimitedContextInjector:
    """
    Injects tool results into the context with size limiting applied.
    Tracks injection history for monitoring and debugging.
    """

    def __init__(
        self,
        limiter: ToolOutputSizeLimiter,
        max_total_tool_result_chars: int = 20000,
    ):
        self._limiter = limiter
        self._max_total = max_total_tool_result_chars
        self._records: List[InjectionRecord] = []
        self._session_total = 0

    def inject(self, tool_name: str, raw_output: Any) -> str:
        result = self._limiter.limit(tool_name, raw_output)
        text = result["text"]

        # Enforce total budget across all tool results this session
        remaining = self._max_total - self._session_total
        if len(text) > remaining:
            text = text[:remaining] + "\n...[global budget exhausted]"

        self._session_total += len(text)
        self._records.append(InjectionRecord(
            tool_name=tool_name,
            original_chars=result["original_chars"],
            final_chars=len(text),
            truncated=result["truncated"] or len(text) < result["final_chars"],
            strategy=result["strategy"],
            injected_at=time.time(),
        ))
        return text

    def session_summary(self) -> dict:
        return {
            "total_chars_injected": self._session_total,
            "injections": len(self._records),
            "truncations": sum(1 for r in self._records if r.truncated),
            "chars_saved": sum(
                r.original_chars - r.final_chars for r in self._records
            ),
        }

    def reset_session(self) -> None:
        self._records = []
        self._session_total = 0
```

## Solution 5: Oversized Output Monitor

```python
import time
from collections import defaultdict
from threading import Lock
from typing import Dict, List


class OversizedOutputMonitor:
    """
    Tracks which tools most frequently produce oversized outputs.
    Helps calibrate per-tool policies and identify upstream data issues.
    """

    def __init__(self):
        self._lock = Lock()
        self._events: Dict[str, List[dict]] = defaultdict(list)

    def record(self, tool_name: str, original_chars: int, limit: int) -> None:
        if original_chars <= limit:
            return
        with self._lock:
            self._events[tool_name].append({
                "ts": time.time(),
                "original_chars": original_chars,
                "limit": limit,
                "ratio": round(original_chars / max(limit, 1), 2),
            })

    def report(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            result = {}
            for tool, events in self._events.items():
                recent = [e for e in events if e["ts"] >= cutoff]
                if not recent:
                    continue
                avg_ratio = sum(e["ratio"] for e in recent) / len(recent)
                result[tool] = {
                    "oversized_outputs": len(recent),
                    "avg_oversize_ratio": round(avg_ratio, 2),
                    "max_chars_seen": max(e["original_chars"] for e in recent),
                }
        return {"window_seconds": window_seconds, "tools": result}
```

## Solution 6: Output Size Dashboard

```python
import time


class ToolOutputSizeDashboard:
    """
    Combines current session injection state and fleet-level
    oversized output patterns into a single operational view.
    """

    def __init__(
        self,
        injector: SizeLimitedContextInjector,
        monitor: OversizedOutputMonitor,
        registry: ToolOutputPolicyRegistry,
    ):
        self._injector = injector
        self._monitor = monitor
        self._registry = registry

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "current_session": self._injector.session_summary(),
            "oversized_outputs_last_hour": self._monitor.report(window_seconds=3600.0),
        }
```

## Comparison

| Approach | Per-Tool Policy | Multiple Strategies | Global Budget | Oversize Monitoring | Session Tracking |
|---|---|---|---|---|---|
| ToolOutputPolicyRegistry | Yes | Via policy | No | No | No |
| ToolOutputSizeLimiter | Via registry | Yes (5 strategies) | No | No | No |
| SizeLimitedContextInjector | Via limiter | Via limiter | Yes | No | Yes |
| OversizedOutputMonitor | No | No | No | Yes | No |
| ToolOutputSizeDashboard | No | No | No | No | Yes (session) |

**Best for production**: Set `max_chars` based on the tool's expected output distribution, not an arbitrary global limit — a web fetch legitimately returns 50KB while a status check returns 100 bytes. Use `OversizeStrategy.TAIL` for log and code execution tools where the most recent output lines are most relevant; `TRUNCATE` for documents where the opening is most informative. Monitor `avg_oversize_ratio > 3.0` in `OversizedOutputMonitor`: a tool consistently returning 3× its limit suggests the upstream query or request parameters need adjustment, not just the limit.
