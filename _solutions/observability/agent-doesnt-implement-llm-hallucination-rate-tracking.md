---
title: "Agent Doesn't Implement LLM Hallucination Rate Tracking"
description: "Agents that don't measure hallucination rates have no visibility into factual reliability degradation, model drift toward confabulation, or which prompt patterns produce unverifiable claims."
difficulty: advanced
category: observability
tags: [hallucination, factuality, quality, llm, tracking, observability, grounding]
---

## Problem

LLM outputs sometimes contain plausible-sounding but factually incorrect information. Without tracking, teams only learn about hallucinations from user complaints. By the time the signal is clear, thousands of incorrect responses may have been delivered. Systematic measurement enables proactive detection of model drift, prompt regressions, and domain-specific reliability gaps.

```python
# Broken: no hallucination tracking — reliability is unknown
async def answer(question: str) -> str:
    response = await client.messages.create(
        model="claude-opus-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": question}]
    )
    return response.content[0].text
# No measurement of factual accuracy over time
```

---

## Solution 1: Citation Grounding Check

```python
import asyncio
import re
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field
from typing import Any

client = AsyncAnthropic()

@dataclass
class GroundingResult:
    claim: str
    grounded: bool   # True if claim is supported by source context
    confidence: float
    source_snippet: str | None = None

async def check_claim_grounding(claim: str, source_context: str) -> GroundingResult:
    """
    Ask a cheap model to verify whether a specific claim is supported
    by the provided source context.
    """
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        system=(
            "You are a fact-checking assistant. Given a claim and source context, "
            "respond with JSON only: "
            '{"supported": true/false, "confidence": 0.0-1.0, "evidence": "quote or null"}'
        ),
        messages=[{
            "role": "user",
            "content": (
                f"Source context:\n{source_context[:2000]}\n\n"
                f"Claim to verify: {claim}"
            )
        }]
    )
    import json
    try:
        result = json.loads(response.content[0].text)
        return GroundingResult(
            claim=claim,
            grounded=result.get("supported", False),
            confidence=float(result.get("confidence", 0.5)),
            source_snippet=result.get("evidence"),
        )
    except Exception:
        return GroundingResult(claim=claim, grounded=False, confidence=0.0)

def extract_factual_claims(text: str) -> list[str]:
    """
    Heuristic extraction of factual claims from LLM output.
    Targets sentences with assertive structure and specific facts.
    """
    sentences = re.split(r'(?<=[.!?])\s+', text)
    claims = []
    for s in sentences:
        s = s.strip()
        if len(s) < 20:
            continue
        # Skip hedged statements
        if any(hedge in s.lower() for hedge in
               ["i think", "i believe", "might", "could be", "perhaps",
                "it seems", "possibly", "approximately"]):
            continue
        # Prefer sentences with numbers, named entities, or specific assertions
        if re.search(r'\d{4}|\d+\s*%|\$\d+|[A-Z][a-z]+\s+[A-Z][a-z]+', s):
            claims.append(s)
        elif re.match(r'^(The|A|An|In|On|At|By|According)', s):
            claims.append(s)
    return claims[:5]  # check at most 5 claims per response

class HallucinationGroundingChecker:
    """Check how many claims in a response are grounded in provided context."""

    def __init__(self):
        self._total_claims = 0
        self._ungrounded_claims = 0
        self._checks_by_type: dict[str, dict] = {}

    async def check_response(self, response_text: str, source_context: str,
                              query_type: str = "general") -> dict:
        claims = extract_factual_claims(response_text)
        if not claims:
            return {"hallucination_rate": 0.0, "claims_checked": 0,
                    "ungrounded": []}

        results = await asyncio.gather(*[
            check_claim_grounding(claim, source_context) for claim in claims
        ])

        ungrounded = [r for r in results if not r.grounded]
        hallucination_rate = len(ungrounded) / len(results)

        self._total_claims += len(results)
        self._ungrounded_claims += len(ungrounded)

        # Track by query type
        if query_type not in self._checks_by_type:
            self._checks_by_type[query_type] = {"total": 0, "ungrounded": 0}
        self._checks_by_type[query_type]["total"] += len(results)
        self._checks_by_type[query_type]["ungrounded"] += len(ungrounded)

        return {
            "hallucination_rate": round(hallucination_rate, 3),
            "claims_checked": len(results),
            "ungrounded": [r.claim for r in ungrounded],
            "query_type": query_type,
        }

    def global_stats(self) -> dict:
        rate = (self._ungrounded_claims / max(1, self._total_claims))
        return {
            "global_hallucination_rate": round(rate, 4),
            "total_claims_checked": self._total_claims,
            "total_ungrounded": self._ungrounded_claims,
            "by_query_type": {
                t: {
                    "rate": round(v["ungrounded"] / max(1, v["total"]), 4),
                    "n": v["total"]
                }
                for t, v in self._checks_by_type.items()
            }
        }
```

