---
title: "Agent Doesn't Implement Tool Result Data Minimization"
description: "Agents that inject full tool results into the LLM context expose more data than the task requires: a user lookup that returns the full database row including salary, medical history, and authentication tokens when the agent only needs the user's name and department. Implement tool result data minimization that strips fields not relevant to the current task, enforces per-field access policies, and logs what data was projected into context."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-tool-result-data-minimization
tags: [data-minimization, field-projection, context-privacy, least-privilege-data, sensitive-field-redaction, tool-result-filtering]
symptoms:
  - "User lookup tool returns full database row including sensitive fields the agent never uses"
  - "Credit card numbers appear in tool results that get injected into LLM context"
  - "No field projection — all tool response fields go into context regardless of task relevance"
  - "Audit log shows sensitive PII in LLM prompt payloads that could appear in completions"
  - "Tool results are passed to the LLM unchanged even when only 2 of 20 fields are relevant"
---

## Why This Happens

Tools are designed to return complete records for flexibility. The database query tool returns the whole row; the API tool returns the full response object. Downstream consumers decide what to use. When the downstream consumer is an LLM, this full-record philosophy breaks down: every field injected into context becomes potential material for the LLM's response, increases token cost, and creates a data exposure surface. Data minimization applies field projection between tool execution and context injection, keeping only the fields that the current task requires.

## Solution 1: Field Access Policy

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set


class FieldSensitivity(str, Enum):
    PUBLIC = "public"           # always injectable
    INTERNAL = "internal"       # injectable within the system, not user-facing
    SENSITIVE = "sensitive"     # requires explicit task authorization
    RESTRICTED = "restricted"   # never inject into LLM context


@dataclass
class FieldPolicy:
    field_name: str
    sensitivity: FieldSensitivity
    always_include: bool = False      # override for critical fields like "id"
    redact_fn: Optional[Callable[[Any], Any]] = None  # e.g. mask last 4 digits
    alias: Optional[str] = None       # rename field in projected output


@dataclass
class ToolDataPolicy:
    tool_name: str
    fields: List[FieldPolicy] = field(default_factory=list)
    default_sensitivity: FieldSensitivity = FieldSensitivity.INTERNAL
    strip_unlisted_fields: bool = True  # if True, unknown fields are dropped

    def field_map(self) -> Dict[str, FieldPolicy]:
        return {f.field_name: f for f in self.fields}
```

## Solution 2: Task Authorization Context

```python
from dataclasses import dataclass, field
from typing import Set


@dataclass
class TaskAuthorizationContext:
    """
    Describes what data sensitivity levels the current task is authorized to access.
    Created at task start and passed to the data minimizer for each tool result.
    """
    task_id: str
    session_id: str
    user_id: str
    authorized_sensitivities: Set[FieldSensitivity] = field(
        default_factory=lambda: {FieldSensitivity.PUBLIC, FieldSensitivity.INTERNAL}
    )
    authorized_fields: Set[str] = field(default_factory=set)  # explicit allow-list overrides
    denied_fields: Set[str] = field(default_factory=set)      # explicit deny-list overrides

    def may_access(self, field_name: str, sensitivity: FieldSensitivity) -> bool:
        if field_name in self.denied_fields:
            return False
        if field_name in self.authorized_fields:
            return True
        if sensitivity == FieldSensitivity.RESTRICTED:
            return False
        return sensitivity in self.authorized_sensitivities
```

## Solution 3: Tool Result Minimizer

```python
import copy
from typing import Any, Dict, List, Optional, Tuple


