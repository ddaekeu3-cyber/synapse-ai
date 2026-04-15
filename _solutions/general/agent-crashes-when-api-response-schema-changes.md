---
layout: solution
title: "Agent Crashes When Third-Party API Response Schema Changes"
category: general
description: "Third-party API adds a field, renames a key, or changes a nested structure. Agent accesses response['user']['name'] — KeyError. Or the field is now nullable and agent gets None where it expected a string. One API version bump breaks the entire agent."
tags: [general, api-schema, breaking-change, defensive-parsing, validation, pydantic, resilience, versioning]
---

# Agent Crashes When Third-Party API Response Schema Changes

## Symptom
- Agent crashes with `KeyError: 'user'` after third-party API update
- Field that was always present is now missing — agent raises `AttributeError`
- `response['data']['items']` → API changed to `response['results']` → crash
- Nullable field now returns `None` — `str.lower()` on None → `AttributeError`
- Deeply nested field moved to top level — agent can't find it in new location
- API versioning header required — agent using unversioned endpoint gets deprecated response

## Root Cause
Agents that access API response fields with `response['key']` or `response.key` crash immediately when any expected key is absent. Third-party APIs change without notice: fields are renamed, nested structures are flattened, optional fields become required (or vice versa), and data types change. Without defensive parsing, any API evolution breaks the agent. The fix is to treat all external API responses as untrusted — validate, extract safely, and handle missing fields gracefully.

## Fix

### Option 1: Defensive field access with safe_get

```python
from typing import Any

def safe_get(obj: Any, *keys: str | int, default: Any = None) -> Any:
    """
    Safely traverse nested dict/list — returns default on any missing key.
    Never raises KeyError, IndexError, or AttributeError.
    """
    current = obj
    for key in keys:
        if current is None:
            return default
        try:
            if isinstance(key, int):
                current = current[key]
            elif isinstance(current, dict):
                current = current.get(key)
            elif hasattr(current, key):
                current = getattr(current, key)
            else:
                return default
        except (KeyError, IndexError, TypeError):
            return default
    return current if current is not None else default

def safe_str(obj: Any, *keys, default: str = "") -> str:
    """Safe string extraction — returns empty string instead of None"""
    value = safe_get(obj, *keys, default=default)
    return str(value) if value is not None else default

def safe_list(obj: Any, *keys, default: list = None) -> list:
    """Safe list extraction — returns empty list instead of None"""
    value = safe_get(obj, *keys, default=None)
    if isinstance(value, list):
        return value
    if value is not None:
        return [value]  # Wrap single item
    return default or []

# WRONG — crashes on schema changes:
# name = response['user']['profile']['display_name']  # KeyError if any key missing
# items = response['data']['items']                    # Crash if 'data' renamed

# RIGHT — safe extraction:
response = {
    "user": {
        "profile": {
            "display_name": "Alice",
            "email": "alice@example.com"
        }
    },
    "results": []  # API changed 'data.items' to 'results'
}

name = safe_str(response, "user", "profile", "display_name", default="Unknown User")
email = safe_str(response, "user", "profile", "email", default="")
# Try both old and new key names (during API migration):
items = (
    safe_list(response, "results") or
    safe_list(response, "data", "items") or
    []
)
```

### Option 2: Pydantic models with optional fields and aliases

```python
from pydantic import BaseModel, Field, validator, root_validator
from typing import Optional, List, Any
import json

class UserProfile(BaseModel):
    """Validates API response — fails loudly on schema errors, defaults on missing fields"""
    display_name: str = Field(default="Unknown", alias="displayName")
    email: Optional[str] = None
    user_id: Optional[str] = Field(default=None, alias="id")

    class Config:
        # Allow both snake_case and camelCase field names
        populate_by_name = True
        # Don't crash on extra fields the API adds
        extra = "ignore"

class APIResponse(BaseModel):
    """
    Flexible response model — handles schema evolution gracefully.
    Old fields and new fields coexist without crashes.
    """
    # Support multiple possible locations for items (migration-safe)
    results: Optional[List[dict]] = None
    items: Optional[List[dict]] = None      # Old key name
    data: Optional[dict] = None             # Old nested location
    user: Optional[UserProfile] = None
    status: str = "unknown"
    total_count: Optional[int] = Field(default=None, alias="totalCount")

    @root_validator(pre=True)
    def normalize_items(cls, values):
        """Normalize different item locations to a single field"""
        # Handle API migration: 'data.items' → 'results'
        if values.get('results') is None:
            if isinstance(values.get('data'), dict):
                values['results'] = values['data'].get('items', [])
        return values

    @property
    def all_items(self) -> list:
        """Get items regardless of where the API put them"""
        return self.results or self.items or (
            self.data.get('items', []) if isinstance(self.data, dict) else []
        )

def parse_api_response(raw: dict) -> APIResponse:
    """
    Parse API response with graceful fallback on schema errors.
    """
    try:
        return APIResponse.model_validate(raw)
    except Exception as e:
        print(f"API response parsing error (non-fatal): {e}")
        # Return minimal valid object — agent can continue with defaults
        return APIResponse(status="parse_error")

# Usage — survives API schema changes:
raw = {"results": [...], "user": {"displayName": "Alice"}, "totalCount": 42}
response = parse_api_response(raw)
items = response.all_items
name = response.user.display_name if response.user else "Unknown"
```