---

## Solution 2: Self-Consistency Sampling for Hallucination Detection

```python
import asyncio
from anthropic import AsyncAnthropic
from dataclasses import dataclass

client = AsyncAnthropic()

@dataclass
class ConsistencyResult:
    question: str
    answers: list[str]
    is_consistent: bool
    agreement_score: float   # 0.0–1.0: fraction of pairs that agree
    consensus_answer: str | None

async def sample_multiple_responses(question: str,
                                     n_samples: int = 3,
                                     temperature: float = 0.7) -> list[str]:
    """Generate N independent responses to the same question."""
    tasks = [
        client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": question}],
        )
        for _ in range(n_samples)
    ]
    responses = await asyncio.gather(*tasks)
    return [r.content[0].text for r in responses]

async def check_agreement(answer_a: str, answer_b: str) -> float:
    """Ask a model to judge if two answers agree semantically (0.0–1.0)."""
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=10,
        system="Reply with only a number between 0.0 and 1.0 indicating semantic agreement.",
        messages=[{
            "role": "user",
            "content": f"Answer A: {answer_a[:200]}\n\nAnswer B: {answer_b[:200]}"
        }]
    )
    try:
        return float(response.content[0].text.strip())
    except ValueError:
        return 0.5

async def self_consistency_check(question: str,
                                  n_samples: int = 3) -> ConsistencyResult:
    """
    High self-consistency → model is confident and likely correct.
    Low self-consistency → model is uncertain → hallucination risk.
    """
    answers = await sample_multiple_responses(question, n_samples)

    # Check all pairs
    pairs = [(i, j) for i in range(len(answers))
             for j in range(i + 1, len(answers))]
    agreement_scores = await asyncio.gather(*[
        check_agreement(answers[i], answers[j]) for i, j in pairs
    ])

    avg_agreement = sum(agreement_scores) / max(1, len(agreement_scores))
    is_consistent = avg_agreement >= 0.75

    # Pick most representative answer (highest mean agreement with others)
    mean_agreements = []
    for i, answer in enumerate(answers):
        related = [score for (a, b), score in zip(pairs, agreement_scores)
                   if a == i or b == i]
        mean_agreements.append(sum(related) / max(1, len(related)))

    best_idx = max(range(len(answers)), key=lambda i: mean_agreements[i])

    return ConsistencyResult(
        question=question,
        answers=answers,
        is_consistent=is_consistent,
        agreement_score=round(avg_agreement, 3),
        consensus_answer=answers[best_idx] if is_consistent else None,
    )

class SelfConsistencyTracker:
    """Track self-consistency scores over time to detect model drift."""

    def __init__(self, low_threshold: float = 0.6):
        self._scores: list[float] = []
        self._low_threshold = low_threshold
        self._low_count = 0

    def record(self, result: ConsistencyResult):
        self._scores.append(result.agreement_score)
        if result.agreement_score < self._low_threshold:
            self._low_count += 1

    def hallucination_risk_rate(self) -> float:
        return self._low_count / max(1, len(self._scores))

    def rolling_avg(self, window: int = 50) -> float:
        if not self._scores:
            return 0.0
        recent = self._scores[-window:]
        return sum(recent) / len(recent)

    def stats(self) -> dict:
        return {
            "total_checks": len(self._scores),
            "low_consistency_rate": round(self.hallucination_risk_rate(), 4),
            "rolling_avg_agreement": round(self.rolling_avg(), 4),
            "trend": "degrading" if self.rolling_avg(10) < self.rolling_avg(50) else "stable",
        }
```

---

## Solution 3: Reference-Based Factual Accuracy Benchmark

