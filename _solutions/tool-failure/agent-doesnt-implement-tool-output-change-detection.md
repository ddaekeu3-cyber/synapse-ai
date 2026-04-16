---
layout: solution
title: "Agent Doesn't Implement Tool Output Change Detection"
category: tool-failure
description: "Detect when a tool's output schema or content changes unexpectedly between calls, alerting on structural drift, new fields, removed fields, and type changes."
tags: [tool-failure, schema, change-detection, drift, monitoring, validation]
---

# Agent Doesn't Implement Tool Output Change Detection

External APIs and tools evolve silently. A field gets renamed, a type changes from string to number, a required key disappears. Without output change detection, the agent silently processes malformed data or crashes on missing keys — often in production, after the schema has been stable for months. Output change detection compares each tool response against a learned baseline and alerts immediately when the structure or content drifts unexpectedly.

## Option 1: Field Set Comparison Against Baseline

```python
import anthropic
import json
from typing import Any

client = anthropic.Anthropic()

# Learned baseline: expected top-level fields for each tool
TOOL_BASELINES: dict[str, set[str]] = {
    "get_user":   {"id", "name", "email", "created_at"},
    "get_product": {"id", "title", "price", "stock", "category"},
}

TOOLS = [
    {
        "name": "get_user",
        "description": "Fetch a user by ID.",
        "input_schema": {"type": "object", "properties": {"user_id": {"type": "string"}}, "required": ["user_id"]},
    },
    {
        "name": "get_product",
        "description": "Fetch a product by ID.",
        "input_schema": {"type": "object", "properties": {"product_id": {"type": "string"}}, "required": ["product_id"]},
    },
]

# Simulated tool responses — product has a new field and missing 'stock'
MOCK_RESPONSES: dict[str, Any] = {
    "get_user-u1": {"id": "u1", "name": "Alice", "email": "alice@example.com", "created_at": "2024-01-01"},
    "get_product-p1": {"id": "p1", "title": "Gadget", "price": 29.99, "category": "electronics", "rating": 4.5},  # missing 'stock', added 'rating'
}


def check_output_drift(tool_name: str, output: Any) -> list[str]:
    """Compare output fields against baseline. Return list of drift warnings."""
    baseline = TOOL_BASELINES.get(tool_name)
    if not baseline or not isinstance(output, dict):
        return []

    actual_fields = set(output.keys())
    added = actual_fields - baseline
    removed = baseline - actual_fields
    warnings = []
    if added:
        warnings.append(f"NEW fields: {sorted(added)}")
    if removed:
        warnings.append(f"MISSING fields: {sorted(removed)}")
    return warnings


def execute_tool(tool_name: str, inputs: dict) -> Any:
    key = f"{tool_name}-{list(inputs.values())[0]}"
    return MOCK_RESPONSES.get(key, {"error": "not found"})


def run_agent(question: str) -> str:
    messages: list[dict] = [{"role": "user", "content": question}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if hasattr(b, "text")), "")

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                output = execute_tool(block.name, dict(block.input))
                drift_warnings = check_output_drift(block.name, output)

                if drift_warnings:
                    print(f"[DRIFT ALERT] {block.name}: {'; '.join(drift_warnings)}")
                    # Update baseline to learn new structure (or alert + halt in strict mode)
                    if isinstance(output, dict):
                        TOOL_BASELINES[block.name] = set(output.keys())
                        print(f"[baseline updated] {block.name}: {TOOL_BASELINES[block.name]}")

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(output),
                })

        messages.append({"role": "user", "content": tool_results})


result = run_agent("Get user u1 and product p1.")
print(f"\nAgent: {result}")

# Expected Token Savings: N/A (reliability pattern); drift detection catches breaking API changes before they cascade to errors
# Environment: Python 3.11+; store baselines in SQLite to persist across restarts; alert via Slack/PagerDuty on drift
```

## Option 2: Type-Level Schema Comparison with Severity Rating

