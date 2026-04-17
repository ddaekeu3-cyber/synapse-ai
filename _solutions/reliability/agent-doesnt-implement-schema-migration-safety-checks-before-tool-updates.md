---
title: "Agent Doesn't Implement Schema Migration Safety Checks Before Tool Updates"
description: "Agents that update tool schemas without validating backward compatibility break in-flight sessions: a deployed tool schema change removes a field the LLM was trained to expect, causing all active sessions to produce malformed tool calls that the dispatcher rejects. Implement schema migration safety checks that detect breaking changes, validate the new schema against a compatibility contract, run shadow validation against recent tool call samples, and gate deployment on passing checks."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-schema-migration-safety-checks-before-tool-updates
tags: [schema-migration, tool-schema, backward-compatibility, breaking-changes, deployment-safety, tool-versioning]
symptoms:
  - "A tool schema update removes a required field and active sessions immediately start failing"
  - "No check verifies that existing LLM tool calls remain valid under the new schema"
  - "Tool schema changes are deployed the same way as any other config — no special validation gate"
  - "Cannot tell whether a schema change is additive or breaking without manual review"
  - "No record of what the previous schema was — rollback requires a git diff and manual revert"
---

## Why This Happens

Tool schemas are passed to the LLM as part of the system context. When a schema changes, the LLM's behavior changes — it may start generating tool calls with the new field names, remove optional fields it no longer sees, or fail to populate fields that became required. In-flight sessions that received the old schema continue to generate old-style tool calls, which are now rejected by the dispatcher enforcing the new schema. A migration safety check must answer: does the new schema accept all tool calls that the old schema would have accepted? If not, it is a breaking change and requires a versioned rollout strategy.

## Solution 1: Tool Schema Version

```python
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ToolSchemaVersion:
    tool_name: str
    version_id: str
    schema: Dict[str, Any]      # JSON Schema object describing tool parameters
    description: str
    created_at: float = field(default_factory=time.time)
    author: str = ""
    changelog: str = ""

    @classmethod
    def from_schema(
        cls,
        tool_name: str,
        schema: Dict[str, Any],
        description: str = "",
        author: str = "",
        changelog: str = "",
    ) -> "ToolSchemaVersion":
        schema_bytes = json.dumps(schema, sort_keys=True).encode()
        version_id = hashlib.sha256(schema_bytes).hexdigest()[:12]
        return cls(
            tool_name=tool_name,
            version_id=version_id,
            schema=schema,
            description=description,
            author=author,
            changelog=changelog,
        )

    def fingerprint(self) -> str:
        return self.version_id
```

## Solution 2: Schema Compatibility Analyzer

```python
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Set


class CompatibilityLevel(str, Enum):
    IDENTICAL = "identical"
    BACKWARD_COMPATIBLE = "backward_compatible"   # new schema accepts old calls
    BREAKING = "breaking"                          # new schema rejects valid old calls
    UNKNOWN = "unknown"


@dataclass
class CompatibilityReport:
    tool_name: str
    old_version_id: str
    new_version_id: str
    level: CompatibilityLevel
    breaking_changes: List[str]
    additive_changes: List[str]
    safe_to_deploy: bool


class SchemaCompatibilityAnalyzer:
    """
    Compares two JSON Schema objects and classifies the change.
    Detects field removals, type narrowing, and new required fields
    as breaking changes.
    """

    def analyze(
        self,
        old_version: ToolSchemaVersion,
        new_version: ToolSchemaVersion,
    ) -> CompatibilityReport:
        old_props = old_version.schema.get("properties", {})
        new_props = new_version.schema.get("properties", {})
        old_required: Set[str] = set(old_version.schema.get("required", []))
        new_required: Set[str] = set(new_version.schema.get("required", []))

        breaking = []
        additive = []

        # Removed fields are breaking
        removed_fields = set(old_props) - set(new_props)
        for field in removed_fields:
            breaking.append(f"field '{field}' removed")

        # New required fields are breaking (old calls won't include them)
        new_mandatory = new_required - old_required
        for field in new_mandatory:
            if field not in old_required:
                breaking.append(f"field '{field}' became required")

        # Added optional fields are additive
        added_optional = set(new_props) - set(old_props)
        for field in added_optional:
            if field not in new_required:
                additive.append(f"optional field '{field}' added")
            else:
                breaking.append(f"new required field '{field}' added")

        # Type changes are breaking
        for field in set(old_props) & set(new_props):
            old_type = old_props[field].get("type")
            new_type = new_props[field].get("type")
            if old_type != new_type and old_type is not None and new_type is not None:
                breaking.append(f"field '{field}' type changed from '{old_type}' to '{new_type}'")

        if not breaking and not additive:
            level = CompatibilityLevel.IDENTICAL
        elif not breaking:
            level = CompatibilityLevel.BACKWARD_COMPATIBLE
        else:
            level = CompatibilityLevel.BREAKING

        return CompatibilityReport(
            tool_name=old_version.tool_name,
            old_version_id=old_version.version_id,
            new_version_id=new_version.version_id,
            level=level,
            breaking_changes=breaking,
            additive_changes=additive,
            safe_to_deploy=len(breaking) == 0,
        )
```

