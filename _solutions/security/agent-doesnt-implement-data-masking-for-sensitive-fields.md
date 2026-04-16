---
title: "Agent Doesn't Implement Data Masking for Sensitive Fields"
description: "Solutions for automatically detecting and masking PII, credentials, and sensitive business data before it reaches LLM context, logs, or API responses."
tags: [security, pii, data-masking, privacy, compliance]
difficulty: intermediate
---

## Problem

Agents receive tool results, database records, and user inputs that contain sensitive data: email addresses, phone numbers, SSNs, API keys, credit card numbers, and business secrets. Without masking, this data flows into LLM context (where it affects model behavior and may be echoed back), appears in logs (creating compliance violations), and leaks to downstream consumers.

---

## Solution 1: Regex-Based PII Detector and Redactor

Apply a regex pattern library to all text flowing into or out of the agent and replace matches with typed placeholders.

```python
import anthropic
import re
from dataclasses import dataclass
from typing import Optional

client = anthropic.Anthropic()

@dataclass
class MaskRule:
    name: str
    pattern: str
    replacement: str
    flags: int = re.IGNORECASE

PII_RULES = [
    MaskRule("email",       r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", "[EMAIL]"),
    MaskRule("phone_us",    r"\b(?:\+1\s?)?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}\b", "[PHONE]"),
    MaskRule("ssn",         r"\b\d{3}[-\s]?\d{2}[-\s]?\d{4}\b", "[SSN]"),
    MaskRule("credit_card", r"\b(?:\d{4}[\s\-]?){3}\d{4}\b", "[CARD]"),
    MaskRule("api_key",     r"\b(sk|pk|api|key|token)[-_]?[a-zA-Z0-9]{20,}\b", "[API-KEY]"),
    MaskRule("ip_address",  r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "[IP]"),
    MaskRule("aws_key",     r"\bAKIA[0-9A-Z]{16}\b", "[AWS-KEY]"),
    MaskRule("jwt",         r"\beyJ[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+\b", "[JWT]"),
    MaskRule("url_with_creds", r"https?://[^:@\s]+:[^@\s]+@[^\s]+", "[URL-WITH-CREDS]"),
]

class PIIMasker:
    def __init__(self, rules: list[MaskRule] = None, preserve_format: bool = False):
        self._rules = rules or PII_RULES
        self._compiled = [(r, re.compile(r.pattern, r.flags)) for r in self._rules]
        self._preserve_format = preserve_format
        self._mask_log: list[dict] = []

    def mask(self, text: str) -> str:
        result = text
        for rule, pattern in self._compiled:
            matches = pattern.findall(result)
            if matches:
                for match in matches:
                    m = match if isinstance(match, str) else match[0]
                    self._mask_log.append({"rule": rule.name, "length": len(m)})
                result = pattern.sub(rule.replacement, result)
        return result

    def mask_dict(self, data: dict, skip_keys: set[str] = None) -> dict:
        skip_keys = skip_keys or set()
        result = {}
        for k, v in data.items():
            if k in skip_keys:
                result[k] = v
            elif isinstance(v, str):
                result[k] = self.mask(v)
            elif isinstance(v, dict):
                result[k] = self.mask_dict(v, skip_keys)
            elif isinstance(v, list):
                result[k] = [self.mask(item) if isinstance(item, str) else item for item in v]
            else:
                result[k] = v
        return result

    def detections_summary(self) -> dict:
        from collections import Counter
        counts = Counter(e["rule"] for e in self._mask_log)
        return dict(counts)

masker = PIIMasker()

# Tool result with sensitive data
tool_result = """
User record retrieved:
Name: Alice Johnson
Email: alice.johnson@company.com
Phone: (415) 555-0142
SSN: 123-45-6789
API Key: sk-prod-a8f9b2c3d4e5f6789012345678901234
Card: 4111 1111 1111 1111
IP: 192.168.1.105
"""

masked = masker.mask(tool_result)
print("Masked tool result:")
print(masked)
print(f"\nDetections: {masker.detections_summary()}")

# Use masked content with agent
response = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=256,
    messages=[{
        "role": "user",
        "content": f"Summarize this user record:\n{masked}"
    }]
)
print(f"\nAgent response (uses masked data): {response.content[0].text[:100]}...")
```

