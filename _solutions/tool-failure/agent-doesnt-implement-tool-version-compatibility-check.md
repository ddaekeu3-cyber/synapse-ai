---
layout: solution
title: "Agent Doesn't Implement Tool Version Compatibility Check"
category: tool-failure
description: "Agents that blindly call tools without verifying API compatibility break silently when tools are updated. Version compatibility checks validate tool schemas at startup, detect breaking changes, and route to compatible implementations."
tags: [tool-failure, versioning, compatibility, schema, validation, sqlite, startup]
---

# Agent Doesn't Implement Tool Version Compatibility Check

## Problem

When a tool's API changes — a parameter is renamed, a field is removed, or a new required argument is added — agents that don't check compatibility continue calling the old schema. The tool either fails with an opaque error or, worse, silently executes with incorrect parameters.

Version compatibility checks validate expected vs. actual tool schemas before any calls are made, preventing runtime failures from schema drift.

---

## Option 1: Simple Schema Hash Check at Startup

```python
import hashlib
import json
import anthropic
from dataclasses import dataclass

@dataclass
class ToolVersion:
    name: str
    version: str
    schema_hash: str

# Pinned tool schema hashes from when the agent was last tested
PINNED_TOOL_VERSIONS: dict[str, ToolVersion] = {}  # Populated after first run

def hash_schema(schema: dict) -> str:
    """Deterministic hash of a tool's input schema."""
    normalized = json.dumps(schema, sort_keys=True)
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def get_current_tools() -> list[dict]:
    """Returns the tool definitions your agent uses."""
    return [
        {
            "name": "search_database",
            "description": "Search records in the database",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "default": 10},
                },
                "required": ["query"],
            },
        },
        {
            "name": "send_notification",
            "description": "Send a notification to a user",
            "input_schema": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "message": {"type": "string"},
                    "channel": {"type": "string", "enum": ["email", "sms", "push"]},
                },
                "required": ["user_id", "message", "channel"],
            },
        },
    ]


def check_compatibility(
    current_tools: list[dict],
    pinned: dict[str, ToolVersion],
) -> dict[str, str]:
    """Returns {tool_name: "ok"|"changed"|"new"} for each tool."""
    results = {}
    current_by_name = {t["name"]: t for t in current_tools}

    for tool in current_tools:
        name = tool["name"]
        current_hash = hash_schema(tool["input_schema"])
        if name not in pinned:
            results[name] = "new"
        elif pinned[name].schema_hash != current_hash:
            results[name] = f"changed (was {pinned[name].schema_hash}, now {current_hash})"
        else:
            results[name] = "ok"

    return results


def run_with_compatibility_check():
    tools = get_current_tools()

    # First run: pin current hashes
    pinned = {
        t["name"]: ToolVersion(
            name=t["name"],
            version="1.0",
            schema_hash=hash_schema(t["input_schema"]),
        )
        for t in tools
    }

    # Simulate a tool schema change
    tools[0]["input_schema"]["properties"]["filter"] = {"type": "string"}  # Added new param
    tools[0]["input_schema"]["required"].append("filter")  # Made it required

    results = check_compatibility(tools, pinned)
    for name, status in results.items():
        icon = "✓" if status == "ok" else "⚠️"
        print(f"{icon} {name}: {status}")

    has_changes = any(s != "ok" for s in results.values())
    if has_changes:
        print("\n[STARTUP] Tool schema changes detected — re-run tests before deploying")
        return False

    # Proceed with agent
    client = anthropic.Anthropic()
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        tools=tools,
        messages=[{"role": "user", "content": "Search for users named Alice."}],
    )
    print(f"\nAgent response: {r.stop_reason}")
    return True


if __name__ == "__main__":
    run_with_compatibility_check()
# Expected Token Savings: None direct — prevents failed API calls caused by schema mismatch
# Environment: pip install anthropic; hashlib, json are stdlib
```

---

## Option 2: Semantic Breaking Change Detector