class ToolResultMinimizer:
    """
    Projects a tool result to only the fields the current task is authorized to access.
    Applies redaction functions to sensitive-but-authorized fields.
    Logs what was dropped for audit purposes.
    """

    def __init__(self):
        self._policies: Dict[str, ToolDataPolicy] = {}

    def register(self, policy: ToolDataPolicy) -> None:
        self._policies[policy.tool_name] = policy

    def minimize(
        self,
        tool_name: str,
        result: Any,
        auth_ctx: TaskAuthorizationContext,
    ) -> Tuple[Any, "MinimizationReport"]:
        policy = self._policies.get(tool_name)

        if policy is None:
            # No policy registered — pass through with a warning
            return result, MinimizationReport(
                tool_name=tool_name,
                policy_found=False,
                fields_included=[],
                fields_dropped=[],
                fields_redacted=[],
            )

        if isinstance(result, list):
            minimized_list = []
            all_reports = []
            for item in result:
                minimized_item, report = self._minimize_dict(item, policy, auth_ctx)
                minimized_list.append(minimized_item)
                all_reports.append(report)
            # Merge reports
            combined = MinimizationReport(
                tool_name=tool_name,
                policy_found=True,
                fields_included=all_reports[0].fields_included if all_reports else [],
                fields_dropped=all_reports[0].fields_dropped if all_reports else [],
                fields_redacted=all_reports[0].fields_redacted if all_reports else [],
                items_processed=len(result),
            )
            return minimized_list, combined

        if isinstance(result, dict):
            return self._minimize_dict(result, policy, auth_ctx)

        return result, MinimizationReport(tool_name=tool_name, policy_found=True)

    def _minimize_dict(
        self,
        record: dict,
        policy: ToolDataPolicy,
        auth_ctx: TaskAuthorizationContext,
    ) -> Tuple[dict, "MinimizationReport"]:
        field_map = policy.field_map()
        output = {}
        included = []
        dropped = []
        redacted = []

        for key, value in record.items():
            fp = field_map.get(key)

            if fp is None:
                if policy.strip_unlisted_fields:
                    dropped.append(key)
                    continue
                sensitivity = policy.default_sensitivity
            else:
                sensitivity = fp.sensitivity

            if fp and fp.always_include:
                out_key = fp.alias or key
                output[out_key] = value
                included.append(key)
                continue

            if not auth_ctx.may_access(key, sensitivity):
                dropped.append(key)
                continue

            out_key = (fp.alias or key) if fp else key
            out_value = fp.redact_fn(value) if (fp and fp.redact_fn) else value
            if fp and fp.redact_fn and out_value != value:
                redacted.append(key)
            output[out_key] = out_value
            included.append(key)

        return output, MinimizationReport(
            tool_name=policy.tool_name,
            policy_found=True,
            fields_included=included,
            fields_dropped=dropped,
            fields_redacted=redacted,
        )


from dataclasses import dataclass


@dataclass
class MinimizationReport:
    tool_name: str
    policy_found: bool = True
    fields_included: list = field(default_factory=list)
    fields_dropped: list = field(default_factory=list)
    fields_redacted: list = field(default_factory=list)
    items_processed: int = 1

    def reduction_pct(self) -> float:
        total = len(self.fields_included) + len(self.fields_dropped)
        return round(len(self.fields_dropped) / max(total, 1) * 100, 1)
```

## Solution 4: Minimization-Gated Tool Dispatcher

```python
import asyncio
from typing import Any, Callable, Dict, Optional


class MinimizationGatedToolDispatcher:
    """
    Wraps tool dispatch with automatic result minimization.
    The LLM context only ever sees the minimized result.
    Raw results are never forwarded to the context injection layer.
    """

    def __init__(
        self,
        minimizer: ToolResultMinimizer,
        auth_ctx_provider: Callable[[str], TaskAuthorizationContext],
    ):
        self._minimizer = minimizer
        self._auth_ctx_provider = auth_ctx_provider
        self._dispatch_count = 0
        self._total_fields_dropped = 0

    async def dispatch(
        self,
        tool_name: str,
        tool_fn: Callable,
        session_id: str,
        **kwargs: Any,
    ) -> tuple:  # (minimized_result, report)
        self._dispatch_count += 1
        raw_result = await tool_fn(**kwargs)
        auth_ctx = self._auth_ctx_provider(session_id)
        minimized, report = self._minimizer.minimize(tool_name, raw_result, auth_ctx)
        self._total_fields_dropped += len(report.fields_dropped)
        return minimized, report

    def stats(self) -> dict:
        return {
            "total_dispatches": self._dispatch_count,
            "total_fields_dropped": self._total_fields_dropped,
            "avg_fields_dropped_per_call": round(
                self._total_fields_dropped / max(self._dispatch_count, 1), 1
            ),
        }
```

## Solution 5: Common Redaction Helpers

```python
import re
from typing import Any