```python
import asyncio
import json
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field
from typing import Any

client = AsyncAnthropic()

@dataclass
class BenchmarkQuestion:
    question: str
    expected_answer: str
    category: str
    difficulty: str = "medium"  # easy, medium, hard

@dataclass
class BenchmarkResult:
    question: str
    model_answer: str
    expected_answer: str
    is_correct: bool
    correctness_score: float  # 0.0–1.0
    category: str

async def judge_answer(question: str, model_answer: str,
                        expected_answer: str) -> tuple[bool, float]:
    """Use an LLM judge to evaluate answer correctness."""
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=60,
        system=(
            "You are an answer grader. Compare the model answer to the expected "
            "answer and respond with JSON only: "
            '{"correct": true/false, "score": 0.0-1.0}'
            " Score 1.0=exact match, 0.5=partially correct, 0.0=wrong."
        ),
        messages=[{
            "role": "user",
            "content": (
                f"Question: {question}\n"
                f"Expected: {expected_answer}\n"
                f"Model answer: {model_answer}"
            )
        }]
    )
    try:
        result = json.loads(response.content[0].text)
        return result.get("correct", False), float(result.get("score", 0.0))
    except Exception:
        return False, 0.0

class HallucinationBenchmark:
    """
    Runs a fixed set of questions and measures factual accuracy.
    Run periodically (daily/per-deploy) to detect regressions.
    """

    def __init__(self, questions: list[BenchmarkQuestion],
                 model_under_test: str = "claude-opus-4-6"):
        self._questions = questions
        self._model = model_under_test
        self._runs: list[dict] = []

    async def run(self) -> dict:
        """Run all benchmark questions and return accuracy metrics."""
        async def evaluate_one(q: BenchmarkQuestion) -> BenchmarkResult:
            response = await client.messages.create(
                model=self._model,
                max_tokens=256,
                messages=[{"role": "user", "content": q.question}]
            )
            answer = response.content[0].text
            correct, score = await judge_answer(q.question, answer, q.expected_answer)
            return BenchmarkResult(
                question=q.question,
                model_answer=answer,
                expected_answer=q.expected_answer,
                is_correct=correct,
                correctness_score=score,
                category=q.category,
            )

        results = await asyncio.gather(*[evaluate_one(q) for q in self._questions])

        by_category: dict[str, list[float]] = {}
        for r in results:
            by_category.setdefault(r.category, []).append(r.correctness_score)

        run_summary = {
            "model": self._model,
            "total": len(results),
            "correct": sum(1 for r in results if r.is_correct),
            "accuracy": round(sum(1 for r in results if r.is_correct) / len(results), 4),
            "avg_score": round(sum(r.correctness_score for r in results) / len(results), 4),
            "by_category": {
                cat: round(sum(scores) / len(scores), 4)
                for cat, scores in by_category.items()
            },
        }
        self._runs.append(run_summary)
        return run_summary

    def detect_regression(self, threshold: float = 0.05) -> dict | None:
        """Compare last two runs. Returns regression report if accuracy dropped > threshold."""
        if len(self._runs) < 2:
            return None
        prev = self._runs[-2]["accuracy"]
        current = self._runs[-1]["accuracy"]
        drop = prev - current
        if drop > threshold:
            return {
                "regression_detected": True,
                "prev_accuracy": prev,
                "current_accuracy": current,
                "accuracy_drop": round(drop, 4),
                "model": self._model,
            }
        return None
```

---

## Solution 4: Continuous Hallucination Rate Monitor with Prometheus