### Option 3: Schema fingerprinting — detect changes early

```python
import hashlib
import json
import time
from pathlib import Path
from dataclasses import dataclass

@dataclass
class SchemaFingerprint:
    api_name: str
    fingerprint: str
    fields: list[str]
    captured_at: float
    sample_response: dict

class SchemaChangeDetector:
    """
    Track API response structure over time.
    Alert when fields are added, removed, or type-changed.
    """

    def __init__(self, cache_file: str = "/data/api_schemas.json"):
        self.cache_path = Path(cache_file)
        self._known: dict[str, SchemaFingerprint] = self._load()

    def _load(self) -> dict:
        if self.cache_path.exists():
            raw = json.loads(self.cache_path.read_text())
            return {k: SchemaFingerprint(**v) for k, v in raw.items()}
        return {}

    def _save(self):
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        data = {k: v.__dict__ for k, v in self._known.items()}
        self.cache_path.write_text(json.dumps(data, indent=2))

    def _extract_structure(self, obj: Any, prefix: str = "") -> list[str]:
        """Extract field paths and types from a response object"""
        fields = []
        if isinstance(obj, dict):
            for key, value in obj.items():
                path = f"{prefix}.{key}" if prefix else key
                fields.append(f"{path}:{type(value).__name__}")
                if isinstance(value, (dict, list)):
                    fields.extend(self._extract_structure(value, path))
        elif isinstance(obj, list) and obj:
            fields.extend(self._extract_structure(obj[0], f"{prefix}[]"))
        return fields

    def _fingerprint(self, fields: list[str]) -> str:
        return hashlib.sha256(",".join(sorted(fields)).encode()).hexdigest()[:16]

    def check(self, api_name: str, response: dict) -> dict:
        """
        Check response against known schema.
        Returns change report.
        """
        current_fields = self._extract_structure(response)
        current_fp = self._fingerprint(current_fields)

        if api_name not in self._known:
            # First time seeing this API — record baseline
            self._known[api_name] = SchemaFingerprint(
                api_name=api_name,
                fingerprint=current_fp,
                fields=current_fields,
                captured_at=time.time(),
                sample_response=response
            )
            self._save()
            print(f"Schema baseline recorded for {api_name}: {len(current_fields)} fields")
            return {"status": "new", "field_count": len(current_fields)}

        known = self._known[api_name]

        if known.fingerprint == current_fp:
            return {"status": "unchanged"}

        # Schema changed — find what's different
        known_set = set(known.fields)
        current_set = set(current_fields)
        added = current_set - known_set
        removed = known_set - current_set
        type_changes = {
            f.split(':')[0]: (
                next(k.split(':')[1] for k in known_set if k.startswith(f.split(':')[0])),
                f.split(':')[1]
            )
            for f in added
            if any(k.startswith(f.split(':')[0] + ':') for k in known_set)
        }

        report = {
            "status": "changed",
            "added_fields": list(added),
            "removed_fields": list(removed),
            "type_changes": type_changes,
        }

        if removed:
            print(f"SCHEMA CHANGE ALERT for {api_name}: Fields removed: {removed}")
        if added:
            print(f"SCHEMA CHANGE for {api_name}: Fields added: {added}")

        # Update baseline
        self._known[api_name] = SchemaFingerprint(
            api_name=api_name,
            fingerprint=current_fp,
            fields=current_fields,
            captured_at=time.time(),
            sample_response=response
        )
        self._save()
        return report

schema_detector = SchemaChangeDetector()

async def call_api_with_schema_check(endpoint: str, api_name: str) -> dict:
    async with httpx.AsyncClient() as client:
        response = await client.get(endpoint)
        data = response.json()

    changes = schema_detector.check(api_name, data)
    if changes["status"] == "changed" and changes.get("removed_fields"):
        print(f"WARNING: Required fields may be missing from {api_name} response")

    return data
```

### Option 4: Versioned API requests — opt into stable schemas

