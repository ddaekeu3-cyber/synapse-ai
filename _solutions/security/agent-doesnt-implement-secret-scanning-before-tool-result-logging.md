---
title: "Agent Doesn't Implement Secret Scanning Before Tool Result Logging"
description: "Agents that log raw tool results without scanning them for secrets can inadvertently persist API keys, tokens, and credentials returned by external APIs into log sinks where they become accessible to anyone with log read access. Implement secret scanning of tool results before they are logged, redacting detected secrets while preserving enough context for debugging."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-secret-scanning-before-tool-result-logging
tags: [secret-scanning, tool-result-logging, credential-redaction, log-security, api-key-exposure, data-leakage]
symptoms:
  - "API keys returned in tool responses appear in plaintext in log aggregation platforms"
  - "GitHub tokens, Slack webhook URLs, or database passwords in tool results are logged unredacted"
  - "Compliance audit finds credentials in cold log storage from months ago"
  - "No distinction between safe-to-log and unsafe-to-log tool result fields"
  - "Tool result payloads are serialized to JSON and logged in full without any scanning"
---

## Why This Happens

Tool results often contain credentials: an API-integration tool returns a freshly-minted token, a configuration-fetch tool returns a config file with embedded passwords, or a user-data tool returns a profile that includes an API key the user stored. Without result scanning, these values land in every downstream log pipeline. Unlike argument masking (which protects known input fields), result scanning must handle arbitrary response structures from external APIs where field names are unpredictable. The approach is to scan result values — not just keys — against entropy and pattern heuristics regardless of field name.

## Solution 1: Secret Pattern Library

```python
import re
from dataclasses import dataclass
from typing import List, Pattern


@dataclass
class SecretPattern:
    name: str
    pattern: Pattern
    redact_strategy: str   # "full" | "partial"
    partial_keep_prefix: int = 6
    partial_keep_suffix: int = 4


RESULT_SECRET_PATTERNS: List[SecretPattern] = [
    SecretPattern(
        name="openai_api_key",
        pattern=re.compile(r"sk-[A-Za-z0-9]{20,}"),
        redact_strategy="partial",
        partial_keep_prefix=5,
        partial_keep_suffix=4,
    ),
    SecretPattern(
        name="anthropic_api_key",
        pattern=re.compile(r"sk-ant-[A-Za-z0-9\-]{20,}"),
        redact_strategy="partial",
        partial_keep_prefix=10,
        partial_keep_suffix=4,
    ),
    SecretPattern(
        name="github_token",
        pattern=re.compile(r"gh[pos]_[A-Za-z0-9]{36}"),
        redact_strategy="partial",
        partial_keep_prefix=4,
        partial_keep_suffix=4,
    ),
    SecretPattern(
        name="aws_access_key",
        pattern=re.compile(r"AKIA[0-9A-Z]{16}"),
        redact_strategy="partial",
        partial_keep_prefix=4,
        partial_keep_suffix=4,
    ),
    SecretPattern(
        name="bearer_token",
        pattern=re.compile(r"Bearer\s+[A-Za-z0-9\-_.]{20,}"),
        redact_strategy="partial",
        partial_keep_prefix=7,
        partial_keep_suffix=0,
    ),
    SecretPattern(
        name="generic_api_key",
        pattern=re.compile(r"[A-Za-z0-9\-_]{32,}"),
        redact_strategy="partial",
        partial_keep_prefix=4,
        partial_keep_suffix=4,
    ),
    SecretPattern(
        name="private_key_block",
        pattern=re.compile(r"-----BEGIN [A-Z ]+ PRIVATE KEY-----"),
        redact_strategy="full",
    ),
    SecretPattern(
        name="slack_webhook",
        pattern=re.compile(r"https://hooks\.slack\.com/services/T[A-Z0-9]+/B[A-Z0-9]+/[A-Za-z0-9]+"),
        redact_strategy="partial",
        partial_keep_prefix=35,
        partial_keep_suffix=0,
    ),
]
```

## Solution 2: Entropy Detector