```python
import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Awaitable

try:
    from prometheus_client import Counter, Histogram, Gauge
    HAS_PROMETHEUS = True
except ImportError:
    HAS_PROMETHEUS = False

@dataclass
class HallucinationEvent:
    timestamp: float = field(default_factory=time.time)
    model: str = ""
    query_type: str = "general"
    hallucination_rate: float = 0.0
    ungrounded_count: int = 0
    total_claims: int = 0
    session_id: str = ""

class ContinuousHallucinationMonitor:
    """
    Records hallucination events and exports metrics.
    Alerts when rolling hallucination rate exceeds threshold.
    """

    def __init__(self, alert_threshold: float = 0.15,
                 window_size: int = 100):
        self._events: deque[HallucinationEvent] = deque(maxlen=window_size)
        self._alert_threshold = alert_threshold
        self._total_events = 0
        self._alert_callbacks: list[Callable[[dict], Awaitable[None]]] = []

        if HAS_PROMETHEUS:
            self._counter = Counter(
                "agent_hallucination_events_total",
                "Total responses flagged for potential hallucination",
                ["model", "query_type"]
            )
            self._rate_gauge = Gauge(
                "agent_hallucination_rate",
                "Rolling hallucination rate",
                ["model"]
            )
            self._score_hist = Histogram(
                "agent_hallucination_score",
                "Per-response hallucination rate",
                ["model", "query_type"],
                buckets=[0.0, 0.1, 0.2, 0.3, 0.5, 0.7, 1.0]
            )

    def add_alert_callback(self, fn: Callable[[dict], Awaitable[None]]):
        self._alert_callbacks.append(fn)

    async def record(self, event: HallucinationEvent):
        self._events.append(event)
        self._total_events += 1

        if HAS_PROMETHEUS:
            if event.hallucination_rate > 0:
                self._counter.labels(event.model, event.query_type).inc()
            self._score_hist.labels(event.model, event.query_type).observe(
                event.hallucination_rate
            )
            rolling = self.rolling_rate()
            self._rate_gauge.labels(event.model).set(rolling)

        # Alert check
        rolling = self.rolling_rate()
        if rolling > self._alert_threshold and len(self._events) >= 10:
            alert = {
                "alert": "hallucination_rate_high",
                "rolling_rate": round(rolling, 4),
                "threshold": self._alert_threshold,
                "window": len(self._events),
                "model": event.model,
            }
            for callback in self._alert_callbacks:
                try:
                    await callback(alert)
                except Exception as e:
                    print(f"[HallucinationMonitor] Alert callback failed: {e}")

    def rolling_rate(self) -> float:
        if not self._events:
            return 0.0
        return sum(e.hallucination_rate for e in self._events) / len(self._events)

    def stats(self) -> dict:
        if not self._events:
            return {"status": "no_data"}
        return {
            "total_responses_monitored": self._total_events,
            "rolling_rate": round(self.rolling_rate(), 4),
            "window_size": len(self._events),
            "alert_threshold": self._alert_threshold,
            "status": "alert" if self.rolling_rate() > self._alert_threshold else "ok",
            "by_model": self._breakdown_by("model"),
            "by_query_type": self._breakdown_by("query_type"),
        }

    def _breakdown_by(self, field: str) -> dict[str, float]:
        groups: dict[str, list[float]] = {}
        for e in self._events:
            key = getattr(e, field, "unknown")
            groups.setdefault(key, []).append(e.hallucination_rate)
        return {k: round(sum(v) / len(v), 4) for k, v in groups.items()}
```

---

## Solution 5: Uncertainty Expression Tracker

```python
import asyncio
import re
from dataclasses import dataclass

# Patterns that signal the model is uncertain (good: hedging)
UNCERTAINTY_MARKERS = [
    r"\b(I think|I believe|I'm not sure|I'm uncertain)\b",
    r"\b(might|could|may|possibly|probably|perhaps|seems? to)\b",
    r"\b(approximately|roughly|around|about|estimated)\b",
    r"\b(as of my (training|knowledge)|I don't have (real-time|current))\b",
    r"\b(you should verify|please confirm|worth checking)\b",
]

# Patterns that signal confident but potentially incorrect assertion (hallucination risk)
OVERCONFIDENT_PATTERNS = [
    r"\b(definitely|certainly|absolutely|always|never|exactly)\b",
    r"\b(the fact that|it is a fact|it is true that)\b",
    r"\b(proven|confirmed|established)\b",
    r"\bid (?:of|number|is)\s+\d{8,}\b",   # long specific IDs (often hallucinated)
]

@dataclass
class UncertaintyAnalysis:
    text: str
    uncertainty_markers: list[str]
    overconfident_phrases: list[str]
    uncertainty_score: float    # higher = more appropriately hedged
    overconfidence_score: float # higher = more hallucination risk
    recommendation: str

def analyze_uncertainty_expression(text: str) -> UncertaintyAnalysis:
    """
    Measure whether the model appropriately expresses uncertainty.
    High overconfidence + low uncertainty = hallucination risk.
    """
    uncertain_found = []
    for pattern in UNCERTAINTY_MARKERS:
        matches = re.findall(pattern, text, re.IGNORECASE)
        uncertain_found.extend(matches)

    overconfident_found = []
    for pattern in OVERCONFIDENT_PATTERNS:
        matches = re.findall(pattern, text, re.IGNORECASE)
        overconfident_found.extend(matches)

    word_count = max(1, len(text.split()))
    uncertainty_score = min(1.0, len(uncertain_found) / (word_count / 50))
    overconfidence_score = min(1.0, len(overconfident_found) / (word_count / 50))

    if overconfidence_score > 0.3 and uncertainty_score < 0.1:
        recommendation = "HIGH RISK: overconfident assertions with no hedging"
    elif uncertainty_score > 0.3:
        recommendation = "Good: appropriate uncertainty expression"
    else:
        recommendation = "Neutral: limited hedging detected"

    return UncertaintyAnalysis(
        text=text,
        uncertainty_markers=uncertain_found[:5],
        overconfident_phrases=overconfident_found[:5],
        uncertainty_score=round(uncertainty_score, 3),
        overconfidence_score=round(overconfidence_score, 3),
        recommendation=recommendation,
    )

class UncertaintyExpressionTracker:
    def __init__(self):
        self._analyses: list[UncertaintyAnalysis] = []

    def track(self, response: str) -> UncertaintyAnalysis:
        analysis = analyze_uncertainty_expression(response)
        self._analyses.append(analysis)
        return analysis

    def summary(self) -> dict:
        if not self._analyses:
            return {}
        avg_oc = sum(a.overconfidence_score for a in self._analyses) / len(self._analyses)
        high_risk = sum(1 for a in self._analyses if a.overconfidence_score > 0.3)
        return {
            "responses_tracked": len(self._analyses),
            "avg_overconfidence_score": round(avg_oc, 4),
            "high_risk_pct": round(high_risk / len(self._analyses) * 100, 1),
        }
```

