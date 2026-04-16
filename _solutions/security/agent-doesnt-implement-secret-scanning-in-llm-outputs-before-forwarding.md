---
title: "Agent Doesn't Implement Secret Scanning in LLM Outputs Before Forwarding"
description: "Agents that forward LLM-generated text directly to users, external APIs, or logs without scanning for secrets risk exfiltrating credentials that the LLM reproduced from its training data, retrieved documents, or injected context. Implement secret scanning on all LLM output before it leaves the trust boundary — detecting API keys, tokens, private keys, and connection strings — and redact or block the output before forwarding."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-secret-scanning-in-llm-outputs-before-forwarding
tags: [secret-scanning, output-filtering, credential-leakage, llm-output-safety, data-exfiltration, pii-detection]
symptoms:
  - "LLM reproduces API keys from retrieved documents and includes them in the response"
  - "Connection strings from injected context appear in agent responses forwarded to clients"
  - "No scanning between LLM output generation and response delivery"
  - "Secrets from training data surface in code-generation outputs without redaction"
  - "Audit log of agent responses contains plaintext credentials"
---

## Why This Happens

LLMs reproduce content from their context window. When an agent injects retrieved documents, database records, or tool results into the prompt, any secrets present in those artifacts can appear verbatim in the LLM's response. Additionally, LLMs trained on code repositories sometimes reproduce real credentials from their training data. Without a scanning pass between generation and forwarding, these credentials reach end users, get logged, or are sent to downstream systems. Secret scanning on output is a mandatory safety layer regardless of how careful the input pipeline is.

## Solution 1: Secret Pattern Registry

```python
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class SecretType(str, Enum):
    API_KEY = "api_key"
    BEARER_TOKEN = "bearer_token"
    PRIVATE_KEY = "private_key"
    CONNECTION_STRING = "connection_string"
    GITHUB_TOKEN = "github_token"
    AWS_KEY = "aws_key"
    GENERIC_HIGH_ENTROPY = "generic_high_entropy"


class SecretAction(str, Enum):
    REDACT = "redact"     # replace with [REDACTED]
    MASK = "mask"         # show prefix only: sk-ab***
    BLOCK = "block"       # refuse to forward the entire output


@dataclass
class SecretPattern:
    name: str
    secret_type: SecretType
    pattern: str
    action: SecretAction
    description: str = ""
    compiled: re.Pattern = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.compiled = re.compile(self.pattern)


def build_default_secret_patterns() -> List[SecretPattern]:
    return [
        SecretPattern(
            name="openai_api_key",
            secret_type=SecretType.API_KEY,
            pattern=r"sk-[A-Za-z0-9]{20,}",
            action=SecretAction.REDACT,
            description="OpenAI API key",
        ),
        SecretPattern(
            name="anthropic_api_key",
            secret_type=SecretType.API_KEY,
            pattern=r"sk-ant-[A-Za-z0-9\-_]{20,}",
            action=SecretAction.REDACT,
            description="Anthropic API key",
        ),
        SecretPattern(
            name="github_pat",
            secret_type=SecretType.GITHUB_TOKEN,
            pattern=r"gh[pousr]_[A-Za-z0-9]{36,}",
            action=SecretAction.REDACT,
            description="GitHub personal access token",
        ),
        SecretPattern(
            name="aws_access_key",
            secret_type=SecretType.AWS_KEY,
            pattern=r"(?<![A-Z0-9])[A-Z0-9]{20}(?![A-Z0-9])",
            action=SecretAction.MASK,
            description="AWS access key ID",
        ),
        SecretPattern(
            name="aws_secret_key",
            secret_type=SecretType.AWS_KEY,
            pattern=r"(?<![A-Za-z0-9/+])[A-Za-z0-9/+]{40}(?![A-Za-z0-9/+])",
            action=SecretAction.REDACT,
            description="AWS secret access key",
        ),
        SecretPattern(
            name="bearer_token",
            secret_type=SecretType.BEARER_TOKEN,
            pattern=r"Bearer\s+[A-Za-z0-9\-_.]{20,}",
            action=SecretAction.MASK,
            description="HTTP Bearer token",
        ),
        SecretPattern(
            name="pem_private_key",
            secret_type=SecretType.PRIVATE_KEY,
            pattern=r"-----BEGIN\s+(?:RSA\s+)?PRIVATE KEY-----[\s\S]*?-----END\s+(?:RSA\s+)?PRIVATE KEY-----",
            action=SecretAction.BLOCK,
            description="PEM private key block",
        ),
        SecretPattern(
            name="db_connection_string",
            secret_type=SecretType.CONNECTION_STRING,
            pattern=r"(?:postgres|mysql|mongodb|redis)://[^:\s]+:[^@\s]+@[^\s]+",
            action=SecretAction.REDACT,
            description="Database connection string with credentials",
        ),
        SecretPattern(
            name="generic_hex_secret",
            secret_type=SecretType.GENERIC_HIGH_ENTROPY,
            pattern=r"(?<![A-Fa-f0-9])[A-Fa-f0-9]{64}(?![A-Fa-f0-9])",
            action=SecretAction.REDACT,
            description="64-char hex string (API secret / hash)",
        ),
    ]
```

