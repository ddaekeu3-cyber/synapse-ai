---
title: "Agent Doesn't Implement Output Field Redaction Before Delivery"
description: "Agents that return structured outputs may include sensitive fields — API keys, tokens, internal IDs, private data — that should never reach the end user. Output field redaction scrubs or masks specific fields before the response leaves the agent boundary."
difficulty: intermediate
category: security
tags: [security, redaction, sensitive-data, output-filtering, structured-output, data-privacy]
---

## Problem

An agent calls internal tools and assembles a structured response that includes the full data returned by those tools — database records with internal IDs, API responses with tokens, logs with file paths, or user records with private fields. Without output redaction, all of that data flows directly to the caller. A single misconfigured tool or prompt injection can leak credentials, internal architecture details, or user PII through the agent's own output.

```python
# BAD: raw tool output sent directly to user
async def get_user_profile(user_id: str) -> dict:
    record = await db.get_user(user_id)
    return record  # includes password_hash, internal_id, auth_token, etc.
```

## Solution 1: Field Allowlist Redaction

Define which fields are permitted in output; strip everything else.

```python
import asyncio
import re
from anthropic import AsyncAnthropic
from typing import Any

client = AsyncAnthropic()

# Fields that are SAFE to expose — everything else is stripped
OUTPUT_ALLOWLISTS: dict[str, set[str]] = {
    "user": {"id", "username", "email", "display_name", "created_at", "role"},
    "order": {"order_id", "status", "total", "items", "created_at"},
    "document": {"doc_id", "title", "author", "created_at", "tags"},
}

# Fields that should be masked even when allowed (partial visibility)
MASK_PATTERNS: dict[str, str] = {
    "email": r"(.{2}).+(@.+)",         # jo***@example.com
    "phone": r"(\d{3})\d+(\d{2})",     # 555***12
}

def mask_value(field: str, value: str) -> str:
    pattern = MASK_PATTERNS.get(field)
    if not pattern:
        return value
    def replacer(m: re.Match) -> str:
        parts = list(m.groups())
        # Mask everything between first and last captured group
        middle = "*" * max(3, len(value) - len(parts[0]) - len(parts[-1]))
        return parts[0] + middle + parts[-1]
    try:
        return re.sub(pattern, replacer, str(value))
    except Exception:
        return "***"

def redact_by_allowlist(data: dict, entity_type: str) -> dict:
    allowed = OUTPUT_ALLOWLISTS.get(entity_type, set())
    redacted: dict[str, Any] = {}
    for key, value in data.items():
        if key not in allowed:
            continue
        if isinstance(value, str) and key in MASK_PATTERNS:
            redacted[key] = mask_value(key, value)
        else:
            redacted[key] = value
    return redacted

def redact_nested(data: Any, entity_type: str) -> Any:
    if isinstance(data, dict):
        return redact_by_allowlist(data, entity_type)
    elif isinstance(data, list):
        return [redact_nested(item, entity_type) for item in data]
    return data

# Simulated database
def fetch_user_record(user_id: str) -> dict:
    return {
        "id": user_id,
        "username": "alice",
        "email": "alice@example.com",
        "display_name": "Alice Smith",
        "role": "editor",
        "password_hash": "$2b$12$xxxxxxxxxxxxxxxxxxxxxxxxxxx",
        "auth_token": "tok_secret_abc123",
        "internal_shard_id": "shard-7",
        "created_at": "2024-01-15",
        "last_login_ip": "192.168.1.42",
        "stripe_customer_id": "cus_internal_xyz",
    }

async def safe_get_user(user_id: str) -> dict:
    raw = fetch_user_record(user_id)
    return redact_by_allowlist(raw, "user")

async def main():
    raw = fetch_user_record("usr-001")
    safe = await safe_get_user("usr-001")

    print(f"Raw fields ({len(raw)}): {list(raw.keys())}")
    print(f"Safe fields ({len(safe)}): {list(safe.keys())}")
    print(f"Safe data: {safe}")

asyncio.run(main())
```

## Solution 2: Pattern-Based Secret Scrubbing

Scan all output text for known secret patterns and replace matches with redaction tokens.

