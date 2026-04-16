---
title: "Agent Doesn't Implement Tool Schema Version Compatibility Checking"
description: "Agents that load tool schemas without version checking silently break when a tool's API evolves: a field renamed from 'query' to 'search_query', a required parameter added, or an enum value removed causes the LLM to generate tool calls that fail at runtime with cryptic errors. Implement tool schema version compatibility checking that validates loaded schemas against the agent's expected version range, detects breaking changes, and surfaces migration guidance before the agent begins executing tasks."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-tool-schema-version-compatibility-checking
tags: [tool-schema, version-compatibility, breaking-changes, schema-validation, tool-registry, api-versioning]
symptoms:
  - "Tool calls fail at runtime with 'unexpected parameter' or 'missing required field' errors after tool update"
  - "Agent generates calls using old field names that the tool no longer accepts"
  - "No version information stored with tool schemas — no way to detect schema drift"
  - "Tool schema changes are discovered only when tasks start failing"
  - "Required parameters added to a tool break all existing agent prompts silently"
---

## Why This Happens

Tool schemas are contracts between the LLM and the tool implementation. When the tool implementation changes — a field is renamed, a parameter type changes, a required field is added — the schema changes too. Agents that load schemas without version metadata have no way to detect that the schema they were designed for has changed. The LLM continues generating tool calls based on stale schema knowledge, and failures appear as runtime errors rather than compatibility warnings. Versioning and compatibility checking shifts this failure left: incompatible schemas are caught at startup, not mid-task.

## Solution 1: Versioned Tool Schema

```python
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ParameterDescriptor:
    name: str
    type: str
    required: bool = False
    description: str = ""
    enum_values: Optional[List[Any]] = None
    deprecated: bool = False


@dataclass
class VersionedToolSchema:
    tool_name: str
    schema_version: str          # semver string: "1.2.3"
    parameters: List[ParameterDescriptor]
    description: str = ""
    deprecated: bool = False
    min_agent_version: str = "0.0.0"
    changelog: Dict[str, str] = field(default_factory=dict)
    # changelog: {"1.1.0": "renamed 'query' to 'search_query'"}

    def parameter_names(self) -> set:
        return {p.name for p in self.parameters}

    def required_parameter_names(self) -> set:
        return {p.name for p in self.parameters if p.required}
```

## Solution 2: Semantic Version Comparator

```python
from typing import Tuple


class SemanticVersionComparator:
    """
    Parses and compares semver strings (MAJOR.MINOR.PATCH).
    Used to determine whether a schema version is within the agent's
    compatible version range.
    """

    @staticmethod
    def parse(version: str) -> Tuple[int, int, int]:
        parts = version.strip().lstrip("v").split(".")
        try:
            major = int(parts[0]) if len(parts) > 0 else 0
            minor = int(parts[1]) if len(parts) > 1 else 0
            patch = int(parts[2]) if len(parts) > 2 else 0
            return major, minor, patch
        except (ValueError, IndexError):
            return 0, 0, 0

    @classmethod
    def compare(cls, v1: str, v2: str) -> int:
        """Returns -1 if v1 < v2, 0 if equal, 1 if v1 > v2."""
        t1 = cls.parse(v1)
        t2 = cls.parse(v2)
        if t1 < t2:
            return -1
        if t1 > t2:
            return 1
        return 0

    @classmethod
    def is_compatible(cls, schema_version: str, min_version: str, max_version: str) -> bool:
        return (
            cls.compare(schema_version, min_version) >= 0
            and cls.compare(schema_version, max_version) <= 0
        )
```

## Solution 3: Schema Breaking Change Detector

```python
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class SchemaBreakingChange:
    change_type: str       # "removed_required_param" | "renamed_param" | "type_changed" | "enum_narrowed"
    parameter_name: str
    detail: str
    severity: str = "error"   # "error" | "warning"


class SchemaBreakingChangeDetector:
    """
    Compares a previously-known schema against a newly-loaded schema
    to detect breaking changes that will cause runtime tool call failures.
    """

    def detect(
        self,
        known: VersionedToolSchema,
        loaded: VersionedToolSchema,
    ) -> List[SchemaBreakingChange]:
        changes: List[SchemaBreakingChange] = []

        known_params = {p.name: p for p in known.parameters}
        loaded_params = {p.name: p for p in loaded.parameters}

        # Removed parameters that were required in known schema
        for name, param in known_params.items():
            if name not in loaded_params:
                severity = "error" if param.required else "warning"
                changes.append(SchemaBreakingChange(
                    change_type="removed_param",
                    parameter_name=name,
                    detail=f"Parameter '{name}' removed from schema",
                    severity=severity,
                ))

        # New required parameters in loaded schema (agent won't know to supply them)
        for name, param in loaded_params.items():
            if name not in known_params and param.required:
                changes.append(SchemaBreakingChange(
                    change_type="added_required_param",
                    parameter_name=name,
                    detail=f"New required parameter '{name}' added — agent may not supply it",
                    severity="error",
                ))

        # Type changes on existing parameters
        for name in set(known_params) & set(loaded_params):
            kp = known_params[name]
            lp = loaded_params[name]
            if kp.type != lp.type:
                changes.append(SchemaBreakingChange(
                    change_type="type_changed",
                    parameter_name=name,
                    detail=f"Type changed: '{kp.type}' -> '{lp.type}'",
                    severity="warning",
                ))
            if kp.enum_values and lp.enum_values:
                removed_enums = set(kp.enum_values) - set(lp.enum_values)
                if removed_enums:
                    changes.append(SchemaBreakingChange(
                        change_type="enum_narrowed",
                        parameter_name=name,
                        detail=f"Enum values removed: {removed_enums}",
                        severity="error",
                    ))

        return changes
```

