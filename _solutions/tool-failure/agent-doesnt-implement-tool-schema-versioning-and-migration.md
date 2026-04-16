---
layout: solution
title: "Agent Doesn't Implement Tool Schema Versioning and Migration"
category: tool-failure
description: "Version tool schemas so in-flight sessions survive schema changes, with migration shims, backward compatibility checks, and rollback support."
tags: [tool-failure, schema, versioning, migration, backward-compatibility, deployment]
---

# Agent Doesn't Implement Tool Schema Versioning and Migration

When tool schemas change between deployments, active agent sessions that cached the old schema start receiving unexpected arguments or missing required fields, causing silent failures or hard crashes. Without schema versioning, a schema change is a breaking change. With versioning, old callers get migration shims, new callers get the latest schema, and rollbacks are safe.

## Option 1: Semver-Tagged Tool Registry

```python
import anthropic
from typing import Any

client = anthropic.Anthropic()

# Schema registry keyed by (tool_name, version)
TOOL_REGISTRY: dict[tuple[str, str], dict] = {
    ("search_web", "1.0"): {
        "name": "search_web",
        "description": "Search the web for information.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
            },
            "required": ["query"],
        },
    },
    ("search_web", "2.0"): {
        "name": "search_web",
        "description": "Search the web with optional filters.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "max_results": {"type": "integer", "description": "Max results", "default": 10},
                "language": {"type": "string", "description": "Language filter", "default": "en"},
            },
            "required": ["query"],
        },
    },
}

LATEST_VERSIONS = {"search_web": "2.0"}


def get_tool_schema(tool_name: str, version: str | None = None) -> dict:
    """Retrieve tool schema at a specific version or latest."""
    v = version or LATEST_VERSIONS.get(tool_name, "1.0")
    schema = TOOL_REGISTRY.get((tool_name, v))
    if not schema:
        raise ValueError(f"Unknown tool {tool_name}@{v}")
    return schema


def execute_tool(tool_name: str, inputs: dict[str, Any], session_version: str) -> str:
    """Execute tool, migrating inputs from session_version to latest if needed."""
    latest = LATEST_VERSIONS.get(tool_name, session_version)
    if session_version != latest and tool_name == "search_web":
        # v1->v2 migration: add defaults for new fields
        inputs.setdefault("max_results", 10)
        inputs.setdefault("language", "en")
    return f"[search_web@{latest}] Results for: {inputs['query']} (lang={inputs.get('language', 'en')}, max={inputs.get('max_results', 10)})"


def run_agent(session_schema_version: str = "2.0") -> None:
    tool_schema = get_tool_schema("search_web", version=session_schema_version)
    messages: list[dict] = [{"role": "user", "content": "Search for Python asyncio best practices."}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=[tool_schema],
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text"):
                    print(block.text)
            break

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = execute_tool(block.name, dict(block.input), session_schema_version)
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
                print(f"Tool called: {block.name} (session schema v{session_schema_version})")

        messages.append({"role": "user", "content": tool_results})


run_agent(session_schema_version="1.0")  # Old session — still works via migration
run_agent(session_schema_version="2.0")  # New session — native

# Expected Token Savings: N/A (correctness pattern); prevents silent failures on schema upgrades
# Environment: Python 3.11+; store session_schema_version in session metadata alongside conversation history
```

## Option 2: Migration Shim Chain with Validators

