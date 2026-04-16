---
title: "Agent Doesn't Implement Response Schema Evolution for Tool Output Versioning"
description: "AI agents that parse tool outputs with hard-coded field names break silently when a tool's response schema adds, renames, or removes fields across versions. Schema evolution strategies — additive-only contracts, version negotiation, field aliasing, and migration transformers — let the agent parse both old and new response shapes without crashing or misinterpreting data."
date: 2025-02-17
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-response-schema-evolution-for-tool-output-versioning
tags:
  - schema-evolution
  - versioning
  - tool-output
  - backward-compatibility
  - migration
  - reliability
  - contract
symptoms:
  - "Agent crashes with KeyError when a tool adds a new required field in its response"
  - "Removing a field from the tool response breaks all downstream consumers simultaneously"
  - "No version field in tool responses — impossible to detect which schema variant was returned"
  - "Agent silently reads None for a renamed field and produces incorrect output"
  - "Tool schema changes require coordinated deployment of both tool and agent"
---

## Problem

Tool response schemas evolve over time: fields are added, renamed, split, or removed. An agent with `result["user_id"]` fails the moment the tool renames the field to `result["userId"]`. Schema evolution strategies make the agent resilient to these changes by treating older schema versions as first-class inputs rather than error conditions, applying migration transforms to normalize any version to the current canonical shape before the rest of the agent pipeline processes the response.

---

## Solution 1: VersionedToolResponse — Tag Every Response with a Schema Version

```python
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class VersionedToolResponse:
    """
    Wraps a raw tool response dict with a schema version tag.
    The version is read from the response payload ('_schema_version' key)
    or injected by the tool adapter if the tool doesn't emit one.

    Usage:
        raw = await tool_call("user_lookup", user_id=uid)
        versioned = VersionedToolResponse.from_raw(raw, default_version="1.0")
        normalized = registry.migrate(versioned)
    """

    data: Dict[str, Any]
    schema_version: str
    tool_name: str
    raw: Dict[str, Any] = field(repr=False, default_factory=dict)

    VERSION_KEY = "_schema_version"

    @classmethod
    def from_raw(cls, raw: Dict[str, Any],
                  tool_name: str = "",
                  default_version: str = "1.0") -> "VersionedToolResponse":
        version = str(raw.get(cls.VERSION_KEY, default_version))
        data = {k: v for k, v in raw.items() if k != cls.VERSION_KEY}
        return cls(data=data, schema_version=version,
                   tool_name=tool_name, raw=raw)

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def __contains__(self, key: str) -> bool:
        return key in self.data
```

---

## Solution 2: SchemaMigrationRegistry — Apply Version-to-Version Transforms

```python
import logging
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


MigrationFn = Callable[[Dict[str, Any]], Dict[str, Any]]


class SchemaMigrationRegistry:
    """
    Stores migration functions keyed by (tool_name, from_version, to_version).
    When an old-versioned response arrives, applies the migration chain
    to produce the current canonical schema.

    Usage:
        registry = SchemaMigrationRegistry(current_version="3.0")

        @registry.migration("user_lookup", "1.0", "2.0")
        def v1_to_v2(data):
            # Rename user_id -> userId
            data["userId"] = data.pop("user_id", data.get("userId"))
            return data

        @registry.migration("user_lookup", "2.0", "3.0")
        def v2_to_v3(data):
            # Split name -> first_name, last_name
            name = data.pop("name", "")
            parts = name.split(" ", 1)
            data["first_name"] = parts[0]
            data["last_name"] = parts[1] if len(parts) > 1 else ""
            return data

        normalized = registry.migrate(versioned_response)
    """

    def __init__(self, current_version: str = "1.0"):
        self._current = current_version
        # (tool_name, from_version, to_version) -> fn
        self._migrations: Dict[Tuple[str, str, str], MigrationFn] = {}
        # Ordered version sequence per tool
        self._version_chains: Dict[str, list] = {}

    def migration(self, tool_name: str,
                   from_version: str, to_version: str):
        """Decorator to register a migration function."""
        def decorator(fn: MigrationFn) -> MigrationFn:
            self._migrations[(tool_name, from_version, to_version)] = fn
            chain = self._version_chains.setdefault(tool_name, [])
            if from_version not in chain:
                chain.append(from_version)
            if to_version not in chain:
                chain.append(to_version)
            return fn
        return decorator

    def migrate(self, response: VersionedToolResponse) -> VersionedToolResponse:
        version = response.schema_version
        tool = response.tool_name
        if version == self._current:
            return response

        data = dict(response.data)
        chain = self._version_chains.get(tool, [])
        try:
            start = chain.index(version)
            end = chain.index(self._current)
        except ValueError:
            logger.warning(
                "schema_migration_unknown tool=%s version=%s current=%s",
                tool, version, self._current,
            )
            return response

        for i in range(start, end):
            from_v = chain[i]
            to_v = chain[i + 1]
            fn = self._migrations.get((tool, from_v, to_v))
            if fn is None:
                logger.error(
                    "schema_migration_missing tool=%s %s->%s",
                    tool, from_v, to_v,
                )
                break
            try:
                data = fn(data)
                logger.debug(
                    "schema_migrated tool=%s %s->%s", tool, from_v, to_v
                )
            except Exception as exc:
                logger.error(
                    "schema_migration_error tool=%s %s->%s error=%s",
                    tool, from_v, to_v, exc,
                )
                break

        return VersionedToolResponse(
            data=data,
            schema_version=self._current,
            tool_name=tool,
            raw=response.raw,
        )
```

