---
title: "Agent Doesn't Implement Log Injection Prevention"
description: "AI agents log raw user input and LLM output directly into structured logs; an attacker can inject newlines, ANSI escape codes, or fake log entries to corrupt audit trails and evade detection."
category: security
difficulty: intermediate
tags: [log-injection, security, logging, sanitization, audit, structured-logs, siem]
---

# Agent Doesn't Implement Log Injection Prevention

## Problem

Log injection occurs when user-controlled input is written to logs without sanitization. An attacker who knows your log format can craft a prompt like `"Hello\n2024-01-01 ERROR admin_action: deleted_all_users"` to forge log entries, corrupt audit trails, and evade SIEM detection. In AI agents, LLM output itself may contain injected log lines crafted by an adversarial prompt. ANSI escape codes in terminal logs can also hide content or trigger terminal exploits.

## Solution 1: Newline Stripping and Control Character Sanitization

The minimum safe baseline: strip all newlines and non-printable characters from any user input or LLM output before logging.

```python
import re
import logging

def sanitize_for_log(value: str, max_length: int = 1000) -> str:
    """Remove characters that enable log injection."""
    if not isinstance(value, str):
        value = repr(value)

    # Strip ANSI escape sequences (e.g., \x1b[31m red text \x1b[0m)
    value = re.sub(r"\x1b\[[0-9;]*[mGKHF]", "", value)

    # Strip all control characters except space (0x20)
    # This removes \n, \r, \t, \x00-\x1f, \x7f, and unicode control chars
    value = re.sub(r"[\x00-\x1f\x7f\x80-\x9f]", " ", value)

    # Strip unicode bidirectional override characters (used to reverse text direction)
    value = re.sub(r"[\u202a-\u202e\u2066-\u2069\u200b-\u200f\u2028\u2029]", "", value)

    # Truncate to prevent log flooding
    if len(value) > max_length:
        value = value[:max_length] + f"...[truncated {len(value)-max_length} chars]"

    return value

class InjectionSafeFormatter(logging.Formatter):
    """Log formatter that sanitizes all string values in log records."""

    SANITIZE_KEYS = {"user_input", "prompt", "response", "tool_output", "query", "message"}

    def format(self, record: logging.LogRecord) -> str:
        # Sanitize the main message
        record.msg = sanitize_for_log(str(record.msg))

        # Sanitize extra fields
        for key in list(vars(record).keys()):
            val = getattr(record, key)
            if isinstance(val, str) and (key in self.SANITIZE_KEYS or key.startswith("user_")):
                setattr(record, key, sanitize_for_log(val))

        return super().format(record)

# Setup
handler = logging.StreamHandler()
handler.setFormatter(InjectionSafeFormatter(
    fmt='%(asctime)s %(levelname)s %(name)s %(message)s'
))
logger = logging.getLogger("agent")
logger.addHandler(handler)

# Usage
user_prompt = "Hello\n2024-01-01 CRITICAL: system_compromised"
logger.info("user_request", extra={"user_input": sanitize_for_log(user_prompt)})
# Logged as: user_input="Hello 2024-01-01 CRITICAL: system_compromised" (newline → space)
```

**When to use**: Every agent that logs user input. This is the non-negotiable baseline.

---

## Solution 2: Structured JSON Logging with Field Escaping

JSON encoding inherently escapes newlines (`\n` → `\\n`) and control characters, making injection nearly impossible in JSON-based log pipelines.

