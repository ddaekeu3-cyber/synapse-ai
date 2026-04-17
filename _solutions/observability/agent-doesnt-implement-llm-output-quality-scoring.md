---
title: "Agent Doesn't Implement LLM Output Quality Scoring"
description: "Agents that emit LLM responses without quality scoring have no automated signal for when output quality degrades — model updates, prompt regressions, or adversarial inputs that produce low-quality responses go undetected until users complain. Implement LLM output quality scoring that evaluates responses against heuristic and structural criteria, tracks score distributions over time, and alerts when quality drops below a baseline threshold."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-llm-output-quality-scoring
tags: [quality-scoring, output-evaluation, llm-evaluation, response-quality, automated-evals, quality-monitoring]
symptoms:
  - "Model update deployed without detecting that response coherence dropped significantly"
  - "No automated signal that the agent began producing truncated or incomplete responses"
  - "Quality regressions detected only via user complaints or manual review, not monitoring"
  - "Cannot compare response quality before and after a system prompt change"
  - "Refusal rate increased silently after a policy update — not detected for days"
---

## Why This Happens

LLM output quality is difficult to measure precisely, but a set of heuristic and structural signals provides a useful proxy. Response length distribution, coherence indicators (sentence completion, grammar), task completion signals (answer present, structured output valid), and refusal rate all correlate with quality. Without automated scoring of every response, quality regressions are invisible until they manifest as user-reported errors. Continuous quality scoring creates a time series that can be compared before and after deployments — a sudden score drop is a reliable signal that something changed in the model, prompt, or input distribution.

## Solution 1: Quality Score Dimensions

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


class QualityDimension(str, Enum):
    LENGTH_ADEQUACY = "length_adequacy"         # not too short, not too long
    STRUCTURAL_COMPLETENESS = "structural"       # sentences end, brackets close
    TASK_COMPLETION = "task_completion"          # appears to answer the question
    COHERENCE = "coherence"                      # not repetitive, varied vocabulary
    REFUSAL_ABSENCE = "refusal_absence"          # did not refuse without reason
    FORMAT_COMPLIANCE = "format_compliance"      # matches expected output format


@dataclass
class QualityScore:
    response_id: str
    session_id: str
    overall_score: float                  # 0.0–1.0
    dimension_scores: Dict[str, float]    # QualityDimension.value -> score
    flags: List[str] = field(default_factory=list)
    scored_at: float = field(default_factory=__import__("time").time)
    response_length: int = 0
    model_id: str = ""
```

## Solution 2: Heuristic Quality Scorer

```python
import re
from typing import Dict, List, Optional