```python
import json
import anthropic
from dataclasses import dataclass

@dataclass
class CompatibilityIssue:
    tool_name: str
    severity: str      # "breaking", "warning", "info"
    description: str

def detect_breaking_changes(old_schema: dict, new_schema: dict, tool_name: str) -> list[CompatibilityIssue]:
    """
    Detect semantic breaking changes between two versions of a tool schema.
    Breaking: added required field, removed field, changed field type.
    Warning: changed optional to required, removed optional field.
    Info: added optional field, added description.
    """
    issues = []

    old_props = old_schema.get("properties", {})
    new_props = new_schema.get("properties", {})
    old_required = set(old_schema.get("required", []))
    new_required = set(new_schema.get("required", []))

    # Removed fields (always breaking — callers may depend on output)
    for field in old_props:
        if field not in new_props:
            issues.append(CompatibilityIssue(
                tool_name=tool_name,
                severity="breaking",
                description=f"Field '{field}' was removed",
            ))

    # Added required fields (breaking — old callers won't send this)
    for field in new_required - old_required:
        if field not in old_props:
            issues.append(CompatibilityIssue(
                tool_name=tool_name,
                severity="breaking",
                description=f"New required field '{field}' added — old callers will fail",
            ))
        else:
            issues.append(CompatibilityIssue(
                tool_name=tool_name,
                severity="warning",
                description=f"Field '{field}' changed from optional to required",
            ))

    # Type changes (breaking)
    for field in old_props:
        if field in new_props:
            old_type = old_props[field].get("type")
            new_type = new_props[field].get("type")
            if old_type != new_type:
                issues.append(CompatibilityIssue(
                    tool_name=tool_name,
                    severity="breaking",
                    description=f"Field '{field}' type changed: {old_type} → {new_type}",
                ))

            # Enum changes
            old_enum = set(old_props[field].get("enum", []))
            new_enum = set(new_props[field].get("enum", []))
            removed_values = old_enum - new_enum
            if removed_values:
                issues.append(CompatibilityIssue(
                    tool_name=tool_name,
                    severity="breaking",
                    description=f"Field '{field}' removed enum values: {removed_values}",
                ))

    # Added optional fields (info only)
    for field in new_props:
        if field not in old_props and field not in new_required:
            issues.append(CompatibilityIssue(
                tool_name=tool_name,
                severity="info",
                description=f"New optional field '{field}' added",
            ))

    return issues


# Example: tool schema evolution
OLD_SEND_NOTIFICATION = {
    "type": "object",
    "properties": {
        "user_id": {"type": "string"},
        "message": {"type": "string"},
        "channel": {"type": "string", "enum": ["email", "sms", "push"]},
    },
    "required": ["user_id", "message"],
}

NEW_SEND_NOTIFICATION = {
    "type": "object",
    "properties": {
        "user_id": {"type": "string"},
        "message": {"type": "string"},
        "channel": {"type": "string", "enum": ["email", "push"]},  # Removed "sms"
        "priority": {"type": "string", "enum": ["normal", "high"]},  # New optional
        "template_id": {"type": "integer"},  # New required below
    },
    "required": ["user_id", "message", "channel", "template_id"],  # template_id now required
}


def run_semantic_check():
    issues = detect_breaking_changes(OLD_SEND_NOTIFICATION, NEW_SEND_NOTIFICATION, "send_notification")

    print("Tool Compatibility Analysis: send_notification v1 → v2")
    print("-" * 55)
    if not issues:
        print("✓ No breaking changes detected")
    else:
        for issue in issues:
            icons = {"breaking": "🚨", "warning": "⚠️", "info": "ℹ️"}
            print(f"{icons[issue.severity]} [{issue.severity.upper()}] {issue.description}")

    breaking = [i for i in issues if i.severity == "breaking"]
    if breaking:
        print(f"\n❌ {len(breaking)} BREAKING change(s) — do not upgrade without updating callers")
        return False

    print("\n✓ Safe to upgrade")

    # Proceed with agent using the new schema
    tools = [{
        "name": "send_notification",
        "description": "Send a notification",
        "input_schema": NEW_SEND_NOTIFICATION,
    }]
    client = anthropic.Anthropic()
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        tools=tools,
        messages=[{"role": "user", "content": "Send a high-priority email to user u-42."}],
    )
    print(f"\nAgent stop_reason: {r.stop_reason}")
    return True


if __name__ == "__main__":
    run_semantic_check()
# Expected Token Savings: None direct — semantic check prevents misuse of updated tool schemas
# Environment: pip install anthropic; json is stdlib
```

---

## Option 3: SQLite Tool Version Registry with Rollback

