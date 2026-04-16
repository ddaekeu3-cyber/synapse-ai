---
title: "Agent Doesn't Implement Prompt Length Abuse Detection"
description: "Agents that accept user-supplied prompt content without length-based abuse detection are vulnerable to context flooding attacks — adversarial users submit extremely long inputs to push safety instructions out of the context window, force expensive token usage, or trigger model confusion. Implement prompt length abuse detection that distinguishes legitimate long documents from adversarial padding, tracks per-user length patterns, and blocks or flags anomalous submissions."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-prompt-length-abuse-detection
tags: [prompt-abuse, context-flooding, length-validation, adversarial-input, token-abuse, input-security]
symptoms:
  - "Users submit 50KB text walls that push the system prompt out of the context window"
  - "Repeated identical padding characters detected in user input"
  - "Token costs spike from a single user submitting abnormally large requests"
  - "No per-user length baseline — cannot distinguish legitimate long docs from abuse"
  - "System prompt injection succeeds because model attention is diluted by preceding noise"
---

## Why This Happens

Context window flooding is a prompt injection technique: by filling the context with irrelevant text, an attacker dilutes the model's attention to the system prompt and safety instructions, then appends actual malicious instructions at the end. Without length abuse detection, the agent has no way to distinguish a legitimate 10,000-token legal document from a 10,000-token padding attack. Detection requires both absolute length limits and statistical anomaly detection against per-user baselines.

## Solution 1: Length Abuse Signature

```python
import re
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional


class LengthAbuseType(str, Enum):
    ABSOLUTE_OVERFLOW = "absolute_overflow"       # exceeds hard limit
    REPETITION_PADDING = "repetition_padding"     # repeated chars/phrases
    WHITESPACE_INFLATION = "whitespace_inflation" # excessive whitespace
    STATISTICAL_ANOMALY = "statistical_anomaly"   # far above user baseline
    STRUCTURED_FLOODING = "structured_flooding"   # many short repeated lines


@dataclass
class LengthAbuseSignal:
    abuse_type: LengthAbuseType
    detected_value: float
    threshold: float
    description: str
    severity: str   # "warning" | "block"


REPETITION_PATTERN = re.compile(r"(.{3,})\1{9,}", re.DOTALL)
WHITESPACE_PATTERN = re.compile(r"\s{200,}")
REPEATED_LINE_PATTERN = re.compile(r"^(.+)$(\n\1){9,}", re.MULTILINE)
```

## Solution 2: Content Structure Analyzer

```python
import math
import re
from typing import Tuple


class ContentStructureAnalyzer:
    """
    Analyzes text structure to distinguish legitimate content from padding attacks.
    Returns per-signal scores rather than a single pass/fail to allow nuanced decisions.
    """

    def repetition_ratio(self, text: str) -> float:
        """Fraction of text covered by repeated n-gram sequences."""
        if not text:
            return 0.0
        matches = REPETITION_PATTERN.findall(text)
        covered = sum(len(m) * 10 for m in matches)
        return min(covered / max(len(text), 1), 1.0)

    def whitespace_ratio(self, text: str) -> float:
        """Fraction of text that is whitespace."""
        if not text:
            return 0.0
        whitespace = sum(1 for c in text if c in " \t\n\r")
        return whitespace / len(text)

    def unique_line_ratio(self, text: str) -> float:
        """Ratio of unique lines to total lines. Low = repeated line attack."""
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        if not lines:
            return 1.0
        return len(set(lines)) / len(lines)

    def shannon_entropy(self, text: str) -> float:
        """Character-level entropy. Very low = highly repetitive padding."""
        if not text:
            return 0.0
        freq: dict = {}
        for c in text:
            freq[c] = freq.get(c, 0) + 1
        total = len(text)
        entropy = -sum((v / total) * math.log2(v / total) for v in freq.values())
        return round(entropy, 4)

    def analyze(self, text: str) -> dict:
        return {
            "length": len(text),
            "repetition_ratio": self.repetition_ratio(text),
            "whitespace_ratio": self.whitespace_ratio(text),
            "unique_line_ratio": self.unique_line_ratio(text),
            "shannon_entropy": self.shannon_entropy(text),
        }
```

## Solution 3: Per-User Length Baseline Tracker