```python
import asyncio
import re
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

# Common secret patterns
SECRET_PATTERNS: list[tuple[str, re.Pattern, str]] = [
    ("api_key",         re.compile(r"\b(sk|pk|rk|ak|api)[-_][a-zA-Z0-9]{20,}\b"), "[REDACTED_API_KEY]"),
    ("bearer_token",    re.compile(r"\bBearer\s+[a-zA-Z0-9\-._~+/]+=*\b"), "Bearer [REDACTED_TOKEN]"),
    ("jwt",             re.compile(r"\beyJ[a-zA-Z0-9\-_]+\.[a-zA-Z0-9\-_]+\.[a-zA-Z0-9\-_]+\b"), "[REDACTED_JWT]"),
    ("aws_key",         re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED_AWS_KEY]"),
    ("aws_secret",      re.compile(r"\b[a-zA-Z0-9/+]{40}\b(?=.*aws|.*secret)", re.IGNORECASE), "[REDACTED_AWS_SECRET]"),
    ("private_ip",      re.compile(r"\b(10|172\.(1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3}\b"), "[INTERNAL_IP]"),
    ("password_field",  re.compile(r'"password[^"]*"\s*:\s*"[^"]{4,}"', re.IGNORECASE), '"password": "[REDACTED]"'),
    ("hash_field",      re.compile(r'"(?:password_hash|pwd_hash|hashed_pwd)[^"]*"\s*:\s*"[^"]{8,}"', re.IGNORECASE), '"hash": "[REDACTED]"'),
    ("credit_card",     re.compile(r"\b(?:4[0-9]{12}(?:[0-9]{3})?|5[1-5][0-9]{14}|3[47][0-9]{13})\b"), "[REDACTED_CARD]"),
    ("ssn",             re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[REDACTED_SSN]"),
]

@dataclass_or_dict = None  # avoid import for clarity

def scrub_text(text: str) -> tuple[str, list[str]]:
    """Scrub secrets from text. Returns (scrubbed_text, list_of_redaction_types)."""
    redacted_types = []
    for name, pattern, replacement in SECRET_PATTERNS:
        new_text, count = pattern.subn(replacement, text)
        if count > 0:
            text = new_text
            redacted_types.append(f"{name}({count})")
    return text, redacted_types

def scrub_dict(data: dict | list | str | any) -> tuple[any, list[str]]:
    """Recursively scrub all string values in a data structure."""
    all_redacted = []
    if isinstance(data, str):
        scrubbed, redacted = scrub_text(data)
        return scrubbed, redacted
    elif isinstance(data, dict):
        result = {}
        for k, v in data.items():
            scrubbed_v, redacted = scrub_dict(v)
            result[k] = scrubbed_v
            all_redacted.extend(redacted)
        return result, all_redacted
    elif isinstance(data, list):
        result = []
        for item in data:
            scrubbed_item, redacted = scrub_dict(item)
            result.append(scrubbed_item)
            all_redacted.extend(redacted)
        return result, all_redacted
    return data, []

async def safe_agent_response(user_query: str, raw_data: dict) -> dict:
    # Scrub the raw data before giving it to the model
    scrubbed_data, pre_redactions = scrub_dict(raw_data)
    if pre_redactions:
        print(f"[Pre-scrub] Redacted from tool output: {pre_redactions}")

    # Also scrub the model's final response
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": f"{user_query}\n\nData: {scrubbed_data}"
        }]
    )
    raw_output = response.content[0].text if response.content else ""
    final_output, post_redactions = scrub_text(raw_output)
    if post_redactions:
        print(f"[Post-scrub] Redacted from model output: {post_redactions}")

    return {"response": final_output, "redactions": pre_redactions + post_redactions}

async def main():
    # Simulate tool returning data with secrets
    raw_tool_data = {
        "user": "alice",
        "api_key": "sk-abc123456789012345678901234",
        "internal_ip": "192.168.1.10",
        "data": "Here is your auth token: Bearer eyJhbGciOiJSUzI1NiJ9.payload.signature"
    }
    result = await safe_agent_response("Summarize the user's account status", raw_tool_data)
    print(f"Response: {result['response'][:300]}")
    print(f"Redactions applied: {result['redactions']}")

asyncio.run(main())
```