```python
import anthropic
from typing import Any, Callable

client = anthropic.Anthropic()

# Migration functions: (from_version, to_version) -> transformer
MigrationFn = Callable[[dict[str, Any]], dict[str, Any]]

MIGRATIONS: dict[tuple[str, str], MigrationFn] = {
    ("1.0", "2.0"): lambda inp: {**inp, "max_results": inp.get("max_results", 10), "language": inp.get("language", "en")},
    ("2.0", "3.0"): lambda inp: {**inp, "safe_search": inp.get("safe_search", True), "query": inp["query"].strip()},
}

VERSIONS = ["1.0", "2.0", "3.0"]
CURRENT_VERSION = "3.0"


def migrate_inputs(inputs: dict[str, Any], from_version: str, to_version: str) -> dict[str, Any]:
    """Walk the migration chain from from_version to to_version."""
    start = VERSIONS.index(from_version)
    end = VERSIONS.index(to_version)
    result = dict(inputs)
    for i in range(start, end):
        fn = MIGRATIONS.get((VERSIONS[i], VERSIONS[i + 1]))
        if fn:
            result = fn(result)
    return result


TOOL_SCHEMA_V3 = {
    "name": "search_web",
    "description": "Search the web with full filtering options.",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "max_results": {"type": "integer", "default": 10},
            "language": {"type": "string", "default": "en"},
            "safe_search": {"type": "boolean", "default": True},
        },
        "required": ["query"],
    },
}


def validate_inputs(inputs: dict[str, Any], version: str) -> list[str]:
    """Return list of validation errors for given inputs at version."""
    errors = []
    if "query" not in inputs:
        errors.append("Missing required field: query")
    if version >= "2.0" and "language" in inputs and len(inputs["language"]) != 2:
        errors.append("language must be 2-letter ISO code")
    return errors


def run_agent_with_migration(user_session_version: str = "1.0") -> None:
    messages: list[dict] = [{"role": "user", "content": "Find recent papers on LLM evaluation."}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=[TOOL_SCHEMA_V3],
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            for b in response.content:
                if hasattr(b, "text"):
                    print(b.text)
            break

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                raw = dict(block.input)
                migrated = migrate_inputs(raw, user_session_version, CURRENT_VERSION)
                errors = validate_inputs(migrated, CURRENT_VERSION)
                if errors:
                    result = f"Tool input error after migration: {errors}"
                else:
                    result = f"Search OK (migrated {user_session_version}->{CURRENT_VERSION}): {migrated}"
                    print(f"Migrated inputs: {migrated}")
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

        messages.append({"role": "user", "content": tool_results})


run_agent_with_migration("1.0")

# Expected Token Savings: N/A; migration chain avoids re-starting sessions on every schema bump
# Environment: Python 3.11+; store VERSIONS list in config, add new migrations as append-only entries
```

## Option 3: SQLite Schema Version Registry with Deprecation Warnings

```python
import sqlite3
import json
import time
import anthropic
from typing import Any

client = anthropic.Anthropic()
DB_PATH = ":memory:"


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS tool_schemas (
            tool_name TEXT NOT NULL,
            version TEXT NOT NULL,
            schema_json TEXT NOT NULL,
            deprecated_at REAL,
            sunset_at REAL,
            created_at REAL NOT NULL,
            PRIMARY KEY (tool_name, version)
        );
        CREATE TABLE IF NOT EXISTS session_tool_versions (
            session_id TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            version TEXT NOT NULL,
            registered_at REAL NOT NULL,
            PRIMARY KEY (session_id, tool_name)
        );
    """)
    conn.commit()


def register_schema(conn: sqlite3.Connection, tool_name: str, version: str, schema: dict,
                    deprecated_at: float | None = None, sunset_at: float | None = None) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO tool_schemas VALUES (?,?,?,?,?,?)",
        (tool_name, version, json.dumps(schema), deprecated_at, sunset_at, time.time())
    )
    conn.commit()


def get_schema(conn: sqlite3.Connection, tool_name: str, version: str) -> dict | None:
    row = conn.execute(
        "SELECT schema_json, deprecated_at, sunset_at FROM tool_schemas WHERE tool_name=? AND version=?",
        (tool_name, version)
    ).fetchone()
    if not row:
        return None
    schema, deprecated_at, sunset_at = row
    now = time.time()
    if sunset_at and now > sunset_at:
        raise RuntimeError(f"Tool {tool_name}@{version} has been sunset. Upgrade required.")
    if deprecated_at and now > deprecated_at:
        print(f"WARNING: {tool_name}@{version} is deprecated. Please upgrade to latest.")
    return json.loads(schema)


def set_session_version(conn: sqlite3.Connection, session_id: str, tool_name: str, version: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO session_tool_versions VALUES (?,?,?,?)",
        (session_id, tool_name, version, time.time())
    )
    conn.commit()


def run_versioned_agent(conn: sqlite3.Connection, session_id: str, tool_name: str,
                        session_version: str) -> None:
    schema = get_schema(conn, tool_name, session_version)
    if schema is None:
        raise ValueError(f"Schema {tool_name}@{session_version} not found")

    set_session_version(conn, session_id, tool_name, session_version)
    messages: list[dict] = [{"role": "user", "content": "Search for AI safety research."}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=[schema],
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason == "end_turn":
            for b in response.content:
                if hasattr(b, "text"):
                    print(b.text)
            break
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                tool_results.append({
                    "type": "tool_result", "tool_use_id": block.id,
                    "content": f"[{tool_name}@{session_version}] Results for: {block.input.get('query', '')}"
                })
        messages.append({"role": "user", "content": tool_results})


conn = sqlite3.connect(DB_PATH)
init_db(conn)

register_schema(conn, "search_web", "1.0", {
    "name": "search_web", "description": "Search the web.",
    "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
}, deprecated_at=time.time() - 1)  # Already deprecated

register_schema(conn, "search_web", "2.0", {
    "name": "search_web", "description": "Search the web with filters.",
    "input_schema": {
        "type": "object",
        "properties": {"query": {"type": "string"}, "max_results": {"type": "integer", "default": 10}},
        "required": ["query"],
    },
})

run_versioned_agent(conn, "session-abc", "search_web", "1.0")  # Triggers deprecation warning

# Expected Token Savings: N/A; SQLite sunset enforcement prevents silent failures weeks after schema change
# Environment: Python 3.11+; swap :memory: for a real path in production; set sunset_at to 30-90 days post-deprecation
```