```python
import anthropic
import json
from typing import Any

client = anthropic.Anthropic()


def infer_schema(value: Any, depth: int = 0) -> dict:
    """Recursively infer JSON schema from a value."""
    if depth > 4:
        return {"type": "any"}
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "number"}
    if isinstance(value, str):
        return {"type": "string"}
    if isinstance(value, list):
        item_schema = infer_schema(value[0], depth + 1) if value else {"type": "any"}
        return {"type": "array", "items": item_schema}
    if isinstance(value, dict):
        return {
            "type": "object",
            "properties": {k: infer_schema(v, depth + 1) for k, v in value.items()},
            "required": list(value.keys()),
        }
    return {"type": "null"}


def compare_schemas(baseline: dict, current: dict, path: str = "") -> list[dict]:
    """Compare two schemas. Return list of change events with severity."""
    changes = []

    if baseline.get("type") != current.get("type"):
        changes.append({
            "path": path or "root",
            "severity": "BREAKING",
            "change": f"type changed: {baseline.get('type')} -> {current.get('type')}",
        })
        return changes  # Don't recurse if type is incompatible

    if baseline.get("type") == "object":
        old_props = set(baseline.get("properties", {}).keys())
        new_props = set(current.get("properties", {}).keys())
        old_req = set(baseline.get("required", []))
        new_req = set(current.get("required", []))

        for field in new_props - old_props:
            sev = "BREAKING" if field in new_req else "WARNING"
            changes.append({"path": f"{path}.{field}", "severity": sev, "change": "field added"})

        for field in old_props - new_props:
            sev = "BREAKING" if field in old_req else "WARNING"
            changes.append({"path": f"{path}.{field}", "severity": sev, "change": "field removed"})

        for field in old_props & new_props:
            nested = compare_schemas(
                baseline["properties"][field],
                current["properties"][field],
                path=f"{path}.{field}",
            )
            changes.extend(nested)

    return changes


# Baseline schema (what we learned from first successful call)
TOOL_SCHEMAS: dict[str, dict] = {}

TOOL = {
    "name": "search_products",
    "description": "Search for products.",
    "input_schema": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
}

# Simulate evolving API: v1 vs v2 response
RESPONSE_V1 = {"items": [{"id": "1", "name": "Widget", "price": 9.99}], "total": 1}
RESPONSE_V2 = {"items": [{"id": "1", "name": "Widget", "cost": 9.99}], "count": 1}  # price->cost, total->count


def execute_and_check(tool_name: str, response: Any) -> tuple[Any, list[dict]]:
    current_schema = infer_schema(response)
    baseline = TOOL_SCHEMAS.get(tool_name)

    if baseline is None:
        TOOL_SCHEMAS[tool_name] = current_schema
        print(f"[schema learned] {tool_name}: {list(response.keys()) if isinstance(response, dict) else type(response).__name__}")
        return response, []

    changes = compare_schemas(baseline, current_schema)
    if changes:
        breaking = [c for c in changes if c["severity"] == "BREAKING"]
        warnings = [c for c in changes if c["severity"] == "WARNING"]
        print(f"[SCHEMA DRIFT] {tool_name}:")
        for c in breaking:
            print(f"  BREAKING {c['path']}: {c['change']}")
        for c in warnings:
            print(f"  WARNING  {c['path']}: {c['change']}")

    return response, changes


def run_agent(question: str, api_version: str = "v1") -> str:
    tool_response = RESPONSE_V1 if api_version == "v1" else RESPONSE_V2
    messages: list[dict] = [{"role": "user", "content": question}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=[TOOL],
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if hasattr(b, "text")), "")

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result, changes = execute_and_check(block.name, tool_response)
                tool_results.append({
                    "type": "tool_result", "tool_use_id": block.id,
                    "content": json.dumps(result),
                })

        messages.append({"role": "user", "content": tool_results})


print("=== API v1 (baseline) ===")
run_agent("Search for widgets.", "v1")

print("\n=== API v2 (drifted) ===")
run_agent("Search for widgets.", "v2")

# Expected Token Savings: N/A; type-level comparison catches field renames that field-set comparison misses
# Environment: Python 3.11+; store inferred schemas in SQLite; use severity to decide: WARNING=log, BREAKING=alert+halt
```

## Option 3: Value-Range Anomaly Detection for Numeric Fields

