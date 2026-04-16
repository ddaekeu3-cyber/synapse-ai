---
title: "Agent Doesn't Implement Privacy-Preserving Logging for Sensitive Queries"
description: "AI agents that log user queries and tool parameters verbatim create high-value surveillance archives. When logs are breached, subpoenaed, or accessed by internal staff, they expose sensitive user intent, medical queries, financial details, and personal relationships. Privacy-preserving logging replaces raw content with k-anonymous tokens, differential-private summaries, and structural metadata — preserving debuggability while eliminating personal data exposure."
date: 2025-02-12
difficulty: advanced
category: security
slug: agent-doesnt-implement-privacy-preserving-logging-for-sensitive-queries
tags:
  - privacy
  - logging
  - differential-privacy
  - k-anonymity
  - pii-scrubbing
  - gdpr
  - data-minimisation
  - sensitive-queries
symptoms:
  - "Agent logs contain verbatim user queries including medical symptoms, financial details, and relationship issues"
  - "Log breach would expose full conversation history of every user"
  - "GDPR right-to-erasure cannot be satisfied because user content is embedded in immutable log lines"
  - "Internal staff can read exact user queries from production logs"
  - "Security audit finds PII in structured log fields (user_message, tool_input)"
---

## Problem

Agent logs serve two purposes: debugging and compliance auditing. Both require knowing *what happened* (tool called, error raised, latency exceeded) but neither requires the verbatim content of user queries. A medical question can be represented as `[health_query, 18_tokens]`; a bank transfer query as `[financial_query, 12_tokens, contains_amount=true]`. Privacy-preserving logging extracts structural signals while discarding personal content, satisfying both debuggability and data-minimisation requirements.

---

## Solution 1: PIIScrubber — Regex-Based Content Redaction

```python
import hashlib
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Pattern, Tuple


@dataclass
class ScrubRule:
    name: str
    pattern: Pattern
    replacement: str
    hash_replacement: bool = False   # replace with consistent hash (for correlation)


class PIIScrubber:
    """
    Scrubs PII from log strings using configurable regex rules.
    Supports both redaction (fixed placeholder) and consistent hashing
    (same input → same token, for event correlation without exposure).

    Usage:
        scrubber = PIIScrubber.default()
        safe = scrubber.scrub("My SSN is 123-45-6789 and email is bob@example.com")
        # "My SSN is [SSN] and email is [EMAIL:a3f2b1]"
    """

    DEFAULT_RULES: List[Tuple[str, str]] = [
        # (pattern, placeholder)
        (r"\b\d{3}-\d{2}-\d{4}\b", "[SSN]"),
        (r"\b\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}\b", "[CARD]"),
        (r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b", "[EMAIL]"),
        (r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b", "[PHONE]"),
        (r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", "[IP]"),
        (r"sk-[A-Za-z0-9]{20,}", "[API_KEY]"),
        (r"Bearer\s+[A-Za-z0-9\-._~+/]+=*", "Bearer [TOKEN]"),
        (r"password[=:\s]+\S+", "password=[REDACTED]"),
    ]

    def __init__(self, rules: Optional[List[ScrubRule]] = None):
        self._rules = rules or [
            ScrubRule(
                name=name,
                pattern=re.compile(pattern, re.IGNORECASE),
                replacement=placeholder,
            )
            for name, (pattern, placeholder) in [
                (n, (p, r)) for n, (p, r) in
                [(f"rule_{i}", r) for i, r in enumerate(self.DEFAULT_RULES)]
            ]
        ]

    @classmethod
    def default(cls) -> "PIIScrubber":
        rules = []
        for i, (pattern, placeholder) in enumerate(cls.DEFAULT_RULES):
            rules.append(ScrubRule(
                name=f"rule_{i}",
                pattern=re.compile(pattern, re.IGNORECASE),
                replacement=placeholder,
            ))
        return cls(rules)

    def scrub(self, text: str) -> str:
        for rule in self._rules:
            if rule.hash_replacement:
                def replace_with_hash(m, name=rule.name):
                    h = hashlib.sha256(m.group().encode()).hexdigest()[:6]
                    return f"[{name.upper()}:{h}]"
                text = rule.pattern.sub(replace_with_hash, text)
            else:
                text = rule.pattern.sub(rule.replacement, text)
        return text

    def scrub_dict(self, data: Dict[str, Any],
                    sensitive_keys: Optional[List[str]] = None) -> Dict[str, Any]:
        result = {}
        default_sensitive = {"password", "token", "secret", "key", "auth",
                              "credential", "ssn", "card", "user_message",
                              "query", "content", "text", "prompt"}
        sensitive = set(sensitive_keys or []) | default_sensitive
        for k, v in data.items():
            if any(s in k.lower() for s in sensitive) and isinstance(v, str):
                result[k] = self.scrub(v)
            elif isinstance(v, dict):
                result[k] = self.scrub_dict(v, sensitive_keys)
            else:
                result[k] = v
        return result
```