class HeuristicQualityScorer:
    """
    Scores LLM responses across multiple quality dimensions using
    heuristic signals. No external model call required.
    """

    MIN_LENGTH = 20          # chars: below this is almost certainly incomplete
    MAX_LENGTH = 8000        # chars: above this may indicate runaway generation
    IDEAL_MIN = 50
    IDEAL_MAX = 3000

    REFUSAL_PATTERNS = re.compile(
        r"\b(cannot|can't|unable to|I must decline|not able to|I won't|I don't)\b",
        re.IGNORECASE,
    )
    REPETITION_WINDOW = 50   # check for repeated n-grams in this window size

    def score(
        self,
        response: str,
        expected_format: Optional[str] = None,   # "json" | "markdown" | None
        question: Optional[str] = None,
    ) -> Dict[str, float]:
        scores = {}

        scores[QualityDimension.LENGTH_ADEQUACY.value] = self._score_length(response)
        scores[QualityDimension.STRUCTURAL_COMPLETENESS.value] = self._score_structure(response)
        scores[QualityDimension.COHERENCE.value] = self._score_coherence(response)
        scores[QualityDimension.REFUSAL_ABSENCE.value] = self._score_refusal(response)

        if expected_format:
            scores[QualityDimension.FORMAT_COMPLIANCE.value] = self._score_format(
                response, expected_format
            )

        return scores

    def _score_length(self, text: str) -> float:
        n = len(text)
        if n < self.MIN_LENGTH:
            return 0.1
        if n < self.IDEAL_MIN:
            return 0.5
        if n <= self.IDEAL_MAX:
            return 1.0
        if n <= self.MAX_LENGTH:
            return 0.8
        return 0.5   # excessively long

    def _score_structure(self, text: str) -> float:
        score = 1.0
        # Check for balanced brackets
        for open_c, close_c in [("{", "}"), ("[", "]"), ("(", ")")]:
            if text.count(open_c) != text.count(close_c):
                score -= 0.15
        # Check for incomplete last sentence
        stripped = text.rstrip()
        if stripped and stripped[-1] not in ".!?\"'`":
            score -= 0.1
        # Check for unclosed markdown code blocks
        if text.count("```") % 2 != 0:
            score -= 0.2
        return max(0.0, min(1.0, score))

    def _score_coherence(self, text: str) -> float:
        words = text.lower().split()
        if len(words) < 10:
            return 0.5
        # Detect severe repetition: same 3-gram repeated > 3 times
        trigrams = [" ".join(words[i:i+3]) for i in range(len(words) - 2)]
        from collections import Counter
        counts = Counter(trigrams)
        max_repeat = counts.most_common(1)[0][1] if counts else 0
        if max_repeat > 5:
            return 0.3
        if max_repeat > 3:
            return 0.7
        return 1.0

    def _score_refusal(self, text: str) -> float:
        if self.REFUSAL_PATTERNS.search(text):
            return 0.4  # refusal isn't always wrong, but it's a quality signal
        return 1.0

    def _score_format(self, text: str, expected_format: str) -> float:
        import json
        if expected_format == "json":
            try:
                json.loads(text.strip())
                return 1.0
            except (json.JSONDecodeError, ValueError):
                # Check if JSON is embedded
                m = re.search(r"\{.*\}", text, re.DOTALL)
                if m:
                    try:
                        json.loads(m.group())
                        return 0.7
                    except (json.JSONDecodeError, ValueError):
                        pass
                return 0.2
        if expected_format == "markdown":
            has_headers = bool(re.search(r"^#{1,3}\s", text, re.MULTILINE))
            has_lists = bool(re.search(r"^[-*]\s", text, re.MULTILINE))
            return 0.8 if (has_headers or has_lists) else 0.5
        return 1.0

    def compute_overall(self, dimension_scores: Dict[str, float]) -> float:
        if not dimension_scores:
            return 0.0
        return round(sum(dimension_scores.values()) / len(dimension_scores), 3)

    def get_flags(self, dimension_scores: Dict[str, float]) -> List[str]:
        flags = []
        if dimension_scores.get(QualityDimension.LENGTH_ADEQUACY.value, 1.0) < 0.5:
            flags.append("length_too_short")
        if dimension_scores.get(QualityDimension.STRUCTURAL_COMPLETENESS.value, 1.0) < 0.7:
            flags.append("structural_issues")
        if dimension_scores.get(QualityDimension.COHERENCE.value, 1.0) < 0.5:
            flags.append("repetition_detected")
        if dimension_scores.get(QualityDimension.REFUSAL_ABSENCE.value, 1.0) < 0.5:
            flags.append("refusal_detected")
        if dimension_scores.get(QualityDimension.FORMAT_COMPLIANCE.value, 1.0) < 0.5:
            flags.append("format_non_compliant")
        return flags
```

## Solution 3: Quality Score Store

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, List, Optional, Tuple


class QualityScoreStore:
    """
    Accumulates quality scores over a sliding window.
    Supports mean, percentile, and flag-rate queries.
    """

    def __init__(
        self,
        max_samples: int = 10000,
        window_seconds: float = 3600.0,
    ):
        self._max = max_samples
        self._window = window_seconds
        self._samples: Deque[Tuple[float, QualityScore]] = deque()
        self._lock = Lock()

    def record(self, score: QualityScore) -> None:
        with self._lock:
            self._samples.append((time.time(), score))
            if len(self._samples) > self._max:
                self._samples.popleft()

    def recent_scores(self, window_seconds: Optional[float] = None) -> List[QualityScore]:
        window = window_seconds or self._window
        cutoff = time.time() - window
        with self._lock:
            return [s for ts, s in self._samples if ts >= cutoff]

    def mean_score(self, window_seconds: Optional[float] = None) -> Optional[float]:
        scores = self.recent_scores(window_seconds)
        if not scores:
            return None
        return round(sum(s.overall_score for s in scores) / len(scores), 4)

    def flag_rate(self, flag: str, window_seconds: Optional[float] = None) -> float:
        scores = self.recent_scores(window_seconds)
        if not scores:
            return 0.0
        return round(sum(1 for s in scores if flag in s.flags) / len(scores), 4)

    def summary(self, window_seconds: Optional[float] = None) -> dict:
        scores = self.recent_scores(window_seconds)
        if not scores:
            return {"samples": 0}
        overall = [s.overall_score for s in scores]
        all_flags = [f for s in scores for f in s.flags]
        from collections import Counter
        flag_counts = Counter(all_flags)
        return {
            "samples": len(scores),
            "mean_score": round(sum(overall) / len(overall), 4),
            "min_score": round(min(overall), 4),
            "p10_score": round(sorted(overall)[len(overall) // 10], 4),
            "flag_rates": {
                flag: round(count / len(scores), 4)
                for flag, count in flag_counts.most_common()
            },
        }
```

## Solution 4: Quality Alert Manager