---

## Solution 3: FieldAliasResolver — Tolerate Field Renames Transparently

```python
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class FieldAliasResolver:
    """
    Resolves field access through a list of historical aliases so that
    code reading `response["userId"]` still works when the tool returns
    `user_id` (or vice versa). Aliases are tried in order; first match wins.

    Usage:
        resolver = FieldAliasResolver()
        resolver.register("userId", aliases=["user_id", "uid", "id"])
        resolver.register("displayName", aliases=["name", "full_name", "display_name"])

        name = resolver.get(response_dict, "displayName")
    """

    def __init__(self):
        self._aliases: Dict[str, List[str]] = {}

    def register(self, canonical: str, aliases: List[str]):
        self._aliases[canonical] = aliases

    def get(self, data: Dict[str, Any], canonical: str,
             default: Any = None) -> Any:
        # Try canonical first
        if canonical in data:
            return data[canonical]
        # Try aliases
        for alias in self._aliases.get(canonical, []):
            if alias in data:
                logger.debug(
                    "field_alias_resolved canonical=%s via=%s",
                    canonical, alias,
                )
                return data[alias]
        return default

    def normalize(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Returns a copy of data where aliased fields are renamed to canonical.
        Safe to call on any response regardless of schema version.
        """
        result = dict(data)
        for canonical, aliases in self._aliases.items():
            if canonical in result:
                continue
            for alias in aliases:
                if alias in result:
                    result[canonical] = result.pop(alias)
                    break
        return result

    def validate_fields(self, data: Dict[str, Any],
                         required: List[str]) -> List[str]:
        """Returns list of canonical fields that cannot be resolved."""
        missing = []
        for field in required:
            if self.get(data, field) is None and field not in data:
                missing.append(field)
        return missing
```

---

## Solution 4: AdditiveSchemaValidator — Enforce Additive-Only Changes

```python
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


@dataclass
class SchemaCompatibilityReport:
    compatible: bool
    breaking_removals: List[str]
    breaking_type_changes: List[str]
    safe_additions: List[str]
    warnings: List[str]


class AdditiveSchemaValidator:
    """
    Validates that a new tool response schema is backward-compatible
    with the previous one: fields may be added but not removed or
    type-changed. Used in CI/CD pipelines to gate tool deployments.

    Usage:
        validator = AdditiveSchemaValidator()
        old_schema = {"user_id": "str", "name": "str", "email": "str"}
        new_schema = {"user_id": "str", "name": "str", "email": "str",
                       "created_at": "float"}
        report = validator.compare(old_schema, new_schema)
        if not report.compatible:
            raise ValueError(f"Breaking changes: {report.breaking_removals}")
    """

    def compare(self, old: Dict[str, str],
                 new: Dict[str, str]) -> SchemaCompatibilityReport:
        old_keys: Set[str] = set(old)
        new_keys: Set[str] = set(new)

        removals = [k for k in old_keys - new_keys]
        additions = [k for k in new_keys - old_keys]
        type_changes = [
            k for k in old_keys & new_keys if old[k] != new[k]
        ]
        warnings = []

        # Optional fields added are safe; required fields added are breaking
        if additions:
            warnings.append(
                f"New fields added: {additions}. "
                "Ensure consumers handle missing fields gracefully."
            )

        compatible = not removals and not type_changes
        if removals:
            logger.warning(
                "schema_breaking_removal fields=%s", removals
            )
        if type_changes:
            logger.warning(
                "schema_type_change fields=%s", type_changes
            )

        return SchemaCompatibilityReport(
            compatible=compatible,
            breaking_removals=removals,
            breaking_type_changes=type_changes,
            safe_additions=additions,
            warnings=warnings,
        )

    def is_compatible(self, old: Dict[str, str],
                       new: Dict[str, str]) -> bool:
        return self.compare(old, new).compatible
```