```python
import sqlite3
import json
import hashlib
import anthropic
from datetime import datetime

class ToolVersionRegistry:
    """
    SQLite-backed registry of known-good tool versions.
    Validates current schemas against pinned versions at startup.
    Supports rollback to last known-good configuration.
    """

    def __init__(self, db_path: str = "tool_versions.db"):
        self.conn = sqlite3.connect(db_path)
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS tool_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tool_name TEXT,
                version TEXT,
                schema_json TEXT,
                schema_hash TEXT,
                is_pinned INTEGER DEFAULT 0,
                is_current INTEGER DEFAULT 0,
                registered_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS compatibility_checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tool_name TEXT,
                pinned_version TEXT,
                current_hash TEXT,
                status TEXT,
                issues TEXT,
                checked_at TEXT DEFAULT (datetime('now'))
            );
        """)
        self.conn.commit()

    def _hash(self, schema: dict) -> str:
        return hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest()[:16]

    def register(self, tool_name: str, schema: dict, version: str, pin: bool = False):
        schema_hash = self._hash(schema)
        # Unset current flag for this tool
        self.conn.execute(
            "UPDATE tool_versions SET is_current=0 WHERE tool_name=?", (tool_name,)
        )
        self.conn.execute(
            "INSERT INTO tool_versions (tool_name, version, schema_json, schema_hash, is_pinned, is_current) VALUES (?,?,?,?,?,1)",
            (tool_name, version, json.dumps(schema), schema_hash, int(pin)),
        )
        self.conn.commit()

    def pin(self, tool_name: str):
        """Mark the current version as the pinned (known-good) baseline."""
        self.conn.execute(
            "UPDATE tool_versions SET is_pinned=0 WHERE tool_name=?", (tool_name,)
        )
        self.conn.execute(
            "UPDATE tool_versions SET is_pinned=1 WHERE tool_name=? AND is_current=1", (tool_name,)
        )
        self.conn.commit()

    def get_pinned(self, tool_name: str) -> dict | None:
        row = self.conn.execute(
            "SELECT schema_json, version FROM tool_versions WHERE tool_name=? AND is_pinned=1 ORDER BY id DESC LIMIT 1",
            (tool_name,),
        ).fetchone()
        return {"schema": json.loads(row[0]), "version": row[1]} if row else None

    def check(self, tool_name: str, current_schema: dict) -> dict:
        current_hash = self._hash(current_schema)
        pinned = self.get_pinned(tool_name)

        if not pinned:
            status = "unregistered"
            issues = ["No pinned version found — register and pin before production use"]
        elif pinned["schema"] == current_schema:
            status = "compatible"
            issues = []
        else:
            status = "changed"
            pinned_hash = self._hash(pinned["schema"])
            issues = [f"Schema changed since pinned version {pinned['version']} (hash {pinned_hash} → {current_hash})"]

        self.conn.execute(
            "INSERT INTO compatibility_checks (tool_name, pinned_version, current_hash, status, issues) VALUES (?,?,?,?,?)",
            (tool_name, pinned["version"] if pinned else None, current_hash, status, json.dumps(issues)),
        )
        self.conn.commit()
        return {"tool": tool_name, "status": status, "issues": issues}

    def rollback(self, tool_name: str) -> dict | None:
        """Return the last pinned schema for rollback."""
        return self.get_pinned(tool_name)

    def audit(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT tool_name, status, issues, checked_at FROM compatibility_checks ORDER BY checked_at DESC LIMIT 20"
        ).fetchall()
        return [{"tool": r[0], "status": r[1], "issues": json.loads(r[2]), "at": r[3]} for r in rows]


TOOL_SCHEMA_V1 = {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "limit": {"type": "integer"},
    },
    "required": ["query"],
}

TOOL_SCHEMA_V2 = {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "limit": {"type": "integer"},
        "filter_by": {"type": "string"},  # New required field
    },
    "required": ["query", "filter_by"],  # Breaking change
}


def run_registry_demo():
    registry = ToolVersionRegistry(db_path=":memory:")

    # Register and pin v1 as baseline
    registry.register("search_db", TOOL_SCHEMA_V1, version="1.0", pin=True)
    print("Registered and pinned v1 schema")

    # Check compatibility of v2 (simulates schema drift)
    result = registry.check("search_db", TOOL_SCHEMA_V2)
    print(f"\nCompatibility check: {result['status']}")
    for issue in result["issues"]:
        print(f"  ⚠️  {issue}")

    if result["status"] != "compatible":
        print("\nRolling back to pinned version...")
        rollback = registry.rollback("search_db")
        if rollback:
            print(f"  Using pinned version: {rollback['version']}")
            schema_to_use = rollback["schema"]
        else:
            schema_to_use = TOOL_SCHEMA_V1
    else:
        schema_to_use = TOOL_SCHEMA_V2

    tools = [{"name": "search_db", "description": "Search DB", "input_schema": schema_to_use}]
    client = anthropic.Anthropic()
    r = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        tools=tools,
        messages=[{"role": "user", "content": "Search for records matching 'sales'."}],
    )
    print(f"\nAgent ran with {'pinned v1' if schema_to_use == TOOL_SCHEMA_V1 else 'v2'} schema: stop_reason={r.stop_reason}")


if __name__ == "__main__":
    run_registry_demo()
# Expected Token Savings: None — registry prevents tool call failures that would require retry API calls
# Environment: pip install anthropic; sqlite3, json, hashlib are stdlib
```