```python
import math
from typing import Optional


class EntropyBasedSecretDetector:
    """
    Flags string values with high Shannon entropy as potential secrets.
    High-entropy strings that are also long are likely tokens or keys.
    """

    def __init__(
        self,
        entropy_threshold: float = 4.5,
        min_length: int = 20,
        max_length: int = 200,
    ):
        self._threshold = entropy_threshold
        self._min_len = min_length
        self._max_len = max_length

    @staticmethod
    def _entropy(s: str) -> float:
        if not s:
            return 0.0
        freq: dict = {}
        for ch in s:
            freq[ch] = freq.get(ch, 0) + 1
        total = len(s)
        return -sum((c / total) * math.log2(c / total) for c in freq.values())

    def is_likely_secret(self, value: str) -> bool:
        if not (self._min_len <= len(value) <= self._max_len):
            return False
        # Only flag alphanumeric+punctuation (not natural language)
        non_alpha = sum(1 for c in value if not c.isalpha() and not c.isspace())
        if non_alpha / max(len(value), 1) < 0.05:
            return False  # looks like natural language
        return self._entropy(value) >= self._threshold
```

## Solution 3: Tool Result Secret Scanner

```python
import copy
import re
from typing import Any, Dict, List


class ToolResultSecretScanner:
    """
    Recursively scans tool result structures for secret patterns
    and high-entropy strings. Returns a redacted deep copy and
    a list of findings. Never mutates the original.
    """

    REDACTED = "[REDACTED]"

    def __init__(
        self,
        patterns: List[SecretPattern] = None,
        entropy_detector: EntropyBasedSecretDetector = None,
    ):
        self._patterns = patterns or RESULT_SECRET_PATTERNS
        self._entropy_detector = entropy_detector or EntropyBasedSecretDetector()

    def scan_and_redact(self, result: Any) -> tuple[Any, List[dict]]:
        findings: List[dict] = []
        redacted = self._process(copy.deepcopy(result), "", findings)
        return redacted, findings

    def _process(self, obj: Any, path: str, findings: List[dict]) -> Any:
        if isinstance(obj, str):
            return self._redact_string(obj, path, findings)
        if isinstance(obj, dict):
            return {k: self._process(v, f"{path}.{k}" if path else k, findings) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._process(item, f"{path}[{i}]", findings) for i, item in enumerate(obj)]
        return obj

    def _redact_string(self, value: str, path: str, findings: List[dict]) -> str:
        for pattern_def in self._patterns:
            if pattern_def.pattern.search(value):
                redacted = self._apply_redaction(value, pattern_def)
                findings.append({
                    "path": path,
                    "pattern_name": pattern_def.name,
                    "original_length": len(value),
                })
                return redacted

        if self._entropy_detector.is_likely_secret(value):
            findings.append({
                "path": path,
                "pattern_name": "high_entropy",
                "original_length": len(value),
            })
            return self._partial_redact(value, 4, 4)

        return value

    def _apply_redaction(self, value: str, pattern_def: SecretPattern) -> str:
        if pattern_def.redact_strategy == "full":
            return self.REDACTED
        return self._partial_redact(
            value, pattern_def.partial_keep_prefix, pattern_def.partial_keep_suffix
        )

    @staticmethod
    def _partial_redact(value: str, prefix: int, suffix: int) -> str:
        if len(value) <= prefix + suffix:
            return ToolResultSecretScanner.REDACTED
        tail = value[-suffix:] if suffix > 0 else ""
        return f"{value[:prefix]}***{tail}"
```

## Solution 4: Secret-Safe Tool Result Logger

