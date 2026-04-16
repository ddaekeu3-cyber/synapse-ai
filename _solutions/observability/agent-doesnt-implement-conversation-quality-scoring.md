---
title: "Agent Doesn't Implement Conversation Quality Scoring"
description: "AI agents track availability and latency but have no signal for response quality; silent degradations in helpfulness, accuracy, or tone go undetected until user churn reveals the problem weeks later."
category: observability
difficulty: intermediate
tags: [quality, scoring, evaluation, llm-judge, metrics, monitoring, feedback]
---

# Agent Doesn't Implement Conversation Quality Scoring

## Problem

Uptime and latency metrics tell you the agent is responding, not whether it's responding *well*. A model regression, a prompt change, or a context drift can cut helpfulness in half while all infrastructure metrics stay green. Quality scoring continuously evaluates agent responses against rubrics (helpfulness, accuracy, safety, format adherence) and alerts when scores drop — before users churn.

## Solution 1: LLM-as-Judge Scorer with Structured Rubric

Use a fast model (Haiku) to judge every Nth response against a rubric, emitting a numeric score per dimension.

```python
import asyncio
import json
import logging
from anthropic import AsyncAnthropic

logger = logging.getLogger(__name__)
client = AsyncAnthropic()

JUDGE_PROMPT = """\
You are an evaluation judge. Rate the assistant's response on these dimensions (0-10 each):
- helpfulness: Does it directly address the user's need?
- accuracy: Is the information correct and precise?
- conciseness: Is it appropriately brief without missing key points?
- safety: Does it avoid harmful, biased, or inappropriate content?
- format: Is the formatting appropriate (markdown when needed, plain when not)?

User message: {user_message}
Assistant response: {assistant_response}

Respond ONLY with JSON:
{{"helpfulness": N, "accuracy": N, "conciseness": N, "safety": N, "format": N, "overall": N, "issues": "..."}}
"""

async def score_response(user_message: str, assistant_response: str) -> dict | None:
    """Returns quality scores or None on judge failure."""
    try:
        resp = await asyncio.wait_for(
            client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[{
                    "role": "user",
                    "content": JUDGE_PROMPT.format(
                        user_message=user_message[:500],
                        assistant_response=assistant_response[:1000],
                    ),
                }],
            ),
            timeout=8.0,
        )
        raw = resp.content[0].text.strip()
        # Strip markdown if present
        if raw.startswith("```"):
            raw = raw.split("```")[1].lstrip("json").strip()
        scores = json.loads(raw)
        scores["judge_model"] = "claude-haiku-4-5-20251001"
        return scores
    except Exception as e:
        logger.warning("quality_judge_failed", extra={"error": str(e)})
        return None

# Sample 10% of conversations
import random

async def agent_turn_with_scoring(user_msg: str, agent_response_fn) -> str:
    response = await agent_response_fn(user_msg)

    if random.random() < 0.10:  # score 10% of turns
        asyncio.create_task(_log_quality(user_msg, response))

    return response

async def _log_quality(user_msg: str, response: str):
    scores = await score_response(user_msg, response)
    if scores:
        logger.info(
            "response_quality",
            extra={
                "helpfulness": scores.get("helpfulness"),
                "accuracy": scores.get("accuracy"),
                "overall": scores.get("overall"),
                "issues": scores.get("issues", ""),
            },
        )
        if scores.get("overall", 10) < 5:
            logger.warning("low_quality_response", extra={"scores": scores})
```

**When to use**: Any production agent. Haiku scoring costs ~$0.001 per evaluation, making 10% sampling affordable.

---

## Solution 2: Rolling Quality Window with Regression Alert

Track average quality score over a rolling window; alert when the window average drops significantly.

```python
import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
import logging

logger = logging.getLogger(__name__)

@dataclass
class QualityWindowTracker:
    window_size: int = 200
    alert_threshold: float = 7.0       # alert if avg drops below this
    regression_drop: float = 1.5       # alert if avg drops by this much from baseline
    _scores: deque = field(default_factory=lambda: deque(maxlen=200))
    _baseline: float | None = None
    _baseline_samples: int = 50        # establish baseline from first N scores

    def record(self, score: float):
        self._scores.append(score)
        if len(self._scores) == self._baseline_samples:
            self._baseline = self.average
            logger.info("quality_baseline_established", extra={"baseline": round(self._baseline, 2)})
        self._check_alerts()

    @property
    def average(self) -> float:
        if not self._scores:
            return 0.0
        return sum(self._scores) / len(self._scores)

    @property
    def p10(self) -> float:
        if not self._scores:
            return 0.0
        s = sorted(self._scores)
        return s[int(len(s) * 0.10)]

    def _check_alerts(self):
        avg = self.average
        if avg < self.alert_threshold and len(self._scores) >= 20:
            logger.error(
                "quality_below_threshold",
                extra={"avg": round(avg, 2), "threshold": self.alert_threshold},
            )
        if self._baseline and avg < self._baseline - self.regression_drop:
            logger.error(
                "quality_regression_detected",
                extra={
                    "current_avg": round(avg, 2),
                    "baseline": round(self._baseline, 2),
                    "drop": round(self._baseline - avg, 2),
                },
            )

    def report(self) -> dict:
        return {
            "samples": len(self._scores),
            "average": round(self.average, 2),
            "p10": round(self.p10, 2),
            "baseline": round(self._baseline, 2) if self._baseline else None,
        }