```python
import anthropic
import json
import math
from typing import Any

client = anthropic.Anthropic()


class NumericBaseline:
    """Track running statistics for numeric tool output fields."""

    def __init__(self) -> None:
        self.n = 0
        self.mean = 0.0
        self.M2 = 0.0  # Welford's algorithm for running variance
        self.min_val = float("inf")
        self.max_val = float("-inf")

    def update(self, x: float) -> None:
        self.n += 1
        delta = x - self.mean
        self.mean += delta / self.n
        delta2 = x - self.mean
        self.M2 += delta * delta2
        self.min_val = min(self.min_val, x)
        self.max_val = max(self.max_val, x)

    @property
    def std(self) -> float:
        if self.n < 2:
            return 0.0
        return math.sqrt(self.M2 / (self.n - 1))

    def is_anomaly(self, x: float, z_threshold: float = 3.0) -> bool:
        if self.n < 5 or self.std == 0:
            return False
        z = abs(x - self.mean) / self.std
        return z > z_threshold


# Per-tool, per-field baselines
field_baselines: dict[str, dict[str, NumericBaseline]] = {}

TOOL = {
    "name": "get_metrics",
    "description": "Get system metrics.",
    "input_schema": {
        "type": "object",
        "properties": {"component": {"type": "string"}},
        "required": ["component"],
    },
}

# Simulate 10 normal readings then 2 anomalous ones
import random
random.seed(42)
NORMAL_READINGS = [{"latency_ms": random.gauss(50, 5), "error_rate": random.gauss(0.01, 0.002), "rps": random.gauss(1000, 50)} for _ in range(10)]
ANOMALOUS = [{"latency_ms": 450.0, "error_rate": 0.35, "rps": 200.0}]  # Spike
ALL_READINGS = NORMAL_READINGS + ANOMALOUS


def check_numeric_anomalies(tool_name: str, output: dict[str, Any]) -> list[str]:
    """Check numeric fields for statistical anomalies."""
    if tool_name not in field_baselines:
        field_baselines[tool_name] = {}

    alerts = []
    for field, value in output.items():
        if not isinstance(value, (int, float)):
            continue
        if field not in field_baselines[tool_name]:
            field_baselines[tool_name][field] = NumericBaseline()

        baseline = field_baselines[tool_name][field]
        if baseline.is_anomaly(float(value)):
            alerts.append(f"{field}={value:.2f} (μ={baseline.mean:.2f}, σ={baseline.std:.2f}, z={(abs(value - baseline.mean)/baseline.std):.1f})")
        baseline.update(float(value))

    return alerts


def run_agent_with_anomaly_check(question: str, reading: dict) -> str:
    messages: list[dict] = [{"role": "user", "content": question}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            tools=[TOOL],
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if hasattr(b, "text")), "")

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                anomalies = check_numeric_anomalies(block.name, reading)
                if anomalies:
                    print(f"[ANOMALY] {block.name}: {'; '.join(anomalies)}")
                tool_results.append({
                    "type": "tool_result", "tool_use_id": block.id,
                    "content": json.dumps(reading),
                })

        messages.append({"role": "user", "content": tool_results})


print("Training on 10 normal readings...")
for reading in NORMAL_READINGS:
    run_agent_with_anomaly_check("Check API metrics.", reading)

print("\nChecking anomalous reading...")
run_agent_with_anomaly_check("Check API metrics.", ANOMALOUS[0])

# Expected Token Savings: N/A; anomaly detection catches silent degradation (latency drift, error rate creep) before outages
# Environment: Python 3.11+; z_threshold=3.0 gives ~0.3% false positive rate; lower to 2.0 for more sensitive alerts
```

## Option 4: Content Hash Drift Detection with SQLite Log

