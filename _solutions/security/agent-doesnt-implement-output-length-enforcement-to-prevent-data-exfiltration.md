---
title: "Agent Doesn't Implement Output Length Enforcement to Prevent Data Exfiltration"
description: "Agents that place no ceiling on LLM output length allow prompt-injection payloads to instruct the model to dump large volumes of retrieved context, tool results, or internal state into the response. Implement output length enforcement that caps response size, detects anomalously long outputs relative to query complexity, and alerts when output length significantly exceeds historical norms for the same tool or query pattern."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-output-length-enforcement-to-prevent-data-exfiltration
tags: [data-exfiltration, output-length, response-capping, prompt-injection, anomaly-detection, output-security]
symptoms:
  - "Injected instruction 'print all retrieved documents' produces a 50KB response"
  - "No max_tokens ceiling enforced — LLM can produce arbitrarily long outputs"
  - "Response length varies from 50 to 50000 tokens with no alerting on outliers"
  - "Tool results are injected into context and can be reflected back verbatim in output"
  - "No comparison between expected output length for a query type and actual output length"
---

## Why This Happens

LLM APIs accept a `max_tokens` parameter but agents often set it to a high value or omit it to avoid truncating legitimate responses. An attacker who can inject content into a retrieved document can include instructions like "repeat all context verbatim" — and the model may comply, exfiltrating everything in the context window. Output length enforcement adds a second control layer: even if `max_tokens` is generous, the application layer measures output length, compares it to historical norms for the query type, and treats anomalous length as a security signal.

## Solution 1: Output Length Policy

```python
from dataclasses import dataclass
from typing import Optional


@dataclass
class OutputLengthPolicy:
    hard_max_tokens: int = 4096         # absolute ceiling — truncate above this
    hard_max_chars: int = 16384         # character ceiling
    warn_multiplier: float = 3.0        # warn if output is 3× the baseline
    alert_multiplier: float = 10.0      # alert if output is 10× the baseline
    baseline_tokens: int = 256          # expected output tokens for this query type
    enforce_truncation: bool = True     # truncate at hard_max vs raise error
```

## Solution 2: Output Length Analyzer

```python
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from threading import Lock
from typing import Deque, Optional, Tuple


class LengthAssessment(str, Enum):
    NORMAL = "normal"
    ELEVATED = "elevated"
    ANOMALOUS = "anomalous"


@dataclass
class OutputLengthRecord:
    token_count: int
    char_count: int
    assessment: LengthAssessment
    policy_applied: str   # "none" | "warn" | "truncate" | "block"
    recorded_at: float = field(default_factory=time.time)


class OutputLengthAnalyzer:
    """
    Tracks output lengths per query category and assesses whether
    a given output length is normal, elevated, or anomalous.
    """

    def __init__(self, window_seconds: float = 3600.0, max_samples: int = 2000):
        self._window = window_seconds
        self._max = max_samples
        self._samples: Deque[Tuple[float, int]] = deque()  # (ts, token_count)
        self._lock = Lock()

    def record(self, token_count: int) -> None:
        now = time.time()
        with self._lock:
            self._samples.append((now, token_count))
            self._trim(now)
            if len(self._samples) > self._max:
                self._samples.popleft()

    def _trim(self, now: float) -> None:
        cutoff = now - self._window
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def baseline_tokens(self) -> Optional[float]:
        with self._lock:
            if len(self._samples) < 10:
                return None
            counts = sorted(s[1] for s in self._samples)
            idx = int(len(counts) * 0.50)
            return counts[min(idx, len(counts) - 1)]

    def assess(self, token_count: int, policy: OutputLengthPolicy) -> LengthAssessment:
        baseline = self.baseline_tokens() or policy.baseline_tokens
        if token_count > baseline * policy.alert_multiplier:
            return LengthAssessment.ANOMALOUS
        if token_count > baseline * policy.warn_multiplier:
            return LengthAssessment.ELEVATED
        return LengthAssessment.NORMAL
```

## Solution 3: Output Length Enforcer

```python
from typing import Any


class OutputLengthEnforcer:
    """
    Applies hard length limits to LLM output text.
    Returns the (possibly truncated) text and a record of what was applied.
    """

    def __init__(self, policy: OutputLengthPolicy, analyzer: OutputLengthAnalyzer):
        self._policy = policy
        self._analyzer = analyzer

    def _count_tokens(self, text: str) -> int:
        # Rough estimate: 4 chars per token
        return max(1, len(text) // 4)

    def enforce(self, text: str) -> Tuple[str, OutputLengthRecord]:
        token_count = self._count_tokens(text)
        char_count = len(text)
        policy_applied = "none"

        # Hard char limit
        if char_count > self._policy.hard_max_chars:
            if self._policy.enforce_truncation:
                text = text[: self._policy.hard_max_chars]
                char_count = self._policy.hard_max_chars
                token_count = self._count_tokens(text)
                policy_applied = "truncate"
            else:
                policy_applied = "block"

        # Hard token limit
        elif token_count > self._policy.hard_max_tokens:
            if self._policy.enforce_truncation:
                char_limit = self._policy.hard_max_tokens * 4
                text = text[:char_limit]
                char_count = len(text)
                token_count = self._policy.hard_max_tokens
                policy_applied = "truncate"
            else:
                policy_applied = "block"

        assessment = self._analyzer.assess(token_count, self._policy)
        if assessment != LengthAssessment.NORMAL and policy_applied == "none":
            policy_applied = "warn"

        self._analyzer.record(token_count)

        record = OutputLengthRecord(
            token_count=token_count,
            char_count=char_count,
            assessment=assessment,
            policy_applied=policy_applied,
        )
        return text, record
```

