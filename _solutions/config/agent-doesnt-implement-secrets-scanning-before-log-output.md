---
layout: solution
title: "Agent Doesn't Implement Secrets Scanning Before Log Output"
category: config
description: "Prevent accidental leakage of API keys, tokens, and passwords into logs and monitoring systems with pre-output secrets scanning."
tags: [secrets, security, logging, scanning, redaction, compliance]
---

# Agent Doesn't Implement Secrets Scanning Before Log Output

AI agents frequently log tool inputs, outputs, and intermediate state for debugging. Without secrets scanning, API keys, bearer tokens, database passwords, and PII silently propagate into log aggregators, monitoring dashboards, and error trackers — where they persist indefinitely.

## Option 1: Regex Pattern Scanner with Redaction

```python
import re
import anthropic

# Common secret patterns
SECRET_PATTERNS = [
    (re.compile(r'sk-[A-Za-z0-9]{20,}'), "ANTHROPIC_KEY"),
    (re.compile(r'ghp_[A-Za-z0-9]{36}'), "GITHUB_PAT"),
    (re.compile(r'xoxb-[0-9]+-[A-Za-z0-9]+'), "SLACK_BOT_TOKEN"),
    (re.compile(r'(?i)(password|passwd|pwd)\s*[:=]\s*\S+'), "PASSWORD"),
    (re.compile(r'(?i)(api[_-]?key|apikey)\s*[:=]\s*\S+'), "API_KEY"),
    (re.compile(r'Bearer\s+[A-Za-z0-9\-._~+/]+=*'), "BEARER_TOKEN"),
    (re.compile(r'[A-Za-z0-9+/]{40,}={0,2}'), "BASE64_SECRET"),
    (re.compile(r'\b[A-Za-z0-9]{32,}\b'), "LONG_TOKEN"),
]


def redact(text: str) -> str:
    for pattern, label in SECRET_PATTERNS:
        text = pattern.sub(f"[REDACTED:{label}]", text)
    return text


def safe_log(label: str, content: str) -> None:
    print(f"[LOG] {label}: {redact(content)}")


client = anthropic.Anthropic()


def run_agent(user_input: str) -> str:
    safe_log("user_input", user_input)

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": user_input}],
    )

    result = response.content[0].text
    safe_log("agent_response", result)
    return result


if __name__ == "__main__":
    # Simulate accidental secret in prompt
    run_agent("Connect using api_key=sk-abc123xyz789DEADBEEF and fetch user data")

# Expected Token Savings: N/A (security pattern, not cost-saving)
# Environment: Any; add SECRET_PATTERNS entries for your stack's token formats
```

## Option 2: Entropy-Based High-Entropy String Detector

```python
import math
import re
import anthropic


def shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    freq = {}
    for ch in s:
        freq[ch] = freq.get(ch, 0) + 1
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in freq.values())


def is_likely_secret(token: str, min_len: int = 16, min_entropy: float = 3.5) -> bool:
    return len(token) >= min_len and shannon_entropy(token) >= min_entropy


TOKEN_RE = re.compile(r'[A-Za-z0-9+/\-_=]{16,}')


def redact_high_entropy(text: str) -> str:
    def maybe_redact(m: re.Match) -> str:
        token = m.group(0)
        if is_likely_secret(token):
            return f"[REDACTED:HIGH_ENTROPY({len(token)}c,H={shannon_entropy(token):.1f})]"
        return token

    return TOKEN_RE.sub(maybe_redact, text)


def safe_log(label: str, content: str) -> None:
    cleaned = redact_high_entropy(content)
    print(f"[LOG] {label}: {cleaned}")


client = anthropic.Anthropic()


def run_agent(prompt: str) -> str:
    safe_log("prompt", prompt)

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )

    output = response.content[0].text
    safe_log("output", output)
    return output


if __name__ == "__main__":
    run_agent("Token: eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1c2VyIjoiYWxpY2UifQ.signature")

# Expected Token Savings: N/A (entropy scanning is CPU-only, no API calls)
# Environment: Python 3.9+; tune min_len and min_entropy thresholds for your token formats
```

## Option 3: LLM-Assisted Secret Classification (Haiku Judge)

