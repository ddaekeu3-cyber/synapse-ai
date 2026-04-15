---
layout: solution
title: "Third-Party API Schema Changed — Agent Breaks Silently"
category: tool-failure
description: "External API adds a required field, renames a key, or changes a value's type. Agent continues sending old request format. Requests fail with 400 or return incomplete data. Agent doesn't detect the schema drift."
tags: [api, schema, breaking-change, drift, validation, versioning, contract-testing]
---

# Third-Party API Schema Changed — Agent Breaks Silently

## Symptom
- API returns 400 Bad Request with cryptic message after working for months
- Response shape changed: agent accesses `.user.name` but API now returns `.user.full_name`
- API added a required field — all requests fail without it
- Numeric field became a string — downstream parsing crashes silently
- Works in dev (old API version) but fails in prod (new API version)
- API deprecated an endpoint — silent redirect to new endpoint with different schema

## Root Cause
Third-party APIs evolve. When an API changes its request or response schema, agents that hard-code field names, types, or endpoint paths fail. Without schema validation or change detection, the agent silently sends invalid requests or parses responses incorrectly. Schema drift is especially hard to catch because it doesn't fail at import time — only at runtime with real data.

## Fix

### Option 1: Response schema validation on every call

```python
from pydantic import BaseModel, ValidationError, Field
from typing import Optional
import httpx

class UserResponse(BaseModel):
    """Expected response schema — raises on unexpected shape"""
    id: str
    email: str
    name: str = Field(alias="full_name")  # Handle field renames
    created_at: str
    status: str

    class Config:
        populate_by_name = True

async def get_user(user_id: str, client: httpx.AsyncClient) -> UserResponse:
    """
    Fetch user and validate response schema.
    Fails immediately with clear message if API schema changed.
    """
    response = await client.get(f"/users/{user_id}")
    response.raise_for_status()
    raw = response.json()

    try:
        return UserResponse(**raw)
    except ValidationError as e:
        # Schema mismatch — detect and report clearly
        print(f"API schema mismatch detected for /users/{user_id}")
        print(f"Response received: {list(raw.keys())}")
        print(f"Expected schema: {list(UserResponse.__fields__.keys())}")
        print(f"Validation errors: {e}")
        raise RuntimeError(
            f"API response schema has changed. "
            f"Update UserResponse model to match new schema.\n{e}"
        ) from e
```

### Option 2: Schema fingerprint — detect changes automatically

```python
import hashlib
import json
from pathlib import Path
from datetime import datetime

class SchemaChangeDetector:
    """
    Track API response schema fingerprint.
    Alert when the schema changes unexpectedly.
    """

    def __init__(self, baseline_file: str = "api_schema_baseline.json"):
        self.baseline_file = Path(baseline_file)
        self.baselines: dict = {}
        if self.baseline_file.exists():
            self.baselines = json.loads(self.baseline_file.read_text())

    def _schema_fingerprint(self, obj: dict | list, depth: int = 3) -> dict:
        """Extract schema structure (keys and types) from a response"""
        if isinstance(obj, dict) and depth > 0:
            return {k: self._schema_fingerprint(v, depth - 1) for k, v in obj.items()}
        elif isinstance(obj, list) and obj and depth > 0:
            return [self._schema_fingerprint(obj[0], depth - 1)]
        else:
            return type(obj).__name__

    def _hash(self, schema: dict) -> str:
        return hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest()[:16]

    def check(self, endpoint: str, response: dict) -> bool:
        """
        Returns True if schema matches baseline.
        Returns False and logs warning if schema changed.
        """
        current_schema = self._schema_fingerprint(response)
        current_hash = self._hash(current_schema)

        if endpoint not in self.baselines:
            # First time seeing this endpoint — establish baseline
            self.baselines[endpoint] = {
                "hash": current_hash,
                "schema": current_schema,
                "established": datetime.utcnow().isoformat()
            }
            self.baseline_file.write_text(json.dumps(self.baselines, indent=2))
            print(f"Schema baseline established for {endpoint}")
            return True

        baseline = self.baselines[endpoint]
        if baseline["hash"] != current_hash:
            print(f"SCHEMA CHANGE DETECTED: {endpoint}")
            print(f"  Baseline schema: {json.dumps(baseline['schema'], indent=2)}")
            print(f"  Current schema:  {json.dumps(current_schema, indent=2)}")
            print(f"  Established: {baseline['established']}")
            return False

        return True

detector = SchemaChangeDetector()

async def monitored_api_call(url: str, client) -> dict:
    response = await client.get(url)
    data = response.json()
    detector.check(url, data)  # Alert if schema changed
    return data
```

### Option 3: Pin API version and warn on version drift

```python
import httpx

class VersionedAPIClient:
    """
    API client that pins to a specific API version.
    Warns when server reports a newer version.
    """

    def __init__(self, base_url: str, api_version: str = "2024-01"):
        self.base_url = base_url
        self.pinned_version = api_version
        self.client = httpx.AsyncClient(
            base_url=base_url,
            headers={
                "Anthropic-Version": api_version,
                "Content-Type": "application/json",
            }
        )

    async def request(self, method: str, path: str, **kwargs) -> dict:
        response = await self.client.request(method, path, **kwargs)

        # Check if server is sending a deprecation warning
        deprecated = response.headers.get("Deprecation")
        sunset = response.headers.get("Sunset")
        if deprecated or sunset:
            print(
                f"API DEPRECATION WARNING for {path}:\n"
                f"  Deprecation: {deprecated}\n"
                f"  Sunset: {sunset}\n"
                f"  Current pinned version: {self.pinned_version}\n"
                f"  Update the client before the sunset date."
            )

        # Check for version mismatch headers
        server_version = response.headers.get("API-Version") or \
                         response.headers.get("X-API-Version")
        if server_version and server_version != self.pinned_version:
            print(
                f"API version mismatch on {path}:\n"
                f"  Client sends: {self.pinned_version}\n"
                f"  Server reports: {server_version}"
            )

        response.raise_for_status()
        return response.json()
```