---

## Solution 6: Hallucination Regression Dashboard

```python
import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

@dataclass
class HallucinationSnapshot:
    timestamp: float = field(default_factory=time.time)
    model: str = ""
    overall_rate: float = 0.0
    grounding_rate: float = 0.0
    self_consistency_score: float = 0.0
    benchmark_accuracy: float = 0.0
    overconfidence_score: float = 0.0
    sample_count: int = 0

class HallucinationDashboard:
    """
    Aggregates all hallucination signals into a persistent dashboard.
    Writes JSONL snapshots for external visualization.
    """

    def __init__(self, output_path: str = "hallucination_dashboard.jsonl"):
        self._path = Path(output_path)
        self._snapshots: list[HallucinationSnapshot] = []

    def record_snapshot(self, snap: HallucinationSnapshot):
        self._snapshots.append(snap)
        with open(self._path, "a") as f:
            f.write(json.dumps({
                "ts": snap.timestamp,
                "model": snap.model,
                "overall_rate": snap.overall_rate,
                "grounding_rate": snap.grounding_rate,
                "consistency": snap.self_consistency_score,
                "benchmark": snap.benchmark_accuracy,
                "overconfidence": snap.overconfidence_score,
                "n": snap.sample_count,
            }) + "\n")

    def detect_regression(self, window: int = 5,
                           threshold: float = 0.05) -> dict | None:
        """Compare latest window against prior window."""
        if len(self._snapshots) < window * 2:
            return None
        prev = self._snapshots[-(window * 2):-window]
        current = self._snapshots[-window:]

        prev_rate = sum(s.overall_rate for s in prev) / len(prev)
        curr_rate = sum(s.overall_rate for s in current) / len(current)

        if curr_rate - prev_rate > threshold:
            return {
                "regression": True,
                "prev_rate": round(prev_rate, 4),
                "current_rate": round(curr_rate, 4),
                "increase": round(curr_rate - prev_rate, 4),
                "model": current[-1].model,
            }
        return None

    def summary(self) -> dict:
        if not self._snapshots:
            return {"status": "no_data"}
        latest = self._snapshots[-1]
        return {
            "latest_overall_rate": latest.overall_rate,
            "latest_benchmark_accuracy": latest.benchmark_accuracy,
            "latest_consistency": latest.self_consistency_score,
            "snapshots_recorded": len(self._snapshots),
            "regression": self.detect_regression() is not None,
        }
```

---

## Comparison

| Solution | Detection Method | Cost | Latency | Continuous | Ground Truth Needed | Best For |
|---|---|---|---|---|---|---|
| 1. Citation grounding | Source context check | Low (Haiku) | +100ms | Yes | Source context | RAG agents with retrievals |
| 2. Self-consistency | Multi-sample agreement | Med (3× calls) | +2-5× | No | No | Uncertainty detection |
| 3. Reference benchmark | Fixed QA benchmark | Low (batch) | Offline | Scheduled | Yes (gold answers) | Regression testing |
| 4. Rate monitor + Prometheus | Aggregated signal | Low | None | Yes | No | Production alerting |
| 5. Uncertainty expression | Lexical analysis | Zero | None | Yes | No | Fast heuristic filter |
| 6. Regression dashboard | Historical comparison | Low | None | Scheduled | No | Long-term trend analysis |

**Key principle**: combine lexical uncertainty analysis (solution 5, zero cost) as a real-time filter with citation grounding (solution 1) for RAG responses and a scheduled benchmark (solution 3) for regression detection. Track the rolling hallucination rate in Prometheus (solution 4) and alert when it exceeds a threshold. Self-consistency sampling (solution 2) is expensive but the most reliable signal for high-stakes queries where ground truth is unavailable.