```python
import anthropic
import json

client = anthropic.Anthropic()

JUDGE_SYSTEM = """You are a security scanner. Given a text snippet, identify all substrings that appear to be secrets (API keys, tokens, passwords, private keys, connection strings, credentials). Return JSON with a list of {value, type, start, end} for each secret found. If none found, return {secrets: []}. Be conservative: only flag clear secrets, not generic long strings."""


def classify_secrets(text: str) -> list[dict]:
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=JUDGE_SYSTEM,
        messages=[{"role": "user", "content": f"Scan this text:\n\n{text[:2000]}"}],
    )
    raw = resp.content[0].text
    try:
        data = json.loads(raw)
        return data.get("secrets", [])
    except json.JSONDecodeError:
        return []


def redact_with_llm(text: str) -> str:
    secrets = classify_secrets(text)
    for s in sorted(secrets, key=lambda x: x.get("start", 0), reverse=True):
        val = s.get("value", "")
        if val and val in text:
            text = text.replace(val, f"[REDACTED:{s.get('type','SECRET')}]")
    return text


def safe_log(label: str, content: str) -> None:
    redacted = redact_with_llm(content)
    print(f"[LOG] {label}: {redacted}")


def run_agent(user_prompt: str) -> str:
    safe_log("user_prompt", user_prompt)

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": user_prompt}],
    )

    result = response.content[0].text
    safe_log("response", result)
    return result


if __name__ == "__main__":
    run_agent("Use password=S3cr3t!Pass to authenticate to postgres://user@db.internal/prod")

# Expected Token Savings: Adds ~100-150 haiku tokens per log call; use sparingly on high-value logs
# Environment: Python 3.9+; LLM judge catches novel/obfuscated secrets that regex misses
```

## Option 4: SQLite Audit Trail with Redaction Log

```python
import re
import sqlite3
import hashlib
import time
from dataclasses import dataclass, field
from contextlib import contextmanager
import anthropic

DB_PATH = "secrets_audit.db"
SECRET_PATTERNS = [
    (re.compile(r'sk-[A-Za-z0-9]{20,}'), "ANTHROPIC_KEY"),
    (re.compile(r'(?i)(password|token|secret|key)\s*[:=]\s*(\S+)'), "CREDENTIAL"),
    (re.compile(r'Bearer\s+\S+'), "BEARER_TOKEN"),
    (re.compile(r'[A-Fa-f0-9]{32,}'), "HEX_TOKEN"),
]


@dataclass
class RedactionEvent:
    timestamp: float
    label: str
    original_hash: str
    pattern_type: str
    redacted_count: int


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS redaction_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL,
            label TEXT,
            original_hash TEXT,
            pattern_type TEXT,
            redacted_count INTEGER
        )
    """)
    conn.commit()


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    try:
        yield conn
    finally:
        conn.close()


def redact_and_audit(label: str, text: str) -> str:
    original_hash = hashlib.sha256(text.encode()).hexdigest()[:16]
    events: list[RedactionEvent] = []

    for pattern, ptype in SECRET_PATTERNS:
        matches = pattern.findall(text)
        if matches:
            count = len(matches)
            text = pattern.sub(f"[REDACTED:{ptype}]", text)
            events.append(RedactionEvent(
                timestamp=time.time(),
                label=label,
                original_hash=original_hash,
                pattern_type=ptype,
                redacted_count=count,
            ))

    if events:
        with get_db() as conn:
            conn.executemany(
                "INSERT INTO redaction_log VALUES (NULL,?,?,?,?,?)",
                [(e.timestamp, e.label, e.original_hash, e.pattern_type, e.redacted_count)
                 for e in events],
            )
            conn.commit()
        print(f"[AUDIT] Redacted {sum(e.redacted_count for e in events)} secret(s) in '{label}'")

    return text


def safe_log(label: str, content: str) -> None:
    clean = redact_and_audit(label, content)
    print(f"[LOG] {label}: {clean}")


client = anthropic.Anthropic()


def run_agent(prompt: str) -> str:
    safe_log("input", prompt)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    output = resp.content[0].text
    safe_log("output", output)
    return output


def print_audit_report() -> None:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT label, pattern_type, SUM(redacted_count) FROM redaction_log GROUP BY label, pattern_type"
        ).fetchall()
    print("\n=== Redaction Audit Report ===")
    for label, ptype, count in rows:
        print(f"  {label} | {ptype}: {count} redaction(s)")


if __name__ == "__main__":
    run_agent("Connect with token=ghp_abc123XYZ456 to GitHub API")
    print_audit_report()

# Expected Token Savings: N/A; SQLite audit adds compliance evidence at zero API cost
# Environment: Python 3.9+, SQLite3; audit DB persists across runs for compliance reporting
```