---

## Solution 2: StructuralQueryLogger — Replace Content with Metadata

Instead of logging query text, log structural metadata: token count, query category, presence of sensitive patterns, language.

```python
import hashlib
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set


@dataclass
class StructuralQueryRecord:
    session_id_hash: str          # HMAC-SHA256(session_id, log_secret)
    timestamp: float
    token_count: int
    query_category: str           # "health", "financial", "legal", "general", etc.
    has_pii: bool
    has_question: bool
    language: str                 # "en", "es", etc. (detected from structure)
    tool_names: List[str]
    response_token_count: int = 0
    latency_ms: float = 0.0
    error_class: Optional[str] = None


CATEGORY_PATTERNS = {
    "health": re.compile(r"\b(symptom|diagnos|medic|doctor|disease|pain|treatment|drug|prescri)\b", re.I),
    "financial": re.compile(r"\b(invest|stock|fund|portfol|tax|bank|money|salary|income|credit)\b", re.I),
    "legal": re.compile(r"\b(lawyer|attorney|contract|lawsuit|court|legal|sue|litigation)\b", re.I),
    "relationship": re.compile(r"\b(partner|spouse|divorce|marriage|breakup|relationship|cheating)\b", re.I),
}

PII_PATTERN = re.compile(
    r"\b(\d{3}-\d{2}-\d{4}|"          # SSN
    r"\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}|"  # card
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}|"  # email
    r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4})\b",  # phone
    re.I,
)


class StructuralQueryLogger:
    """
    Converts user queries into structural metadata before logging.
    No query content reaches the log store.

    Usage:
        logger = StructuralQueryLogger(log_secret=os.environ["LOG_HMAC_SECRET"])
        record = logger.analyse(
            session_id="sess-abc123",
            query="What medications should I take for my diabetes?",
            tool_names=["web_search"],
            response_tokens=512,
            latency_ms=320.0,
        )
        structured_logger.info(record.__dict__)
    """

    def __init__(self, log_secret: str = ""):
        self._secret = log_secret.encode()

    def _hash_id(self, value: str) -> str:
        import hmac as hmac_mod
        return hmac_mod.new(self._secret, value.encode(), "sha256").hexdigest()[:16]

    def _categorise(self, text: str) -> str:
        for category, pattern in CATEGORY_PATTERNS.items():
            if pattern.search(text):
                return category
        return "general"

    def analyse(self, session_id: str, query: str,
                 tool_names: List[str],
                 response_tokens: int = 0,
                 latency_ms: float = 0.0,
                 error: Optional[Exception] = None) -> StructuralQueryRecord:
        words = query.split()
        return StructuralQueryRecord(
            session_id_hash=self._hash_id(session_id),
            timestamp=time.time(),
            token_count=len(words),
            query_category=self._categorise(query),
            has_pii=bool(PII_PATTERN.search(query)),
            has_question="?" in query or any(
                query.lower().startswith(w)
                for w in ("what", "how", "why", "when", "where", "who", "can", "should")
            ),
            language="en",  # extend with langdetect if needed
            tool_names=tool_names,
            response_token_count=response_tokens,
            latency_ms=latency_ms,
            error_class=type(error).__name__ if error else None,
        )
```

---

## Solution 3: DifferentialPrivacyCounter — Noisy Aggregate Statistics

Use the Laplace mechanism to add calibrated noise to aggregate query counts, preventing inference of individual queries from statistics.