---

## Option 4: Runtime Schema Validator with Auto-Coercion

```python
import json
import anthropic
from dataclasses import dataclass
from typing import Any

@dataclass
class ValidationResult:
    valid: bool
    errors: list[str]
    coerced_input: dict | None = None  # Fixed input if coercible


def validate_tool_input(schema: dict, tool_input: dict) -> ValidationResult:
    """
    Validates tool input against expected schema.
    Attempts safe coercions (string→int, missing optional fields).
    """
    errors = []
    coerced = dict(tool_input)
    props = schema.get("properties", {})
    required = set(schema.get("required", []))

    # Check required fields
    for field in required:
        if field not in tool_input:
            errors.append(f"Missing required field: '{field}'")

    # Type coercions and checks
    for field, value in list(tool_input.items()):
        if field not in props:
            # Unknown field — warn but don't fail
            continue
        expected_type = props[field].get("type")
        if expected_type == "integer" and isinstance(value, str):
            try:
                coerced[field] = int(value)
            except ValueError:
                errors.append(f"Field '{field}': cannot coerce '{value}' to integer")
        elif expected_type == "string" and isinstance(value, int):
            coerced[field] = str(value)
        elif expected_type == "boolean" and isinstance(value, str):
            if value.lower() in ("true", "1", "yes"):
                coerced[field] = True
            elif value.lower() in ("false", "0", "no"):
                coerced[field] = False
            else:
                errors.append(f"Field '{field}': cannot coerce '{value}' to boolean")
        elif expected_type and not isinstance(value, {"string": str, "integer": int, "boolean": bool, "array": list, "object": dict}.get(expected_type, object)):
            errors.append(f"Field '{field}': expected {expected_type}, got {type(value).__name__}")

        # Enum validation
        if "enum" in props.get(field, {}):
            allowed = props[field]["enum"]
            if coerced.get(field) not in allowed:
                errors.append(f"Field '{field}': '{coerced.get(field)}' not in {allowed}")

    return ValidationResult(
        valid=len(errors) == 0,
        errors=errors,
        coerced_input=coerced if len(errors) == 0 else None,
    )


SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "limit": {"type": "integer"},
        "sort": {"type": "string", "enum": ["asc", "desc"]},
        "include_archived": {"type": "boolean"},
    },
    "required": ["query"],
}


def safe_execute_tool(tool_name: str, tool_input: dict, schema: dict) -> str:
    validation = validate_tool_input(schema, tool_input)
    if not validation.valid:
        return f"[ValidationError] {tool_name}: {'; '.join(validation.errors)}"

    effective_input = validation.coerced_input or tool_input
    print(f"[Tool] {tool_name} executing with: {json.dumps(effective_input)}")
    return f"Results from {tool_name}: 5 records found"


def run_validated_agent(request: str):
    client = anthropic.Anthropic()
    tools = [{"name": "search", "description": "Search records", "input_schema": SEARCH_SCHEMA}]
    messages = [{"role": "user", "content": request}]

    for _ in range(4):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=tools,
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text"):
                    print(f"Agent: {block.text}")
            break
        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = safe_execute_tool(block.name, block.input, SEARCH_SCHEMA)
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
            messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    run_validated_agent("Search for 'machine learning' records, limit to 5, sort descending.")
# Expected Token Savings: None — validation prevents schema errors that would require re-prompt
# Environment: pip install anthropic; json is stdlib
```