## Solution 2: Secret Scanner

```python
from dataclasses import dataclass
from typing import List


@dataclass
class SecretMatch:
    pattern_name: str
    secret_type: SecretType
    action: SecretAction
    matched_text: str
    start: int
    end: int


@dataclass
class SecretScanResult:
    text: str
    matches: List[SecretMatch]
    has_block_action: bool
    has_secrets: bool

    @classmethod
    def clean(cls, text: str) -> "SecretScanResult":
        return cls(text=text, matches=[], has_block_action=False, has_secrets=False)


class SecretScanner:
    """
    Scans text against all registered secret patterns.
    Returns matches with positions and recommended actions.
    """

    def __init__(self, patterns: List[SecretPattern]):
        self._patterns = patterns

    def scan(self, text: str) -> SecretScanResult:
        matches = []
        for pattern in self._patterns:
            for m in pattern.compiled.finditer(text):
                matches.append(SecretMatch(
                    pattern_name=pattern.name,
                    secret_type=pattern.secret_type,
                    action=pattern.action,
                    matched_text=m.group(),
                    start=m.start(),
                    end=m.end(),
                ))

        has_block = any(m.action == SecretAction.BLOCK for m in matches)
        return SecretScanResult(
            text=text,
            matches=matches,
            has_block_action=has_block,
            has_secrets=len(matches) > 0,
        )
```

## Solution 3: Output Redactor

```python
import re
from typing import List


class LLMOutputRedactor:
    """
    Applies redaction and masking to LLM output text based on scan results.
    Produces a sanitized copy without mutating the original.
    """

    REDACT_PLACEHOLDER = "[REDACTED]"

    def __init__(self, scanner: SecretScanner):
        self._scanner = scanner

    def redact(self, text: str) -> tuple:
        """Returns (sanitized_text, scan_result)."""
        scan = self._scanner.scan(text)
        if not scan.has_secrets:
            return text, scan

        # Sort matches by start position descending to replace without offset shifts
        sorted_matches = sorted(scan.matches, key=lambda m: m.start, reverse=True)
        result = text
        for match in sorted_matches:
            if match.action == SecretAction.REDACT or match.action == SecretAction.BLOCK:
                result = result[:match.start] + self.REDACT_PLACEHOLDER + result[match.end:]
            elif match.action == SecretAction.MASK:
                original = match.matched_text
                prefix_len = min(6, len(original) // 3)
                masked = original[:prefix_len] + "***"
                result = result[:match.start] + masked + result[match.end:]

        return result, scan
```

## Solution 4: Output Safety Gate