quality_tracker = QualityWindowTracker(window_size=200, alert_threshold=6.5)

# Feed scores from the LLM judge
async def quality_monitor_loop(score_queue: asyncio.Queue):
    while True:
        score = await score_queue.get()
        quality_tracker.record(score)
        if len(quality_tracker._scores) % 50 == 0:
            logger.info("quality_window_report", extra=quality_tracker.report())
```

**When to use**: Deployed agents where you want automatic regression detection without manual dashboard review.

---

## Solution 3: Multi-Dimension Quality Prometheus Metrics

Export per-dimension quality scores to Prometheus for Grafana dashboard and alerting.

```python
import asyncio
import json
from prometheus_client import Histogram, Counter, Gauge, start_http_server
from anthropic import AsyncAnthropic
import logging

logger = logging.getLogger(__name__)

# Quality score histograms per dimension
quality_score = Histogram(
    "agent_response_quality_score",
    "Quality score per dimension (0-10)",
    ["dimension"],     # helpfulness, accuracy, conciseness, safety, format, overall
    buckets=[1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
)
quality_evaluations_total = Counter(
    "agent_quality_evaluations_total",
    "Total quality evaluations run",
    ["result"],        # result: scored | failed | skipped
)
quality_low_score_total = Counter(
    "agent_quality_low_score_total",
    "Responses scoring below threshold per dimension",
    ["dimension"],
)
quality_avg_overall = Gauge(
    "agent_quality_avg_overall",
    "Exponential moving average of overall quality score (0-10)",
)

_ema_overall = 8.0  # start optimistic
_alpha = 0.1

client = AsyncAnthropic()

async def evaluate_and_record(user_msg: str, response: str):
    """Score a response and push metrics."""
    global _ema_overall
    try:
        resp = await asyncio.wait_for(
            client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                messages=[{
                    "role": "user",
                    "content": (
                        f"Rate this response 0-10 on: helpfulness, accuracy, conciseness, safety, format, overall.\n"
                        f"User: {user_msg[:300]}\nResponse: {response[:600]}\n"
                        f"JSON only: {{\"helpfulness\":N,\"accuracy\":N,\"conciseness\":N,\"safety\":N,\"format\":N,\"overall\":N}}"
                    ),
                }],
            ),
            timeout=8.0,
        )
        scores = json.loads(resp.content[0].text.strip())
        for dim, score in scores.items():
            if isinstance(score, (int, float)) and 0 <= score <= 10:
                quality_score.labels(dimension=dim).observe(score)
                if score < 5:
                    quality_low_score_total.labels(dimension=dim).inc()

        overall = scores.get("overall", 0)
        _ema_overall = _alpha * overall + (1 - _alpha) * _ema_overall
        quality_avg_overall.set(_ema_overall)
        quality_evaluations_total.labels(result="scored").inc()

        if overall < 5:
            logger.warning("low_quality_flagged", extra={"overall": overall, "scores": scores})

    except Exception as e:
        quality_evaluations_total.labels(result="failed").inc()
        logger.warning("quality_eval_error", extra={"error": str(e)})

# Grafana alert:
# agent_quality_avg_overall < 6 for 10 minutes → page on-call
```

**When to use**: Teams with an existing Prometheus/Grafana observability stack.

---

## Solution 4: User Feedback Signal Collection and Correlation

Collect explicit thumbs up/down feedback; correlate with automated scores to calibrate the judge.

```python
import asyncio
import time
import uuid
import logging
from dataclasses import dataclass, field
from collections import defaultdict

logger = logging.getLogger(__name__)

@dataclass
class ConversationTurn:
    turn_id: str
    user_message: str
    agent_response: str
    automated_score: float | None = None
    user_feedback: int | None = None  # 1 = thumbs up, -1 = thumbs down, None = no feedback
    ts: float = field(default_factory=time.time)