class CommonRedactors:
    """Ready-made redaction functions for common sensitive field types."""

    @staticmethod
    def mask_email(value: Any) -> str:
        s = str(value)
        at = s.find("@")
        if at <= 0:
            return "***"
        return s[0] + "***" + s[at:]

    @staticmethod
    def last_four(value: Any) -> str:
        s = re.sub(r"\D", "", str(value))
        return "****" + s[-4:] if len(s) >= 4 else "****"

    @staticmethod
    def redact_ssn(value: Any) -> str:
        return "***-**-" + str(value)[-4:]

    @staticmethod
    def truncate_token(value: Any) -> str:
        s = str(value)
        return s[:6] + "..." + s[-4:] if len(s) > 12 else "***"

    @staticmethod
    def keep_domain_only(value: Any) -> str:
        s = str(value)
        at = s.find("@")
        return "@" + s[at + 1:] if at >= 0 else "***"

    @staticmethod
    def round_to_decade(value: Any) -> int:
        try:
            return (int(float(value)) // 10) * 10
        except (ValueError, TypeError):
            return 0
```

## Solution 6: Data Minimization Audit Logger

```python
import time
from collections import defaultdict
from typing import List


class DataMinimizationAuditLogger:
    """
    Records minimization events for compliance audit.
    Tracks which sensitive fields were dropped and which tools
    have no policy (unguarded tools that pass through all data).
    """

    def __init__(self, window_seconds: float = 3600.0):
        self._window = window_seconds
        self._events: List[dict] = []

    def record(
        self,
        session_id: str,
        report: MinimizationReport,
    ) -> None:
        self._events.append({
            "ts": time.time(),
            "session_id": session_id,
            "tool_name": report.tool_name,
            "policy_found": report.policy_found,
            "fields_dropped": len(report.fields_dropped),
            "fields_redacted": len(report.fields_redacted),
            "fields_included": len(report.fields_included),
            "dropped_names": list(report.fields_dropped),
        })

    def _trim(self) -> None:
        cutoff = time.time() - self._window
        self._events = [e for e in self._events if e["ts"] >= cutoff]

    def summary(self) -> dict:
        self._trim()
        total = len(self._events)
        no_policy = sum(1 for e in self._events if not e["policy_found"])
        by_tool: dict = defaultdict(lambda: {"calls": 0, "fields_dropped": 0})
        for e in self._events:
            by_tool[e["tool_name"]]["calls"] += 1
            by_tool[e["tool_name"]]["fields_dropped"] += e["fields_dropped"]

        alerts = []
        if no_policy > 0:
            tools_without_policy = list({
                e["tool_name"] for e in self._events if not e["policy_found"]
            })
            alerts.append({
                "type": "tools_without_policy",
                "tools": tools_without_policy,
                "message": f"{len(tools_without_policy)} tool(s) have no data policy — all fields pass through unfiltered.",
            })

        return {
            "total_minimizations": total,
            "tools_without_policy": no_policy,
            "by_tool": {
                tool: {
                    "calls": stats["calls"],
                    "avg_fields_dropped": round(stats["fields_dropped"] / max(stats["calls"], 1), 1),
                }
                for tool, stats in by_tool.items()
            },
            "alerts": alerts,
        }
```

## Comparison

| Approach | Field Projection | Sensitivity Levels | Auth Context | Redaction Helpers | Audit Log |
|---|---|---|---|---|---|
| ToolResultMinimizer | Yes | Yes (4 levels) | Yes | Via redact_fn | No |
| MinimizationGatedToolDispatcher | Via minimizer | Via minimizer | Via provider | No | No |
| CommonRedactors | No | No | No | Yes (6 helpers) | No |
| DataMinimizationAuditLogger | No | No | No | No | Yes |

**Best for production**: Register a `ToolDataPolicy` for every tool that accesses a database or external API. Mark fields like `password_hash`, `auth_token`, `ssn`, and `credit_card` as `RESTRICTED` — these must never appear in LLM context. Mark `email`, `phone`, and `salary` as `SENSITIVE` with redaction functions so they are masked rather than dropped entirely when the task legitimately needs to reference them. Monitor `DataMinimizationAuditLogger.summary()` for tools without policies — each unguarded tool is a data minimization gap that should be addressed before the next compliance review.
