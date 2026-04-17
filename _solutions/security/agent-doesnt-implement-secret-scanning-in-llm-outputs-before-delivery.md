---
title: "Agent Doesn't Implement Secret Scanning in LLM Outputs Before Delivery"
description: "Agents that deliver LLM responses directly to users without scanning for secrets risk exposing API keys, tokens, private keys, and credentials that the model retrieved from context, hallucinated, or was induced to repeat via prompt injection. Implement output secret scanning that detects credential-like patterns in every LLM response before delivery, redacts or blocks the response, and generates a security alert for operator review."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-secret-scanning-in-llm-outputs-before-delivery
tags: [secret-scanning, output-filtering, credential-detection, data-exfiltration, llm-output-security, redaction]
symptoms:
  - "LLM response includes an API key retrieved from a tool result and echoed verbatim to the user"
  - "A prompt injection attack causes the model to repeat secrets from its context window"
  - "No scan between LLM output and user delivery — any credential in context can be exfiltrated"
  - "Hallucinated credential patterns pass through to users who may attempt to use them"
  - "No audit trail of which responses triggered secret detections"
---

## Why This Happens

LLM context windows frequently contain secrets: API keys injected by tool results, database connection strings, tokens retrieved from secret stores. The model may repeat these verbatim in a response if asked directly, if a prompt injection manipulates it, or if it retrieves and echoes them as part of an explanation. A separate scanning layer between model output and user delivery is the last defense against this class of data exfiltration. Scanning must be fast (synchronous in the response path), comprehensive (pattern-based for known formats, heuristic for unknowns), and produce an audit trail for every detection.

## Solution 1: Secret Pattern Registry

```python
import re
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Pattern


class SecretCategory(str, Enum):
    API_KEY = "api_key"
    BEARER_TOKEN = "bearer_token"
    PRIVATE_KEY = "private_key"
    DATABASE_URL = "database_url"
    AWS_CREDENTIAL = "aws_credential"
    GITHUB_TOKEN = "github_token"
    GENERIC_HIGH_ENTROPY = "generic_high_entropy"
    CREDIT_CARD = "credit_card"


@dataclass
class SecretPattern:
    category: SecretCategory
    pattern: str
    description: str
    severity: str = "high"          # "critical" | "high" | "medium"
    redact_full: bool = True        # True = full redact; False = partial mask
    compiled: Optional[re.Pattern] = None

    def __post_init__(self):
        self.compiled = re.compile(self.pattern, re.MULTILINE)


def default_secret_patterns() -> List[SecretPattern]:
    return [
        SecretPattern(SecretCategory.API_KEY,
            r"sk-[A-Za-z0-9]{20,}", "OpenAI-style API key", "critical"),
        SecretPattern(SecretCategory.API_KEY,
            r"sk-ant-[A-Za-z0-9\-_]{20,}", "Anthropic API key", "critical"),
        SecretPattern(SecretCategory.GITHUB_TOKEN,
            r"ghp_[A-Za-z0-9]{36}", "GitHub personal access token", "critical"),
        SecretPattern(SecretCategory.GITHUB_TOKEN,
            r"github_pat_[A-Za-z0-9_]{82}", "GitHub fine-grained PAT", "critical"),
        SecretPattern(SecretCategory.AWS_CREDENTIAL,
            r"AKIA[0-9A-Z]{16}", "AWS access key ID", "critical"),
        SecretPattern(SecretCategory.BEARER_TOKEN,
            r"Bearer\s+[A-Za-z0-9\-_.]{30,}", "Bearer token", "high",
            redact_full=False),
        SecretPattern(SecretCategory.PRIVATE_KEY,
            r"-----BEGIN\s+(RSA\s+|EC\s+|OPENSSH\s+)?PRIVATE KEY-----",
            "PEM private key header", "critical"),
        SecretPattern(SecretCategory.DATABASE_URL,
            r"(postgresql|mysql|mongodb|redis)://[^\s\"']{10,}",
            "Database connection URL", "high"),
        SecretPattern(SecretCategory.CREDIT_CARD,
            r"\b(?:4[0-9]{12}(?:[0-9]{3})?|5[1-5][0-9]{14}|3[47][0-9]{13})\b",
            "Credit card number", "critical"),
        SecretPattern(SecretCategory.GENERIC_HIGH_ENTROPY,
            r"\b[A-Za-z0-9+/]{40,}={0,2}\b",
            "High-entropy base64 string", "medium", redact_full=False),
    ]
```