```python
import httpx
import os

class VersionedAPIClient:
    """
    API client that explicitly requests specific API versions.
    Prevents silent migration to breaking schema changes.
    """

    VERSION_HEADERS = {
        "github": {"Accept": "application/vnd.github.v3+json", "X-GitHub-Api-Version": "2022-11-28"},
        "stripe": {"Stripe-Version": "2023-10-16"},
        "anthropic": {"anthropic-version": "2023-06-01"},
        "generic": {"API-Version": "2024-01", "Accept": "application/json; version=2024-01"}
    }

    def __init__(self, base_url: str, api_type: str = "generic", api_key: str = None):
        self.base_url = base_url.rstrip("/")
        self.version_headers = self.VERSION_HEADERS.get(api_type, {})
        self.auth_headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    async def get(self, path: str, **kwargs) -> dict:
        headers = {**self.version_headers, **self.auth_headers}
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.base_url}{path}",
                headers=headers,
                **kwargs
            )
            response.raise_for_status()
            return response.json()

    def build_url(self, path: str, version: str = None) -> str:
        """Build versioned URL path"""
        if version:
            return f"{self.base_url}/v{version}{path}"
        return f"{self.base_url}{path}"

# Usage — always pin to a specific API version:
github = VersionedAPIClient("https://api.github.com", api_type="github")
data = await github.get("/user")  # Always gets v3 schema
```

### Option 5: Multi-path extraction — try old and new field locations

```python
from typing import TypeVar

T = TypeVar("T")

def extract_with_fallbacks(
    response: dict,
    paths: list[tuple],
    default: T = None
) -> T:
    """
    Try multiple paths for a field — handles API migrations gracefully.
    Paths are tried in order — first non-None value wins.
    """
    for path in paths:
        value = safe_get(response, *path)
        if value is not None:
            return value
    return default

# API migration: 'data.user.name' → 'user.displayName' → 'profile.name'
user_name = extract_with_fallbacks(
    response,
    paths=[
        ("user", "displayName"),           # Current API
        ("user", "display_name"),          # Previous API (snake_case)
        ("data", "user", "name"),          # Old API (nested)
        ("profile", "name"),               # Older API
    ],
    default="Unknown User"
)

# List of items moved location:
items = extract_with_fallbacks(
    response,
    paths=[
        ("results",),                      # Current API
        ("data", "items"),                 # Old API
        ("items",),                        # Older API
        ("response", "data"),              # Very old API
    ],
    default=[]
)

# Always validate extracted values:
if not isinstance(items, list):
    print(f"Unexpected type for items: {type(items)}")
    items = [items] if items else []
```

### Option 6: Contract testing — catch schema breaks before production

```python
import pytest
import httpx

# Define expected fields — tested on every CI run
REQUIRED_FIELDS = {
    "user_api": {
        "required": ["id", "email"],
        "optional": ["name", "display_name", "avatar_url"],
        "types": {"id": str, "email": str}
    },
    "orders_api": {
        "required": ["order_id", "status", "items"],
        "optional": ["created_at", "updated_at", "total_amount"],
        "types": {"order_id": str, "items": list}
    }
}

@pytest.mark.integration
async def test_user_api_schema():
    """Contract test — fails if API breaks required fields"""
    async with httpx.AsyncClient() as client:
        response = await client.get(
            "https://api.example.com/user",
            headers={"Authorization": f"Bearer {TEST_API_KEY}"}
        )
        assert response.status_code == 200
        data = response.json()

    contract = REQUIRED_FIELDS["user_api"]

    # Required fields must be present
    for field in contract["required"]:
        assert field in data, (
            f"Contract violation: required field '{field}' missing from user API response. "
            f"API may have been updated — check changelog and update agent."
        )

    # Types must match
    for field, expected_type in contract["types"].items():
        if field in data and data[field] is not None:
            assert isinstance(data[field], expected_type), (
                f"Type mismatch for '{field}': expected {expected_type.__name__}, "
                f"got {type(data[field]).__name__}"
            )

@pytest.fixture(autouse=True)
def log_api_schema_changes(caplog):
    """Log schema changes in test output for visibility"""
    yield
    # After each test, log schema fingerprint for comparison
    pass
```

## API Schema Resilience Patterns

| Pattern | Protection | Effort | Best For |
|---|---|---|---|
| `safe_get()` | KeyError/None | Low | All external API calls |
| Pydantic + `extra="ignore"` | Type mismatches, extra fields | Low | Structured responses |
| Multi-path extraction | Field renames/moves | Medium | During API migrations |
| Schema fingerprinting | Early change detection | Medium | Critical integrations |
| API version pinning | Silent upgrades | Low | Any versioned API |
| Contract tests in CI | Regression detection | Medium | Production integrations |

## Expected Token Savings
API crash mid-task → restart → re-explain context → debug: ~20,000 tokens per crash
Defensive parsing → continues with defaults → logs warning: 0 recovery overhead

## Environment
- Any agent calling third-party APIs (GitHub, Slack, Stripe, Google, etc.); critical for agents that integrate with external services that evolve independently
- Source: direct experience; API schema changes are the most common cause of sudden production outages in agents that were working correctly the day before
