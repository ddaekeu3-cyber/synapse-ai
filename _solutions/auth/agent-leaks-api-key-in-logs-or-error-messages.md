---
layout: solution
title: "Agent Leaks API Key in Logs or Error Messages"
category: auth
description: "Agent includes the API key in a log line, error message, or exception traceback. Key appears in centralized logging, Sentry, or stdout. Anyone with log access can extract the key and use it."
tags: [security, api-key, secrets, logging, leak, redaction, owasp, credentials]
---

# Agent Leaks API Key in Logs or Error Messages

## Symptom
- Sentry error includes full API key in exception message
- Log line: `Connecting to API with key sk-proj-abc123...` — full key visible
- HTTP request logged with `Authorization: Bearer <full-token>`
- Exception traceback includes the credential in the request headers dict
- `env` dump in debug output includes all secrets
- Stack trace from failed API call shows the key in the URL query params

## Root Cause
Secrets embedded in connection strings, headers, or config dicts get accidentally included in log calls, exception messages, and debug output. Logging libraries serialize objects fully by default. Exception handlers that log `str(exception)` or `repr(request)` capture everything — including Authorization headers, connection strings with passwords, and query params with API keys.

## Fix

### Option 1: Redact secrets in logging with a custom filter

```python
import logging
import re

REDACT_PATTERNS = [
    (re.compile(r'(sk-[a-zA-Z0-9]{20,})', re.IGNORECASE), "[REDACTED_OPENAI_KEY]"),
    (re.compile(r'(sk-ant-[a-zA-Z0-9\-]{20,})', re.IGNORECASE), "[REDACTED_ANTHROPIC_KEY]"),
    (re.compile(r'(Bearer\s+)[a-zA-Z0-9\-._~+/]+=*', re.IGNORECASE), r'\1[REDACTED_TOKEN]'),
    (re.compile(r'(api[_-]?key["\s:=]+)[\'"]?[a-zA-Z0-9\-_]{16,}[\'"]?', re.IGNORECASE), r'\1[REDACTED]'),
    (re.compile(r'(password["\s:=]+)[\'"]?[^\s\'"]{6,}[\'"]?', re.IGNORECASE), r'\1[REDACTED]'),
    (re.compile(r'(secret["\s:=]+)[\'"]?[a-zA-Z0-9\-_]{8,}[\'"]?', re.IGNORECASE), r'\1[REDACTED]'),
]

class SecretRedactFilter(logging.Filter):
    """Redact known secret patterns from all log records"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._redact(str(record.msg))
        record.args = tuple(self._redact(str(a)) for a in (record.args or ()))
        return True

    def _redact(self, text: str) -> str:
        for pattern, replacement in REDACT_PATTERNS:
            text = pattern.sub(replacement, text)
        return text

# Apply to all loggers:
root_logger = logging.getLogger()
root_logger.addFilter(SecretRedactFilter())

# Now this is safe:
logger = logging.getLogger(__name__)
api_key = "sk-proj-abc123verylongkey"
logger.info(f"Connecting with key: {api_key}")
# → LOG: "Connecting with key: [REDACTED_OPENAI_KEY]"
```

### Option 2: Secret wrapper class that hides value in repr/str

```python
class Secret(str):
    """
    String subclass that hides its value in repr() and str().
    Use for all credential values — prevents accidental logging.
    """

    def __repr__(self) -> str:
        return "Secret('***')"

    def __str__(self) -> str:
        return "***"

    def reveal(self) -> str:
        """Explicitly get the actual value when needed for API calls"""
        return super().__str__()

    @classmethod
    def from_env(cls, key: str) -> "Secret":
        import os
        value = os.environ.get(key)
        if not value:
            raise KeyError(f"Environment variable '{key}' not set")
        return cls(value)

# Usage:
api_key = Secret.from_env("ANTHROPIC_API_KEY")
print(api_key)             # → ***
print(repr(api_key))       # → Secret('***')
logger.info(f"Key: {api_key}")  # → "Key: ***"

# Only exposed when explicitly requested:
client = anthropic.Anthropic(api_key=api_key.reveal())

# Dict with secrets is safe to log:
config = {"model": "claude-sonnet-4-6", "api_key": api_key}
logger.info(f"Config: {config}")
# → "Config: {'model': 'claude-sonnet-4-6', 'api_key': Secret('***')}"
```

### Option 3: Sanitize HTTP request/response before logging

```python
import httpx
import copy

SENSITIVE_HEADERS = {"authorization", "x-api-key", "api-key", "x-auth-token", "cookie"}
SENSITIVE_PARAMS = {"api_key", "key", "token", "secret", "password", "access_token"}

def sanitize_request(request: httpx.Request) -> dict:
    """Create a safe loggable version of an HTTP request"""
    safe_headers = {}
    for name, value in request.headers.items():
        if name.lower() in SENSITIVE_HEADERS:
            safe_headers[name] = f"{value[:4]}...[REDACTED]" if value else "[EMPTY]"
        else:
            safe_headers[name] = value

    # Sanitize URL params
    safe_url = str(request.url)
    for param in SENSITIVE_PARAMS:
        import re
        safe_url = re.sub(
            rf'([?&]{param}=)[^&]+',
            rf'\1[REDACTED]',
            safe_url,
            flags=re.IGNORECASE
        )

    return {
        "method": request.method,
        "url": safe_url,
        "headers": safe_headers,
    }

class LoggingHTTPClient:
    """HTTP client that logs requests without exposing secrets"""

    def __init__(self):
        self.client = httpx.AsyncClient()

    async def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        # Create request to log it safely
        req = self.client.build_request(method, url, **kwargs)
        logger.debug(f"HTTP {method} {sanitize_request(req)}")

        try:
            response = await self.client.send(req)
            logger.debug(f"HTTP {response.status_code} ({response.elapsed.total_seconds():.2f}s)")
            return response
        except Exception as e:
            # Log exception without headers (which might contain auth)
            logger.error(f"HTTP {method} {url.split('?')[0]} failed: {type(e).__name__}: {e}")
            raise
```