## Solution 3: Role-Based Output Filtering

Different callers see different fields based on their authorization level.

```python
import asyncio
from anthropic import AsyncAnthropic
from dataclasses import dataclass
from enum import Enum
from typing import Any

client = AsyncAnthropic()

class AccessLevel(Enum):
    PUBLIC = 0
    USER = 1
    ADMIN = 2
    INTERNAL = 3

@dataclass
class FieldPolicy:
    field: str
    min_access_level: AccessLevel
    transform: str = "show"  # "show" | "mask" | "hash"

FIELD_POLICIES: list[FieldPolicy] = [
    FieldPolicy("user_id",           AccessLevel.USER),
    FieldPolicy("username",          AccessLevel.PUBLIC),
    FieldPolicy("display_name",      AccessLevel.PUBLIC),
    FieldPolicy("email",             AccessLevel.USER,     transform="mask"),
    FieldPolicy("phone",             AccessLevel.USER,     transform="mask"),
    FieldPolicy("role",              AccessLevel.USER),
    FieldPolicy("created_at",        AccessLevel.USER),
    FieldPolicy("last_login_at",     AccessLevel.ADMIN),
    FieldPolicy("login_count",       AccessLevel.ADMIN),
    FieldPolicy("internal_shard",    AccessLevel.INTERNAL),
    FieldPolicy("password_hash",     AccessLevel.INTERNAL, transform="hash"),
    FieldPolicy("auth_token",        AccessLevel.INTERNAL),
    FieldPolicy("billing_id",        AccessLevel.ADMIN),
]

POLICY_MAP = {p.field: p for p in FIELD_POLICIES}

def mask_middle(value: str, show_chars: int = 2) -> str:
    if len(value) <= show_chars * 2:
        return "*" * len(value)
    return value[:show_chars] + "*" * (len(value) - show_chars * 2) + value[-show_chars:]

def apply_transform(field: str, value: Any, transform: str) -> Any:
    if transform == "show":
        return value
    elif transform == "mask":
        return mask_middle(str(value))
    elif transform == "hash":
        import hashlib
        return "[SHA256:" + hashlib.sha256(str(value).encode()).hexdigest()[:8] + "...]"
    return "[REDACTED]"

def filter_by_access_level(data: dict, caller_level: AccessLevel) -> dict:
    result = {}
    for field, value in data.items():
        policy = POLICY_MAP.get(field)
        if policy is None:
            # Unknown fields: default deny
            continue
        if caller_level.value >= policy.min_access_level.value:
            result[field] = apply_transform(field, value, policy.transform)
    return result

# Full user record (never leave the service boundary unfiltered)
MOCK_USER = {
    "user_id": "usr-001",
    "username": "alice",
    "display_name": "Alice Smith",
    "email": "alice.smith@example.com",
    "phone": "+1-555-867-5309",
    "role": "editor",
    "created_at": "2024-01-15",
    "last_login_at": "2025-04-15",
    "login_count": 142,
    "internal_shard": "shard-7",
    "password_hash": "$2b$12$secret_hash_value",
    "auth_token": "tok_secret_abc123def456",
    "billing_id": "cus_stripe_xyz789",
}

async def get_user_for_caller(user_id: str, caller_level: AccessLevel) -> dict:
    raw = MOCK_USER.copy()  # in reality: fetch from DB
    return filter_by_access_level(raw, caller_level)

async def main():
    for level in [AccessLevel.PUBLIC, AccessLevel.USER, AccessLevel.ADMIN]:
        filtered = await get_user_for_caller("usr-001", level)
        print(f"\n[{level.name}] sees {len(filtered)} fields: {filtered}")

asyncio.run(main())
```

## Solution 4: LLM Output Auditor

Run the model's response through a separate audit pass to catch sensitive disclosures.

