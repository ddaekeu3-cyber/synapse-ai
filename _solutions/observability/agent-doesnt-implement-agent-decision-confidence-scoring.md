---
title: "Agent Doesn't Implement Agent Decision Confidence Scoring"
description: "Agents that return responses without any confidence signal force downstream systems and users to treat all outputs as equally reliable. A response based on high-quality retrieved evidence and a response based on a hallucinated answer look identical in the output stream. Implement agent decision confidence scoring that aggregates tool result quality, reasoning chain depth, source agreement, and hedging language detection into a per-response confidence score that downstream systems can act on."
date: 2026-04-16
difficulty: advanced
category: observability
slug: agent-doesnt-implement-agent-decision-confidence-scoring
tags: [confidence-scoring, decision-quality, output-reliability, uncertainty-quantification, evidence-quality, hallucination-risk]
symptoms:
  - "Downstream automation treats all agent responses as equally reliable regardless of evidence quality"
  - "No signal distinguishing responses with strong source evidence from speculative responses"
  - "Hedging language in responses ('I think', 'possibly') ignored by consumers"
  - "Agentic pipelines escalate low-confidence decisions at the same rate as high-confidence ones"
  - "No per-response quality signal available for SLO tracking or user trust calibration"
---

## Why This Happens

LLMs do not produce calibrated confidence scores natively. Confidence must be inferred from observable proxies: how many tools were called, how many sources agreed, whether the model used hedging language, how many reasoning steps were completed, and whether the response contradicts earlier steps. Aggregating these signals into a composite score requires instrumenting the reasoning chain throughout execution and scoring the assembled evidence at response generation time.

## Solution 1: Confidence Signal Collector

```python
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class ConfidenceSignalType(str, Enum):
    TOOL_RESULT_QUALITY = "tool_result_quality"       # score from tool validation
    SOURCE_AGREEMENT = "source_agreement"             # multiple sources agree
    SOURCE_DISAGREEMENT = "source_disagreement"       # sources conflict
    HEDGING_LANGUAGE = "hedging_language"             # model expressed uncertainty
    ASSERTION_LANGUAGE = "assertion_language"         # model expressed certainty
    NO_TOOL_EVIDENCE = "no_tool_evidence"             # claim made without tool call
    REASONING_STEPS = "reasoning_steps"               # explicit reasoning chain present
    CONTRADICTION = "contradiction"                   # internal contradiction detected


@dataclass
class ConfidenceSignal:
    signal_type: ConfidenceSignalType
    value: float       # 0.0–1.0 contribution to confidence
    weight: float      # how much this signal matters
    source: str = ""   # e.g., tool name, turn index


_HEDGING_PATTERNS = re.compile(
    r"\b(i think|i believe|possibly|probably|might|may|could be|not sure|"
    r"uncertain|i'm not certain|approximately|roughly|seems like|appears to)\b",
    re.IGNORECASE,
)
_ASSERTION_PATTERNS = re.compile(
    r"\b(definitely|certainly|absolutely|confirmed|verified|according to|"
    r"the data shows|the result is|i can confirm)\b",
    re.IGNORECASE,
)


class ResponseLanguageSignalExtractor:
    """
    Extracts hedging and assertion signals from the final response text.
    """

    def extract(self, response_text: str) -> List[ConfidenceSignal]:
        signals = []
        hedging_count = len(_HEDGING_PATTERNS.findall(response_text))
        assertion_count = len(_ASSERTION_PATTERNS.findall(response_text))

        if hedging_count > 0:
            signals.append(ConfidenceSignal(
                signal_type=ConfidenceSignalType.HEDGING_LANGUAGE,
                value=max(0.0, 1.0 - hedging_count * 0.15),
                weight=0.20,
                source=f"{hedging_count} hedging phrase(s)",
            ))
        if assertion_count > 0:
            signals.append(ConfidenceSignal(
                signal_type=ConfidenceSignalType.ASSERTION_LANGUAGE,
                value=min(1.0, 0.5 + assertion_count * 0.10),
                weight=0.15,
                source=f"{assertion_count} assertion phrase(s)",
            ))
        return signals
```

## Solution 2: Tool Evidence Scorer

