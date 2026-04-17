---
title: "Agent Doesn't Implement LLM Hallucination Rate Tracking"
description: "Agents that do not measure hallucination rates have no signal for when a model change, prompt change, or context shift has increased the frequency of factually incorrect outputs: a model update that doubles the hallucination rate goes undetected until users report errors. Implement hallucination rate tracking using automated fact-checking heuristics, citation verification, and user feedback correlation to surface degradation before it becomes a user-facing incident."
date: 2026-04-16
difficulty: advanced
category: observability
slug: agent-doesnt-implement-llm-hallucination-rate-tracking
tags: [hallucination-detection, output-quality, fact-checking, citation-verification, model-evaluation, quality-regression]
symptoms:
  - "No measurement of how often agent outputs contain factually incorrect information"
  - "Model or prompt changes are deployed without verifying hallucination rate regression"
  - "User feedback about incorrect answers is not correlated with specific output patterns"
  - "No baseline hallucination rate to compare against after deployments"
  - "Hallucination detection is entirely manual — reviewers spot-check randomly"
---

## Why This Happens

Hallucination is hard to detect automatically because it requires knowing what is true — which the agent may not have access to. However, several tractable proxies exist: citation verification (did the agent claim a document says X when it does not?), consistency checks (does the agent give the same answer to the same question twice?), confidence calibration (does the agent express high certainty for answers that are frequently corrected?), and user feedback correlation (do corrected responses share patterns?). Measuring these proxies over time enables detection of hallucination rate regressions without requiring ground truth for every output.

## Solution 1: Hallucination Signal Types

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class HallucinationSignalType(str, Enum):
    CITATION_MISMATCH = "citation_mismatch"      # output claims source says X but source says Y
    CONSISTENCY_FAILURE = "consistency_failure"   # same question, different answers
    USER_CORRECTION = "user_correction"           # user explicitly corrected the agent
    CONFIDENCE_MISCALIBRATION = "confidence_miscalibration"  # high confidence + wrong
    ENTITY_FABRICATION = "entity_fabrication"    # named entity not in source documents
    FACTUAL_CONTRADICTION = "factual_contradiction"  # contradicts known facts


class HallucinationSeverity(str, Enum):
    LOW = "low"        # minor inaccuracy, does not mislead
    MEDIUM = "medium"  # factual error, potentially misleading
    HIGH = "high"      # confident false claim, likely to mislead


@dataclass
class HallucinationSignal:
    signal_type: HallucinationSignalType
    severity: HallucinationSeverity
    conversation_id: str
    turn_number: int
    output_excerpt: str       # first 200 chars of the offending output
    evidence: str             # why this is flagged
    tool_name: str = ""       # which tool provided the grounding (if any)
    model_id: str = ""
    confidence_score: float = 0.0   # agent's expressed confidence (if extractable)
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=lambda: __import__("time").time())
```

## Solution 2: Citation Verifier

```python
import re
from typing import List, Optional, Tuple