## Option 4: Schema Diff Generator with Breaking Change Detection

```python
import anthropic
from typing import Any
from dataclasses import dataclass

client = anthropic.Anthropic()


@dataclass
class SchemaDiff:
    added_required: list[str]
    removed_fields: list[str]
    type_changes: list[tuple[str, str, str]]  # (field, old_type, new_type)

    @property
    def is_breaking(self) -> bool:
        return bool(self.added_required or self.removed_fields or self.type_changes)

    def describe(self) -> str:
        parts = []
        if self.added_required:
            parts.append(f"New required fields: {self.added_required}")
        if self.removed_fields:
            parts.append(f"Removed fields: {self.removed_fields}")
        if self.type_changes:
            parts.append(f"Type changes: {self.type_changes}")
        return "; ".join(parts) if parts else "No breaking changes"


def diff_schemas(old: dict, new: dict) -> SchemaDiff:
    """Detect breaking changes between two tool input_schemas."""
    old_props = old.get("input_schema", {}).get("properties", {})
    new_props = new.get("input_schema", {}).get("properties", {})
    old_req = set(old.get("input_schema", {}).get("required", []))
    new_req = set(new.get("input_schema", {}).get("required", []))

    added_required = list(new_req - old_req)
    removed_fields = [f for f in old_props if f not in new_props]
    type_changes = [
        (f, old_props[f].get("type", "?"), new_props[f].get("type", "?"))
        for f in old_props
        if f in new_props and old_props[f].get("type") != new_props[f].get("type")
    ]
    return SchemaDiff(added_required, removed_fields, type_changes)


OLD_SCHEMA = {
    "name": "run_query",
    "description": "Run a database query.",
    "input_schema": {
        "type": "object",
        "properties": {
            "sql": {"type": "string"},
            "timeout": {"type": "integer"},
        },
        "required": ["sql"],
    },
}

NEW_SCHEMA = {
    "name": "run_query",
    "description": "Run a database query with user context.",
    "input_schema": {
        "type": "object",
        "properties": {
            "sql": {"type": "string"},
            "timeout": {"type": "number"},     # type changed
            "user_id": {"type": "string"},     # new required field
        },
        "required": ["sql", "user_id"],        # added user_id
    },
}


def safe_deploy_schema(old_schema: dict, new_schema: dict, active_sessions: int) -> dict:
    """Return the schema to deploy based on breaking-change analysis."""
    diff = diff_schemas(old_schema, new_schema)
    print(f"Schema diff: {diff.describe()}")
    if diff.is_breaking and active_sessions > 0:
        print(f"BLOCKED: Breaking change with {active_sessions} active sessions. Use compatibility shim.")
        return old_schema  # Keep old until sessions drain
    print("Schema deploy approved.")
    return new_schema


def run_agent(schema: dict) -> None:
    messages: list[dict] = [{"role": "user", "content": "Run: SELECT count(*) FROM users"}]
    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=[schema],
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason == "end_turn":
            for b in response.content:
                if hasattr(b, "text"):
                    print(b.text)
            break
        results = [
            {"type": "tool_result", "tool_use_id": b.id,
             "content": f"Query result: 42 rows (inputs: {b.input})"}
            for b in response.content if b.type == "tool_use"
        ]
        messages.append({"role": "user", "content": results})


deployed = safe_deploy_schema(OLD_SCHEMA, NEW_SCHEMA, active_sessions=5)
run_agent(deployed)

# Expected Token Savings: N/A; diff detection prevents accidental breaking deploys mid-session
# Environment: Python 3.11+; run diff check in CI before merging schema changes; block merge on breaking+active>0
```