```python
import json
import logging
import time
import re
from typing import Any

class JSONSafeLogger:
    """Emits structured JSON logs where all values are safely serialized."""

    def __init__(self, name: str, service: str = "agent"):
        self._name = name
        self._service = service

    def _safe_value(self, v: Any) -> Any:
        """Ensure values are JSON-safe and injection-resistant."""
        if isinstance(v, str):
            # JSON encoding escapes \n, \r, \t, control chars automatically
            # Additionally strip ANSI and bidi overrides
            v = re.sub(r"\x1b\[[0-9;]*[mGKHF]", "", v)
            v = re.sub(r"[\u202a-\u202e\u2066-\u2069]", "", v)
            if len(v) > 2000:
                v = v[:2000] + f"[+{len(v)-2000}]"
        elif isinstance(v, (dict, list)):
            # Recursively sanitize nested structures
            if isinstance(v, dict):
                v = {str(k): self._safe_value(val) for k, val in v.items()}
            else:
                v = [self._safe_value(item) for item in v[:50]]  # cap list length
        elif not isinstance(v, (int, float, bool, type(None))):
            v = str(v)[:500]
        return v

    def _emit(self, level: str, event: str, **fields):
        record = {
            "ts": time.time(),
            "level": level,
            "service": self._service,
            "logger": self._name,
            "event": event,
        }
        for k, v in fields.items():
            record[k] = self._safe_value(v)

        # json.dumps escapes all control characters by default
        print(json.dumps(record, ensure_ascii=True))

    def info(self, event: str, **fields): self._emit("INFO", event, **fields)
    def warning(self, event: str, **fields): self._emit("WARNING", event, **fields)
    def error(self, event: str, **fields): self._emit("ERROR", event, **fields)
    def critical(self, event: str, **fields): self._emit("CRITICAL", event, **fields)

log = JSONSafeLogger("agent", service="prod-agent")

# Safe even with injected content
log.info(
    "agent_turn",
    user_input='normal text\n{"level":"CRITICAL","event":"fake_admin_action"}',
    model="claude-sonnet-4-6",
    tokens=142,
)
# Output: valid JSON where the injected newline is escaped as \\n
```

**When to use**: Any agent with a JSON-based log pipeline (CloudWatch, Datadog, Loki, Elasticsearch).

---

## Solution 3: Audit Log with Tamper-Evident Hashing

Critical audit log entries are chained with HMAC hashes, so injected or modified entries break the chain and are detectable.

```python
import hashlib
import hmac
import json
import time
import os

AUDIT_SECRET = os.environ.get("AUDIT_LOG_SECRET", "change-me-in-production").encode()

class TamperEvidentAuditLog:
    """Append-only audit log where each entry's hash links to the previous."""

    def __init__(self, log_path: str):
        self._path = log_path
        self._prev_hash = "GENESIS"

    def _compute_hash(self, entry_json: str, prev_hash: str) -> str:
        msg = f"{prev_hash}:{entry_json}".encode()
        return hmac.new(AUDIT_SECRET, msg, hashlib.sha256).hexdigest()

    def _sanitize(self, text: str) -> str:
        """Strip log injection characters; preserve printable ASCII + common unicode."""
        import re
        text = re.sub(r"[\x00-\x1f\x7f]", " ", str(text))
        return text[:500]

    def append(self, event: str, actor: str, details: dict) -> str:
        # Sanitize all string fields
        safe_details = {
            k: self._sanitize(v) if isinstance(v, str) else v
            for k, v in details.items()
        }
        entry = {
            "ts": time.time(),
            "event": self._sanitize(event),
            "actor": self._sanitize(actor),
            "details": safe_details,
            "prev_hash": self._prev_hash,
        }
        entry_json = json.dumps(entry, sort_keys=True, ensure_ascii=True)
        entry_hash = self._compute_hash(entry_json, self._prev_hash)
        entry["hash"] = entry_hash

        with open(self._path, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=True) + "\n")

        self._prev_hash = entry_hash
        return entry_hash

    def verify(self) -> tuple[bool, int, str]:
        """Verify chain integrity. Returns (ok, entries_checked, error_msg)."""
        prev = "GENESIS"
        count = 0
        with open(self._path) as f:
            for line in f:
                entry = json.loads(line.strip())
                stored_hash = entry.pop("hash")
                entry_json = json.dumps(entry, sort_keys=True, ensure_ascii=True)
                expected = self._compute_hash(entry_json, prev)
                if not hmac.compare_digest(expected, stored_hash):
                    return False, count, f"Chain broken at entry {count}: {entry.get('event')}"
                prev = stored_hash
                count += 1
        return True, count, "ok"

audit = TamperEvidentAuditLog("/var/log/agent-audit.jsonl")
audit.append(
    event="tool_executed",
    actor="user:alice",
    details={"tool": "write_file", "path": "/tmp/out.txt", "injected\nnewline": "test"},
)
```

