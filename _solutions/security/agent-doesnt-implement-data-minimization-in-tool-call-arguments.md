---
title: "Agent Doesn't Implement Data Minimization in Tool Call Arguments"
description: "Agents that forward full user-provided context objects to every tool call expose more data than each tool needs: a search tool receives the user's full profile when it only needs a query string, a logging tool receives raw PII-laden payloads when it only needs an event type. Implement data minimization that strips each tool call argument to only the fields the tool actually requires, reducing the blast radius of any tool-side data exposure."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-data-minimization-in-tool-call-arguments
tags: [data-minimization, pii-protection, tool-arguments, least-privilege, field-stripping, blast-radius]
symptoms:
  - "Tool receives full user profile dict when only user_id is needed"
  - "Logging tool call contains raw payment method objects passed as context"
  - "Third-party API tool receives session metadata including other users' IDs"
  - "No per-tool field allowlist — tools receive whatever the agent assembled"
  - "Data breach via compromised third-party tool exposes far more than necessary"
---

## Why This Happens

Agents often build a rich context object — user profile, session state, conversation history, retrieved documents — and pass it wholesale to every tool call. The tool may only use one or two fields, but the rest travel across the call boundary anyway. If the tool is a third-party integration, a misconfigured webhook, or a logged call, the excess fields become exposure. Data minimization requires a per-tool field allowlist applied before the call is dispatched: only named fields are forwarded; everything else is stripped before leaving the agent process.

## Solution 1: Tool Argument Schema

```python
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set


@dataclass
class ToolArgumentSchema:
    """
    Declares the fields a tool is allowed to receive.
    Fields not listed in allowed_fields are stripped before dispatch.
    """
    tool_name: str
    allowed_fields: Set[str]
    required_fields: Set[str] = field(default_factory=set)
    allow_extra_fields: bool = False   # if True, schema is advisory only

    def validate_required(self, args: Dict[str, Any]) -> List[str]:
        """Returns list of missing required fields."""
        return [f for f in self.required_fields if f not in args]
```

## Solution 2: Argument Field Stripper

```python
import copy
from typing import Any, Dict, List


class ArgumentFieldStripper:
    """
    Strips a tool call argument dict to only the fields declared
    in the tool's ToolArgumentSchema. Returns a deep copy —
    the original args dict is never mutated.
    """

    def strip(
        self,
        args: Dict[str, Any],
        schema: ToolArgumentSchema,
    ) -> Dict[str, Any]:
        if schema.allow_extra_fields:
            return copy.deepcopy(args)

        stripped = {}
        for field_name in schema.allowed_fields:
            if field_name in args:
                stripped[field_name] = copy.deepcopy(args[field_name])

        return stripped

    def audit(
        self,
        args: Dict[str, Any],
        schema: ToolArgumentSchema,
    ) -> Dict[str, Any]:
        """
        Returns a report of which fields were present and which were stripped.
        """
        present = set(args.keys())
        allowed = schema.allowed_fields
        stripped_fields = present - allowed
        forwarded_fields = present & allowed
        missing_required = schema.validate_required(args)

        return {
            "tool_name": schema.tool_name,
            "fields_present": sorted(present),
            "fields_forwarded": sorted(forwarded_fields),
            "fields_stripped": sorted(stripped_fields),
            "missing_required": missing_required,
            "strip_count": len(stripped_fields),
        }
```

## Solution 3: Tool Argument Schema Registry

```python
from typing import Dict, Optional, Set


class ToolArgumentSchemaRegistry:
    """
    Stores per-tool ToolArgumentSchema declarations.
    Provides a fallback policy for tools without a registered schema.
    """

    def __init__(self, default_allow_extra: bool = False):
        self._schemas: Dict[str, ToolArgumentSchema] = {}
        self._default_allow_extra = default_allow_extra

    def register(self, schema: ToolArgumentSchema) -> None:
        self._schemas[schema.tool_name] = schema

    def get(self, tool_name: str) -> ToolArgumentSchema:
        if tool_name in self._schemas:
            return self._schemas[tool_name]
        # Unknown tool: return a permissive or restrictive schema based on policy
        return ToolArgumentSchema(
            tool_name=tool_name,
            allowed_fields=set(),
            allow_extra_fields=self._default_allow_extra,
        )

    def register_many(self, schemas: list) -> None:
        for schema in schemas:
            self.register(schema)

    def all_tool_names(self) -> list:
        return sorted(self._schemas.keys())
```

## Solution 4: Minimizing Tool Call Dispatcher