```python
import asyncio
import json
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

AUDITOR_SYSTEM = """You are a security auditor reviewing AI-generated responses for sensitive data leaks.

Check for:
1. API keys, tokens, passwords, or credentials
2. Internal IP addresses, hostnames, or infrastructure details
3. Personal identifiable information (SSN, full card numbers, etc.)
4. Internal system identifiers (shard IDs, database keys, internal paths)
5. Sensitive user data that shouldn't be in a public response

Respond with JSON:
{
  "safe": true/false,
  "issues": ["description of issue 1", ...],
  "redacted_response": "the response with sensitive parts replaced by [REDACTED]"
}

If safe, set issues to [] and redacted_response to the original response."""

async def audit_response(response_text: str, context: str = "") -> dict:
    prompt = f"Context: {context}\n\nResponse to audit:\n{response_text}" if context else f"Response to audit:\n{response_text}"
    audit = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=AUDITOR_SYSTEM,
        messages=[{"role": "user", "content": prompt}]
    )
    text = audit.content[0].text.strip()
    try:
        start, end = text.find("{"), text.rfind("}") + 1
        return json.loads(text[start:end])
    except Exception:
        return {"safe": False, "issues": ["Audit parse failed"], "redacted_response": response_text}

async def agent_with_audit(user_query: str) -> str:
    # Agent generates response
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system="You are a helpful assistant. Answer questions about the system.",
        messages=[{
            "role": "user",
            "content": user_query + "\n\nNote: internal auth token is tok_sk_prod_abc123xyz789"
        }]
    )
    raw_output = response.content[0].text if response.content else ""

    # Audit before delivery
    audit_result = await audit_response(raw_output, context=user_query)

    if not audit_result.get("safe", True):
        issues = audit_result.get("issues", [])
        print(f"[Audit] BLOCKED — issues: {issues}")
        return audit_result.get("redacted_response", "[Response redacted for security]")

    return raw_output

async def main():
    result = await agent_with_audit("What is the system status and how do I authenticate?")
    print(f"Delivered response:\n{result[:400]}")

asyncio.run(main())
```

## Solution 5: Structured Output Schema Enforcement

Use JSON schema validation to ensure the response only contains declared fields.

```python
import asyncio
import json
from anthropic import AsyncAnthropic
from typing import Any

client = AsyncAnthropic()

def validate_against_schema(data: Any, schema: dict, path: str = "") -> tuple[Any, list[str]]:
    """
    Validate and filter data against a JSON schema.
    Returns (filtered_data, list_of_violations).
    """
    violations = []

    if schema.get("type") == "object":
        if not isinstance(data, dict):
            return {}, [f"{path}: expected object, got {type(data).__name__}"]

        allowed_props = set(schema.get("properties", {}).keys())
        required = set(schema.get("required", []))
        additional = schema.get("additionalProperties", False)

        result = {}
        # Remove undeclared fields
        for key in data:
            if key not in allowed_props:
                if not additional:
                    violations.append(f"{path}.{key}: field not in schema, stripped")
                else:
                    result[key] = data[key]

        # Process declared fields
        for key, prop_schema in schema.get("properties", {}).items():
            if key in data:
                filtered_val, sub_violations = validate_against_schema(
                    data[key], prop_schema, f"{path}.{key}"
                )
                result[key] = filtered_val
                violations.extend(sub_violations)
            elif key in required:
                violations.append(f"{path}.{key}: required field missing")

        return result, violations

    elif schema.get("type") == "array":
        if not isinstance(data, list):
            return [], [f"{path}: expected array"]
        item_schema = schema.get("items", {})
        result = []
        for i, item in enumerate(data):
            filtered, sub_v = validate_against_schema(item, item_schema, f"{path}[{i}]")
            result.append(filtered)
            violations.extend(sub_v)
        return result, violations

    else:
        # Scalar — return as-is
        return data, []

# Declared safe output schema
USER_RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "user_id": {"type": "string"},
        "username": {"type": "string"},
        "display_name": {"type": "string"},
        "role": {"type": "string"},
        "account_status": {"type": "string"},
    },
    "required": ["user_id", "username"]
}

async def safe_structured_response(raw_data: dict) -> dict:
    filtered, violations = validate_against_schema(raw_data, USER_RESPONSE_SCHEMA)
    if violations:
        print(f"[Schema Filter] Violations: {violations}")
    return filtered

async def main():
    raw = {
        "user_id": "usr-001",
        "username": "alice",
        "display_name": "Alice Smith",
        "role": "editor",
        "account_status": "active",
        "password_hash": "$2b$12$secret",     # stripped
        "auth_token": "tok_abc123",            # stripped
        "internal_shard": "shard-7",           # stripped
        "billing_id": "cus_xyz",               # stripped
    }

    safe = await safe_structured_response(raw)
    print(f"Safe output: {json.dumps(safe, indent=2)}")

asyncio.run(main())
```