## Solution 3: Tool Call Sample Validator

```python
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class SampleValidationResult:
    sample_count: int
    valid_count: int
    invalid_count: int
    failure_rate: float
    sample_failures: List[dict]
    safe_to_deploy: bool


class ToolCallSampleValidator:
    """
    Validates a set of recent tool call samples against the new schema.
    Samples that were valid under the old schema but fail the new schema
    indicate a breaking change in practice, not just in theory.
    """

    def _validate_against_schema(
        self,
        call_args: Dict[str, Any],
        schema: Dict[str, Any],
    ) -> List[str]:
        errors = []
        required = schema.get("required", [])
        properties = schema.get("properties", {})

        for field in required:
            if field not in call_args:
                errors.append(f"missing required field '{field}'")

        for field, value in call_args.items():
            if field not in properties:
                # additionalProperties check
                if not schema.get("additionalProperties", True):
                    errors.append(f"unexpected field '{field}'")
                continue
            expected_type = properties[field].get("type")
            if expected_type and not self._type_matches(value, expected_type):
                errors.append(f"field '{field}' expected type '{expected_type}'")

        return errors

    @staticmethod
    def _type_matches(value: Any, expected: str) -> bool:
        type_map = {
            "string": str, "number": (int, float),
            "integer": int, "boolean": bool,
            "array": list, "object": dict, "null": type(None),
        }
        expected_type = type_map.get(expected)
        return isinstance(value, expected_type) if expected_type else True

    def validate_samples(
        self,
        samples: List[Dict[str, Any]],   # list of args dicts from recent calls
        new_version: ToolSchemaVersion,
        max_failures_to_report: int = 5,
    ) -> SampleValidationResult:
        failures = []
        for sample in samples:
            errors = self._validate_against_schema(sample, new_version.schema)
            if errors:
                failures.append({"args": sample, "errors": errors})

        failure_rate = len(failures) / max(len(samples), 1)

        return SampleValidationResult(
            sample_count=len(samples),
            valid_count=len(samples) - len(failures),
            invalid_count=len(failures),
            failure_rate=round(failure_rate, 4),
            sample_failures=failures[:max_failures_to_report],
            safe_to_deploy=failure_rate < 0.01,   # allow up to 1% failure
        )
```

## Solution 4: Schema Version Store

```python
import json
from pathlib import Path
from threading import Lock
from typing import Dict, List, Optional


class SchemaVersionStore:
    """
    Persists tool schema versions to a local JSON file.
    Supports rollback by restoring the previous version.
    """

    def __init__(self, path: str = "/tmp/tool_schema_versions.json"):
        self._path = Path(path)
        self._lock = Lock()

    def save(self, version: ToolSchemaVersion) -> None:
        with self._lock:
            all_versions = self._load_all()
            tool_history = all_versions.get(version.tool_name, [])
            tool_history.append({
                "version_id": version.version_id,
                "schema": version.schema,
                "description": version.description,
                "created_at": version.created_at,
                "author": version.author,
                "changelog": version.changelog,
            })
            all_versions[version.tool_name] = tool_history[-10:]  # keep last 10
            self._path.write_text(json.dumps(all_versions, indent=2))

    def current(self, tool_name: str) -> Optional[ToolSchemaVersion]:
        with self._lock:
            history = self._load_all().get(tool_name, [])
        if not history:
            return None
        data = history[-1]
        return ToolSchemaVersion(
            tool_name=tool_name,
            version_id=data["version_id"],
            schema=data["schema"],
            description=data["description"],
            created_at=data["created_at"],
            author=data.get("author", ""),
            changelog=data.get("changelog", ""),
        )

    def previous(self, tool_name: str) -> Optional[ToolSchemaVersion]:
        with self._lock:
            history = self._load_all().get(tool_name, [])
        if len(history) < 2:
            return None
        data = history[-2]
        return ToolSchemaVersion(
            tool_name=tool_name,
            version_id=data["version_id"],
            schema=data["schema"],
            description=data["description"],
            created_at=data["created_at"],
        )

    def _load_all(self) -> dict:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text())
        except Exception:
            return {}
```