```python
import time
from typing import Any, Callable, Dict, Optional


class MinimizingToolCallDispatcher:
    """
    Strips tool call arguments to their declared schema before dispatch.
    Logs a minimization report for each call.
    """

    def __init__(
        self,
        registry: ToolArgumentSchemaRegistry,
        stripper: ArgumentFieldStripper,
        audit_log_fn: Optional[Callable[[dict], None]] = None,
    ):
        self._registry = registry
        self._stripper = stripper
        self._audit_log = audit_log_fn
        self._total_strips = 0
        self._total_fields_stripped = 0

    async def dispatch(
        self,
        tool_name: str,
        raw_args: Dict[str, Any],
        tool_fn: Callable,
    ) -> Any:
        schema = self._registry.get(tool_name)
        audit = self._stripper.audit(raw_args, schema)
        minimized_args = self._stripper.strip(raw_args, schema)

        self._total_strips += 1
        self._total_fields_stripped += audit["strip_count"]

        if self._audit_log and audit["strip_count"] > 0:
            self._audit_log({
                "event": "argument_minimization",
                "ts": time.time(),
                **audit,
            })

        missing = schema.validate_required(minimized_args)
        if missing:
            raise ValueError(
                f"Tool '{tool_name}' missing required fields after minimization: {missing}"
            )

        return await tool_fn(**minimized_args)

    def stats(self) -> dict:
        return {
            "total_dispatches": self._total_strips,
            "total_fields_stripped": self._total_fields_stripped,
        }
```

## Solution 5: Nested Field Path Stripper

```python
import copy
from typing import Any, Dict, List, Set


class NestedFieldPathStripper:
    """
    Supports dot-notation allowlists for nested dicts.
    E.g., allowed_paths={"user.id", "user.locale"} strips all other
    subfields from the "user" dict while keeping id and locale.
    """

    def strip(
        self,
        args: Dict[str, Any],
        allowed_paths: Set[str],
    ) -> Dict[str, Any]:
        # Group paths by top-level key
        top_level: Dict[str, Any] = {}
        nested_paths: Dict[str, Set[str]] = {}

        for path in allowed_paths:
            parts = path.split(".", 1)
            if len(parts) == 1:
                top_level[parts[0]] = True
            else:
                parent, child = parts
                if parent not in nested_paths:
                    nested_paths[parent] = set()
                nested_paths[parent].add(child)

        result = {}
        for key in args:
            if key in top_level:
                result[key] = copy.deepcopy(args[key])
            elif key in nested_paths:
                val = args[key]
                if isinstance(val, dict):
                    result[key] = self.strip(val, nested_paths[key])
                else:
                    result[key] = copy.deepcopy(val)

        return result
```

## Solution 6: Data Minimization Coverage Auditor

```python
import time
from typing import Any, Dict, List


class DataMinimizationCoverageAuditor:
    """
    Scans recent tool call argument samples to identify tools that are
    receiving fields they likely do not need (high cardinality, PII-like names).
    Surfaces gaps in the schema registry.
    """

    PII_FIELD_HINTS = {
        "email", "phone", "address", "ssn", "dob", "birth",
        "credit_card", "card_number", "password", "token", "secret",
        "full_name", "first_name", "last_name",
    }

    def __init__(self, registry: ToolArgumentSchemaRegistry):
        self._registry = registry

    def audit_samples(
        self,
        samples: List[Dict[str, Any]],   # list of {"tool": str, "args": dict}
    ) -> List[dict]:
        findings = []
        for sample in samples:
            tool_name = sample.get("tool", "unknown")
            args = sample.get("args", {})
            schema = self._registry.get(tool_name)

            for field_name, value in args.items():
                is_pii_hint = any(
                    hint in field_name.lower() for hint in self.PII_FIELD_HINTS
                )
                not_in_schema = field_name not in schema.allowed_fields
                if is_pii_hint and not_in_schema and not schema.allow_extra_fields:
                    findings.append({
                        "tool_name": tool_name,
                        "field_name": field_name,
                        "value_type": type(value).__name__,
                        "recommendation": (
                            f"Field '{field_name}' looks like PII but is not in "
                            f"'{tool_name}' allowed_fields schema. "
                            "Add explicit allow or remove from call."
                        ),
                    })
        return findings

    def summary(self, samples: List[Dict[str, Any]]) -> dict:
        findings = self.audit_samples(samples)
        tools_with_gaps = len({f["tool_name"] for f in findings})
        return {
            "samples_scanned": len(samples),
            "pii_exposure_risks": len(findings),
            "tools_with_gaps": tools_with_gaps,
            "findings": findings,
        }
```

## Comparison

| Approach | Field Stripping | Nested Paths | Schema Registry | Audit Log | PII Gap Detection |
|---|---|---|---|---|---|
| ArgumentFieldStripper | Yes (flat) | No | No | Yes (per-call) | No |
| NestedFieldPathStripper | Yes (nested) | Yes (dot-notation) | No | No | No |
| ToolArgumentSchemaRegistry | No | No | Yes | No | No |
| MinimizingToolCallDispatcher | Via stripper | No | Via registry | Yes | No |
| DataMinimizationCoverageAuditor | No | No | Via registry | No | Yes |

**Best for production**: Register a `ToolArgumentSchema` for every tool before deployment and set `default_allow_extra=False` in the registry — unknown tools receive zero fields by default, forcing explicit allowlisting. Use `NestedFieldPathStripper` for tools that receive structured objects (user dicts, request payloads) rather than flat keyword args. Run `DataMinimizationCoverageAuditor.audit_samples()` against a sample of staging call logs before each release to catch PII fields that are being forwarded without justification. Log every minimization event with `strip_count > 0` to `audit_log_fn` — a tool consistently receiving and then stripping 10+ fields means the call-site is passing the wrong context object.