---

## Solution 2: Schema-Aware Field-Level Masking with Allowlisting

Define per-field masking policies in a schema registry — mask by field name rather than pattern-matching content.

```python
import anthropic
import copy
import json
from dataclasses import dataclass
from typing import Any, Optional
from enum import Enum

client = anthropic.Anthropic()

class MaskPolicy(Enum):
    REDACT   = "redact"    # Replace with [FIELD_NAME]
    HASH     = "hash"      # SHA-256 hash
    PARTIAL  = "partial"   # Show first/last N chars
    SUPPRESS = "suppress"  # Remove field entirely
    ALLOW    = "allow"     # Pass through unchanged

@dataclass
class FieldPolicy:
    field_name: str
    policy: MaskPolicy
    partial_show: int = 4   # chars to show for PARTIAL
    partial_end: bool = True  # show end (True) or start (False)

FIELD_POLICIES: dict[str, FieldPolicy] = {
    "email":          FieldPolicy("email",          MaskPolicy.PARTIAL, 3, end:=False),
    "phone":          FieldPolicy("phone",          MaskPolicy.PARTIAL, 4, True),
    "ssn":            FieldPolicy("ssn",            MaskPolicy.REDACT),
    "password":       FieldPolicy("password",       MaskPolicy.SUPPRESS),
    "password_hash":  FieldPolicy("password_hash",  MaskPolicy.SUPPRESS),
    "credit_card":    FieldPolicy("credit_card",    MaskPolicy.PARTIAL, 4, True),
    "card_number":    FieldPolicy("card_number",    MaskPolicy.PARTIAL, 4, True),
    "api_key":        FieldPolicy("api_key",        MaskPolicy.PARTIAL, 8, False),
    "secret":         FieldPolicy("secret",         MaskPolicy.SUPPRESS),
    "token":          FieldPolicy("token",          MaskPolicy.HASH),
    "access_token":   FieldPolicy("access_token",   MaskPolicy.SUPPRESS),
    "refresh_token":  FieldPolicy("refresh_token",  MaskPolicy.SUPPRESS),
    "ip_address":     FieldPolicy("ip_address",     MaskPolicy.REDACT),
    "user_agent":     FieldPolicy("user_agent",     MaskPolicy.ALLOW),
    "name":           FieldPolicy("name",           MaskPolicy.ALLOW),
    "user_id":        FieldPolicy("user_id",        MaskPolicy.ALLOW),
}

SUPPRESS_SENTINEL = "__SUPPRESSED__"

def apply_field_policy(field_name: str, value: Any) -> Any:
    import hashlib
    policy_def = FIELD_POLICIES.get(field_name.lower())
    if policy_def is None:
        return value  # No policy — allow

    if not isinstance(value, str):
        return value

    policy = policy_def.policy
    if policy == MaskPolicy.ALLOW:
        return value
    elif policy == MaskPolicy.SUPPRESS:
        return SUPPRESS_SENTINEL
    elif policy == MaskPolicy.REDACT:
        return f"[{field_name.upper()}]"
    elif policy == MaskPolicy.HASH:
        return "sha256:" + hashlib.sha256(value.encode()).hexdigest()[:16]
    elif policy == MaskPolicy.PARTIAL:
        n = policy_def.partial_show
        if len(value) <= n:
            return "****"
        if policy_def.partial_end:
            return "****" + value[-n:]
        else:
            return value[:n] + "****"
    return value

def mask_object(data: Any, parent_key: str = "") -> Any:
    if isinstance(data, dict):
        result = {}
        for k, v in data.items():
            masked_v = mask_object(v, k)
            if masked_v != SUPPRESS_SENTINEL:
                result[k] = masked_v
        return result
    elif isinstance(data, list):
        return [mask_object(item, parent_key) for item in data]
    elif isinstance(data, str) and parent_key:
        return apply_field_policy(parent_key, data)
    return data

# Test
user_record = {
    "user_id": "usr_12345",
    "name": "Alice Johnson",
    "email": "alice.johnson@company.com",
    "phone": "+1-415-555-0142",
    "ssn": "123-45-6789",
    "password_hash": "$2b$12$abcdefghijklmnopqrstuuu",
    "credit_card": "4111111111111111",
    "api_key": "sk-prod-a8f9b2c3d4e5f6789012",
    "access_token": "eyJhbGciOiJSUzI1NiJ9...",
    "ip_address": "192.168.1.105",
    "user_agent": "Mozilla/5.0 (Macintosh)",
    "plan": "pro",
}

masked_record = mask_object(user_record)
print("Original fields:", list(user_record.keys()))
print("Masked record:")
print(json.dumps(masked_record, indent=2))

# Agent sees only masked data
context = f"User info: {json.dumps(masked_record)}"
response = client.messages.create(
    model="claude-haiku-4-5-20251001", max_tokens=256,
    messages=[{"role": "user", "content": f"Summarize this user account: {context}"}]
)
print(f"\nAgent output: {response.content[0].text[:120]}...")
```