## Solution 5: Schema Migration Gate

```python
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class MigrationGateResult:
    tool_name: str
    approved: bool
    compatibility: Optional[CompatibilityReport]
    sample_validation: Optional[SampleValidationResult]
    rejection_reasons: List[str]


class SchemaMigrationGate:
    """
    Orchestrates all safety checks before a schema update is applied.
    Returns a gate result indicating whether deployment is approved.
    """

    def __init__(
        self,
        store: SchemaVersionStore,
        analyzer: SchemaCompatibilityAnalyzer,
        sample_validator: ToolCallSampleValidator,
    ):
        self._store = store
        self._analyzer = analyzer
        self._validator = sample_validator

    def check(
        self,
        new_version: ToolSchemaVersion,
        recent_call_samples: List[Dict[str, Any]],
        allow_breaking: bool = False,
    ) -> MigrationGateResult:
        rejections = []
        compat_report = None
        sample_result = None

        current = self._store.current(new_version.tool_name)

        if current:
            compat_report = self._analyzer.analyze(current, new_version)
            if not compat_report.safe_to_deploy and not allow_breaking:
                rejections.append(
                    f"breaking changes detected: {'; '.join(compat_report.breaking_changes)}"
                )

        if recent_call_samples:
            sample_result = self._validator.validate_samples(recent_call_samples, new_version)
            if not sample_result.safe_to_deploy:
                rejections.append(
                    f"sample validation failed: {sample_result.invalid_count}/{sample_result.sample_count} "
                    f"recent calls would be rejected"
                )

        approved = len(rejections) == 0
        if approved:
            self._store.save(new_version)

        return MigrationGateResult(
            tool_name=new_version.tool_name,
            approved=approved,
            compatibility=compat_report,
            sample_validation=sample_result,
            rejection_reasons=rejections,
        )
```

## Solution 6: Schema Migration Audit Log

```python
import time
from typing import List


class SchemaMigrationAuditLog:
    """
    Records all schema migration gate decisions for compliance and rollback planning.
    """

    def __init__(self, max_records: int = 1000):
        self._max = max_records
        self._records: List[dict] = []

    def record(self, gate_result: MigrationGateResult) -> None:
        if len(self._records) >= self._max:
            self._records.pop(0)
        self._records.append({
            "ts": time.time(),
            "tool_name": gate_result.tool_name,
            "approved": gate_result.approved,
            "compatibility_level": (
                gate_result.compatibility.level.value
                if gate_result.compatibility else None
            ),
            "breaking_changes": (
                gate_result.compatibility.breaking_changes
                if gate_result.compatibility else []
            ),
            "sample_failure_rate": (
                gate_result.sample_validation.failure_rate
                if gate_result.sample_validation else None
            ),
            "rejection_reasons": gate_result.rejection_reasons,
        })

    def recent(self, limit: int = 20) -> List[dict]:
        return self._records[-limit:]

    def blocked_deployments(self) -> List[dict]:
        return [r for r in self._records if not r["approved"]]
```

## Comparison

| Approach | Schema Fingerprinting | Breaking Change Detection | Sample Validation | Deployment Gate | Rollback Support |
|---|---|---|---|---|---|
| ToolSchemaVersion | Yes (SHA-256) | No | No | No | No |
| SchemaCompatibilityAnalyzer | No | Yes (field/type analysis) | No | No | No |
| ToolCallSampleValidator | No | No | Yes (against new schema) | No | No |
| SchemaVersionStore | No | No | No | No | Yes (previous()) |
| SchemaMigrationGate | Via analyzer | Via analyzer | Via validator | Yes | Via store |
| SchemaMigrationAuditLog | No | No | No | No | No |

**Best for production**: Run `SchemaMigrationGate.check()` as part of the CI/CD pipeline for any configuration change that touches tool schemas — treat it as a required check the same way type checking or linting is required. Collect `recent_call_samples` from the last 500 production tool calls (stored in your logging backend) and pass them to the gate: theoretical compatibility analysis misses cases where the LLM consistently omits optional fields that became required in the new version. Use `allow_breaking=True` only when you can simultaneously version the tool endpoint and route old sessions to the old schema — never deploy a breaking schema change to a live endpoint shared by old and new sessions. Keep the last 10 schema versions in `SchemaVersionStore` to support multi-step rollbacks during prolonged incidents.