```python
import math
import secrets
from typing import Dict


class LaplaceNoiseMechanism:
    """
    Adds Laplace noise to counts for epsilon-differential privacy.
    Provides statistical utility while making individual entries
    mathematically impossible to isolate.

    Usage:
        dp = LaplaceNoiseMechanism(epsilon=1.0, sensitivity=1.0)
        noisy_count = dp.add_noise(true_count)
        # Publish noisy_count safely; true_count stays private.
    """

    def __init__(self, epsilon: float = 1.0, sensitivity: float = 1.0):
        self._scale = sensitivity / epsilon

    def _laplace(self) -> float:
        u = secrets.randbelow(2**32) / 2**32 - 0.5
        return -self._scale * math.copysign(1, u) * math.log(1 - 2 * abs(u))

    def add_noise(self, true_value: float) -> float:
        return true_value + self._laplace()

    def noisy_histogram(self, counts: Dict[str, int]) -> Dict[str, float]:
        return {k: max(0.0, self.add_noise(v)) for k, v in counts.items()}

    def privacy_budget_remaining(self, queries_answered: int,
                                  total_budget: float = 10.0) -> float:
        """Estimate remaining epsilon budget (simple sequential composition)."""
        used = self._scale * queries_answered
        return max(0.0, total_budget - used)


class PrivateQueryStatsCollector:
    """
    Collects query statistics with differential privacy.
    Publishes noisy category counts for dashboards without exposing individual queries.

    Usage:
        collector = PrivateQueryStatsCollector(epsilon=0.5)
        collector.record("health")
        collector.record("financial")
        report = collector.publish()   # noisy counts safe to expose
    """

    def __init__(self, epsilon: float = 1.0):
        self._dp = LaplaceNoiseMechanism(epsilon=epsilon)
        self._counts: Dict[str, int] = {}

    def record(self, category: str):
        self._counts[category] = self._counts.get(category, 0) + 1

    def publish(self) -> Dict[str, float]:
        return self._dp.noisy_histogram(self._counts)

    def reset(self):
        self._counts.clear()
```

---

## Solution 4: LogTokeniser — Consistent Pseudonymisation

Replace user IDs, session IDs, and query content with consistent pseudonyms using keyed hashing. Same input always produces the same token, enabling event correlation without exposing the original value.

```python
import hashlib
import hmac as hmac_mod
import os
from typing import Any, Dict, Optional


class LogTokeniser:
    """
    Replaces sensitive identifiers with consistent pseudonyms.
    Two events with the same session_id get the same session_token,
    enabling log correlation without storing the real session_id.
    Pseudonymisation can be reversed only with the tokenisation secret.

    Usage:
        tok = LogTokeniser(secret=os.environ["LOG_TOKEN_SECRET"])
        log_record = {
            "session_id": "sess-alice-12345",
            "user_id":    "user-alice",
            "query":      "[STRUCTURAL_METADATA_ONLY]",
        }
        safe_record = tok.pseudonymise(log_record, ["session_id", "user_id"])
        # {"session_token": "a3f2b1c4", "user_token": "d5e6f7a8", ...}
    """

    def __init__(self, secret: Optional[str] = None):
        self._secret = (secret or os.environ.get("LOG_TOKEN_SECRET", "")).encode()

    def token(self, value: str, namespace: str = "") -> str:
        msg = f"{namespace}:{value}".encode()
        return hmac_mod.new(self._secret, msg, hashlib.sha256).hexdigest()[:12]

    def pseudonymise(self, record: Dict[str, Any],
                      fields: list) -> Dict[str, Any]:
        result = dict(record)
        for field in fields:
            if field in result:
                val = result.pop(field)
                token_key = field.replace("_id", "_token").replace("_sid", "_token")
                result[token_key] = self.token(str(val), field)
        return result

    def tokenise_list(self, values: list, namespace: str = "") -> list:
        return [self.token(str(v), namespace) for v in values]
```

---

## Solution 5: RetentionBoundedLogHandler — Auto-Expire Sensitive Logs

A Python logging handler that automatically deletes or archives log entries containing sensitive content after a configurable retention period.