## Option 5: Dual-Schema Compatibility Proxy

```python
import asyncio
import anthropic
from typing import Any

client = anthropic.AsyncAnthropic()

# The tool as exposed to Claude (always latest)
CURRENT_TOOL = {
    "name": "fetch_data",
    "description": "Fetch data from the data service.",
    "input_schema": {
        "type": "object",
        "properties": {
            "resource_id": {"type": "string"},
            "fields": {"type": "array", "items": {"type": "string"}},
            "format": {"type": "string", "enum": ["json", "csv", "parquet"], "default": "json"},
        },
        "required": ["resource_id", "fields"],
    },
}

# Legacy backend still expects v1 shape
def to_v1_request(v2_inputs: dict[str, Any]) -> dict[str, Any]:
    """Downgrade v2 tool inputs to v1 backend format."""
    return {
        "id": v2_inputs["resource_id"],                          # renamed field
        "columns": v2_inputs.get("fields", []),                  # renamed field
        "output_format": v2_inputs.get("format", "json"),        # renamed field
    }

# Modern backend expects v2 shape
def to_v2_request(v2_inputs: dict[str, Any]) -> dict[str, Any]:
    return v2_inputs  # no-op — already in v2 format


async def execute_fetch(inputs: dict[str, Any], backend_version: str) -> str:
    """Route to the appropriate backend."""
    if backend_version == "v1":
        v1_req = to_v1_request(inputs)
        return f"[v1 backend] id={v1_req['id']} columns={v1_req['columns']} format={v1_req['output_format']}"
    return f"[v2 backend] resource={inputs['resource_id']} fields={inputs['fields']} format={inputs.get('format', 'json')}"


async def run_agent(backend_version: str = "v2") -> None:
    messages: list[dict] = [{"role": "user", "content": "Fetch resource R-42 with fields name, age."}]

    while True:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=[CURRENT_TOOL],
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            for b in response.content:
                if hasattr(b, "text"):
                    print(b.text)
            break

        results = []
        for block in response.content:
            if block.type == "tool_use":
                result = await execute_fetch(dict(block.input), backend_version)
                print(f"Proxy routed to {backend_version}: {result}")
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

        messages.append({"role": "user", "content": results})


async def main() -> None:
    print("=== v1 backend ===")
    await run_agent("v1")
    print("\n=== v2 backend ===")
    await run_agent("v2")


asyncio.run(main())

# Expected Token Savings: N/A; dual proxy lets you migrate backends independently from agent schema
# Environment: Python 3.11+; remove v1 path once all backends are upgraded; proxy adds ~0.1ms overhead
```

## Option 6: Schema Negotiation Handshake at Session Start