---

## Option 5: Multi-Version Tool Router

```python
import json
import anthropic
from dataclasses import dataclass
from typing import Callable

@dataclass
class ToolImplementation:
    version: str
    schema: dict
    handler: Callable[[dict], str]
    min_client_version: str = "1.0"

class MultiVersionToolRouter:
    """
    Routes tool calls to the correct implementation based on
    the negotiated API version. Supports graceful degradation to
    older implementations when newer schemas are unavailable.
    """

    def __init__(self):
        self._tools: dict[str, list[ToolImplementation]] = {}

    def register(self, tool_name: str, impl: ToolImplementation):
        self._tools.setdefault(tool_name, []).append(impl)
        # Keep sorted by version descending
        self._tools[tool_name].sort(key=lambda x: x.version, reverse=True)

    def get_schema(self, tool_name: str, client_version: str = "latest") -> dict | None:
        impls = self._tools.get(tool_name, [])
        if not impls:
            return None
        if client_version == "latest":
            return impls[0].schema
        # Find best compatible version
        for impl in impls:
            if impl.min_client_version <= client_version:
                return impl.schema
        return impls[-1].schema  # Fall back to oldest

    def execute(self, tool_name: str, tool_input: dict, client_version: str = "latest") -> str:
        impls = self._tools.get(tool_name, [])
        if not impls:
            return f"Unknown tool: {tool_name}"

        if client_version == "latest":
            impl = impls[0]
        else:
            impl = next((i for i in impls if i.min_client_version <= client_version), impls[-1])

        print(f"[Router] {tool_name} → v{impl.version} (client={client_version})")
        return impl.handler(tool_input)

    def anthropic_tools(self, client_version: str = "latest") -> list[dict]:
        result = []
        for name, impls in self._tools.items():
            schema = self.get_schema(name, client_version)
            if schema:
                result.append({"name": name, "description": f"Tool: {name} (v{impls[0].version})", "input_schema": schema})
        return result


# Register two versions of the same tool
router = MultiVersionToolRouter()

# v1: simple schema
router.register("get_user", ToolImplementation(
    version="1.0",
    min_client_version="1.0",
    schema={
        "type": "object",
        "properties": {"user_id": {"type": "string"}},
        "required": ["user_id"],
    },
    handler=lambda inp: json.dumps({"id": inp["user_id"], "name": "Alice", "email": "alice@example.com"}),
))

# v2: extended schema with additional fields
router.register("get_user", ToolImplementation(
    version="2.0",
    min_client_version="2.0",
    schema={
        "type": "object",
        "properties": {
            "user_id": {"type": "string"},
            "include_preferences": {"type": "boolean", "default": False},
            "fields": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["user_id"],
    },
    handler=lambda inp: json.dumps({
        "id": inp["user_id"], "name": "Alice",
        "email": "alice@example.com",
        "preferences": {"theme": "dark"} if inp.get("include_preferences") else None,
    }),
))


def run_versioned_tool_agent(request: str, client_version: str = "latest"):
    tools = router.anthropic_tools(client_version)
    print(f"Using {len(tools)} tools for client_version={client_version}")

    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": request}]

    for _ in range(4):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=tools,
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text"):
                    print(f"Agent: {block.text}")
            break
        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = router.execute(block.name, block.input, client_version)
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
            messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    print("=== v1 client ===")
    run_versioned_tool_agent("Get user info for user ID u-99.", client_version="1.0")
    print("\n=== v2 client ===")
    run_versioned_tool_agent("Get user u-99 with preferences included.", client_version="2.0")
# Expected Token Savings: None — versioned routing prevents tool call failures on mismatched schemas
# Environment: pip install anthropic; json is stdlib
```

---

## Option 6: CI-Gate Tool Compatibility Test

