---
title: "Agent Doesn't Implement Data Minimization Before Tool Calls"
description: "Agents that pass full user context — including PII, credentials, and sensitive fields — to every tool call violate data minimization principles: a weather tool does not need the user's email address, and a search tool does not need their account ID. Implement data minimization that strips irrelevant sensitive fields from tool arguments before dispatch, based on per-tool field allowlists, preventing unnecessary exposure of personal data to third-party tool backends."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-data-minimization-before-tool-calls
tags: [data-minimization, pii, tool-arguments, privacy, gdpr, least-data-principle]
symptoms:
  - "Weather tool receives the user's full name, email, and account ID as part of the context"
  - "Search tool called with user's date of birth included in the request metadata"
  - "Third-party tool backends receive more user data than they need to fulfill the request"
  - "No per-tool definition of which fields are required versus optional versus prohibited"
  - "GDPR audit finds that personal data is being sent to external tools without a legal basis"
---

## Why This Happens

Agent frameworks often pass a rich context object containing all known user attributes to every tool call — it is simpler to pass everything than to enumerate what each tool needs. When tools are third-party services, this over-sharing violates data minimization principles (GDPR Article 5(1)(c)) and creates unnecessary data exposure risk. Data minimization requires defining, per tool, which fields are permitted in the arguments and which must be stripped before dispatch. This requires a field allowlist per tool, a stripping step in the dispatch path, and audit logging of what was stripped.

## Solution 1: Tool Data Policy

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, FrozenSet, Set


class FieldSensitivity(str, Enum):
    PUBLIC = "public"           # always safe to pass
    INTERNAL = "internal"       # safe for internal tools only
    PII = "pii"                 # personal identifiable information
    CREDENTIAL = "credential"   # API keys, passwords, tokens
    PROHIBITED = "prohibited"   # must never appear in tool arguments


@dataclass(frozen=True)
class ToolDataPolicy:
    tool_name: str
    allowed_fields: FrozenSet[str]          # only these fields pass through
    prohibited_fields: FrozenSet[str] = field(default_factory=frozenset)
    strip_unknown_fields: bool = True       # drop any field not in allowed_fields
    allow_nested_context: bool = False      # if False, no nested dicts pass through
    description: str = ""
```

## Solution 2: Data Policy Registry

```python
from typing import Dict, Optional


class ToolDataPolicyRegistry:
    """
    Stores per-tool data policies and provides lookup by tool name.
    A default deny-all policy is returned for unregistered tools.
    """

    _DENY_ALL = ToolDataPolicy(
        tool_name="__default__",
        allowed_fields=frozenset(),
        prohibited_fields=frozenset(),
        strip_unknown_fields=True,
        description="default deny-all for unregistered tools",
    )

    def __init__(self):
        self._policies: Dict[str, ToolDataPolicy] = {}

    def register(self, policy: ToolDataPolicy) -> None:
        self._policies[policy.tool_name] = policy

    def get(self, tool_name: str) -> ToolDataPolicy:
        return self._policies.get(tool_name, self._DENY_ALL)

    def registered_tools(self) -> list:
        return list(self._policies.keys())


def build_example_registry() -> ToolDataPolicyRegistry:
    registry = ToolDataPolicyRegistry()

    registry.register(ToolDataPolicy(
        tool_name="weather",
        allowed_fields=frozenset({"location", "units", "forecast_days"}),
        prohibited_fields=frozenset({"email", "user_id", "account_id", "phone"}),
        description="Weather tool: location data only",
    ))

    registry.register(ToolDataPolicy(
        tool_name="web_search",
        allowed_fields=frozenset({"query", "num_results", "language", "safe_search"}),
        prohibited_fields=frozenset({"email", "user_id", "phone", "address"}),
        description="Search tool: query parameters only",
    ))

    registry.register(ToolDataPolicy(
        tool_name="send_email",
        allowed_fields=frozenset({"to", "subject", "body", "cc"}),
        prohibited_fields=frozenset({"password", "api_key", "secret", "token"}),
        description="Email tool: message fields only, no credentials",
    ))

    registry.register(ToolDataPolicy(
        tool_name="database_query",
        allowed_fields=frozenset({"query", "parameters", "timeout_seconds"}),
        prohibited_fields=frozenset({"raw_password", "connection_string"}),
        allow_nested_context=True,
        description="DB query: parameterized query only",
    ))

    return registry