---

## Solution 3: Context-Window PII Scanner with Pre-Submission Hook

Before every API call, scan the full message list for PII and either block, redact, or warn.

```python
import anthropic
import re
from dataclasses import dataclass
from typing import Optional
from copy import deepcopy

client = anthropic.Anthropic()

@dataclass
class ScanResult:
    has_pii: bool
    detections: list[dict]
    risk_level: str  # low, medium, high, critical
    action_taken: str  # none, redacted, blocked

PATTERNS = {
    "ssn":         (re.compile(r"\b\d{3}[-\s]?\d{2}[-\s]?\d{4}\b"), "critical"),
    "credit_card": (re.compile(r"\b(?:\d{4}[\s\-]?){3}\d{4}\b"), "critical"),
    "api_key":     (re.compile(r"\b(sk|pk|api_key)[-_][a-zA-Z0-9]{20,}\b", re.I), "high"),
    "email":       (re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"), "medium"),
    "phone":       (re.compile(r"\b(?:\+1\s?)?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}\b"), "medium"),
    "ip_address":  (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "low"),
}

RISK_HIERARCHY = {"low": 0, "medium": 1, "high": 2, "critical": 3}

def scan_text(text: str) -> list[dict]:
    findings = []
    for name, (pattern, severity) in PATTERNS.items():
        matches = pattern.findall(text)
        if matches:
            findings.append({"type": name, "count": len(matches), "severity": severity})
    return findings

def max_risk(findings: list[dict]) -> str:
    if not findings:
        return "low"
    return max(findings, key=lambda f: RISK_HIERARCHY[f["severity"]])["severity"]

def redact_text(text: str) -> str:
    result = text
    for name, (pattern, _) in PATTERNS.items():
        replacement = f"[{name.upper()}]"
        result = pattern.sub(replacement, result)
    return result

class PIIScanningProxy:
    def __init__(self, block_level: str = "critical", redact_level: str = "high"):
        self._block_level = RISK_HIERARCHY[block_level]
        self._redact_level = RISK_HIERARCHY[redact_level]
        self._scan_log: list[ScanResult] = []

    def _process_messages(self, messages: list[dict]) -> tuple[list[dict], ScanResult]:
        all_findings = []
        clean_messages = deepcopy(messages)

        for msg in clean_messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                findings = scan_text(content)
                all_findings.extend(findings)
                risk = max_risk(findings)

                if RISK_HIERARCHY.get(risk, 0) >= self._block_level:
                    return messages, ScanResult(True, findings, risk, "blocked")
                elif RISK_HIERARCHY.get(risk, 0) >= self._redact_level:
                    msg["content"] = redact_text(content)

        overall_risk = max_risk(all_findings)
        action = "none" if not all_findings else "redacted" if RISK_HIERARCHY.get(overall_risk, 0) >= self._redact_level else "warned"

        result = ScanResult(
            has_pii=bool(all_findings),
            detections=all_findings,
            risk_level=overall_risk,
            action_taken=action,
        )
        return clean_messages, result

    def create(self, model: str, messages: list[dict], **kwargs) -> Optional[anthropic.types.Message]:
        clean_messages, scan_result = self._process_messages(messages)
        self._scan_log.append(scan_result)

        if scan_result.action_taken == "blocked":
            raise PermissionError(
                f"Message blocked: contains {scan_result.risk_level}-risk PII "
                f"({[d['type'] for d in scan_result.detections]})"
            )

        if scan_result.action_taken == "redacted":
            print(f"[PII Scanner] Redacted {len(scan_result.detections)} PII instance(s) before sending")

        return client.messages.create(model=model, messages=clean_messages, **kwargs)

proxy = PIIScanningProxy(block_level="critical", redact_level="medium")

# Test: message with email (medium — gets redacted)
try:
    resp = proxy.create(
        model="claude-haiku-4-5-20251001", max_tokens=256,
        messages=[{"role": "user", "content": "The user alice@example.com called about their order."}]
    )
    print(f"Response: {resp.content[0].text[:80]}...")
except PermissionError as e:
    print(f"Blocked: {e}")

# Test: message with SSN (critical — gets blocked)
try:
    resp = proxy.create(
        model="claude-haiku-4-5-20251001", max_tokens=256,
        messages=[{"role": "user", "content": "Process SSN 123-45-6789 for identity verification."}]
    )
except PermissionError as e:
    print(f"Blocked: {e}")

print(f"\nTotal scans: {len(proxy._scan_log)}")
```

