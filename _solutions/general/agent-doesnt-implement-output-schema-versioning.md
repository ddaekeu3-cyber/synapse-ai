---
layout: solution
title: "Agent Doesn't Implement Output Schema Versioning"
category: general
description: "Agent output format changes silently break downstream consumers — a new field added, a field renamed, or a type changed causes parsers to crash or silently corrupt data with no indication that the schema changed."
tags: [reliability, schema, versioning, backwards-compatibility, api-contracts]
---

## Symptom

A downstream service that was working fine starts failing after an agent prompt update:

```python
# Downstream consumer that worked for months
data = json.loads(agent_response)
user_id = data["userId"]       # KeyError: 'userId' — agent now returns "user_id"
amount  = data["amount"]       # Was a float; now a string "12.50" — type mismatch
```

Or worse: the parse succeeds silently but uses stale field names that map to wrong values.

## Root Cause

Agent output is treated as informal text rather than a versioned API contract. The output format is hardcoded in a prompt with no schema version, no migration path, and no way for consumers to detect that the format changed.

```python
import anthropic
import json

client = anthropic.Anthropic(api_key="sk-live-...")

# v1 system prompt — defines the format implicitly
SYSTEM = """Extract order info as JSON: {"userId": int, "amount": float, "status": string}"""

# Later a developer changes to snake_case without telling downstream consumers:
SYSTEM_V2 = """Extract order info as JSON: {"user_id": int, "amount": string, "status": string}"""

def extract_order(text: str) -> dict:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        system=SYSTEM_V2,  # ← Silent breaking change
        messages=[{"role": "user", "content": text}]
    )
    return json.loads(response.content[0].text)
```

---

## Fix

### Option 1 — Include schema version in every output

Add a `schema_version` field to every output. Consumers check this before parsing.

```python
import anthropic
import json
from typing import Any

client = anthropic.Anthropic(api_key="sk-live-...")

CURRENT_SCHEMA_VERSION = "2.0"

SYSTEM = f"""Extract order information from text.

Return JSON matching this exact schema (version {CURRENT_SCHEMA_VERSION}):
{{
  "schema_version": "{CURRENT_SCHEMA_VERSION}",
  "user_id": <integer>,
  "amount_cents": <integer, amount in cents>,
  "status": <"pending"|"confirmed"|"cancelled">,
  "currency": <"USD"|"EUR"|"GBP">
}}

Always include schema_version. Use amount_cents (integer) not a float amount."""

SUPPORTED_VERSIONS = {"1.0", "2.0"}


def extract_order(text: str) -> dict[str, Any]:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        system=SYSTEM,
        messages=[{"role": "user", "content": text}],
    )

    raw = response.content[0].text.strip()
    data = json.loads(raw)

    version = data.get("schema_version", "unknown")
    if version not in SUPPORTED_VERSIONS:
        raise ValueError(
            f"Unsupported schema version {version!r}. "
            f"Consumer supports: {SUPPORTED_VERSIONS}"
        )

    return data


# Consumer always gets the version before using fields
order = extract_order("John Smith ordered 3 items totalling $47.50, confirmed.")
print(f"v{order['schema_version']} | user={order['user_id']} | {order['amount_cents']}¢")

# Expected Token Savings: version check catches mismatches before silent data corruption
# Environment: any agent whose output is consumed by automated downstream systems
```

---

### Option 2 — Pydantic versioned models with migration layer

Define each schema version as a Pydantic model. Parse the response, detect the version, and migrate to the latest version automatically.

```python
import anthropic
import json
from pydantic import BaseModel, field_validator
from typing import Literal, Union

client = anthropic.Anthropic(api_key="sk-live-...")


class OrderV1(BaseModel):
    schema_version: Literal["1.0"]
    userId: int           # old camelCase field name
    amount: float         # old float type
    status: str


class OrderV2(BaseModel):
    schema_version: Literal["2.0"]
    user_id: int          # snake_case
    amount_cents: int     # integer cents
    status: Literal["pending", "confirmed", "cancelled"]
    currency: str = "USD"


def migrate_v1_to_v2(v1: OrderV1) -> OrderV2:
    return OrderV2(
        schema_version="2.0",
        user_id=v1.userId,
        amount_cents=int(v1.amount * 100),
        status=v1.status if v1.status in ("pending", "confirmed", "cancelled") else "pending",
        currency="USD",
    )


def parse_order(raw_json: str) -> OrderV2:
    """Parse any supported version and return a normalised OrderV2."""
    data = json.loads(raw_json)
    version = data.get("schema_version", "1.0")

    if version == "1.0":
        v1 = OrderV1(**data)
        return migrate_v1_to_v2(v1)
    elif version == "2.0":
        return OrderV2(**data)
    else:
        raise ValueError(f"Unknown schema version: {version!r}")


# Simulate v1 response (legacy)
v1_response = '{"schema_version": "1.0", "userId": 42, "amount": 47.50, "status": "confirmed"}'
order = parse_order(v1_response)
print(f"Migrated: user_id={order.user_id}, amount_cents={order.amount_cents}")

# Simulate v2 response (current)
v2_response = '{"schema_version": "2.0", "user_id": 42, "amount_cents": 4750, "status": "confirmed", "currency": "USD"}'
order = parse_order(v2_response)
print(f"Current:  user_id={order.user_id}, amount_cents={order.amount_cents}")

# Expected Token Savings: migration layer means consumers never need emergency hotfixes
# Environment: long-lived agents with multiple downstream consumers at different upgrade cadences
```