```python
import time
from typing import Callable, Optional


class LLMOutputSafetyGate:
    """
    Intercepts LLM output before forwarding. Blocks outputs containing
    private keys or other block-action secrets; redacts others.
    Records all interventions for audit.
    """

    def __init__(
        self,
        redactor: LLMOutputRedactor,
        on_block: Optional[Callable[[SecretScanResult], str]] = None,
    ):
        self._redactor = redactor
        self._on_block = on_block or (lambda _: "[Response blocked: contained sensitive credentials]")
        self._interventions: list = []

    def process(self, llm_output: str, session_id: str = "") -> tuple:
        """
        Returns (safe_output, was_modified, was_blocked).
        """
        sanitized, scan = self._redactor.redact(llm_output)

        if scan.has_block_action:
            self._record(session_id, scan, blocked=True)
            return self._on_block(scan), True, True

        if scan.has_secrets:
            self._record(session_id, scan, blocked=False)
            return sanitized, True, False

        return llm_output, False, False

    def _record(self, session_id: str, scan: SecretScanResult, blocked: bool) -> None:
        self._interventions.append({
            "ts": time.time(),
            "session_id": session_id,
            "blocked": blocked,
            "secret_types": list({m.secret_type.value for m in scan.matches}),
            "match_count": len(scan.matches),
        })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._interventions if r["ts"] >= cutoff]
        return {
            "window_seconds": window_seconds,
            "total_interventions": len(recent),
            "blocked": sum(1 for r in recent if r["blocked"]),
            "redacted": sum(1 for r in recent if not r["blocked"]),
        }
```

## Solution 5: False Positive Tracker

```python
import time
from typing import List


class SecretScanFalsePositiveTracker:
    """
    Tracks patterns that fire frequently so operators can tune thresholds.
    High false-positive patterns should be refined or demoted.
    """

    def __init__(self):
        self._fp_reports: List[dict] = []

    def report_false_positive(
        self,
        pattern_name: str,
        matched_text: str,
        context: str = "",
    ) -> None:
        self._fp_reports.append({
            "ts": time.time(),
            "pattern_name": pattern_name,
            "matched_preview": matched_text[:30],
            "context": context[:100],
        })

    def top_false_positive_patterns(
        self,
        window_seconds: float = 86400.0,
        top_n: int = 5,
    ) -> List[dict]:
        cutoff = time.time() - window_seconds
        from collections import Counter
        recent = [r for r in self._fp_reports if r["ts"] >= cutoff]
        counts = Counter(r["pattern_name"] for r in recent)
        return [{"pattern": p, "reports": c} for p, c in counts.most_common(top_n)]
```

## Solution 6: Output Secret Scanning Dashboard

```python
import time


class OutputSecretScanningDashboard:
    """
    Combines safety gate stats and false positive reports.
    """

    def __init__(
        self,
        gate: LLMOutputSafetyGate,
        fp_tracker: SecretScanFalsePositiveTracker,
    ):
        self._gate = gate
        self._fp_tracker = fp_tracker

    def render(self, window_seconds: float = 3600.0) -> dict:
        return {
            "generated_at": time.time(),
            "intervention_summary": self._gate.summary(window_seconds),
            "top_false_positive_patterns": self._fp_tracker.top_false_positive_patterns(
                window_seconds
            ),
        }
```

## Comparison

| Approach | Pattern Registry | Scanning | Redaction | Block Action | Audit | FP Tracking |
|---|---|---|---|---|---|---|
| SecretPattern registry | Yes (9 patterns) | No | No | No | No | No |
| SecretScanner | Via registry | Yes | No | No | No | No |
| LLMOutputRedactor | Via scanner | Via scanner | Yes | No | No | No |
| LLMOutputSafetyGate | Via redactor | Via redactor | Via redactor | Yes | Yes | No |
| SecretScanFalsePositiveTracker | No | No | No | No | No | Yes |
| OutputSecretScanningDashboard | No | No | No | No | No | Yes |

**Best for production**: Apply `LLMOutputSafetyGate.process()` to every LLM response before it reaches any downstream consumer — user-facing responses, tool inputs, log sinks, and webhook payloads. Use `SecretAction.BLOCK` only for private keys and certificates (irreversible credential compromise); use `SecretAction.REDACT` for API keys and tokens (the user can regenerate them); use `SecretAction.MASK` for bearer tokens in diagnostic contexts where partial visibility aids debugging. Run `SecretScanFalsePositiveTracker.top_false_positive_patterns()` weekly — the `aws_access_key` pattern (uppercase hex strings) has the highest false positive rate and should be refined with surrounding context requirements if it generates noise.