```python
import json
import hashlib
import sqlite3
import sys
import anthropic
from datetime import datetime

KNOWN_GOOD_TOOL_HASHES = {}  # Populated at first CI run and stored in repo

def schema_hash(schema: dict) -> str:
    return hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest()[:20]


def get_production_tools() -> list[dict]:
    """The actual tools used in production."""
    return [
        {
            "name": "query_database",
            "description": "Query records from the database",
            "input_schema": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string"},
                    "params": {"type": "array", "items": {"type": "string"}},
                    "timeout_sec": {"type": "integer", "default": 30},
                },
                "required": ["sql"],
            },
        },
        {
            "name": "call_external_api",
            "description": "Call an external REST API",
            "input_schema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "method": {"type": "string", "enum": ["GET", "POST", "PUT", "DELETE"]},
                    "body": {"type": "object"},
                },
                "required": ["url", "method"],
            },
        },
    ]


class ToolCompatibilityCI:
    def __init__(self, db_path: str = ":memory:"):
        self.conn = sqlite3.connect(db_path)
        self._init_schema()

    def _init_schema(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS ci_checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tool_name TEXT,
                expected_hash TEXT,
                actual_hash TEXT,
                status TEXT,
                checked_at TEXT DEFAULT (datetime('now'))
            )
        """)
        self.conn.commit()

    def run(self, tools: list[dict], expected_hashes: dict[str, str]) -> tuple[bool, list[str]]:
        failures = []
        for tool in tools:
            name = tool["name"]
            actual = schema_hash(tool["input_schema"])
            expected = expected_hashes.get(name)

            if expected is None:
                status = "new_tool"
                print(f"  ℹ️  {name}: new tool (hash={actual}) — add to expected_hashes")
            elif actual == expected:
                status = "pass"
                print(f"  ✓  {name}: schema matches pinned hash")
            else:
                status = "fail"
                failures.append(f"{name}: expected hash {expected}, got {actual}")
                print(f"  ✗  {name}: schema CHANGED (expected {expected}, got {actual})")

            self.conn.execute(
                "INSERT INTO ci_checks (tool_name, expected_hash, actual_hash, status) VALUES (?,?,?,?)",
                (name, expected, actual, status),
            )
        self.conn.commit()
        return len(failures) == 0, failures

    def generate_lockfile(self, tools: list[dict]) -> dict:
        """Generate a lockfile with current hashes to commit to the repo."""
        return {
            "generated_at": datetime.utcnow().isoformat(),
            "tools": {t["name"]: schema_hash(t["input_schema"]) for t in tools},
        }

    def verify_agent_calls_work(self, tools: list[dict]) -> bool:
        """Smoke test: verify the agent can actually use the tools."""
        client = anthropic.Anthropic()
        try:
            r = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=64,
                tools=tools,
                messages=[{"role": "user", "content": "Query the database for recent users."}],
            )
            return r.stop_reason in ("tool_use", "end_turn")
        except Exception as e:
            print(f"  ✗  Agent smoke test failed: {e}")
            return False


def run_ci_compatibility_gate() -> int:
    ci = ToolCompatibilityCI()
    tools = get_production_tools()

    # Generate the lockfile (in real CI, this is committed to the repo)
    lockfile = ci.generate_lockfile(tools)
    print("Tool Compatibility Gate")
    print(f"Checking {len(tools)} tools against lockfile...")

    passed, failures = ci.run(tools, lockfile["tools"])

    if not passed:
        print(f"\n✗ {len(failures)} compatibility failure(s):")
        for f in failures:
            print(f"  {f}")
        return 1

    print("\nRunning agent smoke test...")
    smoke_ok = ci.verify_agent_calls_work(tools)
    if not smoke_ok:
        print("✗ Agent smoke test failed")
        return 1

    print("\n✓ All tool compatibility checks passed")
    return 0


if __name__ == "__main__":
    exit_code = run_ci_compatibility_gate()
    print(f"\nCI exit code: {exit_code}")
    sys.exit(exit_code)
# Expected Token Savings: None — CI gate prevents deploying incompatible tool schemas to production
# Environment: pip install anthropic; sqlite3, json, hashlib, sys are stdlib
```

---

## Comparison

| Option | Change Detection | Granularity | Auto-Rollback | SQLite | CI Integration | Best For |
|--------|-----------------|-------------|---------------|--------|----------------|----------|
| 1 | Schema hash diff | Tool-level | No | No | No | Quick startup check |
| 2 | Semantic field analysis | Field-level | No | No | No | Understanding what broke |
| 3 | Hash + registry | Tool-level | Yes (pinned) | Yes | No | Production with rollback capability |
| 4 | Runtime input validation | Field-level + coercion | No (coerce) | No | No | Tolerant callers with type coercion |
| 5 | Version routing | Per-version | No (route) | No | No | Multi-version backward compatibility |
| 6 | Hash lockfile | Tool-level | No | Yes | Exit code | CI/CD gate before deployment |