class QualityCorrelationTracker:
    def __init__(self):
        self._turns: dict[str, ConversationTurn] = {}
        self._feedback_auto_scores: list[tuple[float, int]] = []  # (auto_score, user_feedback)

    def record_turn(self, user_message: str, response: str) -> str:
        turn_id = str(uuid.uuid4())[:8]
        self._turns[turn_id] = ConversationTurn(
            turn_id=turn_id,
            user_message=user_message,
            agent_response=response,
        )
        return turn_id

    def record_auto_score(self, turn_id: str, score: float):
        if turn_id in self._turns:
            self._turns[turn_id].automated_score = score

    def record_user_feedback(self, turn_id: str, feedback: int):
        if turn_id not in self._turns:
            return
        turn = self._turns[turn_id]
        turn.user_feedback = feedback
        if turn.automated_score is not None:
            self._feedback_auto_scores.append((turn.automated_score, feedback))
            self._log_correlation()

        logger.info(
            "user_feedback_received",
            extra={
                "turn_id": turn_id,
                "feedback": "positive" if feedback > 0 else "negative",
                "auto_score": turn.automated_score,
            },
        )

    def _log_correlation(self):
        if len(self._feedback_auto_scores) < 10:
            return
        # Compute point-biserial correlation
        auto_scores = [s for s, _ in self._feedback_auto_scores]
        feedback = [f for _, f in self._feedback_auto_scores]
        positive_scores = [s for s, f in self._feedback_auto_scores if f > 0]
        negative_scores = [s for s, f in self._feedback_auto_scores if f < 0]

        avg_pos = sum(positive_scores) / max(len(positive_scores), 1)
        avg_neg = sum(negative_scores) / max(len(negative_scores), 1)

        logger.info(
            "quality_feedback_correlation",
            extra={
                "n_pairs": len(self._feedback_auto_scores),
                "avg_score_on_thumbsup": round(avg_pos, 2),
                "avg_score_on_thumbsdown": round(avg_neg, 2),
                "judge_discriminates": avg_pos > avg_neg,
            },
        )

tracker = QualityCorrelationTracker()

# API endpoint for thumbs feedback
from fastapi import FastAPI
app = FastAPI()

@app.post("/feedback/{turn_id}")
async def submit_feedback(turn_id: str, thumbs_up: bool):
    tracker.record_user_feedback(turn_id, 1 if thumbs_up else -1)
    return {"ok": True}
```

**When to use**: Consumer-facing agents with UI feedback buttons. Calibrate automated judge against real user preferences.

---

## Solution 5: Semantic Similarity to Golden Answers

Maintain a set of golden (expert-written) answer examples; score production responses by cosine similarity to the nearest golden answer.

```python
import asyncio
import json
import numpy as np
from anthropic import AsyncAnthropic
import logging

logger = logging.getLogger(__name__)
client = AsyncAnthropic()

# Golden answers: (question_pattern, expert_answer)
GOLDEN_EXAMPLES = [
    ("how to handle rate limit", "Implement exponential backoff with jitter. On a 429 response, wait min(cap, base * 2^attempt + random_ms) before retrying. Respect the Retry-After header if present."),
    ("what is a circuit breaker", "A circuit breaker monitors failure rate to a dependency. After a threshold of failures it opens (stops sending requests). After a timeout it half-opens (allows one probe). On probe success it closes again."),
    ("explain async await", "async/await is syntactic sugar over coroutines. An async function returns a coroutine object. await suspends the current coroutine until the awaited coroutine completes, yielding control to the event loop."),
]

async def get_embedding(text: str) -> list[float]:
    """Get embedding using Claude's messages API as a proxy (use a real embedding model in prod)."""
    # In production: use text-embedding-3-small or voyage-3
    # Placeholder: return mock embedding
    import hashlib
    seed = int(hashlib.md5(text[:100].encode()).hexdigest(), 16) % (2**31)
    rng = np.random.default_rng(seed)
    vec = rng.standard_normal(256)
    return (vec / np.linalg.norm(vec)).tolist()

def cosine_similarity(a: list[float], b: list[float]) -> float:
    va, vb = np.array(a), np.array(b)
    return float(np.dot(va, vb) / (np.linalg.norm(va) * np.linalg.norm(vb) + 1e-9))

class GoldenAnswerScorer:
    def __init__(self):
        self._golden_embeddings: list[tuple[str, list[float]]] = []
        self._ready = False

    async def build_index(self):
        for _, answer in GOLDEN_EXAMPLES:
            emb = await get_embedding(answer)
            self._golden_embeddings.append((answer, emb))
        self._ready = True
        logger.info("golden_answer_index_built", extra={"count": len(self._golden_embeddings)})

    async def score(self, response: str) -> float | None:
        if not self._ready:
            return None
        response_emb = await get_embedding(response)
        similarities = [
            cosine_similarity(response_emb, golden_emb)
            for _, golden_emb in self._golden_embeddings
        ]
        best = max(similarities)
        return round(best * 10, 2)  # scale to 0-10

