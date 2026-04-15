---
layout: solution
title: "Agent Misidentifies Error Source — Blames Wrong Component"
category: general
description: "Agent gets a database error and decides the API is broken. Or gets a timeout and assumes the model is down. Spends 20 turns debugging the wrong thing before finding the real cause."
tags: [debugging, root-cause, error-attribution, diagnosis, component, misidentification]
---

# Agent Misidentifies Error Source — Blames Wrong Component

## Symptom
- Agent gets a 500 error and immediately blames the external API, but the bug is in its own code
- Database connection refused — agent blames the database host, but the config file has a typo
- Timeout on an HTTP call — agent retries the endpoint, but the actual issue is a firewall rule
- Agent spends 10 turns checking the wrong system before finding the real fault
- "The API is broken" when the API is fine and the auth token is expired

## Root Cause
Error messages are often generic and originate from a different layer than the true source. A `ConnectionRefused` could mean: wrong host, wrong port, service down, firewall, or DNS failure. Without methodical elimination, agents latch onto the first plausible explanation and pursue it, burning tokens on the wrong path.

## Fix

### Option 1: Structured component isolation before blaming

```python
import httpx
import socket

async def diagnose_connection_failure(host: str, port: int, url: str) -> dict:
    """
    Isolate exactly which layer failed before blaming a component.
    Returns dict with which checks passed and which failed.
    """
    results = {}

    # Layer 1: DNS
    try:
        ip = socket.gethostbyname(host)
        results["dns"] = {"ok": True, "resolved_to": ip}
    except socket.gaierror as e:
        results["dns"] = {"ok": False, "error": str(e)}
        return results  # DNS failed — don't check further

    # Layer 2: TCP connectivity
    try:
        with socket.create_connection((host, port), timeout=5):
            results["tcp"] = {"ok": True}
    except (ConnectionRefusedError, TimeoutError) as e:
        results["tcp"] = {"ok": False, "error": str(e)}
        return results  # TCP failed — service is not listening

    # Layer 3: HTTP
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=10)
            results["http"] = {"ok": True, "status": resp.status_code}
    except Exception as e:
        results["http"] = {"ok": False, "error": str(e)}

    return results

# Usage
diagnosis = await diagnose_connection_failure("db.internal", 5432, "http://api.internal/health")
# → {"dns": {"ok": True}, "tcp": {"ok": False, "error": "Connection refused"}}
# → Clear: service not listening on port 5432. Database is down or wrong port.
```

### Option 2: Error signature matching to probable causes

```python
ERROR_CAUSE_MAP = {
    "Connection refused": [
        "Service not running on that port",
        "Wrong port number in config",
        "Firewall blocking connection",
    ],
    "Name or service not known": [
        "DNS resolution failed — hostname typo or DNS outage",
        "Service not in /etc/hosts or DNS zone",
        "Missing DNS entry for service in Kubernetes/Docker network",
    ],
    "SSL: CERTIFICATE_VERIFY_FAILED": [
        "Self-signed cert not in CA bundle",
        "System CA certs not installed (missing ca-certificates package)",
        "Cert expired",
    ],
    "401 Unauthorized": [
        "Token missing, expired, or malformed",
        "Wrong auth scheme (Bearer vs Basic vs API-Key header)",
    ],
    "403 Forbidden": [
        "Authenticated but lacks required permission",
        "IP allowlist blocking this origin",
        "Missing OAuth scope",
    ],
    "timeout": [
        "Service is up but slow — network latency or overload",
        "Firewall drops packets silently (connection hangs, not refused)",
        "Correct host/port but operation is too expensive",
    ],
}

def suggest_causes(error_message: str) -> list[str]:
    """Map error string to probable root causes"""
    suggestions = []
    for pattern, causes in ERROR_CAUSE_MAP.items():
        if pattern.lower() in error_message.lower():
            suggestions.extend(causes)
    return suggestions or ["Unknown error — check logs at the receiving service"]
```

### Option 3: Check your own code/config before blaming external services

```python
def self_check_before_blaming_external(config: dict) -> list[str]:
    """
    Run local sanity checks before assuming external service is at fault.
    Returns list of issues found locally.
    """
    issues = []

    # Check config values are non-empty
    required = ["DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD"]
    for key in required:
        val = config.get(key)
        if not val:
            issues.append(f"Config missing or empty: {key}")
        elif key == "DB_PORT":
            try:
                int(val)
            except ValueError:
                issues.append(f"DB_PORT is not a valid integer: '{val}'")

    # Check for common typos in hostnames
    host = config.get("DB_HOST", "")
    if host.startswith("http://") or host.startswith("https://"):
        issues.append(f"DB_HOST should be a hostname, not a URL: '{host}'")
    if " " in host:
        issues.append(f"DB_HOST contains a space: '{host}'")
    if host.endswith("/"):
        issues.append(f"DB_HOST has trailing slash: '{host}'")

    return issues

# Example: catches "DB_HOST=localhost " (trailing space) before wasting 10 turns on DB
issues = self_check_before_blaming_external(os.environ)
if issues:
    for issue in issues:
        print(f"LOCAL CONFIG ISSUE: {issue}")
    raise RuntimeError("Fix local configuration before retrying")
```