## Option 5: Streaming Output Interceptor

```python
import re
import anthropic
from collections.abc import Iterator

SECRET_PATTERNS = [
    (re.compile(r'sk-[A-Za-z0-9]{20,}'), "ANTHROPIC_KEY"),
    (re.compile(r'(?i)password\s*[:=]\s*\S+'), "PASSWORD"),
    (re.compile(r'Bearer\s+\S+'), "BEARER_TOKEN"),
    (re.compile(r'[A-Za-z0-9+/]{40,}={0,2}'), "TOKEN"),
]

CHUNK_BUFFER_SIZE = 128  # bytes to buffer before scanning


def redact(text: str) -> str:
    for pattern, label in SECRET_PATTERNS:
        text = pattern.sub(f"[REDACTED:{label}]", text)
    return text


def stream_with_redaction(prompt: str) -> Iterator[str]:
    """Yield redacted chunks from a streaming response."""
    client = anthropic.Anthropic()
    buffer = ""

    with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        for chunk in stream.text_stream:
            buffer += chunk

            # Flush when buffer is large enough to scan safely
            if len(buffer) >= CHUNK_BUFFER_SIZE:
                # Keep tail to avoid splitting a secret across chunk boundary
                safe_len = max(0, len(buffer) - 64)
                safe_part = buffer[:safe_len]
                buffer = buffer[safe_len:]

                redacted = redact(safe_part)
                yield redacted

        # Flush remaining buffer
        if buffer:
            yield redact(buffer)


def run_streaming_agent(prompt: str) -> str:
    print("[STREAM] ", end="", flush=True)
    full_output = ""
    for chunk in stream_with_redaction(prompt):
        print(chunk, end="", flush=True)
        full_output += chunk
    print()
    return full_output


if __name__ == "__main__":
    run_streaming_agent(
        "Here is a config: DB_PASSWORD=hunter2 and API_TOKEN=sk-abc123XYZ789DEADBEEF456. "
        "Summarize what this config does."
    )

# Expected Token Savings: N/A; streaming interceptor adds zero API overhead
# Environment: Python 3.9+; CHUNK_BUFFER_SIZE tunes latency vs. split-secret safety
```

## Option 6: Multi-Layer Pipeline with Allow-List and Severity Scoring