```

## Solution 3: Field Stripper

```python
import copy
from typing import Any, Dict, List, Tuple


class ToolArgumentFieldStripper:
    """
    Applies a ToolDataPolicy to a dict of tool arguments.
    Returns a minimized copy and a report of what was stripped.
    """

    def strip(
        self,
        args: Dict[str, Any],
        policy: ToolDataPolicy,
    ) -> Tuple[Dict[str, Any], List[str]]:
        """
        Returns (minimized_args, list_of_stripped_field_names).
        """
        stripped_fields = []
        minimized = {}

        for key, value in args.items():
            # Prohibited fields are always removed
            if key in policy.prohibited_fields:
                stripped_fields.append(f"{key}:prohibited")
                continue

            # Nested dicts: strip if not allowed
            if isinstance(value, dict) and not policy.allow_nested_context:
                stripped_fields.append(f"{key}:nested_dict_not_allowed")
                continue

            # Unknown fields: strip if policy requires
            if policy.strip_unknown_fields and key not in policy.allowed_fields:
                stripped_fields.append(f"{key}:not_in_allowlist")
                continue

            minimized[key] = copy.deepcopy(value)

        return minimized, stripped_fields
```

## Solution 4: Data Minimizing Tool Dispatcher

```python
import time
from typing import Any, Callable, Dict


class DataMinimizingToolDispatcher:
    """
    Applies data minimization before every tool call.
    Logs stripping events and rejects calls where required fields are absent
    after minimization.
    """

    def __init__(
        self,
        policy_registry: ToolDataPolicyRegistry,
        stripper: ToolArgumentFieldStripper,
        tool_executors: Dict[str, Callable],
    ):
        self._registry = policy_registry
        self._stripper = stripper
        self._executors = tool_executors
        self._total_dispatched = 0
        self._total_fields_stripped = 0

    async def dispatch(
        self,
        tool_name: str,
        args: Dict[str, Any],
    ) -> dict:
        policy = self._registry.get(tool_name)
        minimized_args, stripped = self._stripper.strip(args, policy)

        self._total_dispatched += 1
        self._total_fields_stripped += len(stripped)

        executor = self._executors.get(tool_name)
        if executor is None:
            return {
                "success": False,
                "error": f"tool '{tool_name}' not registered",
                "stripped_fields": stripped,
            }

        try:
            result = await executor(**minimized_args)
            return {
                "success": True,
                "result": result,
                "stripped_fields": stripped,
                "fields_sent": list(minimized_args.keys()),
            }
        except Exception as exc:
            return {
                "success": False,
                "error": str(exc),
                "stripped_fields": stripped,
            }

    def stats(self) -> dict:
        return {
            "total_dispatched": self._total_dispatched,
            "total_fields_stripped": self._total_fields_stripped,
            "avg_fields_stripped_per_call": round(
                self._total_fields_stripped / max(self._total_dispatched, 1), 2
            ),
        }
```

## Solution 5: Data Minimization Audit Logger

```python
import json
import time
from pathlib import Path
from threading import Lock
from typing import List