```python
import time
from collections import deque
from typing import Deque, Dict, Optional, Tuple


class UserLengthBaselineTracker:
    """
    Tracks rolling length statistics per user to detect
    anomalous submissions relative to that user's own history.
    """

    def __init__(
        self,
        window_size: int = 50,
        anomaly_multiplier: float = 5.0,
    ) -> None:
        self._histories: Dict[str, Deque[int]] = {}
        self._window = window_size
        self._multiplier = anomaly_multiplier

    def record(self, user_id: str, length: int) -> None:
        if user_id not in self._histories:
            self._histories[user_id] = deque(maxlen=self._window)
        self._histories[user_id].append(length)

    def baseline(self, user_id: str) -> Optional[float]:
        history = self._histories.get(user_id)
        if not history or len(history) < 5:
            return None
        return sum(history) / len(history)

    def is_anomalous(self, user_id: str, length: int) -> Tuple[bool, Optional[float]]:
        base = self.baseline(user_id)
        if base is None or base == 0:
            return False, None
        ratio = length / base
        return ratio >= self._multiplier, ratio

    def user_stats(self, user_id: str) -> dict:
        history = self._histories.get(user_id, deque())
        if not history:
            return {"user_id": user_id, "samples": 0}
        lengths = list(history)
        return {
            "user_id": user_id,
            "samples": len(lengths),
            "mean_length": round(sum(lengths) / len(lengths), 1),
            "max_length": max(lengths),
            "min_length": min(lengths),
        }
```

## Solution 4: Prompt Length Abuse Detector

```python
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class LengthAbuseAssessment:
    user_id: Optional[str]
    text_length: int
    signals: List[LengthAbuseSignal]
    blocked: bool
    warning: bool

    def is_clean(self) -> bool:
        return not self.blocked and not self.warning

    def summary(self) -> str:
        if self.blocked:
            return f"BLOCKED: {'; '.join(s.description for s in self.signals if s.severity == 'block')}"
        if self.warning:
            return f"WARNING: {'; '.join(s.description for s in self.signals if s.severity == 'warning')}"
        return "CLEAN"


class PromptLengthAbuseDetector:
    """
    Combines absolute limits, structural analysis, and per-user baselines
    to detect prompt length abuse before the input reaches the LLM.
    """

    def __init__(
        self,
        hard_limit_chars: int = 100_000,
        soft_limit_chars: int = 40_000,
        max_repetition_ratio: float = 0.30,
        max_whitespace_ratio: float = 0.60,
        min_unique_line_ratio: float = 0.20,
        min_entropy: float = 2.0,
        analyzer: Optional[ContentStructureAnalyzer] = None,
        baseline_tracker: Optional[UserLengthBaselineTracker] = None,
    ) -> None:
        self._hard_limit = hard_limit_chars
        self._soft_limit = soft_limit_chars
        self._max_repetition = max_repetition_ratio
        self._max_whitespace = max_whitespace_ratio
        self._min_unique_lines = min_unique_line_ratio
        self._min_entropy = min_entropy
        self._analyzer = analyzer or ContentStructureAnalyzer()
        self._tracker = baseline_tracker

    def assess(self, text: str, user_id: Optional[str] = None) -> LengthAbuseAssessment:
        signals: List[LengthAbuseSignal] = []
        length = len(text)

        if length > self._hard_limit:
            signals.append(LengthAbuseSignal(
                LengthAbuseType.ABSOLUTE_OVERFLOW, length, self._hard_limit,
                f"Input length {length} exceeds hard limit {self._hard_limit}", "block"
            ))

        analysis = self._analyzer.analyze(text)

        if analysis["repetition_ratio"] > self._max_repetition:
            sev = "block" if analysis["repetition_ratio"] > 0.60 else "warning"
            signals.append(LengthAbuseSignal(
                LengthAbuseType.REPETITION_PADDING,
                analysis["repetition_ratio"], self._max_repetition,
                f"Repetition ratio {analysis['repetition_ratio']:.2f} exceeds {self._max_repetition}", sev
            ))

        if analysis["whitespace_ratio"] > self._max_whitespace:
            signals.append(LengthAbuseSignal(
                LengthAbuseType.WHITESPACE_INFLATION,
                analysis["whitespace_ratio"], self._max_whitespace,
                f"Whitespace ratio {analysis['whitespace_ratio']:.2f} exceeds {self._max_whitespace}", "warning"
            ))

        if analysis["unique_line_ratio"] < self._min_unique_lines and length > 1000:
            signals.append(LengthAbuseSignal(
                LengthAbuseType.STRUCTURED_FLOODING,
                analysis["unique_line_ratio"], self._min_unique_lines,
                f"Unique line ratio {analysis['unique_line_ratio']:.2f} below {self._min_unique_lines}", "warning"
            ))

        if analysis["shannon_entropy"] < self._min_entropy and length > 500:
            signals.append(LengthAbuseSignal(
                LengthAbuseType.REPETITION_PADDING,
                analysis["shannon_entropy"], self._min_entropy,
                f"Shannon entropy {analysis['shannon_entropy']:.2f} below {self._min_entropy} (highly repetitive)", "warning"
            ))

        if user_id and self._tracker:
            self._tracker.record(user_id, length)
            is_anomalous, ratio = self._tracker.is_anomalous(user_id, length)
            if is_anomalous and ratio:
                signals.append(LengthAbuseSignal(
                    LengthAbuseType.STATISTICAL_ANOMALY, ratio, 5.0,
                    f"Input is {ratio:.1f}× user's baseline length", "warning"
                ))
            elif length > self._soft_limit:
                signals.append(LengthAbuseSignal(
                    LengthAbuseType.ABSOLUTE_OVERFLOW, length, self._soft_limit,
                    f"Input length {length} exceeds soft limit {self._soft_limit}", "warning"
                ))

        blocked = any(s.severity == "block" for s in signals)
        warned = any(s.severity == "warning" for s in signals)

        return LengthAbuseAssessment(
            user_id=user_id,
            text_length=length,
            signals=signals,
            blocked=blocked,
            warning=warned,
        )
```

