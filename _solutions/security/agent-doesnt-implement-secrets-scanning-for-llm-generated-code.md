---
title: "Agent Doesn't Implement Secrets Scanning for LLM-Generated Code"
description: "Agents that generate and execute or return code without scanning for embedded secrets expose API keys, passwords, and tokens that the LLM included in its output — either hallucinated, inferred from context, or injected via prompt manipulation. Implement secrets scanning that runs on all LLM-generated code before it is stored, executed, or returned to users, redacts detected secrets, and logs scanning events for audit."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-secrets-scanning-for-llm-generated-code
tags: [secrets-scanning, llm-generated-code, api-key-detection, credential-leakage, code-safety, output-scanning]
symptoms:
  - "LLM generates a code example with a hardcoded API key that matches a pattern from the prompt"
  - "Generated configuration files contain placeholder secrets that look like real credentials"
  - "No scan between LLM output and code storage/execution — secrets pass through undetected"
  - "Code examples returned to users include tokens the LLM hallucinated that match real formats"
  - "Audit log contains no record of whether generated code was scanned before delivery"
---

## Why This Happens

LLMs generate plausible-looking code by pattern-matching on training data. When asked to write code that connects to a service, the model may complete the pattern with a realistic-looking API key, password string, or token — either entirely hallucinated or inferred from context in the prompt. These outputs are syntactically valid and semantically dangerous: they look like real credentials, may actually be real credentials if the model was trained on or provided leaked data, and will be copied into production by users who assume the example is safe. Scanning must intercept every code output before it leaves the agent.

## Solution 1: Secret Pattern Definitions

```python
import re
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Pattern


class SecretSeverity(str, Enum):
    CRITICAL = "critical"   # high-entropy real-looking credential
    HIGH = "high"           # service-specific key format
    MEDIUM = "medium"       # generic password/token pattern
    LOW = "low"             # possible but lower confidence


@dataclass
class SecretPattern:
    name: str
    pattern: re.Pattern
    severity: SecretSeverity
    redaction_placeholder: str = "[REDACTED]"
    entropy_threshold: Optional[float] = None   # min Shannon entropy to fire


# Built-in pattern library
BUILTIN_SECRET_PATTERNS: List[SecretPattern] = [
    SecretPattern("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}"), SecretSeverity.CRITICAL),
    SecretPattern("aws_secret_key", re.compile(r"(?i)aws.{0,20}secret.{0,20}['\"][0-9a-zA-Z/+]{40}['\"]"), SecretSeverity.CRITICAL),
    SecretPattern("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9_]{36,255}"), SecretSeverity.CRITICAL),
    SecretPattern("openai_key", re.compile(r"sk-[A-Za-z0-9]{20,60}"), SecretSeverity.CRITICAL),
    SecretPattern("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9\-_]{90,}"), SecretSeverity.CRITICAL),
    SecretPattern("stripe_key", re.compile(r"(?:sk|pk)_(?:live|test)_[0-9a-zA-Z]{24,}"), SecretSeverity.CRITICAL),
    SecretPattern("google_api_key", re.compile(r"AIza[0-9A-Za-z\-_]{35}"), SecretSeverity.HIGH),
    SecretPattern("slack_token", re.compile(r"xox[baprs]-[0-9A-Za-z\-]{10,}"), SecretSeverity.HIGH),
    SecretPattern("jwt_token", re.compile(r"eyJ[A-Za-z0-9\-_=]+\.[A-Za-z0-9\-_=]+\.?[A-Za-z0-9\-_=]*"), SecretSeverity.HIGH),
    SecretPattern("private_key_block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"), SecretSeverity.CRITICAL),
    SecretPattern("generic_password", re.compile(r"""(?i)(?:password|passwd|pwd)\s*[=:]\s*['"][^'"]{8,}['"]"""), SecretSeverity.MEDIUM),
    SecretPattern("generic_token", re.compile(r"""(?i)(?:token|api_key|secret)\s*[=:]\s*['"][A-Za-z0-9\-_]{16,}['"]"""), SecretSeverity.MEDIUM),
    SecretPattern("connection_string", re.compile(r"(?i)(?:mongodb|postgresql|mysql|redis)://[^@\s]+:[^@\s]+@"), SecretSeverity.HIGH),
]
```

## Solution 2: Shannon Entropy Calculator