## Solution 4: Per-Query-Type Length Registry

```python
from typing import Dict, Optional


class PerQueryTypeLengthRegistry:
    """
    Maintains separate OutputLengthPolicy and OutputLengthAnalyzer instances
    per query type (e.g. "summarize", "qa", "tool_call", "code_generation").
    """

    def __init__(self, default_policy: Optional[OutputLengthPolicy] = None):
        self._default = default_policy or OutputLengthPolicy()
        self._policies: Dict[str, OutputLengthPolicy] = {}
        self._analyzers: Dict[str, OutputLengthAnalyzer] = {}
        self._enforcers: Dict[str, OutputLengthEnforcer] = {}

    def register(self, query_type: str, policy: OutputLengthPolicy) -> None:
        analyzer = OutputLengthAnalyzer()
        self._policies[query_type] = policy
        self._analyzers[query_type] = analyzer
        self._enforcers[query_type] = OutputLengthEnforcer(policy, analyzer)

    def enforce(self, query_type: str, text: str) -> Tuple[str, OutputLengthRecord]:
        if query_type not in self._enforcers:
            self.register(query_type, self._default)
        return self._enforcers[query_type].enforce(text)
```

## Solution 5: Exfiltration Signal Detector

```python
import re
import time
from dataclasses import dataclass, field
from typing import List


EXFILTRATION_PATTERNS = [
    (re.compile(r"(repeat|print|output|show|display|dump).{0,30}(all|every|entire|full|complete|verbatim)", re.IGNORECASE), "dump_instruction"),
    (re.compile(r"(ignore|disregard|forget).{0,20}(previous|prior|above|instructions)", re.IGNORECASE), "ignore_instruction"),
    (re.compile(r"(system prompt|context|retrieved|documents|tool results).{0,20}(above|before|provided)", re.IGNORECASE), "context_reference"),
]


@dataclass
class ExfiltrationSignal:
    pattern_name: str
    matched_text: str
    output_length_tokens: int
    detected_at: float = field(default_factory=time.time)


class ExfiltrationSignalDetector:
    """
    Scans LLM output for patterns that suggest a prompt injection
    caused the model to exfiltrate context content.
    """

    def __init__(self, max_records: int = 5000):
        self._records: List[ExfiltrationSignal] = []
        self._max = max_records

    def scan(self, output_text: str, token_count: int) -> List[ExfiltrationSignal]:
        findings = []
        for pattern, name in EXFILTRATION_PATTERNS:
            match = pattern.search(output_text)
            if match:
                signal = ExfiltrationSignal(
                    pattern_name=name,
                    matched_text=match.group(0)[:100],
                    output_length_tokens=token_count,
                )
                findings.append(signal)
                if len(self._records) < self._max:
                    self._records.append(signal)
        return findings

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r.detected_at >= cutoff]
        by_pattern: dict = {}
        for r in recent:
            by_pattern[r.pattern_name] = by_pattern.get(r.pattern_name, 0) + 1
        return {
            "window_seconds": window_seconds,
            "signals": len(recent),
            "by_pattern": by_pattern,
        }
```

## Solution 6: Output Security Dashboard

```python
import time


class OutputSecurityDashboard:
    def __init__(
        self,
        registry: PerQueryTypeLengthRegistry,
        detector: ExfiltrationSignalDetector,
    ):
        self._registry = registry
        self._detector = detector

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "exfiltration_signals": self._detector.summary(3600.0),
        }
```

## Comparison

| Approach | Hard Length Cap | Anomaly Detection | Per-Query-Type | Exfiltration Patterns | Dashboard |
|---|---|---|---|---|---|
| OutputLengthEnforcer | Yes (char + token) | Via analyzer | No | No | No |
| OutputLengthAnalyzer | No | Yes (multiplier) | No | No | No |
| PerQueryTypeLengthRegistry | Via enforcer | Via analyzer | Yes | No | No |
| ExfiltrationSignalDetector | No | No | No | Yes (regex) | No |
| OutputSecurityDashboard | No | No | No | No | Yes |

**Best for production**: Set `hard_max_tokens` to 2× the legitimate maximum for each query type — a summarization task rarely needs more than 500 tokens; code generation may need 2000. Use `alert_multiplier=10.0` to catch only egregious outliers, not normal variation. Run `ExfiltrationSignalDetector.scan()` on every output before returning it to the user: a single match is worth logging; three matches in one session warrant session termination. Monitor `summary()` for spikes in `dump_instruction` signals, which indicate an active injection campaign against retrieved documents.