---

## Solution 4: Streaming Output Masker for Real-Time Redaction

Redact sensitive data from streaming responses in real-time, preventing PII from reaching the client mid-stream.

```python
import anthropic
import re
from dataclasses import dataclass

client = anthropic.Anthropic()

# Patterns with buffering context (some patterns span multiple chunks)
STREAMING_PATTERNS = [
    (re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"), "[EMAIL]"),
    (re.compile(r"\b\d{3}[-\s]?\d{2}[-\s]?\d{4}\b"), "[SSN]"),
    (re.compile(r"\b(?:\d{4}[\s\-]?){3}\d{4}\b"), "[CARD]"),
    (re.compile(r"\b(sk|pk)[-_][a-zA-Z0-9]{20,}\b", re.I), "[API-KEY]"),
]

class StreamingPIIMasker:
    """
    Buffers streaming text and applies PII masking.
    Flushes safe portions while holding back incomplete potential matches.
    """
    def __init__(self, flush_threshold: int = 50):
        self._buffer = ""
        self._flush_threshold = flush_threshold
        self._total_redacted = 0

    def _apply_masks(self, text: str) -> tuple[str, int]:
        result = text
        count = 0
        for pattern, replacement in STREAMING_PATTERNS:
            matches = len(pattern.findall(result))
            if matches:
                result = pattern.sub(replacement, result)
                count += matches
        return result, count

    def process_chunk(self, chunk: str) -> str:
        self._buffer += chunk
        # Safe to flush if buffer is long enough and no partial pattern at the end
        if len(self._buffer) < self._flush_threshold:
            return ""  # Buffer more

        # Apply masks to buffer
        masked, count = self._apply_masks(self._buffer)
        self._total_redacted += count

        # Keep last 30 chars buffered in case a pattern spans the next chunk
        to_emit = masked[:-30] if len(masked) > 30 else ""
        self._buffer = self._buffer[-30:] if len(self._buffer) > 30 else self._buffer
        return to_emit

    def flush(self) -> str:
        masked, count = self._apply_masks(self._buffer)
        self._total_redacted += count
        self._buffer = ""
        return masked

def stream_with_masking(prompt: str) -> str:
    masker = StreamingPIIMasker()
    full_output = []

    with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        for text in stream.text_stream:
            safe_chunk = masker.process_chunk(text)
            if safe_chunk:
                full_output.append(safe_chunk)
                print(safe_chunk, end="", flush=True)

    # Flush remaining buffer
    final = masker.flush()
    if final:
        full_output.append(final)
        print(final, end="", flush=True)

    print()
    print(f"\n[Streaming masker: {masker._total_redacted} PII instances redacted]")
    return "".join(full_output)

# Test with prompt that causes model to echo PII back
result = stream_with_masking(
    "The user's record is: email=alice@company.com, card=4111-1111-1111-1111, "
    "SSN=123-45-6789. Please summarize what we know about this user."
)
```

---

## Solution 5: Differential Privacy Noise Injector for Aggregate Data