```python
import math
from typing import Optional


class ShannonEntropyCalculator:
    """
    Computes Shannon entropy of a string.
    High-entropy strings (>4.5 bits/char) in credential positions
    are more likely to be real secrets than placeholder values like 'your_api_key_here'.
    """

    @staticmethod
    def entropy(text: str) -> float:
        if not text:
            return 0.0
        freq: dict = {}
        for ch in text:
            freq[ch] = freq.get(ch, 0) + 1
        n = len(text)
        return round(
            -sum((c / n) * math.log2(c / n) for c in freq.values()),
            4,
        )

    @classmethod
    def is_high_entropy(cls, text: str, threshold: float = 4.0) -> bool:
        return cls.entropy(text) >= threshold
```

## Solution 3: Code Secret Scanner

```python
import re
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class SecretFinding:
    pattern_name: str
    severity: SecretSeverity
    match_start: int
    match_end: int
    matched_text: str
    entropy: Optional[float] = None
    line_number: Optional[int] = None
    context_snippet: str = ""   # surrounding chars for audit, value redacted


class CodeSecretScanner:
    """
    Scans text (typically LLM-generated code) for secret patterns.
    Returns findings without modifying the input — call redact() separately.
    """

    def __init__(
        self,
        patterns: Optional[List[SecretPattern]] = None,
        entropy_check: bool = True,
        min_entropy_for_medium: float = 3.5,
    ):
        self._patterns = patterns or BUILTIN_SECRET_PATTERNS
        self._entropy_check = entropy_check
        self._min_entropy = min_entropy_for_medium

    def scan(self, code: str) -> List[SecretFinding]:
        findings: List[SecretFinding] = []
        lines = code.split("\n")
        line_starts = self._build_line_index(code)

        for pat in self._patterns:
            for match in pat.pattern.finditer(code):
                matched = match.group(0)
                entropy = ShannonEntropyCalculator.entropy(matched) if self._entropy_check else None

                # Skip LOW/MEDIUM patterns with low entropy (likely placeholders)
                if pat.severity in (SecretSeverity.LOW, SecretSeverity.MEDIUM):
                    if entropy is not None and entropy < self._min_entropy:
                        continue

                line_no = self._line_number(match.start(), line_starts)
                ctx_start = max(0, match.start() - 20)
                ctx_end = min(len(code), match.end() + 20)
                context = code[ctx_start:match.start()] + "[REDACTED]" + code[match.end():ctx_end]

                findings.append(SecretFinding(
                    pattern_name=pat.name,
                    severity=pat.severity,
                    match_start=match.start(),
                    match_end=match.end(),
                    matched_text=matched,
                    entropy=entropy,
                    line_number=line_no,
                    context_snippet=context,
                ))

        # Deduplicate overlapping findings
        findings.sort(key=lambda f: f.match_start)
        return self._deduplicate(findings)

    def _build_line_index(self, text: str) -> List[int]:
        starts = [0]
        for i, ch in enumerate(text):
            if ch == "\n":
                starts.append(i + 1)
        return starts

    def _line_number(self, pos: int, line_starts: List[int]) -> int:
        import bisect
        return bisect.bisect_right(line_starts, pos)

    def _deduplicate(self, findings: List[SecretFinding]) -> List[SecretFinding]:
        if not findings:
            return findings
        result = [findings[0]]
        for f in findings[1:]:
            if f.match_start >= result[-1].match_end:
                result.append(f)
        return result
```

## Solution 4: Secret Redactor

```python
from typing import List, Tuple


class SecretRedactor:
    """
    Replaces detected secrets in code with placeholders.
    Returns the redacted code and a list of (original, placeholder) pairs
    so callers can audit what was removed without logging the secrets.
    """

    def __init__(self, patterns: Optional[List[SecretPattern]] = None):
        self._patterns = {p.name: p for p in (patterns or BUILTIN_SECRET_PATTERNS)}

    def redact(
        self,
        code: str,
        findings: List[SecretFinding],
    ) -> Tuple[str, List[dict]]:
        if not findings:
            return code, []

        redacted = list(code)
        log_entries = []
        # Process in reverse order so indices stay valid
        for f in sorted(findings, key=lambda x: -x.match_start):
            pat = self._patterns.get(f.pattern_name)
            placeholder = pat.redaction_placeholder if pat else "[REDACTED]"
            redacted[f.match_start:f.match_end] = list(placeholder)
            log_entries.append({
                "pattern": f.pattern_name,
                "severity": f.severity,
                "line": f.line_number,
                "entropy": f.entropy,
                "placeholder": placeholder,
                "length_chars": f.match_end - f.match_start,
            })

        return "".join(redacted), log_entries
```

## Solution 5: Scan-Gated Code Output Handler