```python
import asyncio
import anthropic
from dataclasses import dataclass

client = anthropic.AsyncAnthropic()


@dataclass
class SessionCapabilities:
    session_id: str
    supported_versions: list[str]
    negotiated_version: str
    features: set[str]


SCHEMA_VERSIONS = {
    "1.0": {
        "name": "analyze_text",
        "description": "Analyze text content.",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        "features": {"basic_analysis"},
    },
    "2.0": {
        "name": "analyze_text",
        "description": "Analyze text with sentiment and entities.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "include_sentiment": {"type": "boolean", "default": True},
                "include_entities": {"type": "boolean", "default": True},
            },
            "required": ["text"],
        },
        "features": {"basic_analysis", "sentiment", "entities"},
    },
    "3.0": {
        "name": "analyze_text",
        "description": "Analyze text with full NLP pipeline.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "include_sentiment": {"type": "boolean", "default": True},
                "include_entities": {"type": "boolean", "default": True},
                "language": {"type": "string", "default": "auto"},
                "batch_id": {"type": "string"},
            },
            "required": ["text"],
        },
        "features": {"basic_analysis", "sentiment", "entities", "multilingual", "batching"},
    },
}

SERVER_SUPPORTED = {"2.0", "3.0"}


def negotiate_version(client_versions: list[str]) -> str:
    """Choose highest mutually supported version."""
    mutual = sorted(set(client_versions) & SERVER_SUPPORTED, reverse=True)
    if not mutual:
        raise RuntimeError(f"No compatible schema version. Client: {client_versions}, Server: {SERVER_SUPPORTED}")
    return mutual[0]


def build_session(session_id: str, client_versions: list[str]) -> SessionCapabilities:
    version = negotiate_version(client_versions)
    features = set(SCHEMA_VERSIONS[version].get("features", set()))
    return SessionCapabilities(session_id, client_versions, version, features)


async def run_agent_with_negotiation(session_id: str, client_versions: list[str]) -> None:
    session = build_session(session_id, client_versions)
    print(f"Session {session.session_id}: negotiated v{session.negotiated_version}, features={session.features}")

    schema_entry = SCHEMA_VERSIONS[session.negotiated_version]
    tool_schema = {k: v for k, v in schema_entry.items() if k != "features"}

    messages: list[dict] = [{"role": "user", "content": "Analyze: 'The new release exceeded all expectations.'"}]

    while True:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=[tool_schema],
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            for b in response.content:
                if hasattr(b, "text"):
                    print(b.text)
            break

        results = []
        for block in response.content:
            if block.type == "tool_use":
                inp = dict(block.input)
                parts = [f"text_len={len(inp['text'])}"]
                if "sentiment" in session.features:
                    parts.append(f"sentiment=positive ({inp.get('include_sentiment', True)})")
                if "entities" in session.features:
                    parts.append("entities=[release]")
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": ", ".join(parts)})

        messages.append({"role": "user", "content": results})


async def main() -> None:
    await run_agent_with_negotiation("session-old", ["1.0", "2.0"])   # Gets v2.0
    await run_agent_with_negotiation("session-new", ["2.0", "3.0"])   # Gets v3.0
    await run_agent_with_negotiation("session-latest", ["3.0"])       # Gets v3.0


asyncio.run(main())

# Expected Token Savings: N/A; negotiation ensures each session uses the best schema both sides understand
# Environment: Python 3.11+; send supported_versions list in session init metadata; version negotiation adds ~0ms (in-process)
```

## Comparison

| Option | Approach | Breaking Change Detection | Migration Shim | Backward Compat | Best For |
|--------|----------|--------------------------|---------------|-----------------|----------|
| 1. Semver Registry | Version tag per session | No | Yes (manual) | Yes | Simple deploys with known consumers |
| 2. Migration Chain | Walk version graph | No | Yes (automatic) | Yes | Multi-hop schema evolution |
| 3. SQLite Deprecation | DB-tracked sunset dates | No | No | Warn+block | Long deprecation windows |
| 4. Diff + Guard | Detect breaking fields | Yes | No | Block deploy | CI gate before merge |
| 5. Dual Proxy | Translate at execution | No | Yes (backend) | Yes | Backend migration without schema freeze |
| 6. Negotiation | Handshake at session start | No | No | Yes | Multi-client environments |