When agents report aggregate statistics from sensitive datasets, add calibrated noise to prevent reconstruction of individual records.

```python
import anthropic
import math
import random
from dataclasses import dataclass
from typing import Union

client = anthropic.Anthropic()

@dataclass
class PrivacyBudget:
    epsilon: float  # Privacy parameter (smaller = more private)
    delta: float    # Failure probability
    consumed: float = 0.0

    @property
    def remaining(self) -> float:
        return max(0.0, self.epsilon - self.consumed)

class DifferentialPrivacyMasker:
    def __init__(self, epsilon: float = 1.0, sensitivity: float = 1.0):
        self._epsilon = epsilon
        self._sensitivity = sensitivity
        self._budget = PrivacyBudget(epsilon=epsilon, delta=1e-5)

    def _laplace_noise(self, scale: float) -> float:
        """Sample from Laplace(0, scale)."""
        u = random.uniform(-0.5, 0.5)
        return -scale * math.copysign(1, u) * math.log(1 - 2 * abs(u))

    def noisy_count(self, true_count: int, query_name: str = "") -> dict:
        if self._budget.remaining <= 0:
            return {"error": "Privacy budget exhausted", "query": query_name}

        scale = self._sensitivity / self._epsilon
        noise = self._laplace_noise(scale)
        noisy = max(0, round(true_count + noise))
        self._budget.consumed += self._epsilon

        return {
            "value": noisy,
            "epsilon_used": self._epsilon,
            "budget_remaining": round(self._budget.remaining, 3),
            "query": query_name,
        }

    def noisy_mean(self, values: list[float], clamp_range: tuple[float, float],
                   query_name: str = "") -> dict:
        if not values:
            return {"error": "Empty dataset"}
        if self._budget.remaining <= 0:
            return {"error": "Privacy budget exhausted"}

        lo, hi = clamp_range
        clamped = [max(lo, min(hi, v)) for v in values]
        true_mean = sum(clamped) / len(clamped)

        sensitivity = (hi - lo) / len(values)
        scale = sensitivity / self._epsilon
        noisy = true_mean + self._laplace_noise(scale)
        noisy = max(lo, min(hi, noisy))
        self._budget.consumed += self._epsilon

        return {
            "value": round(noisy, 2),
            "query": query_name,
            "budget_remaining": round(self._budget.remaining, 3),
        }

def privacy_aware_agent_response(prompt: str, stats: dict) -> str:
    """Inject differentially private stats into agent context."""
    masker = DifferentialPrivacyMasker(epsilon=0.5)

    private_stats = {}
    if "user_count" in stats:
        private_stats["user_count"] = masker.noisy_count(stats["user_count"], "user_count")
    if "revenue_values" in stats:
        private_stats["avg_revenue"] = masker.noisy_mean(
            stats["revenue_values"], (0, 10000), "avg_revenue"
        )
    if "age_values" in stats:
        private_stats["avg_age"] = masker.noisy_mean(
            stats["age_values"], (18, 100), "avg_age"
        )

    context = "\n".join([
        f"{k}: {v['value']} (±noise for privacy)"
        for k, v in private_stats.items() if 'value' in v
    ])

    response = client.messages.create(
        model="claude-haiku-4-5-20251001", max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"{prompt}\n\nAggregate statistics (privacy-protected):\n{context}"
        }]
    )
    return response.content[0].text

# Real sensitive data — never leave the server
real_data = {
    "user_count": 15420,
    "revenue_values": [450, 1200, 89, 3400, 780, 2100, 560, 990, 125, 4200],
    "age_values": [25, 34, 42, 28, 55, 31, 67, 23, 45, 39],
}

result = privacy_aware_agent_response(
    "Summarize our user base for the board report.",
    real_data,
)
print(f"Privacy-protected report:\n{result}")
```

---

## Solution 6: Output-Side PII Scrubber with Audit Log

Scrub sensitive data from agent responses before they reach the client, and log every redaction for compliance.

