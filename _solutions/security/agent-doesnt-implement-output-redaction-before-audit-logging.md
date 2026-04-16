---
title: "Agent doesn't implement output redaction before audit logging"
description: "The agent writes full model responses, tool arguments, and API payloads directly to audit logs. Secrets, PII, and session tokens appear in plaintext log files that are retained for months and accessible to anyone with log access."
difficulty: intermediate
category: security
tags: [redaction, audit-logging, PII, secrets, GDPR, data-protection]
---

## Problem

Audit logs are essential for debugging, compliance, and incident response. But they're also a high-value target for data exfiltration. When an agent logs the raw tool call arguments (which might contain API keys), the full model response (which might echo back user-submitted PII), or the system prompt (which might contain internal credentials), the log files become a liability:

- Compliance violations (GDPR, HIPAA, SOC 2) when PII is retained in plaintext
- Credential leaks if an API key was mentioned in a user message and echoed in a response
- Session token exposure through request/response logging

```python
# BAD: logs everything verbatim — PII and secrets visible in plaintext
import logging
log = logging.getLogger()

def log_tool_call(tool: str, args: dict, result: dict):
    log.info(f"Tool call: {tool} args={args} result={result}")
    # args might contain: {"api_key": "sk-...", "user_email": "alice@...", "ssn": "123-45-6789"}
```

## Solution 1: Regex-based PII and secret scrubber for log records

Apply a set of regex patterns to scrub known secret and PII formats before any string reaches the log sink.

```python
import re
import logging
import json
from typing import Any


# ── Redaction patterns ────────────────────────────────────────────────
REDACTION_RULES: list[tuple[re.Pattern, str]] = [
    # API keys / tokens
    (re.compile(r"(sk-[a-zA-Z0-9]{20,})", re.IGNORECASE), "[REDACTED:API_KEY]"),
    (re.compile(r"(Bearer\s+[a-zA-Z0-9\-._~+/]+=*)", re.IGNORECASE), "Bearer [REDACTED:TOKEN]"),
    (re.compile(r"(ghp_[a-zA-Z0-9]{36})", re.IGNORECASE), "[REDACTED:GITHUB_TOKEN]"),
    (re.compile(r"(xoxb-[a-zA-Z0-9\-]{50,})", re.IGNORECASE), "[REDACTED:SLACK_TOKEN]"),
    # PII
    (re.compile(r"\b([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,})\b"), "[REDACTED:EMAIL]"),
    (re.compile(r"\b(\d{3}-\d{2}-\d{4})\b"), "[REDACTED:SSN]"),
    (re.compile(r"\b(\d{4}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4})\b"), "[REDACTED:CARD]"),
    (re.compile(r"\b(\+?1?\s?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4})\b"), "[REDACTED:PHONE]"),
    # Passwords in key=value patterns
    (re.compile(r'(password|passwd|secret|token|key)\s*[=:]\s*\S+', re.IGNORECASE),
     r"\1=[REDACTED]"),
]


def scrub_string(text: str) -> str:
    for pattern, replacement in REDACTION_RULES:
        text = pattern.sub(replacement, text)
    return text


def scrub_value(value: Any) -> Any:
    if isinstance(value, str):
        return scrub_string(value)
    elif isinstance(value, dict):
        return {k: scrub_value(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [scrub_value(item) for item in value]
    return value


class RedactingFormatter(logging.Formatter):
    """Drop-in log formatter that scrubs all log messages and extra fields."""

    def format(self, record: logging.LogRecord) -> str:
        # Scrub the message
        record.msg = scrub_string(str(record.msg))
        # Scrub any extra attributes
        for key in list(vars(record).keys()):
            if key not in logging.LogRecord.__dict__:
                setattr(record, key, scrub_value(getattr(record, key)))
        return super().format(record)


# ── Setup ────────────────────────────────────────────────────────────
handler = logging.StreamHandler()
handler.setFormatter(RedactingFormatter(
    fmt="%(asctime)s %(levelname)s %(name)s - %(message)s"
))
log = logging.getLogger("agent.audit")
log.addHandler(handler)
log.setLevel(logging.INFO)


# ── Usage ────────────────────────────────────────────────────────────
def log_tool_call(tool: str, args: dict, result: dict):
    log.info(
        "Tool call",
        extra={"tool": tool, "args": scrub_value(args), "result_size": len(str(result))},
    )


log_tool_call(
    "send_email",
    {"to": "alice@example.com", "api_key": "sk-1234567890abcdef1234"},
    {"status": "sent"},
)
# Output: args={'to': '[REDACTED:EMAIL]', 'api_key': '[REDACTED:API_KEY]'}
```

## Solution 2: Schema-aware field-level redaction for structured payloads