scorer = GoldenAnswerScorer()

async def startup():
    await scorer.build_index()

async def score_with_golden(response: str) -> float | None:
    return await scorer.score(response)
```

**When to use**: Domain-specific agents (support, legal, medical) where expert-written answers define quality.

---

## Solution 6: Multi-Signal Quality Aggregator with Weighted Composite Score

Combine automated judge, similarity, user feedback, and heuristics into a single composite quality score.

```python
import asyncio
import time
import logging
from dataclasses import dataclass
from typing import Callable, Awaitable

logger = logging.getLogger(__name__)

@dataclass
class QualitySignal:
    name: str
    weight: float
    score: float | None  # None = signal unavailable

@dataclass
class CompositeQualityScore:
    signals: list[QualitySignal]
    composite: float
    confidence: float  # fraction of weighted signals available

    def __str__(self):
        parts = [f"{s.name}={s.score:.1f}" for s in self.signals if s.score is not None]
        return f"composite={self.composite:.2f} ({', '.join(parts)}) confidence={self.confidence:.0%}"

def compute_composite(signals: list[QualitySignal]) -> CompositeQualityScore:
    available = [(s, s.score) for s in signals if s.score is not None]
    if not available:
        return CompositeQualityScore(signals=signals, composite=0.0, confidence=0.0)

    total_weight = sum(s.weight for s, _ in available)
    weighted_sum = sum(s.weight * score for s, score in available)
    composite = weighted_sum / total_weight if total_weight > 0 else 0.0

    all_weight = sum(s.weight for s in signals)
    confidence = total_weight / all_weight if all_weight > 0 else 0.0

    return CompositeQualityScore(signals=signals, composite=round(composite, 2), confidence=confidence)

def heuristic_score(response: str) -> float:
    """Fast heuristic scoring without any API calls."""
    score = 5.0
    if len(response) < 10:  return 0.0      # too short
    if len(response) > 5000: score -= 1.0   # suspiciously long
    if response.count("I'm sorry") > 2: score -= 1.0  # over-apologetic
    if "I don't know" in response and len(response) < 100: score -= 2.0
    if any(bad in response.lower() for bad in ["as an ai language model", "i cannot assist"]): score -= 1.0
    score = max(0.0, min(10.0, score))
    return score

async def evaluate_quality(user_msg: str, response: str) -> CompositeQualityScore:
    from typing import Optional

    # Heuristic (always fast, no API)
    heuristic = heuristic_score(response)

    # LLM judge (async, may timeout)
    async def _llm_judge() -> float | None:
        try:
            from anthropic import AsyncAnthropic
            import json
            c = AsyncAnthropic()
            r = await asyncio.wait_for(
                c.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=100,
                    messages=[{"role": "user", "content": f"Rate 0-10: User={user_msg[:200]} Response={response[:400]}. JSON: {{\"score\": N}}"}],
                ),
                timeout=6.0,
            )
            return json.loads(r.content[0].text)["score"]
        except Exception:
            return None

    llm_score = await _llm_judge()

    signals = [
        QualitySignal("heuristic",   weight=0.2, score=heuristic),
        QualitySignal("llm_judge",   weight=0.6, score=llm_score),
        QualitySignal("user_feedback", weight=0.2, score=None),  # filled in later
    ]
    result = compute_composite(signals)
    logger.info("composite_quality", extra={"score": result.composite, "confidence": result.confidence})

    if result.composite < 5.0 and result.confidence > 0.5:
        logger.warning("quality_alert", extra={"composite": result.composite})

    return result
```

**When to use**: Production agents where you want a single quality KPI that combines all available signals.

---

## Comparison

| Solution | Cost per Eval | Latency | Requires Human | Real-time | Dimensions | Best For |
|---|---|---|---|---|---|---|
| LLM-as-judge | ~$0.001 | ~2s | No | Yes (async) | Multi-dimension | General agent quality |
| Rolling window + alert | None (uses judge) | None (background) | No | Yes | Overall | Regression detection |
| Prometheus metrics | None (uses judge) | None | No | Yes | Multi-dimension | Grafana dashboards |
| User feedback correlation | None | None | Yes | Deferred | Binary | Calibrating judges |
| Golden answer similarity | Embedding cost | ~1s | Yes (setup) | Yes | Semantic | Domain-specific agents |
| Composite aggregator | ~$0.001 + heuristic | ~2s | Optional | Yes | All combined | Production KPI |

**Rule of thumb**: Start with heuristic scoring (free) + LLM judge on 10% of traffic (~$30/month at 1K req/day). Add user feedback collection within the first month to validate judge calibration.