```python
import json
import time
from typing import Any, Callable, Dict, Optional


class SecretSafeToolResultLogger:
    """
    Logs tool results after scanning and redacting secrets.
    Accepts a write_fn for integration with any log sink.
    """

    def __init__(
        self,
        scanner: ToolResultSecretScanner,
        write_fn: Optional[Callable[[dict], None]] = None,
    ):
        self._scanner = scanner
        self._write = write_fn or (lambda r: print(json.dumps(r)))
        self._logged = 0
        self._findings_total = 0

    def log_result(
        self,
        tool_name: str,
        raw_result: Any,
        latency_ms: float = 0.0,
        session_id: str = "",
    ) -> int:
        redacted_result, findings = self._scanner.scan_and_redact(raw_result)
        self._logged += 1
        self._findings_total += len(findings)

        record: dict = {
            "event": "tool_result",
            "tool_name": tool_name,
            "result": redacted_result,
            "latency_ms": latency_ms,
            "timestamp": time.time(),
            "session_id": session_id,
        }
        if findings:
            record["secrets_redacted"] = len(findings)
            record["redacted_paths"] = [f["path"] for f in findings]

        self._write(record)
        return len(findings)

    def stats(self) -> dict:
        return {
            "logged_results": self._logged,
            "total_secrets_redacted": self._findings_total,
        }
```

## Solution 5: Redaction Coverage Auditor

```python
from typing import Any, Dict, List


class RedactionCoverageAuditor:
    """
    Samples tool results in a staging environment to measure what
    fraction contain secrets and whether patterns cover them all.
    Used to tune the pattern library before production deployment.
    """

    def __init__(self, scanner: ToolResultSecretScanner):
        self._scanner = scanner

    def audit(self, samples: List[tuple[str, Any]]) -> dict:
        """samples: list of (tool_name, raw_result)"""
        total = len(samples)
        tools_with_secrets: dict = {}
        total_findings = 0

        for tool_name, result in samples:
            _, findings = self._scanner.scan_and_redact(result)
            if findings:
                tools_with_secrets[tool_name] = tools_with_secrets.get(tool_name, 0) + len(findings)
                total_findings += len(findings)

        return {
            "samples_audited": total,
            "samples_with_secrets": len([s for _, s in samples
                                         if self._scanner.scan_and_redact(s)[1]]),
            "total_findings": total_findings,
            "findings_per_tool": tools_with_secrets,
            "coverage_rate": round(len(tools_with_secrets) / max(total, 1), 4),
        }
```

## Solution 6: Secret Scanning Dashboard

```python
import time


class SecretScanningDashboard:
    """
    Combines logger stats and auditor results into an operational
    security report for the tool result scanning pipeline.
    """

    def __init__(
        self,
        logger: SecretSafeToolResultLogger,
        scanner: ToolResultSecretScanner,
    ):
        self._logger = logger
        self._scanner = scanner

    def render(self) -> dict:
        stats = self._logger.stats()
        redaction_rate = round(
            stats["total_secrets_redacted"] / max(stats["logged_results"], 1), 4
        )
        return {
            "generated_at": time.time(),
            "logger_stats": stats,
            "redaction_rate_per_result": redaction_rate,
            "patterns_active": len(self._scanner._patterns),
            "entropy_detection_enabled": self._scanner._entropy_detector is not None,
        }
```

## Comparison

| Approach | Pattern Scanning | Entropy Detection | Recursive Deep Scan | Findings Report | Log Integration |
|---|---|---|---|---|---|
| ToolResultSecretScanner | Yes (regex) | Via detector | Yes | Yes | No |
| EntropyBasedSecretDetector | No | Yes (Shannon) | No | No | No |
| SecretSafeToolResultLogger | Via scanner | Via scanner | Via scanner | Partial (counts) | Yes |
| RedactionCoverageAuditor | Via scanner | Via scanner | Via scanner | Yes (per-tool) | No |
| SecretScanningDashboard | No | No | No | No | Yes |

**Best for production**: The `generic_api_key` pattern (32+ alphanumeric chars) will generate false positives on UUIDs and base64-encoded content — tune `min_length=40` and combine with entropy threshold to reduce noise. Run `RedactionCoverageAuditor.audit()` against a week of sampled tool results in staging to discover tool-specific secret patterns before go-live. Log `secrets_redacted` counts to a separate security metrics stream so a spike (a new tool returning credentials) triggers an alert. Never log the unredacted value even for debugging — the redacted prefix/suffix provides enough correlation without exposing the full secret.