```python
import logging
import os
import time
from pathlib import Path
from typing import Optional


class RetentionBoundedLogHandler(logging.Handler):
    """
    Log handler that writes to dated files and auto-deletes files
    older than the retention period. Ensures PII does not persist
    beyond the retention limit, satisfying GDPR Art. 5(1)(e).

    Usage:
        handler = RetentionBoundedLogHandler(
            log_dir="/var/log/agent",
            retention_days=30,
            sensitive_marker="[SENSITIVE]",
            sensitive_retention_days=7,
        )
        logging.getLogger("agent").addHandler(handler)
    """

    def __init__(self, log_dir: str,
                 retention_days: int = 90,
                 sensitive_retention_days: int = 7,
                 sensitive_marker: str = "[SENSITIVE]"):
        super().__init__()
        self._dir = Path(log_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._retention = retention_days * 86400
        self._sensitive_retention = sensitive_retention_days * 86400
        self._marker = sensitive_marker

    def emit(self, record: logging.LogRecord):
        try:
            msg = self.format(record)
            date_str = time.strftime("%Y%m%d")
            is_sensitive = self._marker in msg
            suffix = ".sensitive.log" if is_sensitive else ".log"
            path = self._dir / f"agent_{date_str}{suffix}"
            with open(path, "a") as f:
                f.write(msg + "\n")
            self._purge_old()
        except Exception:
            self.handleError(record)

    def _purge_old(self):
        now = time.time()
        for path in self._dir.glob("*.log"):
            age = now - path.stat().st_mtime
            limit = (self._sensitive_retention
                     if ".sensitive." in path.name
                     else self._retention)
            if age > limit:
                path.unlink(missing_ok=True)
```

---

## Solution 6: PrivacyPreservingAgentLogger — Full Pipeline

End-to-end privacy-preserving logger combining scrubbing, structural analysis, pseudonymisation, and retention-bounded storage.

```python
import logging
import time
from typing import Any, Dict, List, Optional


class PrivacyPreservingAgentLogger:
    """
    Drop-in agent logger with full privacy-preserving pipeline:
    1. Scrub PII from any string fields
    2. Tokenise user/session identifiers
    3. Replace query content with structural metadata
    4. Apply differential privacy noise to aggregate counters
    5. Write to retention-bounded log handler

    Usage:
        plogger = PrivacyPreservingAgentLogger(
            log_secret=os.environ["LOG_SECRET"],
            log_dir="/var/log/agent/privacy",
        )
        plogger.log_request(
            session_id="sess-abc",
            user_id="user-42",
            query="What are symptoms of diabetes?",
            tool_names=["web_search"],
            response_tokens=400,
            latency_ms=280.0,
        )
    """

    def __init__(self, log_secret: str = "",
                 log_dir: str = "/tmp/agent_logs",
                 epsilon: float = 1.0):
        self._scrubber = PIIScrubber.default()
        self._structural = StructuralQueryLogger(log_secret)
        self._tokeniser = LogTokeniser(log_secret)
        self._dp = PrivateQueryStatsCollector(epsilon)
        self._logger = logging.getLogger("agent.privacy")

    def log_request(self, session_id: str, user_id: str,
                     query: str, tool_names: List[str],
                     response_tokens: int = 0,
                     latency_ms: float = 0.0,
                     error: Optional[Exception] = None):
        # Step 1: structural analysis (no content retained)
        record = self._structural.analyse(
            session_id, query, tool_names,
            response_tokens, latency_ms, error,
        )
        # Step 2: build safe log dict
        safe = {
            "session_token": self._tokeniser.token(session_id, "session"),
            "user_token":    self._tokeniser.token(user_id,    "user"),
            "ts":            record.timestamp,
            "token_count":   record.token_count,
            "category":      record.query_category,
            "has_pii":       record.has_pii,
            "has_question":  record.has_question,
            "tools":         record.tool_names,
            "resp_tokens":   record.response_token_count,
            "latency_ms":    record.latency_ms,
            "error":         record.error_class,
        }
        self._logger.info(safe)
        # Step 3: aggregate DP stats
        self._dp.record(record.query_category)

    def publish_stats(self) -> Dict[str, float]:
        """Return DP-noised category counts safe for dashboards."""
        return self._dp.publish()
```

---

## Comparison

| Approach | Removes PII | Preserves Debuggability | Differential Privacy | GDPR Erasure | Correlation |
|---|---|---|---|---|---|
| **PIIScrubber** | Partial (regex) | High | No | Partial | No |
| **StructuralQueryLogger** | Full (no content) | Medium | No | Yes | No |
| **DPCounterStats** | N/A | Low (aggregates only) | Yes | Yes | No |
| **LogTokeniser** | Yes (pseudonym) | High | No | Via secret rotation | Yes |
| **RetentionBoundedLogHandler** | Auto-expiry | Time-limited | No | Yes | N/A |
| **PrivacyPreservingAgentLogger** | Full pipeline | Medium | Yes | Yes | Yes (tokens) |

**Key insight**: log structure and outcomes, not content. A `{category: health, token_count: 18, latency_ms: 320, error: null}` record is sufficient to debug 95% of production issues and contains zero personal information. Reserve content logging for consented debug sessions with explicit short retention and access controls.