```python
import time
from typing import Any, Optional, Tuple


class ScanGatedCodeOutputHandler:
    """
    Intercepts LLM-generated code before delivery.
    Blocks output containing CRITICAL findings; redacts MEDIUM/HIGH
    and annotates the response; passes LOW findings with a note.
    """

    def __init__(
        self,
        scanner: CodeSecretScanner,
        redactor: SecretRedactor,
        block_on_critical: bool = True,
    ):
        self._scanner = scanner
        self._redactor = redactor
        self._block_critical = block_on_critical
        self._scan_count = 0
        self._blocked_count = 0
        self._redacted_count = 0

    def process(self, code: str) -> Tuple[Optional[str], dict]:
        """
        Returns (processed_code_or_None, scan_report).
        None means the output was blocked.
        """
        self._scan_count += 1
        findings = self._scanner.scan(code)

        critical = [f for f in findings if f.severity == SecretSeverity.CRITICAL]
        non_critical = [f for f in findings if f.severity != SecretSeverity.CRITICAL]

        if critical and self._block_critical:
            self._blocked_count += 1
            return None, {
                "action": "blocked",
                "reason": "CRITICAL secrets detected",
                "finding_count": len(findings),
                "critical_patterns": [f.pattern_name for f in critical],
                "scanned_at": time.time(),
            }

        if findings:
            self._redacted_count += 1
            redacted_code, log_entries = self._redactor.redact(code, findings)
            return redacted_code, {
                "action": "redacted",
                "finding_count": len(findings),
                "redacted_entries": log_entries,
                "scanned_at": time.time(),
            }

        return code, {
            "action": "passed",
            "finding_count": 0,
            "scanned_at": time.time(),
        }

    def stats(self) -> dict:
        return {
            "total_scanned": self._scan_count,
            "blocked": self._blocked_count,
            "redacted": self._redacted_count,
            "clean": self._scan_count - self._blocked_count - self._redacted_count,
        }
```

## Solution 6: Secrets Scan Audit Logger

```python
import time
from typing import List


class SecretsScanAuditLogger:
    """
    Records all scan events for security audit.
    High blocked/redacted rates indicate either overly sensitive patterns
    or that the LLM is being prompted in a way that produces credentials.
    """

    def __init__(self, window_seconds: float = 3600.0):
        self._window = window_seconds
        self._events: List[dict] = []

    def record(self, session_id: str, report: dict) -> None:
        self._events.append({
            "ts": time.time(),
            "session_id": session_id,
            "action": report.get("action"),
            "finding_count": report.get("finding_count", 0),
            "critical_patterns": report.get("critical_patterns", []),
        })

    def _trim(self) -> None:
        cutoff = time.time() - self._window
        self._events = [e for e in self._events if e["ts"] >= cutoff]

    def summary(self) -> dict:
        self._trim()
        total = len(self._events)
        blocked = sum(1 for e in self._events if e["action"] == "blocked")
        redacted = sum(1 for e in self._events if e["action"] == "redacted")
        pattern_counts: dict = {}
        for e in self._events:
            for p in e.get("critical_patterns", []):
                pattern_counts[p] = pattern_counts.get(p, 0) + 1
        return {
            "total_scans": total,
            "blocked": blocked,
            "redacted": redacted,
            "clean": total - blocked - redacted,
            "block_rate": round(blocked / max(total, 1), 4),
            "top_triggered_patterns": dict(
                sorted(pattern_counts.items(), key=lambda x: -x[1])[:5]
            ),
        }
```

## Comparison

| Approach | Pattern Matching | Entropy Filter | Redaction | Block on Critical | Audit Log |
|---|---|---|---|---|---|
| CodeSecretScanner | Yes (16 patterns) | Yes | No | No | No |
| SecretRedactor | No | No | Yes | No | No |
| ScanGatedCodeOutputHandler | Via scanner | Via scanner | Via redactor | Yes | No |
| SecretsScanAuditLogger | No | No | No | No | Yes |

**Best for production**: Run `ScanGatedCodeOutputHandler.process()` on every LLM code output before it is stored, executed, or returned to a user — no exceptions. Set `block_on_critical=True` for code that will be executed in your infrastructure; set it to `False` (redact-only) for code examples returned to users, since blocking would degrade UX. Add `BUILTIN_SECRET_PATTERNS` as a starting set and extend with your organization's internal credential formats (internal API prefixes, service account key patterns). Monitor `SecretsScanAuditLogger.summary()` — a `block_rate` above 1% suggests the LLM is being prompted in a context that frequently causes it to generate realistic credentials, which warrants reviewing system prompt instructions about credential handling.