```python
from typing import Any, List, Optional


@dataclass
class ToolResultEvidence:
    tool_name: str
    result_quality: float    # 0.0–1.0: caller-provided quality signal
    result_chars: int
    succeeded: bool


class ToolEvidenceScorer:
    """
    Scores the quality of tool evidence assembled during a request.
    Penalizes missing evidence, failed tools, and very short results.
    """

    def score(self, evidence: List[ToolResultEvidence]) -> List[ConfidenceSignal]:
        signals = []

        if not evidence:
            signals.append(ConfidenceSignal(
                signal_type=ConfidenceSignalType.NO_TOOL_EVIDENCE,
                value=0.30,
                weight=0.40,
                source="no tool calls made",
            ))
            return signals

        succeeded = [e for e in evidence if e.succeeded]
        failed = [e for e in evidence if not e.succeeded]
        avg_quality = sum(e.result_quality for e in succeeded) / max(len(succeeded), 1)

        signals.append(ConfidenceSignal(
            signal_type=ConfidenceSignalType.TOOL_RESULT_QUALITY,
            value=round(avg_quality, 4),
            weight=0.35,
            source=f"{len(succeeded)} successful tool(s)",
        ))

        if failed:
            penalty = min(0.30, len(failed) * 0.10)
            signals.append(ConfidenceSignal(
                signal_type=ConfidenceSignalType.TOOL_RESULT_QUALITY,
                value=max(0.0, 1.0 - penalty),
                weight=0.10,
                source=f"{len(failed)} failed tool(s)",
            ))

        return signals
```

## Solution 3: Source Agreement Detector

```python
import re
from typing import List


class SourceAgreementDetector:
    """
    Checks whether multiple tool results corroborate the same key claim.
    Uses simple keyword overlap as a proxy for factual agreement.
    """

    def __init__(self, agreement_threshold: float = 0.30):
        self._threshold = agreement_threshold

    def _keywords(self, text: str) -> set:
        return set(re.findall(r"\b[a-zA-Z]{4,}\b", text.lower()))

    def _jaccard(self, a: set, b: set) -> float:
        if not a or not b:
            return 0.0
        return len(a & b) / len(a | b)

    def detect(self, result_texts: List[str]) -> List[ConfidenceSignal]:
        signals = []
        if len(result_texts) < 2:
            return signals

        kw_sets = [self._keywords(t) for t in result_texts]
        agreements = 0
        disagreements = 0
        pairs = 0

        for i in range(len(kw_sets)):
            for j in range(i + 1, len(kw_sets)):
                sim = self._jaccard(kw_sets[i], kw_sets[j])
                pairs += 1
                if sim >= self._threshold:
                    agreements += 1
                elif sim < 0.05:
                    disagreements += 1

        if agreements > 0:
            signals.append(ConfidenceSignal(
                signal_type=ConfidenceSignalType.SOURCE_AGREEMENT,
                value=min(1.0, 0.7 + agreements / max(pairs, 1) * 0.3),
                weight=0.25,
                source=f"{agreements}/{pairs} source pairs agree",
            ))
        if disagreements > 0:
            signals.append(ConfidenceSignal(
                signal_type=ConfidenceSignalType.SOURCE_DISAGREEMENT,
                value=max(0.2, 1.0 - disagreements / max(pairs, 1) * 0.5),
                weight=0.20,
                source=f"{disagreements}/{pairs} source pairs disagree",
            ))
        return signals
```

## Solution 4: Confidence Score Aggregator

```python
from dataclasses import dataclass
from typing import List


@dataclass
class ConfidenceScore:
    score: float            # 0.0–1.0 composite
    label: str              # "high" | "medium" | "low" | "very_low"
    signals: List[ConfidenceSignal]
    contributing_weight: float

    @staticmethod
    def label_for(score: float) -> str:
        if score >= 0.75:
            return "high"
        if score >= 0.55:
            return "medium"
        if score >= 0.35:
            return "low"
        return "very_low"


class ConfidenceScoreAggregator:
    """
    Computes a weighted composite confidence score from all signals.
    """

    def aggregate(self, signals: List[ConfidenceSignal]) -> ConfidenceScore:
        if not signals:
            return ConfidenceScore(score=0.5, label="medium", signals=[], contributing_weight=0.0)

        total_weight = sum(s.weight for s in signals)
        if total_weight == 0:
            return ConfidenceScore(score=0.5, label="medium", signals=signals, contributing_weight=0.0)

        weighted_sum = sum(s.value * s.weight for s in signals)
        composite = round(weighted_sum / total_weight, 4)
        composite = max(0.0, min(1.0, composite))

        return ConfidenceScore(
            score=composite,
            label=ConfidenceScore.label_for(composite),
            signals=signals,
            contributing_weight=total_weight,
        )
```