## Solution 6: Differential Redaction with Audit Log

Redact fields for the caller, but preserve full data in an encrypted audit log.

```python
import asyncio
import json
import time
import hashlib
from pathlib import Path
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field

client = AsyncAnthropic()
AUDIT_LOG = Path("/tmp/agent_redaction_audit.jsonl")

@dataclass
class RedactionAuditEntry:
    timestamp: float
    request_id: str
    caller_id: str
    fields_returned: list[str]
    fields_redacted: list[str]
    redaction_reason: dict[str, str]

def log_redaction(entry: RedactionAuditEntry):
    with AUDIT_LOG.open("a") as f:
        f.write(json.dumps({
            "timestamp": entry.timestamp,
            "request_id": entry.request_id,
            "caller_id": entry.caller_id,
            "fields_returned": entry.fields_returned,
            "fields_redacted": entry.fields_redacted,
            "redaction_reason": entry.redaction_reason,
        }) + "\n")

REDACTION_RULES: dict[str, str] = {
    "password_hash": "credential",
    "auth_token": "credential",
    "internal_shard": "infrastructure",
    "stripe_customer_id": "payment_data",
    "last_login_ip": "pii",
    "raw_api_key": "credential",
}

def apply_differential_redaction(
    data: dict,
    caller_id: str,
    request_id: str
) -> dict:
    redacted_fields = []
    redaction_reasons = {}
    safe_output = {}

    for field, value in data.items():
        reason = REDACTION_RULES.get(field)
        if reason:
            redacted_fields.append(field)
            redaction_reasons[field] = reason
        else:
            safe_output[field] = value

    entry = RedactionAuditEntry(
        timestamp=time.time(),
        request_id=request_id,
        caller_id=caller_id,
        fields_returned=list(safe_output.keys()),
        fields_redacted=redacted_fields,
        redaction_reason=redaction_reasons,
    )
    log_redaction(entry)

    if redacted_fields:
        print(f"[Redaction] {len(redacted_fields)} fields stripped for {caller_id}: {redacted_fields}")

    return safe_output

async def deliver_with_audit(
    raw_data: dict,
    caller_id: str
) -> dict:
    request_id = hashlib.md5(
        f"{caller_id}:{time.time()}".encode()
    ).hexdigest()[:8]
    return apply_differential_redaction(raw_data, caller_id, request_id)

async def main():
    full_record = {
        "user_id": "usr-001",
        "username": "alice",
        "display_name": "Alice Smith",
        "role": "editor",
        "email": "alice@example.com",
        "password_hash": "$2b$12$xxxxxxxxxxx",
        "auth_token": "tok_secret_abc",
        "internal_shard": "shard-7",
        "stripe_customer_id": "cus_xyz789",
        "last_login_ip": "203.0.113.42",
    }

    safe = await deliver_with_audit(full_record, caller_id="api-user-public")
    print(f"Delivered to caller: {list(safe.keys())}")
    print(f"Audit log: {AUDIT_LOG}")

asyncio.run(main())
```

## Comparison

| Approach | Coverage | False Positives | Latency | Best For |
|---|---|---|---|---|
| Field Allowlist | Structural only | None | None | Known structured outputs |
| Pattern Scrubbing | Text patterns | Low-medium | None | Free-text responses |
| Role-Based Filtering | Access control | None | None | Multi-tenant systems |
| LLM Auditor | Semantic | Low | +1 call | High-stakes outputs |
| Schema Enforcement | Structural | None | None | JSON API responses |
| Differential + Audit | All fields | None | None | Compliance-required systems |

**Rule of thumb**: Apply schema enforcement first (zero cost, catches structural leaks), then pattern scrubbing for free-text outputs, and add the LLM auditor only for high-stakes responses where missing a semantic leak is unacceptable. Always log what was redacted for audit purposes.