---

### Option 3 — Schema changelog injected into system prompt

Maintain a changelog of breaking changes. Inject the current schema with a diff vs previous version so the model never produces deprecated fields.

```python
import anthropic
import json

client = anthropic.Anthropic(api_key="sk-live-...")

SCHEMA_CHANGELOG = {
    "2.1": {
        "released": "2026-04-01",
        "breaking_changes": [
            "Renamed 'userId' to 'user_id'",
            "Changed 'amount' (float) to 'amount_cents' (integer)",
            "Added required 'currency' field",
            "status now enum: pending|confirmed|cancelled",
        ],
        "schema": {
            "schema_version": "2.1",
            "user_id": "integer",
            "amount_cents": "integer (amount × 100)",
            "status": "pending|confirmed|cancelled",
            "currency": "USD|EUR|GBP",
        }
    }
}

CURRENT_VERSION = "2.1"
CURRENT = SCHEMA_CHANGELOG[CURRENT_VERSION]


def make_system_prompt() -> str:
    schema_str = json.dumps(CURRENT["schema"], indent=2)
    breaking = "\n".join(f"  - {c}" for c in CURRENT["breaking_changes"])
    return f"""Extract order information. Return JSON matching schema v{CURRENT_VERSION} exactly.

Schema v{CURRENT_VERSION}:
{schema_str}

IMPORTANT — Breaking changes from previous versions:
{breaking}

Do NOT use old field names like 'userId' or 'amount' (float).
Always set schema_version to "{CURRENT_VERSION}"."""


def extract_order(text: str) -> dict:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        system=make_system_prompt(),
        messages=[{"role": "user", "content": text}],
    )
    data = json.loads(response.content[0].text.strip())

    # Enforce version
    if data.get("schema_version") != CURRENT_VERSION:
        raise ValueError(f"Model returned wrong schema version: {data.get('schema_version')}")

    return data


order = extract_order("Alice placed order #789 for €23.99, status: pending")
print(order)
# → {"schema_version": "2.1", "user_id": 789, "amount_cents": 2399, "status": "pending", "currency": "EUR"}

# Expected Token Savings: model never produces deprecated fields → no migration cost downstream
# Environment: frequently updated agents where the prompt changelog lives in code
```

---

### Option 4 — Response envelope with backward-compatible extension

Wrap all responses in an envelope. Core fields are stable forever; new data goes in an `extensions` dict so old consumers ignore it safely.

```python
import anthropic
import json
from typing import Any

client = anthropic.Anthropic(api_key="sk-live-...")

ENVELOPE_SYSTEM = """Extract order data. Return a JSON envelope:
{
  "v": 1,
  "core": {
    "user_id": <integer>,
    "amount_cents": <integer>,
    "status": <"pending"|"confirmed"|"cancelled">
  },
  "extensions": {
    <any additional fields the text mentions: currency, items, notes, etc.>
  }
}

"core" fields are FIXED and will never change.
Put any new or uncertain fields in "extensions"."""


def extract_order(text: str) -> dict[str, Any]:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=200,
        system=ENVELOPE_SYSTEM,
        messages=[{"role": "user", "content": text}],
    )
    envelope = json.loads(response.content[0].text.strip())

    # Old consumers only read envelope["core"] — always safe
    # New consumers can also read envelope["extensions"]
    assert envelope.get("v") == 1, f"Unexpected envelope version: {envelope.get('v')}"
    assert "core" in envelope, "Missing 'core' in envelope"

    return envelope


order = extract_order("Bob ordered 5 items totalling $129.99 (USD), confirmed, note: gift wrap")
print("Core fields:", order["core"])
print("Extensions:", order.get("extensions", {}))
# Core fields: {"user_id": ..., "amount_cents": 12999, "status": "confirmed"}
# Extensions: {"currency": "USD", "items": 5, "notes": "gift wrap"}

# Expected Token Savings: no migration code needed for new fields — extensions are additive
# Environment: agents serving consumers at multiple stages of upgrade readiness
```

---

### Option 5 — Schema hash in header for cache-busting

Embed a hash of the current schema in the output. Consumers compare hashes to detect schema drift without maintaining version numbers manually.