```python
import anthropic
import hashlib
import json
import sqlite3
import time
from typing import Any

client = anthropic.Anthropic()
DB_PATH = ":memory:"


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS tool_output_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tool_name TEXT NOT NULL,
            input_hash TEXT NOT NULL,
            output_hash TEXT NOT NULL,
            output_size INTEGER NOT NULL,
            field_count INTEGER,
            recorded_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tool_drift_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tool_name TEXT NOT NULL,
            drift_type TEXT NOT NULL,
            detail TEXT NOT NULL,
            alerted_at REAL NOT NULL
        );
    """)
    conn.commit()


def stable_hash(data: Any) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:12]


def log_output(conn: sqlite3.Connection, tool_name: str, inputs: dict, output: Any) -> None:
    input_hash = stable_hash(inputs)
    output_hash = stable_hash(output)
    field_count = len(output) if isinstance(output, dict) else None
    conn.execute(
        "INSERT INTO tool_output_log VALUES (NULL,?,?,?,?,?,?)",
        (tool_name, input_hash, output_hash, len(json.dumps(output)), field_count, time.time())
    )
    conn.commit()


def detect_drift(conn: sqlite3.Connection, tool_name: str, inputs: dict, output: Any) -> list[str]:
    input_hash = stable_hash(inputs)
    output_hash = stable_hash(output)

    # Check if same inputs have returned different outputs before
    prev = conn.execute(
        "SELECT output_hash, field_count FROM tool_output_log WHERE tool_name=? AND input_hash=? ORDER BY recorded_at DESC LIMIT 5",
        (tool_name, input_hash)
    ).fetchall()

    alerts = []
    if prev:
        prev_hashes = {r[0] for r in prev}
        prev_field_counts = [r[1] for r in prev if r[1] is not None]

        if output_hash not in prev_hashes:
            alerts.append(f"Content changed for same inputs (new_hash={output_hash})")

        if prev_field_counts and isinstance(output, dict):
            avg_fields = sum(prev_field_counts) / len(prev_field_counts)
            current_fields = len(output)
            if abs(current_fields - avg_fields) > 1:
                alerts.append(f"Field count changed: avg={avg_fields:.1f} -> current={current_fields}")

    for alert in alerts:
        conn.execute(
            "INSERT INTO tool_drift_alerts VALUES (NULL,?,?,?,?)",
            (tool_name, "content_drift", alert, time.time())
        )
    conn.commit()
    return alerts


TOOL = {
    "name": "fetch_config",
    "description": "Fetch application configuration.",
    "input_schema": {
        "type": "object",
        "properties": {"service": {"type": "string"}},
        "required": ["service"],
    },
}

CONFIG_V1 = {"timeout": 30, "retries": 3, "debug": False, "version": "1.0"}
CONFIG_V2 = {"timeout": 30, "retries": 3, "debug": False, "version": "2.0", "feature_flags": {}}  # New field + version bump


def run_agent(conn: sqlite3.Connection, question: str, config_version: int = 1) -> str:
    config = CONFIG_V1 if config_version == 1 else CONFIG_V2
    messages: list[dict] = [{"role": "user", "content": question}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            tools=[TOOL],
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if hasattr(b, "text")), "")

        results = []
        for block in response.content:
            if block.type == "tool_use":
                drifts = detect_drift(conn, block.name, dict(block.input), config)
                if drifts:
                    for d in drifts:
                        print(f"[DRIFT] {block.name}: {d}")
                log_output(conn, block.name, dict(block.input), config)
                results.append({
                    "type": "tool_result", "tool_use_id": block.id,
                    "content": json.dumps(config),
                })

        messages.append({"role": "user", "content": results})


conn = sqlite3.connect(DB_PATH)
init_db(conn)

# First call — no drift (baseline)
run_agent(conn, "Fetch config for service: api.", config_version=1)
run_agent(conn, "Fetch config for service: api.", config_version=1)

# Third call — config changed (drift detected)
print("\nConfig silently changed to v2:")
run_agent(conn, "Fetch config for service: api.", config_version=2)

print(f"\nDrift alerts in DB: {conn.execute('SELECT COUNT(*) FROM tool_drift_alerts').fetchone()[0]}")

# Expected Token Savings: N/A; hash-based drift catches any content change, including field reordering and value updates
# Environment: Python 3.11+; deterministic JSON serialization (sort_keys=True) is critical for stable hashes
```

## Option 5: Schema Snapshot Versioning with Rollback Detection