### Option 4: Contract test — verify API shape on startup

```python
import asyncio
import httpx

EXPECTED_CONTRACTS = [
    {
        "name": "list users",
        "method": "GET",
        "path": "/users",
        "params": {"limit": 1},
        "required_response_keys": ["users", "total", "page"],
        "required_item_keys": ["id", "email", "created_at"],
        "item_path": "users",
    },
    {
        "name": "create order",
        "method": "POST",
        "path": "/orders",
        "body": {"product_id": "test", "quantity": 1},
        "required_response_keys": ["order_id", "status", "created_at"],
    }
]

async def run_contract_tests(base_url: str) -> list[str]:
    """
    Verify API contracts at startup — fail fast if API schema changed.
    Returns list of failures (empty = all contracts pass).
    """
    failures = []

    async with httpx.AsyncClient(base_url=base_url) as client:
        for contract in EXPECTED_CONTRACTS:
            try:
                if contract["method"] == "GET":
                    resp = await client.get(
                        contract["path"],
                        params=contract.get("params", {})
                    )
                else:
                    resp = await client.request(
                        contract["method"],
                        contract["path"],
                        json=contract.get("body", {})
                    )

                if resp.status_code >= 400:
                    failures.append(
                        f"Contract '{contract['name']}': HTTP {resp.status_code}"
                    )
                    continue

                data = resp.json()
                # Check top-level required keys
                for key in contract.get("required_response_keys", []):
                    if key not in data:
                        failures.append(
                            f"Contract '{contract['name']}': missing key '{key}' in response"
                        )

                # Check item-level required keys
                item_path = contract.get("item_path")
                if item_path and data.get(item_path):
                    item = data[item_path][0]
                    for key in contract.get("required_item_keys", []):
                        if key not in item:
                            failures.append(
                                f"Contract '{contract['name']}': "
                                f"missing key '{key}' in {item_path}[0]"
                            )

            except Exception as e:
                failures.append(f"Contract '{contract['name']}': {type(e).__name__}: {e}")

    return failures

# Run at agent startup:
failures = await run_contract_tests("https://api.example.com")
if failures:
    for f in failures:
        print(f"CONTRACT FAILURE: {f}")
    raise RuntimeError("API contract tests failed — schema may have changed")
```

### Option 5: Graceful field fallback with multiple aliases

```python
from typing import Any

def extract_field(data: dict, *aliases: str, default: Any = None) -> Any:
    """
    Try multiple field names in order — handles API field renames gracefully.
    Logs which alias was used so schema drift is visible.
    """
    for alias in aliases:
        # Support dot-path notation: "user.full_name"
        parts = alias.split(".")
        value = data
        for part in parts:
            if isinstance(value, dict):
                value = value.get(part)
            else:
                value = None
                break
        if value is not None:
            if alias != aliases[0]:
                print(f"Schema drift: used fallback field '{alias}' instead of '{aliases[0]}'")
            return value

    return default

# Usage: tries "full_name" first, falls back to "name", then "display_name"
user_name = extract_field(user_data, "full_name", "name", "display_name", default="Unknown")

# Works when API renames field without breaking agent:
# Old API: {"name": "Alice"}          → uses "name"
# New API: {"full_name": "Alice"}     → uses "full_name"
# Both: {"full_name": "Alice", "name": "Alice"} → uses "full_name" (primary)
```

### Option 6: Changelog monitoring and alerting

```python
import httpx
import hashlib

CHANGELOG_URLS = {
    "stripe": "https://stripe.com/docs/upgrades",
    "github": "https://developer.github.com/changes/",
    # Add your API providers here
}

async def check_api_changelog(api_name: str, last_seen_hash: str = None) -> dict:
    """
    Fetch API changelog page and detect if it changed since last check.
    Use in a scheduled job to get early warning of upcoming changes.
    """
    url = CHANGELOG_URLS.get(api_name)
    if not url:
        return {"checked": False, "reason": "No changelog URL configured"}

    async with httpx.AsyncClient() as client:
        resp = await client.get(url, timeout=15)
        current_hash = hashlib.md5(resp.text.encode()).hexdigest()

    changed = last_seen_hash is not None and current_hash != last_seen_hash
    return {
        "api": api_name,
        "changed": changed,
        "current_hash": current_hash,
        "url": url,
    }
```

## Schema Change Risk by Change Type

| Change type | Impact | Detectability | Fix |
|---|---|---|---|
| Field renamed | Silent wrong data | Medium — pydantic error | Multiple aliases + schema validation |
| Field added (required) | 400 on requests | Easy — immediate failure | Contract tests catch it |
| Field type changed (str→int) | Silent parse error | Hard — silent | Response schema validation |
| Field removed | KeyError/AttributeError | Easy — immediate crash | Defensive `get()` with defaults |
| Endpoint path changed | 404 | Easy — immediate | URL constants, not strings |
| Auth scheme changed | 401 | Easy — immediate | Auth validation on startup |

## Expected Token Savings
Silent schema drift causing wrong data for 1 week before detection: priceless (data integrity) + ~50,000 debug tokens
Schema validation on every call + contract tests: catches drift in first failed call

## Environment
- Any agent integrating with third-party APIs that evolve independently of the agent codebase
- Source: direct experience; API schema drift is the leading cause of "worked yesterday, broken today" incidents