## Solution 5: Length Abuse Audit Logger

```python
import time
from collections import defaultdict
from typing import List


class LengthAbuseAuditLogger:
    """
    Records abuse assessments for security audit and repeat-offender detection.
    Identifies users with persistent abuse patterns.
    """

    def __init__(self, window_seconds: float = 3600.0) -> None:
        self._events: List[dict] = []
        self._window = window_seconds

    def record(self, assessment: LengthAbuseAssessment) -> None:
        if assessment.blocked or assessment.warning:
            self._events.append({
                "ts": time.time(),
                "user_id": assessment.user_id,
                "length": assessment.text_length,
                "blocked": assessment.blocked,
                "signal_types": [s.abuse_type.value for s in assessment.signals],
            })

    def _trim(self) -> None:
        cutoff = time.time() - self._window
        self._events = [e for e in self._events if e["ts"] >= cutoff]

    def summary(self) -> dict:
        self._trim()
        blocked = sum(1 for e in self._events if e["blocked"])
        by_user: dict = defaultdict(int)
        for e in self._events:
            if e["user_id"]:
                by_user[e["user_id"]] += 1
        repeat_offenders = {u: c for u, c in by_user.items() if c >= 3}

        return {
            "total_flagged": len(self._events),
            "blocked": blocked,
            "warned": len(self._events) - blocked,
            "repeat_offenders": dict(sorted(repeat_offenders.items(), key=lambda x: -x[1])),
        }
```

## Solution 6: Length Abuse Dashboard

```python
import time


class LengthAbuseDashboard:
    """
    Combines abuse detection stats, audit summary, and per-user
    baseline profiles into a security operational view.
    """

    def __init__(
        self,
        detector: PromptLengthAbuseDetector,
        audit_logger: LengthAbuseAuditLogger,
        baseline_tracker: UserLengthBaselineTracker,
    ) -> None:
        self._detector = detector
        self._audit = audit_logger
        self._tracker = baseline_tracker

    def render(self, sample_user_ids: List[str] = None) -> dict:
        audit = self._audit.summary()
        user_profiles = {}
        for uid in (sample_user_ids or []):
            user_profiles[uid] = self._tracker.user_stats(uid)

        return {
            "generated_at": time.time(),
            "abuse_summary": audit,
            "user_profiles": user_profiles,
        }
```

## Comparison

| Approach | Absolute Limits | Structural Analysis | Per-User Baseline | Audit Trail | Dashboard |
|---|---|---|---|---|---|
| ContentStructureAnalyzer | No | Yes (4 metrics) | No | No | No |
| UserLengthBaselineTracker | No | No | Yes | No | No |
| PromptLengthAbuseDetector | Yes (hard+soft) | Via analyzer | Via tracker | No | No |
| LengthAbuseAuditLogger | No | No | No | Yes | No |
| LengthAbuseDashboard | No | No | No | Via logger | Yes |

**Best for production**: Set `hard_limit_chars=100_000` (approximately 25K tokens) and `soft_limit_chars=40_000` for general chat agents — legitimate queries rarely exceed this. Enable per-user baselines after 5+ samples; before that, rely on absolute limits only to avoid penalizing new users. Run `ContentStructureAnalyzer` on all inputs above 2,000 characters — below that, structural padding attacks are too short to be effective. Treat `repetition_ratio > 0.60` as an immediate block: no legitimate document has 60% repeated content. Route `repeat_offenders` (3+ violations per hour) to a security review queue rather than silently blocking — coordinated abuse requires human investigation.