## Solution 2: Secret Detector

```python
import re
from dataclasses import dataclass
from typing import List


@dataclass
class SecretMatch:
    category: SecretCategory
    description: str
    severity: str
    start: int
    end: int
    matched_text: str
    redact_full: bool

    def preview(self, chars: int = 8) -> str:
        if len(self.matched_text) <= chars:
            return "[REDACTED]"
        return self.matched_text[:4] + "***" + self.matched_text[-4:]


class SecretDetector:
    """
    Scans text for secret patterns and returns all matches.
    """

    def __init__(self, patterns: List[SecretPattern]):
        self._patterns = patterns

    def scan(self, text: str) -> List[SecretMatch]:
        matches = []
        for pattern in self._patterns:
            for m in pattern.compiled.finditer(text):
                matches.append(SecretMatch(
                    category=pattern.category,
                    description=pattern.description,
                    severity=pattern.severity,
                    start=m.start(),
                    end=m.end(),
                    matched_text=m.group(0),
                    redact_full=pattern.redact_full,
                ))
        # Deduplicate overlapping matches, keep highest severity
        return self._deduplicate(matches)

    def _deduplicate(self, matches: List[SecretMatch]) -> List[SecretMatch]:
        if not matches:
            return []
        severity_rank = {"critical": 3, "high": 2, "medium": 1}
        matches_sorted = sorted(matches, key=lambda m: (-severity_rank.get(m.severity, 0), m.start))
        deduped = []
        for match in matches_sorted:
            overlaps = any(
                not (match.end <= d.start or match.start >= d.end)
                for d in deduped
            )
            if not overlaps:
                deduped.append(match)
        return sorted(deduped, key=lambda m: m.start)
```

## Solution 3: Output Redactor

```python
class OutputRedactor:
    """
    Applies redaction to detected secrets in LLM output text.
    Supports full redaction and partial masking.
    """

    FULL_REDACT_PLACEHOLDER = "[SECRET REDACTED]"

    def redact(self, text: str, matches: List[SecretMatch]) -> str:
        if not matches:
            return text

        # Process in reverse order to preserve string indices
        result = text
        for match in sorted(matches, key=lambda m: m.start, reverse=True):
            replacement = self._replacement(match)
            result = result[:match.start] + replacement + result[match.end:]
        return result

    def _replacement(self, match: SecretMatch) -> str:
        if match.redact_full:
            return self.FULL_REDACT_PLACEHOLDER
        # Partial mask: show category label with partial content
        preview = match.matched_text[:6] + "***"
        return f"[{match.category.value.upper()}:{preview}]"
```

## Solution 4: Output Secret Scan Gate

```python
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional


class ScanAction(str, Enum):
    ALLOW = "allow"
    REDACT = "redact"
    BLOCK = "block"


@dataclass
class ScanDecision:
    action: ScanAction
    original_text: str
    safe_text: str
    matches: List[SecretMatch]
    blocked: bool
    redacted_count: int


class OutputSecretScanGate:
    """
    Scans LLM output for secrets, redacts or blocks based on severity,
    and records the decision for audit.
    """

    def __init__(
        self,
        detector: SecretDetector,
        redactor: OutputRedactor,
        audit_logger: "SecretScanAuditLogger",
        block_on_critical: bool = True,
        redact_on_high: bool = True,
        redact_on_medium: bool = False,
    ):
        self._detector = detector
        self._redactor = redactor
        self._logger = audit_logger
        self._block_critical = block_on_critical
        self._redact_high = redact_on_high
        self._redact_medium = redact_on_medium

    def process(
        self,
        text: str,
        session_id: str = "",
    ) -> ScanDecision:
        matches = self._detector.scan(text)

        if not matches:
            decision = ScanDecision(
                action=ScanAction.ALLOW,
                original_text=text,
                safe_text=text,
                matches=[],
                blocked=False,
                redacted_count=0,
            )
            return decision

        critical = [m for m in matches if m.severity == "critical"]
        high = [m for m in matches if m.severity == "high"]
        medium = [m for m in matches if m.severity == "medium"]

        if self._block_critical and critical:
            action = ScanAction.BLOCK
            safe_text = "[Response blocked: contains credentials. Please contact support.]"
            blocked = True
        elif (self._redact_high and high) or (self._redact_medium and medium):
            to_redact = []
            if self._redact_high:
                to_redact.extend(high)
            if self._redact_medium:
                to_redact.extend(medium)
            safe_text = self._redactor.redact(text, to_redact)
            action = ScanAction.REDACT
            blocked = False
        else:
            safe_text = text
            action = ScanAction.ALLOW
            blocked = False

        decision = ScanDecision(
            action=action,
            original_text=text,
            safe_text=safe_text,
            matches=matches,
            blocked=blocked,
            redacted_count=len(matches),
        )
        self._logger.record(decision, session_id)
        return decision
```