```python
import re
import math
import sqlite3
import time
import hashlib
from dataclasses import dataclass
from enum import IntEnum
import anthropic


class Severity(IntEnum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4


@dataclass
class SecretFinding:
    pattern_name: str
    severity: Severity
    redacted_value: str
    position: int


PATTERN_REGISTRY: list[tuple[re.Pattern, str, Severity]] = [
    (re.compile(r'sk-[A-Za-z0-9]{20,}'), "ANTHROPIC_KEY", Severity.CRITICAL),
    (re.compile(r'ghp_[A-Za-z0-9]{36}'), "GITHUB_PAT", Severity.CRITICAL),
    (re.compile(r'xoxb-[0-9]+-[A-Za-z0-9]+'), "SLACK_BOT", Severity.HIGH),
    (re.compile(r'(?i)password\s*[:=]\s*(\S+)'), "PASSWORD", Severity.HIGH),
    (re.compile(r'Bearer\s+[A-Za-z0-9\-._~+/]+=*'), "BEARER_TOKEN", Severity.MEDIUM),
    (re.compile(r'(?i)api[_-]?key\s*[:=]\s*(\S+)'), "API_KEY", Severity.MEDIUM),
    (re.compile(r'[A-Fa-f0-9]{32}'), "HEX_DIGEST", Severity.LOW),
]

ALLOW_LIST = {
    "0000000000000000000000000000000000000000",  # git null SHA
    "deadbeef",
}


def shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    freq: dict[str, int] = {}
    for ch in s:
        freq[ch] = freq.get(ch, 0) + 1
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in freq.values())


def scan_text(text: str) -> tuple[str, list[SecretFinding]]:
    findings: list[SecretFinding] = []
    for pattern, name, severity in PATTERN_REGISTRY:
        for m in pattern.finditer(text):
            val = m.group(0)
            if val.lower() in ALLOW_LIST:
                continue
            # Skip low-entropy matches for LOW severity
            if severity == Severity.LOW and shannon_entropy(val) < 3.0:
                continue
            findings.append(SecretFinding(
                pattern_name=name,
                severity=severity,
                redacted_value=f"[REDACTED:{name}]",
                position=m.start(),
            ))
        text = pattern.sub(f"[REDACTED:{name}]", text)

    return text, findings


DB_PATH = "secrets_pipeline.db"


def ensure_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS findings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL, call_label TEXT, pattern_name TEXT,
            severity INTEGER, text_hash TEXT
        )
    """)
    conn.commit()
    return conn


def safe_log(label: str, text: str, conn: sqlite3.Connection) -> str:
    redacted, findings = scan_text(text)
    text_hash = hashlib.sha256(text.encode()).hexdigest()[:16]

    if findings:
        conn.executemany(
            "INSERT INTO findings VALUES (NULL,?,?,?,?,?)",
            [(time.time(), label, f.pattern_name, int(f.severity), text_hash)
             for f in findings],
        )
        conn.commit()

        max_sev = max(f.severity for f in findings)
        print(f"[SECURITY] {label}: {len(findings)} secret(s) redacted "
              f"(max severity={max_sev.name})")

        if max_sev >= Severity.CRITICAL:
            print(f"[ALERT] CRITICAL secret detected in '{label}' — check your code!")

    return redacted


def severity_report(conn: sqlite3.Connection) -> None:
    rows = conn.execute("""
        SELECT call_label, pattern_name, severity, COUNT(*) as cnt
        FROM findings
        GROUP BY call_label, pattern_name, severity
        ORDER BY severity DESC, cnt DESC
    """).fetchall()

    print("\n=== Secret Findings Report ===")
    if not rows:
        print("  No secrets detected.")
        return
    for label, pname, sev, cnt in rows:
        sname = Severity(sev).name
        print(f"  [{sname}] {label} | {pname}: {cnt}x")


client = anthropic.Anthropic()


def run_agent(prompt: str) -> str:
    conn = ensure_db()
    try:
        clean_prompt = safe_log("prompt", prompt, conn)

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            messages=[{"role": "user", "content": clean_prompt}],
        )

        output = response.content[0].text
        clean_output = safe_log("response", output, conn)

        severity_report(conn)
        return clean_output
    finally:
        conn.close()


if __name__ == "__main__":
    run_agent(
        "Config: ANTHROPIC_API_KEY=sk-realkey12345ABCDEFGHIJ, "
        "DB_PASSWORD=MyS3cr3t!, Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig. "
        "What should I do with this config?"
    )

# Expected Token Savings: N/A; pipeline adds CRITICAL alerts at zero extra API cost
# Environment: Python 3.9+, SQLite3; extend PATTERN_REGISTRY for custom token formats
```

## Comparison

| Option | Detection Method | False Positive Risk | Audit Trail | Streaming Support | Best For |
|--------|-----------------|--------------------|--------------|--------------------|----------|
| 1. Regex Scanner | Pattern matching | Medium | No | Manual | Quick setup, known formats |
| 2. Entropy Detector | Shannon entropy | Low-Medium | No | Manual | Unknown token formats |
| 3. LLM Judge | Haiku classification | Low | No | No | Novel/obfuscated secrets |
| 4. SQLite Audit | Regex + DB log | Medium | Yes | No | Compliance reporting |
| 5. Stream Interceptor | Regex on chunks | Medium | No | Yes | Streaming agent outputs |
| 6. Multi-Layer Pipeline | Regex+entropy+severity | Low | Yes | No | Production security hardening |