Instead of regex over serialized strings, walk the data structure and redact specific key names that are known to carry sensitive data.

```python
import copy
from typing import Any


# Fields that must always be redacted regardless of value
SENSITIVE_KEYS = frozenset({
    "api_key", "apikey", "api_secret", "secret", "password", "passwd",
    "token", "access_token", "refresh_token", "session_token",
    "authorization", "x-api-key", "private_key", "client_secret",
    "ssn", "social_security", "credit_card", "card_number",
})

# Fields that contain PII — redact value but preserve key
PII_KEYS = frozenset({
    "email", "phone", "mobile", "address", "full_name", "first_name",
    "last_name", "date_of_birth", "dob", "ip_address", "user_id",
    "account_number",
})

REDACTED = "[REDACTED]"
REDACTED_PII = "[REDACTED:PII]"


def redact_payload(data: Any, *, depth: int = 0, max_depth: int = 20) -> Any:
    """
    Recursively redact sensitive fields.
    Returns a deep-copy with sensitive values replaced.
    """
    if depth > max_depth:
        return REDACTED

    if isinstance(data, dict):
        result = {}
        for key, value in data.items():
            key_lower = key.lower().replace("-", "_").replace(" ", "_")
            if key_lower in SENSITIVE_KEYS:
                result[key] = REDACTED
            elif key_lower in PII_KEYS:
                result[key] = REDACTED_PII
            else:
                result[key] = redact_payload(value, depth=depth + 1)
        return result

    elif isinstance(data, list):
        return [redact_payload(item, depth=depth + 1) for item in data]

    elif isinstance(data, str) and len(data) > 500:
        # Truncate very long strings to prevent log bloat; also scrub
        from solution1 import scrub_string
        return scrub_string(data[:500]) + f"...[truncated {len(data)} chars]"

    return data


# ── Usage ────────────────────────────────────────────────────────────
payload = {
    "user_id": "usr_42",
    "email": "alice@example.com",
    "password": "hunter2",
    "preferences": {"theme": "dark"},
    "payment": {
        "card_number": "4111-1111-1111-1111",
        "api_key": "sk-prod-xyz",
    },
}

safe = redact_payload(payload)
import json
print(json.dumps(safe, indent=2))
# {
#   "user_id": "[REDACTED:PII]",
#   "email": "[REDACTED:PII]",
#   "password": "[REDACTED]",
#   "preferences": {"theme": "dark"},
#   "payment": {"card_number": "[REDACTED]", "api_key": "[REDACTED]"}
# }
```

## Solution 3: Audit log sink with built-in redaction and tamper detection

Create a dedicated audit logger that scrubs before writing and signs each entry with an HMAC so the log file can't be silently altered.

```python
import hashlib
import hmac
import json
import time
import os
from dataclasses import dataclass, field
from typing import Any
from pathlib import Path


AUDIT_LOG_PATH = Path("audit.jsonl")
AUDIT_SIGNING_KEY = os.environ.get("AUDIT_SIGNING_KEY", "change-me-in-production").encode()


@dataclass
class AuditEntry:
    timestamp: float
    event_type: str
    actor: str
    resource: str
    action: str
    outcome: str       # "success" | "failure" | "denied"
    metadata: dict = field(default_factory=dict)
    signature: str = ""


def sign_entry(entry_dict: dict, key: bytes) -> str:
    payload = json.dumps(
        {k: v for k, v in entry_dict.items() if k != "signature"},
        sort_keys=True,
    ).encode()
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def verify_entry(entry_dict: dict, key: bytes) -> bool:
    expected = sign_entry(entry_dict, key)
    return hmac.compare_digest(expected, entry_dict.get("signature", ""))


class SecureAuditLogger:
    def __init__(
        self,
        path: Path = AUDIT_LOG_PATH,
        signing_key: bytes = AUDIT_SIGNING_KEY,
    ):
        self.path = path
        self.signing_key = signing_key

    def log(
        self,
        event_type: str,
        actor: str,
        resource: str,
        action: str,
        outcome: str,
        metadata: dict | None = None,
    ):
        # Redact sensitive fields from metadata
        safe_metadata = redact_payload(metadata or {})

        entry = AuditEntry(
            timestamp=time.time(),
            event_type=event_type,
            actor=actor,
            resource=resource,
            action=action,
            outcome=outcome,
            metadata=safe_metadata,
        )
        entry_dict = entry.__dict__.copy()
        entry_dict["signature"] = sign_entry(entry_dict, self.signing_key)

        with open(self.path, "a") as f:
            f.write(json.dumps(entry_dict) + "\n")

    def verify_log(self) -> list[dict]:
        """Return entries that failed signature verification (tampered)."""
        tampered = []
        try:
            with open(self.path) as f:
                for i, line in enumerate(f):
                    entry = json.loads(line)
                    if not verify_entry(entry, self.signing_key):
                        tampered.append({"line": i + 1, "entry": entry})
        except FileNotFoundError:
            pass
        return tampered


# ── Usage ────────────────────────────────────────────────────────────
def redact_payload(data):
    # Re-use Solution 2's redact_payload
    return data  # placeholder — use actual implementation above

audit = SecureAuditLogger()

audit.log(
    event_type="tool_call",
    actor="agent:orchestrator",
    resource="web_search",
    action="execute",
    outcome="success",
    metadata={
        "query": "AI agent best practices",
        "api_key": "sk-secret-key",  # will be redacted
        "result_count": 5,
    },
)

tampered = audit.verify_log()
print(f"Tampered entries: {len(tampered)}")
```