## Solution 4: Schema Compatibility Checker

```python
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class CompatibilityCheckResult:
    tool_name: str
    schema_version: str
    compatible: bool
    breaking_changes: List[SchemaBreakingChange]
    warnings: List[str]
    recommendation: str = ""


class ToolSchemaCompatibilityChecker:
    """
    Validates loaded tool schemas against a registry of known-good schemas
    and the agent's expected version range. Reports breaking changes
    and version mismatches before the agent begins executing tasks.
    """

    def __init__(
        self,
        comparator: SemanticVersionComparator,
        detector: SchemaBreakingChangeDetector,
        agent_min_schema_version: str = "0.0.0",
        agent_max_schema_version: str = "999.999.999",
    ):
        self._comparator = comparator
        self._detector = detector
        self._min = agent_min_schema_version
        self._max = agent_max_schema_version
        self._known_schemas: Dict[str, VersionedToolSchema] = {}

    def register_known_schema(self, schema: VersionedToolSchema) -> None:
        self._known_schemas[schema.tool_name] = schema

    def check(self, loaded: VersionedToolSchema) -> CompatibilityCheckResult:
        warnings = []
        breaking = []

        version_ok = SemanticVersionComparator.is_compatible(
            loaded.schema_version, self._min, self._max
        )
        if not version_ok:
            warnings.append(
                f"Schema version {loaded.schema_version} outside agent range "
                f"[{self._min}, {self._max}]"
            )

        if loaded.deprecated:
            warnings.append(f"Tool '{loaded.tool_name}' is marked deprecated")

        known = self._known_schemas.get(loaded.tool_name)
        if known:
            breaking = self._detector.detect(known, loaded)

        has_errors = any(c.severity == "error" for c in breaking)
        compatible = version_ok and not has_errors

        recommendation = ""
        if breaking:
            recommendation = (
                "Update agent tool call generation prompts to match new schema. "
                + " ".join(c.detail for c in breaking if c.severity == "error")
            )
        elif not version_ok:
            recommendation = f"Pin tool to a version within agent range [{self._min}, {self._max}]"

        return CompatibilityCheckResult(
            tool_name=loaded.tool_name,
            schema_version=loaded.schema_version,
            compatible=compatible,
            breaking_changes=breaking,
            warnings=warnings,
            recommendation=recommendation,
        )
```

## Solution 5: Startup Schema Validator

```python
from typing import Dict, List


class StartupSchemaValidator:
    """
    Runs schema compatibility checks for all registered tools at agent startup.
    Fails fast if any tool has an incompatible schema — preventing mid-task failures.
    """

    def __init__(self, checker: ToolSchemaCompatibilityChecker):
        self._checker = checker

    def validate_all(
        self,
        loaded_schemas: List[VersionedToolSchema],
        fail_on_incompatible: bool = True,
    ) -> Dict[str, CompatibilityCheckResult]:
        results: Dict[str, CompatibilityCheckResult] = {}
        incompatible = []

        for schema in loaded_schemas:
            result = self._checker.check(schema)
            results[schema.tool_name] = result
            if not result.compatible:
                incompatible.append(schema.tool_name)

        if fail_on_incompatible and incompatible:
            raise IncompatibleToolSchemaError(incompatible, results)

        return results


class IncompatibleToolSchemaError(Exception):
    def __init__(self, tool_names: List[str], results: Dict[str, CompatibilityCheckResult]):
        super().__init__(
            f"Incompatible tool schemas detected at startup: {tool_names}"
        )
        self.tool_names = tool_names
        self.results = results
```

## Solution 6: Schema Compatibility Dashboard

```python
import time
from typing import Dict, List


class SchemaCompatibilityDashboard:
    """
    Surfaces schema version status, breaking change summaries, and
    incompatibility counts for all registered tools.
    """

    def __init__(self, checker: ToolSchemaCompatibilityChecker):
        self._checker = checker

    def render(self, loaded_schemas: List[VersionedToolSchema]) -> dict:
        results = {}
        for schema in loaded_schemas:
            r = self._checker.check(schema)
            results[schema.tool_name] = {
                "version": r.schema_version,
                "compatible": r.compatible,
                "breaking_changes": len(r.breaking_changes),
                "warnings": r.warnings,
                "recommendation": r.recommendation,
            }

        incompatible_count = sum(1 for r in results.values() if not r["compatible"])
        return {
            "generated_at": time.time(),
            "tool_count": len(loaded_schemas),
            "incompatible_count": incompatible_count,
            "all_compatible": incompatible_count == 0,
            "tools": results,
        }
```

## Comparison

| Approach | Version Range Check | Breaking Change Detection | Enum Narrowing | Startup Validation | Dashboard |
|---|---|---|---|---|---|
| SemanticVersionComparator | Yes | No | No | No | No |
| SchemaBreakingChangeDetector | No | Yes (structural) | Yes | No | No |
| ToolSchemaCompatibilityChecker | Yes | Via detector | Via detector | No | No |
| StartupSchemaValidator | Via checker | Via checker | Via checker | Yes | No |
| SchemaCompatibilityDashboard | Via checker | Via checker | Via checker | No | Yes |

**Best for production**: Store a snapshot of each tool's schema at the last known-good version in the agent's configuration — this is the baseline for `SchemaBreakingChangeDetector`. Run `StartupSchemaValidator` with `fail_on_incompatible=True` in staging and `fail_on_incompatible=False` in production (emit a critical alert instead): staging catches schema drift before deployment, while production degraded-mode allows manual intervention. Pay special attention to `added_required_param` changes — the LLM has no way to learn about new required fields unless the system prompt or tool description is updated simultaneously. Version tool schemas using the same semver convention as the tool service itself and include the version in every tool definition returned from the registry.