class DataMinimizationAuditLogger:
    """
    Logs field stripping events for compliance audit trails.
    Records which fields were stripped for each tool call.
    """

    def __init__(self, log_path: str = "/tmp/data_minimization_audit.jsonl"):
        self._path = Path(log_path)
        self._lock = Lock()

    def log(
        self,
        tool_name: str,
        stripped_fields: List[str],
        session_id: str = "",
        user_id: str = "",
    ) -> None:
        if not stripped_fields:
            return  # no-op if nothing was stripped
        record = {
            "ts": time.time(),
            "tool_name": tool_name,
            "stripped_fields": stripped_fields,
            "session_id": session_id,
            "user_id": user_id,
        }
        with self._lock:
            with self._path.open("a") as f:
                f.write(json.dumps(record) + "\n")

    def summary(self, window_seconds: float = 86400.0) -> dict:
        cutoff = time.time() - window_seconds
        records = []
        if not self._path.exists():
            return {"window_seconds": window_seconds, "events": 0}
        with self._lock:
            for line in self._path.read_text().splitlines():
                try:
                    r = json.loads(line)
                    if r.get("ts", 0) >= cutoff:
                        records.append(r)
                except json.JSONDecodeError:
                    continue

        field_counts: dict = {}
        for r in records:
            for field_entry in r.get("stripped_fields", []):
                field = field_entry.split(":")[0]
                field_counts[field] = field_counts.get(field, 0) + 1

        return {
            "window_seconds": window_seconds,
            "events": len(records),
            "most_stripped_fields": sorted(
                field_counts.items(), key=lambda x: x[1], reverse=True
            )[:10],
        }
```

## Solution 6: Data Minimization Coverage Auditor

```python
from typing import Any, Dict, List


COMMON_PII_FIELDS = {
    "email", "phone", "address", "dob", "date_of_birth",
    "ssn", "social_security", "passport", "driver_license",
    "credit_card", "bank_account", "ip_address", "user_id",
    "first_name", "last_name", "full_name",
}


class DataMinimizationCoverageAuditor:
    """
    Scans tool call argument samples to identify PII fields that are
    not covered by any registered policy — indicating a policy gap.
    """

    def __init__(self, registry: ToolDataPolicyRegistry):
        self._registry = registry

    def audit(
        self,
        tool_name: str,
        sample_args: List[Dict[str, Any]],
    ) -> dict:
        policy = self._registry.get(tool_name)
        gaps = []

        for sample in sample_args:
            for field_name, value in sample.items():
                if field_name in COMMON_PII_FIELDS:
                    if field_name not in policy.prohibited_fields:
                        if field_name in policy.allowed_fields or not policy.strip_unknown_fields:
                            gaps.append({
                                "field": field_name,
                                "tool": tool_name,
                                "issue": "pii_field_permitted_by_policy",
                                "recommendation": f"Add '{field_name}' to prohibited_fields for tool '{tool_name}'",
                            })

        return {
            "tool_name": tool_name,
            "policy_found": policy.tool_name != "__default__",
            "gaps": gaps,
        }
```

## Comparison

| Approach | Field Allowlist | Prohibited Fields | Strip Unknown | Audit Logging | Gap Detection |
|---|---|---|---|---|---|
| ToolDataPolicy | Yes (frozen set) | Yes | Configurable | No | No |
| ToolDataPolicyRegistry | Via policies | Via policies | Via policies | No | No |
| ToolArgumentFieldStripper | Via policy | Via policy | Via policy | No | No |
| DataMinimizingToolDispatcher | Via registry | Via registry | Via registry | No | Stats |
| DataMinimizationAuditLogger | No | No | No | Yes (JSONL) | No |
| DataMinimizationCoverageAuditor | No | No | No | No | Yes |

**Best for production**: Run `DataMinimizationCoverageAuditor.audit()` against real tool call samples in staging before each production deployment — this surfaces PII fields that are being sent to tools without a legal basis. Default all unregistered tools to deny-all (`strip_unknown_fields=True`, empty `allowed_fields`) so that adding a new tool without registering a policy results in it receiving empty arguments rather than the full context. Export `DataMinimizationAuditLogger.summary()` to your compliance team weekly — the `most_stripped_fields` list reveals which PII fields the LLM most often attempts to include in tool calls, indicating where the system prompt needs stronger data handling instructions.