## Solution 4: Streaming redaction filter for large model response chunks

When streaming model output, redact each chunk before it reaches the log sink. Pattern matching across chunk boundaries requires a sliding buffer.

```python
import asyncio
import re
from typing import AsyncIterator


class StreamingRedactor:
    """
    Applies regex redaction to a stream of text chunks.
    Maintains a lookahead buffer to handle patterns that span chunk boundaries.
    """

    def __init__(self, patterns: list[tuple[re.Pattern, str]], buffer_size: int = 256):
        self.patterns = patterns
        self.buffer_size = buffer_size
        self._buffer = ""

    def _redact(self, text: str) -> str:
        for pattern, replacement in self.patterns:
            text = pattern.sub(replacement, text)
        return text

    async def filter(self, stream: AsyncIterator[str]) -> AsyncIterator[str]:
        """Yield redacted chunks from an async text stream."""
        async for chunk in stream:
            self._buffer += chunk
            # Hold back enough to catch cross-boundary patterns
            if len(self._buffer) > self.buffer_size * 2:
                safe_to_flush = self._buffer[:-self.buffer_size]
                self._buffer = self._buffer[-self.buffer_size:]
                yield self._redact(safe_to_flush)

        # Flush remainder
        if self._buffer:
            yield self._redact(self._buffer)
            self._buffer = ""


# ── Example streaming patterns ────────────────────────────────────────
STREAM_PATTERNS = [
    (re.compile(r"sk-[a-zA-Z0-9]{20,}"), "[REDACTED:API_KEY]"),
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"), "[REDACTED:EMAIL]"),
]

redactor = StreamingRedactor(STREAM_PATTERNS)


async def fake_model_stream() -> AsyncIterator[str]:
    chunks = [
        "The user's email is ali",
        "ce@example.com and their ",
        "API key is sk-1234567890abcdef",
        "1234 — please keep this safe.",
    ]
    for chunk in chunks:
        await asyncio.sleep(0.01)
        yield chunk


async def log_streaming_response(stream: AsyncIterator[str]):
    log_lines = []
    async for chunk in redactor.filter(stream):
        log_lines.append(chunk)
    full_log = "".join(log_lines)
    print(f"Logged (redacted): {full_log}")


asyncio.run(log_streaming_response(fake_model_stream()))
# Output: "The user's email is [REDACTED:EMAIL] and their API key is [REDACTED:API_KEY] — please keep this safe."
```

## Solution 5: Differential privacy noise injection for numeric audit metrics

For numeric fields in audit logs (counts, durations, user IDs as integers), apply Laplace noise to prevent re-identification while preserving statistical utility.

```python
import math
import random
from typing import Any


class DifferentialPrivacyAuditor:
    """
    Adds calibrated Laplace noise to numeric fields before logging.
    Sensitivity and epsilon are configured per field.
    """

    def __init__(self, epsilon: float = 1.0):
        self.epsilon = epsilon  # privacy budget; smaller = more private

    def _laplace_noise(self, sensitivity: float) -> float:
        scale = sensitivity / self.epsilon
        u = random.uniform(-0.5, 0.5)
        return -scale * math.copysign(1, u) * math.log(1 - 2 * abs(u))

    def privatize(
        self,
        value: Any,
        sensitivity: float = 1.0,
        min_val: float | None = None,
        max_val: float | None = None,
    ) -> Any:
        if not isinstance(value, (int, float)):
            return value
        noisy = value + self._laplace_noise(sensitivity)
        if min_val is not None:
            noisy = max(min_val, noisy)
        if max_val is not None:
            noisy = min(max_val, noisy)
        return round(noisy, 2)

    def privatize_record(self, record: dict, field_configs: dict[str, dict]) -> dict:
        """
        field_configs: {field_name: {"sensitivity": 1.0, "min": 0, "max": None}}
        """
        result = dict(record)
        for field, config in field_configs.items():
            if field in result:
                result[field] = self.privatize(
                    result[field],
                    sensitivity=config.get("sensitivity", 1.0),
                    min_val=config.get("min"),
                    max_val=config.get("max"),
                )
        return result


# ── Usage ────────────────────────────────────────────────────────────
dp = DifferentialPrivacyAuditor(epsilon=0.5)

FIELD_CONFIGS = {
    "user_id": {"sensitivity": 100, "min": 0},
    "request_count": {"sensitivity": 5, "min": 0},
    "latency_ms": {"sensitivity": 50, "min": 0},
}

raw_record = {"user_id": 12345, "request_count": 47, "latency_ms": 234, "action": "search"}
private_record = dp.privatize_record(raw_record, FIELD_CONFIGS)
print(f"Raw:     {raw_record}")
print(f"Private: {private_record}")
# user_id and counts are noised; action (string) is unchanged
```