## Solution 5: Confidence-Aware Response Wrapper

```python
from typing import Any, List, Optional


class ConfidenceAwareResponseWrapper:
    """
    Attaches a confidence score to every agent response.
    Allows downstream systems to gate on confidence label.
    """

    def __init__(
        self,
        language_extractor: ResponseLanguageSignalExtractor,
        evidence_scorer: ToolEvidenceScorer,
        agreement_detector: SourceAgreementDetector,
        aggregator: ConfidenceScoreAggregator,
    ):
        self._language = language_extractor
        self._evidence = evidence_scorer
        self._agreement = agreement_detector
        self._aggregator = aggregator

    def score_response(
        self,
        response_text: str,
        tool_evidence: List[ToolResultEvidence],
        source_texts: Optional[List[str]] = None,
    ) -> ConfidenceScore:
        signals = []
        signals.extend(self._language.extract(response_text))
        signals.extend(self._evidence.score(tool_evidence))
        if source_texts:
            signals.extend(self._agreement.detect(source_texts))
        return self._aggregator.aggregate(signals)
```

## Solution 6: Confidence Score Fleet Monitor

```python
import time
from collections import deque
from threading import Lock
from typing import Deque


class ConfidenceScoreFleetMonitor:
    """
    Tracks confidence score distributions across all responses
    and alerts when the proportion of low-confidence responses rises.
    """

    def __init__(self, max_records: int = 50_000):
        self._records: Deque[dict] = deque(maxlen=max_records)
        self._lock = Lock()

    def record(self, score: ConfidenceScore) -> None:
        with self._lock:
            self._records.append({"ts": time.time(), "score": score.score, "label": score.label})

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [r for r in self._records if r["ts"] >= cutoff]
        if not recent:
            return {"responses": 0}

        label_counts: dict = {}
        for r in recent:
            label_counts[r["label"]] = label_counts.get(r["label"], 0) + 1

        scores = sorted(r["score"] for r in recent)
        low_pct = (label_counts.get("low", 0) + label_counts.get("very_low", 0)) / len(recent)

        return {
            "responses": len(recent),
            "score_p50": scores[len(scores) // 2],
            "score_p25": scores[len(scores) // 4],
            "label_distribution": label_counts,
            "low_confidence_rate": round(low_pct, 4),
            "alert": low_pct > 0.30,
        }
```

## Comparison

| Approach | Language Signals | Tool Evidence | Source Agreement | Composite Score | Fleet Monitoring |
|---|---|---|---|---|---|
| ResponseLanguageSignalExtractor | Yes (hedging/assertion) | No | No | No | No |
| ToolEvidenceScorer | No | Yes | No | No | No |
| SourceAgreementDetector | No | No | Yes (Jaccard) | No | No |
| ConfidenceScoreAggregator | Via signals | Via signals | Via signals | Yes | No |
| ConfidenceAwareResponseWrapper | Via extractor | Via scorer | Via detector | Via aggregator | No |
| ConfidenceScoreFleetMonitor | No | No | No | No | Yes |

**Best for production**: Use `label` rather than `score` for downstream gating decisions — "low" and "very_low" responses should trigger human review or a disclaimer before automation acts on them. Set `weight=0.40` for tool evidence (highest) and `weight=0.15` for language signals (lowest) — tool evidence is objective, language signals are heuristic. Alert via `ConfidenceScoreFleetMonitor` when `low_confidence_rate > 0.30` — this indicates either a retrieval degradation (tools returning poor results) or a prompt regression (model expressing more uncertainty). Expose `confidence_label` as a response header in your API so downstream consumers can route low-confidence responses appropriately.