class CitationVerifier:
    """
    Verifies that claims attributed to source documents are actually
    present in those documents. Detects fabricated citations.
    """

    def __init__(self, min_match_length: int = 30):
        self._min_match = min_match_length

    def _normalize(self, text: str) -> str:
        return re.sub(r"\s+", " ", text.lower().strip())

    def verify_citation(
        self,
        agent_claim: str,
        source_documents: List[str],
    ) -> Tuple[bool, Optional[str]]:
        """
        Returns (verified, supporting_document_excerpt).
        verified=False means the claim was not found in any source.
        """
        norm_claim = self._normalize(agent_claim)
        if len(norm_claim) < self._min_match:
            return True, None  # too short to verify

        # Check for substantial overlap with any source
        for doc in source_documents:
            norm_doc = self._normalize(doc)
            # Sliding window check
            words = norm_claim.split()
            for size in range(min(10, len(words)), max(3, len(words) // 2), -1):
                window = " ".join(words[:size])
                if window in norm_doc:
                    return True, doc[:100]

        return False, None

    def extract_claims(self, output: str) -> List[str]:
        """Extract sentences that make verifiable factual claims."""
        sentences = re.split(r"(?<=[.!?])\s+", output)
        # Heuristic: sentences with specific numbers, names, or dates
        claim_pattern = re.compile(
            r"\b(\d{4}|\d+%|[A-Z][a-z]+ [A-Z][a-z]+|\$\d+)\b"
        )
        return [s for s in sentences if claim_pattern.search(s) and len(s) > 20]
```

## Solution 3: Consistency Checker

```python
import math
from typing import Dict, List, Optional, Tuple


class ConsistencyChecker:
    """
    Detects hallucination by comparing responses to the same or similar
    queries across conversations. Inconsistent answers suggest fabrication.
    """

    def __init__(self, similarity_threshold: float = 0.85):
        self._threshold = similarity_threshold
        self._response_cache: Dict[str, List[str]] = {}  # query_hash -> responses

    @staticmethod
    def _shingles(text: str, k: int = 5) -> set:
        import re
        norm = re.sub(r"\s+", " ", text.lower().strip())
        return {norm[i:i+k] for i in range(len(norm) - k + 1)}

    def _jaccard(self, a: str, b: str) -> float:
        sa, sb = self._shingles(a), self._shingles(b)
        if not sa and not sb:
            return 1.0
        return len(sa & sb) / len(sa | sb)

    def record_response(self, query: str, response: str) -> Optional[float]:
        """
        Records a response and returns minimum similarity to prior responses.
        Low similarity to prior responses for same query signals inconsistency.
        """
        import hashlib
        query_hash = hashlib.sha256(query.encode()).hexdigest()[:16]
        prior = self._response_cache.get(query_hash, [])

        min_similarity = 1.0
        for prev_response in prior[-5:]:  # compare against last 5
            sim = self._jaccard(response, prev_response)
            min_similarity = min(min_similarity, sim)

        prior.append(response)
        self._response_cache[query_hash] = prior[-10:]  # keep last 10
        return min_similarity if prior else None

    def is_inconsistent(self, similarity: Optional[float]) -> bool:
        if similarity is None:
            return False
        return similarity < 1.0 - self._threshold
```

## Solution 4: Hallucination Rate Tracker

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, Dict, List, Optional, Tuple


class HallucinationRateTracker:
    """
    Accumulates hallucination signals and computes rates over sliding windows.
    Provides regression detection by comparing current rate to a baseline.
    """

    def __init__(self, window_seconds: int = 86400, max_signals: int = 100_000):
        self._window = window_seconds
        self._max = max_signals
        self._signals: Deque[HallucinationSignal] = deque()
        self._total_outputs = 0
        self._baseline_rate: Optional[float] = None
        self._lock = Lock()

    def record_output(self) -> None:
        with self._lock:
            self._total_outputs += 1

    def record_signal(self, signal: HallucinationSignal) -> None:
        with self._lock:
            self._signals.append(signal)
            if len(self._signals) > self._max:
                self._signals.popleft()

    def _recent_signals(self, sub_window: Optional[int] = None) -> List[HallucinationSignal]:
        cutoff = time.time() - (sub_window or self._window)
        with self._lock:
            return [s for s in self._signals if s.timestamp >= cutoff]

    def hallucination_rate(self, sub_window_seconds: Optional[int] = None) -> float:
        """Signals per total outputs in the window (proxy for hallucination rate)."""
        signals = self._recent_signals(sub_window_seconds)
        if self._total_outputs == 0:
            return 0.0
        return len(signals) / self._total_outputs

    def set_baseline(self) -> float:
        rate = self.hallucination_rate()
        self._baseline_rate = rate
        return rate

    def regression_detected(self, threshold_multiplier: float = 2.0) -> bool:
        if self._baseline_rate is None:
            return False
        current = self.hallucination_rate()
        return current > self._baseline_rate * threshold_multiplier

    def by_type(self, sub_window_seconds: Optional[int] = None) -> Dict[str, int]:
        signals = self._recent_signals(sub_window_seconds)
        result: dict = {}
        for s in signals:
            result[s.signal_type.value] = result.get(s.signal_type.value, 0) + 1
        return result

    def by_severity(self, sub_window_seconds: Optional[int] = None) -> Dict[str, int]:
        signals = self._recent_signals(sub_window_seconds)
        result: dict = {}
        for s in signals:
            result[s.severity.value] = result.get(s.severity.value, 0) + 1
        return result

    def summary(self, sub_window_seconds: Optional[int] = None) -> dict:
        signals = self._recent_signals(sub_window_seconds)
        return {
            "window_seconds": sub_window_seconds or self._window,
            "signal_count": len(signals),
            "total_outputs": self._total_outputs,
            "hallucination_rate": round(self.hallucination_rate(sub_window_seconds), 6),
            "baseline_rate": self._baseline_rate,
            "regression_detected": self.regression_detected(),
            "by_type": self.by_type(sub_window_seconds),
            "by_severity": self.by_severity(sub_window_seconds),
        }
```

## Solution 5: User Correction Correlator

```python
import time
from typing import List


class UserCorrectionCorrelator:
    """
    Correlates user corrections (explicit "that's wrong" or "actually...")
    with the outputs that preceded them to identify high-hallucination patterns.
    """

    CORRECTION_PATTERNS = [
        "that's wrong", "that is wrong", "actually,", "no, that's",
        "incorrect", "you made an error", "that's not right",
        "the correct answer", "you're mistaken",
    ]

    def __init__(self, tracker: HallucinationRateTracker):
        self._tracker = tracker

    def check_user_message(
        self,
        user_message: str,
        previous_output: str,
        conversation_id: str,
        turn_number: int,
        model_id: str = "",
    ) -> bool:
        """Returns True if user message appears to be a correction."""
        lower = user_message.lower().strip()
        for pattern in self.CORRECTION_PATTERNS:
            if lower.startswith(pattern) or f" {pattern}" in lower:
                signal = HallucinationSignal(
                    signal_type=HallucinationSignalType.USER_CORRECTION,
                    severity=HallucinationSeverity.MEDIUM,
                    conversation_id=conversation_id,
                    turn_number=turn_number,
                    output_excerpt=previous_output[:200],
                    evidence=f"user message starts with correction pattern: '{pattern}'",
                    model_id=model_id,
                )
                self._tracker.record_signal(signal)
                return True
        return False
```

## Solution 6: Hallucination Rate Dashboard

```python
import time


class HallucinationRateDashboard:
    """
    Combines hallucination rate tracking, regression detection, and
    citation verification results into a model quality health view.
    """

    def __init__(
        self,
        tracker: HallucinationRateTracker,
        regression_threshold: float = 2.0,
    ):
        self._tracker = tracker
        self._regression_threshold = regression_threshold

    def render(self) -> dict:
        summary_1h = self._tracker.summary(sub_window_seconds=3600)
        summary_24h = self._tracker.summary(sub_window_seconds=86400)
        regression = self._tracker.regression_detected(self._regression_threshold)

        return {
            "generated_at": time.time(),
            "last_1h": summary_1h,
            "last_24h": summary_24h,
            "baseline_rate": self._tracker._baseline_rate,
            "regression_detected": regression,
            "alert": regression or summary_1h.get("signal_count", 0) > 50,
        }
```

## Comparison

| Approach | Citation Check | Consistency Check | User Feedback | Rate Tracking | Regression Detection |
|---|---|---|---|---|---|
| CitationVerifier | Yes (overlap) | No | No | No | No |
| ConsistencyChecker | No | Yes (Jaccard) | No | No | No |
| HallucinationRateTracker | No | No | No | Yes | Yes |
| UserCorrectionCorrelator | No | No | Yes (pattern) | Via tracker | No |
| HallucinationRateDashboard | No | No | No | Via tracker | Yes |

**Best for production**: Call `set_baseline()` immediately after each model or prompt deployment — this captures the post-deployment baseline so `regression_detected()` compares current performance to the new baseline, not the pre-deployment one. `UserCorrectionCorrelator` provides the highest-signal hallucination proxy because it is grounded in actual user behavior — prioritize it over automated heuristics. Alert on `regression_detected()` with `threshold_multiplier=2.0` (rate doubled): a single hallucination spike does not necessarily indicate a regression, but a sustained 2× increase almost always does. Track `by_type` over time: a sudden spike in `citation_mismatch` signals that retrieved documents have changed format in a way that breaks citation grounding.