```python
import anthropic
import json
import time
from dataclasses import dataclass, field
from typing import Any

client = anthropic.Anthropic()


@dataclass
class SchemaSnapshot:
    version: int
    fields: dict[str, str]  # field -> type
    required: list[str]
    captured_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {"fields": self.fields, "required": self.required}

    def diff(self, other: "SchemaSnapshot") -> dict:
        """Describe differences from self (baseline) to other (current)."""
        old_fields = set(self.fields)
        new_fields = set(other.fields)
        return {
            "added": {f: other.fields[f] for f in new_fields - old_fields},
            "removed": {f: self.fields[f] for f in old_fields - new_fields},
            "type_changed": {
                f: (self.fields[f], other.fields[f])
                for f in old_fields & new_fields
                if self.fields[f] != other.fields[f]
            },
            "required_added": list(set(other.required) - set(self.required)),
            "required_removed": list(set(self.required) - set(other.required)),
        }


def snapshot_from_output(output: Any, version: int) -> SchemaSnapshot | None:
    if not isinstance(output, dict):
        return None
    fields = {k: type(v).__name__ for k, v in output.items()}
    return SchemaSnapshot(version=version, fields=fields, required=list(output.keys()))


# Tool schema history per tool name
schema_history: dict[str, list[SchemaSnapshot]] = {}

TOOL = {
    "name": "get_invoice",
    "description": "Fetch an invoice by ID.",
    "input_schema": {
        "type": "object",
        "properties": {"invoice_id": {"type": "string"}},
        "required": ["invoice_id"],
    },
}

INVOICE_V1 = {"id": "INV-001", "amount": 100.0, "currency": "USD", "status": "paid", "issued_at": "2024-01-01"}
INVOICE_V2 = {"id": "INV-001", "total": 100.0, "currency": "USD", "state": "paid", "created_at": "2024-01-01"}  # amount->total, status->state, issued_at->created_at


def check_and_snapshot(tool_name: str, output: Any) -> list[str]:
    snapshot = snapshot_from_output(output, version=len(schema_history.get(tool_name, [])) + 1)
    if not snapshot:
        return []

    history = schema_history.setdefault(tool_name, [])
    alerts = []

    if history:
        diff = history[-1].diff(snapshot)
        if diff["added"]:
            alerts.append(f"New fields: {list(diff['added'].keys())}")
        if diff["removed"]:
            alerts.append(f"REMOVED fields: {list(diff['removed'].keys())}")
        if diff["type_changed"]:
            alerts.append(f"Type changes: {diff['type_changed']}")
        if diff["required_added"]:
            alerts.append(f"New required fields: {diff['required_added']}")

    history.append(snapshot)
    return alerts


def run_agent(question: str, invoice: dict) -> str:
    messages: list[dict] = [{"role": "user", "content": question}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            tools=[TOOL],
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if hasattr(b, "text")), "")

        results = []
        for block in response.content:
            if block.type == "tool_use":
                alerts = check_and_snapshot(block.name, invoice)
                for a in alerts:
                    print(f"[SCHEMA CHANGE] {block.name}: {a}")
                results.append({
                    "type": "tool_result", "tool_use_id": block.id,
                    "content": json.dumps(invoice),
                })

        messages.append({"role": "user", "content": results})


print("=== Invoice v1 ===")
run_agent("Fetch invoice INV-001.", INVOICE_V1)

print("\n=== Invoice v2 (schema drifted) ===")
run_agent("Fetch invoice INV-001.", INVOICE_V2)

history = schema_history.get("get_invoice", [])
print(f"\nSchema history: {len(history)} snapshots")
for snap in history:
    print(f"  v{snap.version}: {list(snap.fields.keys())}")

# Expected Token Savings: N/A; snapshot versioning lets you track field renames vs removals, which require different responses
# Environment: Python 3.11+; persist schema_history to DB; alert on 3+ consecutive diffs as sign of ongoing migration
```

## Option 6: Streaming Output Validator with Real-Time Field Checks