```python
import anthropic
import re
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional
from pathlib import Path

client = anthropic.Anthropic()

@dataclass
class RedactionEvent:
    event_id: str
    timestamp: float
    session_id: str
    pii_types_found: list[str]
    original_length: int
    redacted_length: int
    redaction_count: int

class OutputPIIScrubber:
    def __init__(self, audit_log_path: Optional[Path] = None, session_id: str = None):
        self._audit_path = audit_log_path or Path("/tmp/pii_audit.jsonl")
        self._session_id = session_id or str(uuid.uuid4())[:8]
        self._patterns = {
            "email":       re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"),
            "phone":       re.compile(r"\b(?:\+1\s?)?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}\b"),
            "ssn":         re.compile(r"\b\d{3}[-\s]?\d{2}[-\s]?\d{4}\b"),
            "credit_card": re.compile(r"\b(?:\d{4}[\s\-]?){3}\d{4}\b"),
            "api_key":     re.compile(r"\b(sk|pk|api)[-_][a-zA-Z0-9]{20,}\b", re.I),
        }

    def scrub(self, text: str) -> tuple[str, list[str]]:
        result = text
        found_types = []
        for pii_type, pattern in self._patterns.items():
            if pattern.search(result):
                found_types.append(pii_type)
                result = pattern.sub(f"[{pii_type.upper()}]", result)
        return result, found_types

    def scrub_response(self, text: str) -> str:
        scrubbed, found = self.scrub(text)
        if found:
            event = RedactionEvent(
                event_id=str(uuid.uuid4())[:12],
                timestamp=time.time(),
                session_id=self._session_id,
                pii_types_found=found,
                original_length=len(text),
                redacted_length=len(scrubbed),
                redaction_count=sum(
                    len(p.findall(text)) for p in self._patterns.values()
                ),
            )
            self._write_audit(event)
            print(f"[PII Scrubbed] Types: {found}, redactions: {event.redaction_count}")
        return scrubbed

    def _write_audit(self, event: RedactionEvent):
        with open(self._audit_path, "a") as f:
            f.write(json.dumps({
                "event_id": event.event_id,
                "timestamp": event.timestamp,
                "session_id": event.session_id,
                "pii_types": event.pii_types_found,
                "original_length": event.original_length,
                "redacted_length": event.redacted_length,
                "redaction_count": event.redaction_count,
            }) + "\n")

    def compliant_call(self, messages: list, model: str = "claude-haiku-4-5-20251001", **kwargs) -> str:
        response = client.messages.create(model=model, max_tokens=512, messages=messages, **kwargs)
        raw = response.content[0].text
        return self.scrub_response(raw)

# Usage
scrubber = OutputPIIScrubber()

# Prompt that causes model to repeat PII back
raw_prompt = (
    "Here is the customer data: Alice Johnson, alice@company.com, "
    "phone 415-555-0123, card 4111-1111-1111-1111. "
    "Please confirm what data we have on file."
)
safe_response = scrubber.compliant_call(
    [{"role": "user", "content": raw_prompt}]
)
print(f"Safe response:\n{safe_response}")

# Check audit log
if Path("/tmp/pii_audit.jsonl").exists():
    with open("/tmp/pii_audit.jsonl") as f:
        for line in f:
            entry = json.loads(line)
            print(f"\nAudit entry: {entry}")
```

---

## Comparison

| Solution | Coverage | Performance Overhead | Compliance Friendly | False Positive Risk | Best For |
|---|---|---|---|---|---|
| Regex PII Detector | Common PII patterns | <1ms | Medium | Medium | Quick integration |
| Schema-Aware Field Masking | Field-level by name | <1ms | High | Low | Structured data (JSON/DB) |
| Context-Window Scanner | Pre-call protection | ~5ms | High | Medium | Input sanitization |
| Streaming Output Masker | Real-time redaction | Low | Medium | Medium | Streaming APIs |
| Differential Privacy | Aggregate data | <5ms | Very High | N/A (adds noise) | Analytics/reporting |
| Output-Side Scrubber + Audit | Post-call cleanup | ~2ms | Very High (audit log) | Medium | Compliance-driven |

**Recommended stack:** Solution 2 (schema-aware) for structured data + Solution 3 (context scanner) for input protection + Solution 6 (output scrubber + audit) for compliance. Add Solution 4 (streaming masker) if you expose streaming APIs to end users.