---

## Solution 5: SchemaVersionNegotiator — Request Preferred Schema Version

```python
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class SchemaVersionNegotiator:
    """
    Adds schema version negotiation to tool calls: the agent declares
    which schema version it wants via a request header/parameter, and
    the tool returns the requested version if supported, falling back
    to its latest if not. Eliminates the need for migration transforms
    when both sides can agree on a version at call time.

    Usage:
        negotiator = SchemaVersionNegotiator(
            preferred_version="2.0",
            supported_versions={"1.0", "2.0"},
        )
        params = negotiator.enrich_request({"user_id": uid})
        raw = await tool_call("user_lookup", **params)
        version = negotiator.detect_version(raw)
    """

    VERSION_REQUEST_KEY = "_request_schema_version"
    VERSION_RESPONSE_KEY = "_schema_version"

    def __init__(self, preferred_version: str = "1.0",
                  supported_versions: Optional[set] = None):
        self._preferred = preferred_version
        self._supported = supported_versions or {preferred_version}

    def enrich_request(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Add version preference to outgoing tool call parameters."""
        return {**params, self.VERSION_REQUEST_KEY: self._preferred}

    def detect_version(self, raw: Dict[str, Any]) -> str:
        """Read schema version from tool response."""
        v = str(raw.get(self.VERSION_RESPONSE_KEY, "1.0"))
        if v not in self._supported:
            logger.warning(
                "schema_version_unsupported version=%s supported=%s",
                v, self._supported,
            )
        return v

    def version_matches(self, raw: Dict[str, Any]) -> bool:
        return self.detect_version(raw) == self._preferred
```

---

## Solution 6: EvolvingToolAdapter — Full Schema Evolution Stack

```python
import logging
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


class EvolvingToolAdapter:
    """
    Wraps a tool call function with full schema evolution support:
    version negotiation on request, alias resolution on response,
    migration to current schema, and validation of required fields.

    Usage:
        adapter = EvolvingToolAdapter(
            tool_fn=raw_user_lookup,
            tool_name="user_lookup",
            migration_registry=registry,
            alias_resolver=resolver,
            required_fields=["userId", "email"],
        )
        result = await adapter.call(user_id="u-123")
        # result.data always has canonical field names in current schema
    """

    def __init__(self, tool_fn: Callable,
                  tool_name: str,
                  migration_registry: SchemaMigrationRegistry,
                  alias_resolver: Optional[FieldAliasResolver] = None,
                  required_fields: Optional[list] = None,
                  default_version: str = "1.0"):
        self._fn = tool_fn
        self._name = tool_name
        self._migrations = migration_registry
        self._aliases = alias_resolver or FieldAliasResolver()
        self._required = required_fields or []
        self._default_version = default_version

    async def call(self, **kwargs) -> VersionedToolResponse:
        raw = await self._fn(**kwargs)
        if not isinstance(raw, dict):
            raw = {"result": raw}

        # Alias normalization first (handles renamed fields)
        normalized = self._aliases.normalize(raw)

        # Wrap as versioned
        versioned = VersionedToolResponse.from_raw(
            normalized,
            tool_name=self._name,
            default_version=self._default_version,
        )

        # Migrate to current schema
        current = self._migrations.migrate(versioned)

        # Validate required fields
        missing = self._aliases.validate_fields(current.data, self._required)
        if missing:
            logger.error(
                "schema_required_fields_missing tool=%s fields=%s version=%s",
                self._name, missing, current.schema_version,
            )

        return current

    def health(self) -> Dict[str, Any]:
        return {
            "tool": self._name,
            "current_version": self._migrations._current,
            "required_fields": self._required,
        }
```

---

## Comparison

| Approach | Version Tagging | Migration Chain | Alias Resolution | Additive Validation | Version Negotiation | Integrated |
|---|---|---|---|---|---|---|
| **VersionedToolResponse** | Yes | No | No | No | No | No |
| **SchemaMigrationRegistry** | No | Yes | No | No | No | No |
| **FieldAliasResolver** | No | No | Yes | No | No | No |
| **AdditiveSchemaValidator** | No | No | No | Yes | No | No |
| **SchemaVersionNegotiator** | No | No | No | No | Yes | No |
| **EvolvingToolAdapter** | Yes | Yes | Yes | No | No | Yes |

**Key insight**: the safest schema evolution rule is additive-only — new fields may be added, existing fields must never be removed or renamed within the same major version. When a breaking change is unavoidable, introduce a new major version and run both versions in parallel until all consumers migrate. The `FieldAliasResolver` is the cheapest defense: it costs one dict lookup per field and eliminates the most common failure mode (field rename) without requiring any migration infrastructure.