```python
import time
from typing import Callable, Optional


class QualityScoreAlertManager:
    """
    Fires alerts when mean quality score drops below a threshold
    or when a specific flag rate spikes above a threshold.
    """

    def __init__(
        self,
        store: QualityScoreStore,
        min_mean_score: float = 0.65,
        max_refusal_rate: float = 0.20,
        cooldown_seconds: float = 300.0,
        alert_fn: Optional[Callable[[dict], None]] = None,
    ):
        self._store = store
        self._min_mean = min_mean_score
        self._max_refusal = max_refusal_rate
        self._cooldown = cooldown_seconds
        self._alert = alert_fn or self._default_alert
        self._last_alerted: dict = {}

    @staticmethod
    def _default_alert(event: dict) -> None:
        import json
        print(json.dumps({"event": "quality_alert", **event}))

    def check(self) -> list:
        now = time.time()
        alerts = []
        summary = self._store.summary(window_seconds=600.0)  # 10-min window

        if summary.get("samples", 0) < 10:
            return []

        mean = summary.get("mean_score", 1.0)
        if mean < self._min_mean:
            key = "low_mean_score"
            if now - self._last_alerted.get(key, 0) >= self._cooldown:
                self._last_alerted[key] = now
                alerts.append({
                    "type": key,
                    "mean_score": mean,
                    "threshold": self._min_mean,
                    "ts": now,
                })
                self._alert(alerts[-1])

        refusal_rate = summary.get("flag_rates", {}).get("refusal_detected", 0.0)
        if refusal_rate > self._max_refusal:
            key = "high_refusal_rate"
            if now - self._last_alerted.get(key, 0) >= self._cooldown:
                self._last_alerted[key] = now
                alerts.append({
                    "type": key,
                    "refusal_rate": refusal_rate,
                    "threshold": self._max_refusal,
                    "ts": now,
                })
                self._alert(alerts[-1])

        return alerts
```

## Solution 5: Scoring Interceptor

```python
import time
import uuid
from typing import Any, Callable, Optional


class QualityScoringInterceptor:
    """
    Wraps LLM calls to automatically score every response.
    """

    def __init__(
        self,
        scorer: HeuristicQualityScorer,
        store: QualityScoreStore,
        alert_manager: Optional[QualityScoreAlertManager] = None,
        score_every_n: int = 1,
    ):
        self._scorer = scorer
        self._store = store
        self._alerts = alert_manager
        self._every_n = score_every_n
        self._call_count = 0

    async def score_response(
        self,
        response_text: str,
        session_id: str = "",
        model_id: str = "",
        expected_format: Optional[str] = None,
    ) -> QualityScore:
        self._call_count += 1
        if self._call_count % self._every_n != 0:
            return None

        dim_scores = self._scorer.score(response_text, expected_format)
        overall = self._scorer.compute_overall(dim_scores)
        flags = self._scorer.get_flags(dim_scores)

        score = QualityScore(
            response_id=str(uuid.uuid4())[:8],
            session_id=session_id,
            overall_score=overall,
            dimension_scores=dim_scores,
            flags=flags,
            response_length=len(response_text),
            model_id=model_id,
        )
        self._store.record(score)

        if self._alerts:
            self._alerts.check()

        return score
```

## Solution 6: Quality Score Dashboard

```python
import time


class LLMOutputQualityDashboard:
    """
    Combines quality score summaries, flag rates, and alert status
    into a single operational view.
    """

    def __init__(
        self,
        store: QualityScoreStore,
        alert_manager: QualityScoreAlertManager,
    ):
        self._store = store
        self._alerts = alert_manager

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "last_10min": self._store.summary(600.0),
            "last_1h": self._store.summary(3600.0),
            "active_alerts": self._alerts.check(),
        }
```

## Comparison

| Approach | Heuristic Scoring | Score Persistence | Alert on Drop | Per-Call Integration | Dashboard |
|---|---|---|---|---|---|
| HeuristicQualityScorer | Yes (5 dimensions) | No | No | No | No |
| QualityScoreStore | No | Yes (sliding window) | No | No | No |
| QualityScoreAlertManager | No | No | Yes (mean + refusal) | No | No |
| QualityScoringInterceptor | Via scorer | Via store | Via alerts | Yes | No |
| LLMOutputQualityDashboard | No | No | No | No | Yes |

**Best for production**: Score every response by default (`score_every_n=1`) — the heuristic scorer is CPU-only and takes under 1ms. Set `min_mean_score=0.65` as the alert threshold — below this, quality degradation is noticeable to users. Track `refusal_detected` flag rate separately from overall score: a sudden spike in refusals often indicates a system prompt change or an adversarial input campaign rather than a model quality regression. Compare `last_10min.mean_score` against `last_1h.mean_score` in the dashboard — a 10-minute score that is 0.1 below the hourly baseline is a reliable early signal of a regression introduced by a recent deployment.