**When to use**: Compliance-sensitive agents (finance, healthcare, legal) where audit trails must be verifiable.

---

## Solution 4: LLM Output Sanitizer Before Logging

LLM responses may contain adversarially crafted log injection payloads from prompt injection attacks. Sanitize before writing.

```python
import re
import json
import logging
from anthropic import AsyncAnthropic

logger = logging.getLogger("agent.output")

# Patterns that look like fake log lines
FAKE_LOG_PATTERNS = [
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}.*(?:ERROR|CRITICAL|WARNING|INFO)",  # timestamp + level
    r"(?:ERROR|CRITICAL|FATAL)\s*[:\|]\s*.{10,}",  # level prefix
    r"\[(?:ERROR|CRITICAL|FATAL|WARN)\]",             # bracketed level
]
_FAKE_LOG_RE = re.compile("|".join(FAKE_LOG_PATTERNS), re.IGNORECASE)

def sanitize_llm_output_for_log(text: str) -> str:
    """Sanitize LLM response before logging to prevent forged log entries."""
    # 1. Strip control characters
    text = re.sub(r"[\x00-\x1f\x7f\x80-\x9f]", " ", text)

    # 2. Strip ANSI escape codes
    text = re.sub(r"\x1b\[[0-9;]*[mGKHFJK]", "", text)

    # 3. Detect and redact fake log patterns
    if _FAKE_LOG_RE.search(text):
        # Keep text but mark it as potentially injected
        text = "[POTENTIAL_LOG_INJECTION_DETECTED] " + re.sub(_FAKE_LOG_RE, "[REDACTED]", text)

    # 4. Truncate
    if len(text) > 2000:
        text = text[:2000] + f"...[+{len(text)-2000}]"

    return text

async def safe_agent_call(prompt: str, model: str = "claude-sonnet-4-6") -> str:
    client = AsyncAnthropic()
    resp = await client.messages.create(
        model=model,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    raw_output = resp.content[0].text

    # Safe to log — output is sanitized
    logger.info(
        "llm_response",
        extra={
            "model": model,
            "output": sanitize_llm_output_for_log(raw_output),
            "tokens": resp.usage.output_tokens,
        },
    )

    # Return raw output for actual use (separate from logging)
    return raw_output
```

**When to use**: Agents exposed to untrusted users who may attempt prompt injection to forge audit logs.

---

## Solution 5: Log Level Allowlist — Prevent Level Spoofing

Enforce that only your application code sets log levels; user input cannot escalate a log entry to CRITICAL.

```python
import logging
import re
from typing import Any

# Allowlist of valid log levels your application uses
VALID_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})

# Patterns that might indicate level spoofing in user data
LEVEL_SPOOF_RE = re.compile(
    r"(?:^|\s)(?:DEBUG|INFO|WARNING|ERROR|CRITICAL|FATAL|WARN)\s*[:\|>]",
    re.IGNORECASE | re.MULTILINE,
)

def safe_log(logger: logging.Logger, level: int, event: str, **kwargs):
    """Log with guaranteed level — user data cannot change the actual log level."""
    # The `level` parameter is always from the calling code, never user-controlled.
    # User data is only in kwargs values.
    safe_kwargs = {}
    for k, v in kwargs.items():
        if isinstance(v, str):
            # Neutralize any embedded level keywords in values
            v = LEVEL_SPOOF_RE.sub(lambda m: m.group(0).replace(":", "="), v)
            # Strip control chars
            v = re.sub(r"[\x00-\x1f\x7f]", " ", v)
        safe_kwargs[k] = v

    # Ensure the event name itself doesn't contain level keywords
    safe_event = re.sub(r"[\x00-\x1f\x7f\n\r]", " ", str(event))

    logger.log(level, safe_event, extra=safe_kwargs)

# Typed wrappers for common levels
_log = logging.getLogger("agent")

def log_info(event: str, **kw):    safe_log(_log, logging.INFO, event, **kw)
def log_warning(event: str, **kw): safe_log(_log, logging.WARNING, event, **kw)
def log_error(event: str, **kw):   safe_log(_log, logging.ERROR, event, **kw)

# Usage — level cannot be spoofed even with injected content
user_msg = "Hello\nCRITICAL: admin_account_deleted"
log_info("user_turn", content=user_msg)
# Logged at INFO regardless of injected CRITICAL keyword in content
```