## Solution 6: Audit log compliance scanner — detect unredacted sensitive data at rest

Scan existing audit log files for unredacted PII and secrets. Run as a scheduled job or CI step.

```python
import re
import json
from pathlib import Path
from dataclasses import dataclass
from typing import Iterator


DETECTION_PATTERNS = [
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b")),
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("CREDIT_CARD", re.compile(r"\b\d{4}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}\b")),
    ("API_KEY", re.compile(r"sk-[a-zA-Z0-9]{20,}")),
    ("GITHUB_TOKEN", re.compile(r"ghp_[a-zA-Z0-9]{36}")),
    ("BEARER_TOKEN", re.compile(r"Bearer\s+[a-zA-Z0-9\-._~+/]+=*", re.IGNORECASE)),
    ("PHONE", re.compile(r"\b\+?1?\s?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}\b")),
]


@dataclass
class ScanViolation:
    file: str
    line_number: int
    violation_type: str
    excerpt: str
    field_path: str | None = None


def scan_jsonl_file(path: Path) -> Iterator[ScanViolation]:
    with open(path) as f:
        for line_num, line in enumerate(f, 1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            # Flatten the record to key-path: value pairs for precise reporting
            for key_path, value in _flatten(record):
                if not isinstance(value, str):
                    continue
                for ptype, pattern in DETECTION_PATTERNS:
                    # Skip already-redacted values
                    if "[REDACTED" in value:
                        continue
                    matches = pattern.findall(value)
                    for match in matches:
                        yield ScanViolation(
                            file=str(path),
                            line_number=line_num,
                            violation_type=ptype,
                            excerpt=match[:50],
                            field_path=key_path,
                        )


def _flatten(obj: Any, prefix: str = "") -> Iterator[tuple[str, Any]]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _flatten(v, f"{prefix}.{k}" if prefix else k)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            yield from _flatten(item, f"{prefix}[{i}]")
    else:
        yield prefix, obj


from typing import Any


def scan_audit_logs(log_dir: str = ".") -> dict:
    log_dir_path = Path(log_dir)
    violations: list[ScanViolation] = []

    for path in log_dir_path.glob("**/*.jsonl"):
        for v in scan_jsonl_file(path):
            violations.append(v)

    summary = {
        "total_violations": len(violations),
        "by_type": {},
        "files_affected": len({v.file for v in violations}),
    }
    for v in violations:
        summary["by_type"][v.violation_type] = summary["by_type"].get(v.violation_type, 0) + 1

    if violations:
        print(f"COMPLIANCE VIOLATION: {len(violations)} unredacted sensitive values found:")
        for v in violations[:10]:
            print(f"  [{v.violation_type}] {v.file}:{v.line_number} @ {v.field_path}")
    else:
        print("Compliance scan passed — no unredacted sensitive data found.")

    return summary


# ── Run as CI step ────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    result = scan_audit_logs(".")
    if result["total_violations"] > 0:
        sys.exit(1)  # Fail CI
```

## Comparison

| Approach | Coverage | Stream support | Tamper detection | DP noise | Compliance scan |
|---|---|---|---|---|---|
| Regex scrubber | Text patterns | Yes | No | No | No |
| Schema-aware field redaction | Structural | No | No | No | No |
| HMAC-signed audit sink | Full payload | No | Yes | No | No |
| Streaming chunk redactor | Text stream | Yes | No | No | No |
| Differential privacy noise | Numeric fields | No | No | Yes | No |
| At-rest compliance scanner | Full log history | N/A | No | No | Yes |

**Recommendation**: Apply **regex scrubber** (Solution 1) and **schema-aware redaction** (Solution 2) together at the point of log creation. Use the **HMAC-signed audit sink** (Solution 3) to detect tampering. Run the **compliance scanner** (Solution 6) as a nightly CI job to catch any gaps in redaction coverage.