### Option 4: Configure error tracking tools to scrub secrets

```python
# Sentry configuration — scrub sensitive data before sending
import sentry_sdk
from sentry_sdk import configure_scope

def before_send_sentry(event: dict, hint: dict) -> dict:
    """Scrub secrets from Sentry events before they leave the process"""

    def redact_value(value):
        if isinstance(value, str):
            for pattern, replacement in REDACT_PATTERNS:
                value = pattern.sub(replacement, value)
        elif isinstance(value, dict):
            return {k: redact_value(v) for k, v in value.items()}
        elif isinstance(value, list):
            return [redact_value(v) for v in value]
        return value

    # Scrub request headers in Sentry events
    if "request" in event:
        req = event["request"]
        if "headers" in req:
            for header in list(req["headers"].keys()):
                if header.lower() in SENSITIVE_HEADERS:
                    req["headers"][header] = "[REDACTED]"
        if "env" in req:
            event["request"]["env"] = "[REMOVED — may contain secrets]"

    # Scrub exception values
    if "exception" in event:
        for exc in event["exception"].get("values", []):
            if "value" in exc:
                exc["value"] = redact_value(exc["value"])

    return event

sentry_sdk.init(
    dsn="https://...",
    before_send=before_send_sentry,
    # Also: never send environment variables (they may contain secrets)
    send_default_pii=False,
)
```

### Option 5: Startup secret audit — verify secrets are not in unsafe places

```python
import os
import re

def audit_secret_exposure() -> list[str]:
    """
    Check common places secrets might be accidentally exposed.
    Run at startup to catch misconfigurations.
    """
    issues = []
    secrets_to_check = {
        k: v for k, v in os.environ.items()
        if any(word in k.upper() for word in ["KEY", "SECRET", "TOKEN", "PASSWORD", "CREDENTIAL"])
    }

    for name, value in secrets_to_check.items():
        if not value:
            continue

        # Check if secret is in a place it shouldn't be
        # 1. Is it also set as a non-sensitive env var?
        for other_key, other_value in os.environ.items():
            if other_key != name and other_value == value and not any(
                word in other_key.upper() for word in ["KEY", "SECRET", "TOKEN", "PASSWORD"]
            ):
                issues.append(
                    f"Secret '{name}' value also found in non-secret env var '{other_key}'"
                )

        # 2. Is it suspiciously short? (might be a placeholder, not a real secret)
        if len(value) < 16:
            issues.append(f"'{name}' is only {len(value)} chars — may not be a real secret")

        # 3. Is it a known test/placeholder value?
        placeholders = {"test", "example", "placeholder", "changeme", "secret", "password123"}
        if value.lower() in placeholders:
            issues.append(f"'{name}' appears to be a placeholder value: '{value}'")

    return issues

# At startup:
issues = audit_secret_exposure()
for issue in issues:
    logger.warning(f"SECRET AUDIT: {issue}")
```

### Option 6: Git pre-commit hook to prevent committing secrets

```bash
#!/bin/bash
# .git/hooks/pre-commit — run before every commit

SECRET_PATTERNS=(
    "sk-[a-zA-Z0-9]{20,}"           # OpenAI keys
    "sk-ant-[a-zA-Z0-9-]{20,}"      # Anthropic keys
    "AKIA[0-9A-Z]{16}"              # AWS access keys
    "ghp_[a-zA-Z0-9]{36}"          # GitHub personal tokens
    "AIza[0-9A-Za-z-_]{35}"        # Google API keys
)

found=0
for pattern in "${SECRET_PATTERNS[@]}"; do
    matches=$(git diff --cached --unified=0 | grep "^+" | grep -oE "$pattern" 2>/dev/null)
    if [ -n "$matches" ]; then
        echo "ERROR: Potential secret detected in staged changes:"
        echo "  Pattern: $pattern"
        echo "  Match: ${matches:0:20}..."
        found=1
    fi
done

if [ $found -eq 1 ]; then
    echo "Commit blocked. Remove secrets from staged files."
    echo "If this is a false positive, use: git commit --no-verify"
    exit 1
fi
exit 0
```

```python
# Python: use detect-secrets library
# pip install detect-secrets
# detect-secrets scan > .secrets.baseline
# detect-secrets audit .secrets.baseline
# Then in CI: detect-secrets-hook --baseline .secrets.baseline
```

## Secret Exposure Risk by Location

| Location | Risk | Fix |
|---|---|---|
| Log files | High | SecretRedactFilter on all loggers |
| Exception messages | High | Sanitize before logging exceptions |
| Sentry/error tracking | High | before_send scrubbing |
| HTTP request logs | High | Redact Authorization headers |
| Git commit history | Critical | Pre-commit hook + git-filter-repo |
| `.env` file committed | Critical | .gitignore + git history cleanup |
| Debug output / `print()` | Medium | Use Secret wrapper class |

## Expected Token Savings
Secret leaked, rotated, all integrations updated, incident report: ~100,000+ tokens (plus operational cost)
Logging redaction prevents exposure: 0 tokens wasted

## Environment
- Any agent that logs requests, uses error tracking, or runs in an environment with log aggregation
- Source: OWASP A02:2021 Cryptographic Failures; direct experience auditing production agent deployments