```python
import anthropic
import json
import hashlib

client = anthropic.Anthropic(api_key="sk-live-...")

# The canonical schema definition — hash this, not the prompt
CANONICAL_SCHEMA = {
    "user_id": "integer",
    "amount_cents": "integer",
    "status": "pending|confirmed|cancelled",
    "currency": "USD|EUR|GBP",
}

SCHEMA_HASH = hashlib.sha256(
    json.dumps(CANONICAL_SCHEMA, sort_keys=True).encode()
).hexdigest()[:8]

SYSTEM = f"""Extract order info as JSON.
Schema hash: {SCHEMA_HASH}
Fields: {json.dumps(CANONICAL_SCHEMA)}

Return:
{{
  "_schema": "{SCHEMA_HASH}",
  "user_id": <int>,
  "amount_cents": <int>,
  "status": "pending|confirmed|cancelled",
  "currency": "USD|EUR|GBP"
}}"""


KNOWN_SCHEMA_HASHES = {SCHEMA_HASH}  # expand when schema evolves


def extract_order(text: str) -> dict:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        system=SYSTEM,
        messages=[{"role": "user", "content": text}],
    )
    data = json.loads(response.content[0].text.strip())

    received_hash = data.get("_schema", "")
    if received_hash not in KNOWN_SCHEMA_HASHES:
        raise ValueError(
            f"Schema hash mismatch: got {received_hash!r}, "
            f"expected one of {KNOWN_SCHEMA_HASHES}"
        )

    return data


order = extract_order("Carol bought 2 items for £35.00, status confirmed")
print(f"Schema: {order['_schema']} | {order['amount_cents']}p {order['currency']}")

# Expected Token Savings: hash check catches prompt drift before silent corruption
# Environment: CI pipelines that redeploy prompts; hash comparison is a deploy-gate check
```

---

### Option 6 — Multi-version adapter with automatic negotiation

Consumer sends its supported schema versions; agent returns the highest mutually supported version. Enables gradual migration.

```python
import anthropic
import json
from typing import Any

client = anthropic.Anthropic(api_key="sk-live-...")

SUPPORTED_VERSIONS_BY_SERVER = ["1.0", "2.0", "2.1"]

SCHEMAS = {
    "1.0": '{"userId": int, "amount": float, "status": str}',
    "2.0": '{"schema_version": "2.0", "user_id": int, "amount_cents": int, "status": str}',
    "2.1": '{"schema_version": "2.1", "user_id": int, "amount_cents": int, "status": str, "currency": str}',
}


def negotiate_version(client_supported: list[str]) -> str:
    """Pick highest version both sides support."""
    common = set(client_supported) & set(SUPPORTED_VERSIONS_BY_SERVER)
    if not common:
        raise ValueError(
            f"No common schema version. Client: {client_supported}, "
            f"Server: {SUPPORTED_VERSIONS_BY_SERVER}"
        )
    return sorted(common)[-1]  # Highest common version


def extract_order(text: str, client_supported_versions: list[str]) -> dict[str, Any]:
    version = negotiate_version(client_supported_versions)
    schema = SCHEMAS[version]

    system = f"""Extract order info. Return JSON matching schema version {version}:
{schema}
Always set schema_version to "{version}" (except v1.0 which has no schema_version field)."""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        system=system,
        messages=[{"role": "user", "content": text}],
    )

    data = json.loads(response.content[0].text.strip())
    data["_negotiated_version"] = version
    return data


# Old consumer only knows v1.0
old_result = extract_order("Dave ordered for $55, confirmed", client_supported_versions=["1.0"])
print(f"Old consumer got v{old_result['_negotiated_version']}: {old_result}")

# New consumer supports up to v2.1
new_result = extract_order("Eve ordered for €22, pending", client_supported_versions=["1.0", "2.0", "2.1"])
print(f"New consumer got v{new_result['_negotiated_version']}: {new_result}")

# Expected Token Savings: old consumers get simpler schemas (fewer fields = fewer tokens)
# Environment: agents with heterogeneous consumers upgrading at different rates
```

---

## Comparison

| Option | Breaking Change Detection | Auto-Migration | Additive-Safe | Negotiation | Complexity |
|--------|--------------------------|----------------|---------------|-------------|------------|
| 1 | Version field check | No | No | No | Low |
| 2 | Pydantic + migration | Yes | No | No | Medium |
| 3 | Changelog in prompt | Prevents breaking | No | No | Low |
| 4 | Envelope + extensions | Core is stable | Yes | No | Medium |
| 5 | Schema hash | Hash mismatch | No | No | Low |
| 6 | Version negotiation | No | No | Yes | High |

**Recommended starting point:** Option 1 — add `schema_version` to every output immediately. Zero downstream cost, immediate visibility into schema drift. Add Option 2's Pydantic migration layer when you have 2+ active consumers on different versions.