### Option 4: Read the error from the receiving side, not just the caller

```python
import logging

# Pattern: log at BOTH sides of a service boundary
# When the agent sees a 500, it should check the *server* logs, not just the client error

async def call_with_server_side_request_id(url: str, payload: dict) -> dict:
    """
    Include a request ID in every call so server-side logs can be correlated.
    When debugging, search for the request ID in the server logs first.
    """
    import uuid
    request_id = str(uuid.uuid4())[:8]

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(
                url,
                json=payload,
                headers={"X-Request-ID": request_id},
                timeout=30
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            # Don't just blame the downstream service
            print(f"Request {request_id} failed: {e.response.status_code}")
            print(f"Debugging tip: grep server logs for request_id={request_id}")
            print(f"Response body: {e.response.text[:500]}")
            raise

# Diagnosis discipline:
# 1. Got an error? Note the request_id
# 2. Check SERVER logs for that ID — find the actual exception there
# 3. Only then determine which component caused it
```

### Option 5: System prompt for disciplined root-cause analysis

```
System prompt:
"Debugging protocol — follow this order strictly:

1. Before blaming any component, state: 'The error is: [exact error text]'

2. List which components are in the call path:
   [caller] → [middleware] → [service] → [database]

3. Eliminate from the bottom up:
   - Is the config valid? (no typos, correct types, not empty)
   - Can I reach the host? (DNS, TCP, not firewall-blocked)
   - Is the service running? (health check endpoint, process list)
   - Is the error from the service itself or from the caller?

4. Only after eliminating upstream causes, blame a downstream component.

5. When you find the real cause, state it explicitly:
   'Root cause: [component] is [what is wrong] because [evidence]'
   NOT 'The API seems broken' (too vague, no evidence cited)"
```

### Option 6: Error provenance tracker

```python
from dataclasses import dataclass, field
from datetime import datetime

@dataclass
class ErrorEvent:
    timestamp: str
    component: str          # "db", "api", "config", "auth", "network"
    error_type: str
    message: str
    is_local: bool          # True if the error originates in our own code/config
    evidence: list[str] = field(default_factory=list)

class ErrorProvenance:
    """Track which components have been verified innocent vs. guilty"""

    def __init__(self):
        self.checked: dict[str, bool] = {}  # component -> is_ok
        self.events: list[ErrorEvent] = []

    def mark_ok(self, component: str, evidence: str):
        self.checked[component] = True
        print(f"[CLEARED] {component}: {evidence}")

    def mark_suspect(self, component: str, evidence: str):
        self.checked[component] = False
        print(f"[SUSPECT] {component}: {evidence}")

    def unchecked(self) -> list[str]:
        all_components = ["config", "auth", "network", "database", "api", "own_code"]
        return [c for c in all_components if c not in self.checked]

    def summary(self) -> str:
        lines = []
        for comp, ok in self.checked.items():
            lines.append(f"  {'✓' if ok else '✗'} {comp}")
        if self.unchecked():
            lines.append(f"  ? unchecked: {', '.join(self.unchecked())}")
        return "\n".join(lines)

# Usage in debug session:
probe = ErrorProvenance()
probe.mark_ok("config", "DB_HOST=db.internal, DB_PORT=5432, no typos")
probe.mark_ok("network", "TCP connect to db.internal:5432 succeeded")
probe.mark_suspect("auth", "PGPASSWORD env var is empty string")
# → Root cause found: auth, not database
```

## Error Attribution Mistakes

| Symptom | Wrong blame | Real cause (often) |
|---|---|---|
| `Connection refused` | "Database is down" | Wrong port in config |
| `500 Internal Server Error` | "API is broken" | Our payload is invalid |
| `timeout` | "Service is slow" | Firewall drops silently |
| `401 Unauthorized` | "Key not provisioned" | Key expired yesterday |
| `SSL error` | "TLS misconfigured on server" | Missing CA cert on client |
| `ImportError` | "Library not installed" | Wrong virtual environment active |

## Expected Token Savings
Debugging wrong component for 20 turns: ~40,000 tokens
Structured isolation finds real cause in 3 checks: ~3,000 tokens

## Environment
- Any agent doing integration work across multiple services or components
- Source: direct experience; wrong-component debugging is the most expensive debugging pattern