## Solution 5: Secret Scan Audit Logger

```python
import time
from typing import List


class SecretScanAuditLogger:
    """
    Records all secret scan events where matches were found.
    """

    def __init__(self, max_records: int = 10000):
        self._max = max_records
        self._records: List[dict] = []

    def record(self, decision: ScanDecision, session_id: str = "") -> None:
        if not decision.matches:
            return
        if len(self._records) >= self._max:
            self._records.pop(0)
        self._records.append({
            "ts": time.time(),
            "session_id": session_id,
            "action": decision.action.value,
            "blocked": decision.blocked,
            "match_count": len(decision.matches),
            "categories": list({m.category.value for m in decision.matches}),
            "severities": list({m.severity for m in decision.matches}),
            "output_preview": decision.original_text[:100] + "...",
        })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "events": 0}
        blocked = sum(1 for r in recent if r["blocked"])
        category_counts: dict = {}
        for r in recent:
            for cat in r["categories"]:
                category_counts[cat] = category_counts.get(cat, 0) + 1
        return {
            "window_seconds": window_seconds,
            "events": len(recent),
            "blocked": blocked,
            "redacted": len(recent) - blocked,
            "category_breakdown": category_counts,
        }
```

## Solution 6: Pattern Coverage Tester

```python
from typing import List, Tuple


class PatternCoverageTester:
    """
    Tests secret patterns against known positive and negative examples
    to ensure patterns are neither too broad nor too narrow.
    """

    def __init__(self, detector: SecretDetector):
        self._detector = detector

    def test(
        self,
        positive_examples: List[Tuple[str, SecretCategory]],
        negative_examples: List[str],
    ) -> dict:
        false_negatives = []
        false_positives = []

        for text, expected_category in positive_examples:
            matches = self._detector.scan(text)
            matched_cats = {m.category for m in matches}
            if expected_category not in matched_cats:
                false_negatives.append({
                    "text_preview": text[:40],
                    "expected_category": expected_category.value,
                    "matched_categories": [c.value for c in matched_cats],
                })

        for text in negative_examples:
            matches = self._detector.scan(text)
            if matches:
                false_positives.append({
                    "text_preview": text[:40],
                    "matched_categories": [m.category.value for m in matches],
                })

        return {
            "positive_examples": len(positive_examples),
            "negative_examples": len(negative_examples),
            "false_negatives": len(false_negatives),
            "false_positives": len(false_positives),
            "precision": round(1 - len(false_positives) / max(len(negative_examples), 1), 4),
            "recall": round(1 - len(false_negatives) / max(len(positive_examples), 1), 4),
            "fn_details": false_negatives,
            "fp_details": false_positives,
        }
```

## Comparison

| Approach | Pattern Detection | Full Redaction | Partial Masking | Block on Critical | Audit Log |
|---|---|---|---|---|---|
| SecretDetector | Yes (regex + dedup) | No | No | No | No |
| OutputRedactor | No | Yes | Yes | No | No |
| OutputSecretScanGate | Via detector | Via redactor | Via redactor | Yes | Via logger |
| SecretScanAuditLogger | No | No | No | No | Yes |
| PatternCoverageTester | No | No | No | No | No |

**Best for production**: Set `block_on_critical=True` and `redact_on_high=True` — critical secrets (API keys, PEM keys) should never reach users even in redacted form because the audit trail alone is insufficient; the session should be flagged for review. Set `redact_on_medium=False` for base64 strings: most base64 in legitimate responses (encoded images, encoded data URIs) is not sensitive, and aggressive redaction degrades response quality. Run `PatternCoverageTester` in CI against a curated set of positive/negative examples — pattern regressions that cause false negatives on known secret formats are the most dangerous outcome. Emit an alert to your security SIEM whenever `SecretScanAuditLogger` records a blocked response: this is the highest-priority security signal in the agent's output pipeline.