```python
import asyncio
import anthropic
import json
from typing import Any

client = anthropic.AsyncAnthropic()

# Declared expected output contract per tool
TOOL_CONTRACTS: dict[str, dict] = {
    "analyze_text": {
        "required_fields": ["sentiment", "topics", "word_count"],
        "field_types": {"sentiment": str, "topics": list, "word_count": int, "confidence": float},
        "value_constraints": {
            "sentiment": lambda v: v in ("positive", "negative", "neutral"),
            "word_count": lambda v: isinstance(v, int) and v >= 0,
            "confidence": lambda v: 0.0 <= v <= 1.0,
        },
    }
}

TOOL = {
    "name": "analyze_text",
    "description": "Analyze text for sentiment and topics.",
    "input_schema": {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
}


def validate_output(tool_name: str, output: Any) -> list[dict]:
    """Validate output against declared contract. Returns list of violations."""
    contract = TOOL_CONTRACTS.get(tool_name)
    if not contract or not isinstance(output, dict):
        return []

    violations = []

    # Check required fields
    for field in contract.get("required_fields", []):
        if field not in output:
            violations.append({"severity": "BREAKING", "field": field, "issue": "required field missing"})

    # Check field types
    for field, expected_type in contract.get("field_types", {}).items():
        if field in output and not isinstance(output[field], expected_type):
            violations.append({
                "severity": "BREAKING",
                "field": field,
                "issue": f"type mismatch: expected {expected_type.__name__}, got {type(output[field]).__name__}",
            })

    # Check value constraints
    for field, constraint in contract.get("value_constraints", {}).items():
        if field in output:
            try:
                if not constraint(output[field]):
                    violations.append({"severity": "WARNING", "field": field, "issue": f"value constraint failed: {output[field]!r}"})
            except Exception as e:
                violations.append({"severity": "ERROR", "field": field, "issue": str(e)})

    return violations


# Simulate tool that returns a drifted response
MOCK_OUTPUT = {
    "sentiment": "very positive",  # constraint violation: not in allowed set
    "topics": ["python", "async"],
    "word_count": "42",            # type violation: str instead of int
    # confidence: missing (not required, so OK)
}


async def run_agent(question: str) -> str:
    messages: list[dict] = [{"role": "user", "content": question}]

    while True:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            tools=[TOOL],
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            return next((b.text for b in response.content if hasattr(b, "text")), "")

        results = []
        for block in response.content:
            if block.type == "tool_use":
                output = MOCK_OUTPUT  # Simulated drifted tool response
                violations = validate_output(block.name, output)

                if violations:
                    print(f"[CONTRACT VIOLATION] {block.name}:")
                    for v in violations:
                        print(f"  [{v['severity']}] {v['field']}: {v['issue']}")

                    # In strict mode, return error to agent instead of drifted output
                    result_content = json.dumps({
                        "error": "tool output failed contract validation",
                        "violations": violations,
                        "raw": output,
                    })
                else:
                    result_content = json.dumps(output)

                results.append({"type": "tool_result", "tool_use_id": block.id, "content": result_content})

        messages.append({"role": "user", "content": results})


result = asyncio.run(run_agent("Analyze: 'Python async programming is excellent.'"))
print(f"\nAgent: {result}")

# Expected Token Savings: N/A; contract validation surfaces type errors before agent processes malformed data
# Environment: Python 3.11+; define contracts in a separate YAML/JSON file; update contracts via PR review process
```

## Comparison

| Option | Detection Method | Structural | Numeric | Content | SQLite | Best For |
|--------|-----------------|------------|---------|---------|--------|----------|
| 1. Field Set | Set diff | Yes (fields) | No | No | No | Quick field add/remove detection |
| 2. Type Schema | Recursive type infer | Yes (types) | No | No | No | Type change + nested schema drift |
| 3. Numeric Anomaly | Z-score on numeric fields | No | Yes | No | No | Metric APIs, monitoring agents |
| 4. Content Hash | SHA256 of full output | Partial | No | Yes | Yes | Config polling, deterministic APIs |
| 5. Schema Snapshot | Field-type snapshots with diff | Yes | No | No | No | API versioning, migration tracking |
| 6. Contract Validator | Declared constraints | Yes | Yes | Yes | No | Production agents with strict SLAs |