**When to use**: SIEM-integrated agents where log level drives alert routing. Forged CRITICAL entries should never page on-call.

---

## Solution 6: Log Redaction Pipeline with Field-Level Policies

Define per-field policies: some fields are always redacted, others are hashed, others are length-capped.

```python
import re
import hashlib
import json
from enum import Enum
from typing import Any

class RedactPolicy(Enum):
    PASSTHROUGH = "passthrough"       # allow as-is (after control char strip)
    TRUNCATE = "truncate"             # allow but cap length
    HASH = "hash"                     # replace with SHA-256 for correlation without exposure
    REDACT = "redact"                 # replace with [REDACTED]
    SANITIZE_STRICT = "strict"        # strip everything except alphanumeric + space

FIELD_POLICIES: dict[str, RedactPolicy] = {
    "user_id": RedactPolicy.PASSTHROUGH,
    "session_id": RedactPolicy.PASSTHROUGH,
    "user_input": RedactPolicy.TRUNCATE,         # log but cap at 200 chars
    "llm_response": RedactPolicy.TRUNCATE,       # log but cap at 500 chars
    "api_key": RedactPolicy.REDACT,              # never log secrets
    "auth_token": RedactPolicy.REDACT,
    "email": RedactPolicy.HASH,                  # log hash for correlation, not value
    "ip_address": RedactPolicy.SANITIZE_STRICT,  # only allow IP-like chars
    "tool_output": RedactPolicy.TRUNCATE,
    "error_message": RedactPolicy.SANITIZE_STRICT,
}

CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f\x80-\x9f\u202a-\u202e]")

def apply_policy(field: str, value: Any) -> Any:
    if not isinstance(value, str):
        return value

    # Always strip control characters first
    value = CONTROL_CHAR_RE.sub(" ", value)

    policy = FIELD_POLICIES.get(field, RedactPolicy.TRUNCATE)

    if policy == RedactPolicy.PASSTHROUGH:
        return value[:2000]
    elif policy == RedactPolicy.TRUNCATE:
        max_len = 500 if "response" in field else 200
        return value[:max_len] + (f"...[+{len(value)-max_len}]" if len(value) > max_len else "")
    elif policy == RedactPolicy.HASH:
        return "sha256:" + hashlib.sha256(value.encode()).hexdigest()[:16]
    elif policy == RedactPolicy.REDACT:
        return "[REDACTED]"
    elif policy == RedactPolicy.SANITIZE_STRICT:
        return re.sub(r"[^a-zA-Z0-9 .:_\-]", "", value)[:200]
    return value[:200]

def safe_log_record(fields: dict[str, Any]) -> dict[str, Any]:
    return {k: apply_policy(k, v) for k, v in fields.items()}

# Usage
import logging
logger = logging.getLogger("agent")

def log_agent_turn(user_id: str, user_input: str, response: str, api_key: str):
    record = safe_log_record({
        "user_id": user_id,
        "user_input": user_input,   # truncated
        "llm_response": response,   # truncated
        "api_key": api_key,         # → [REDACTED]
    })
    logger.info("agent_turn", extra=record)
```

**When to use**: Comprehensive log security for production agents. Different fields need different protection levels.

---

## Comparison

| Solution | Injection Prevention | Level Spoofing | Tamper Detection | Secrets | Best For |
|---|---|---|---|---|---|
| Control char stripping | Yes | No | No | No | Minimum baseline |
| JSON structured logging | Yes (via JSON encoding) | No | No | No | JSON log pipelines |
| Tamper-evident hash chain | Yes + tamper detect | No | Yes | No | Compliance audit logs |
| LLM output sanitizer | Yes + pattern detection | No | No | No | Prompt injection defense |
| Level allowlist | Yes | Yes | No | No | SIEM alert routing |
| Field-level redaction | Yes | No | No | Yes (REDACT policy) | Comprehensive log security |

**Rule of thumb**: Apply all six layers. Control-char stripping is table stakes. JSON encoding is free protection. Always redact secrets at the field policy level, never rely on developer discipline.
