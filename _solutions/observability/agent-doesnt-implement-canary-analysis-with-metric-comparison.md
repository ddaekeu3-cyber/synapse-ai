---
title: "Agent Doesn't Implement Canary Analysis with Metric Comparison"
description: "AI agents that deploy model updates or prompt changes without canary analysis risk silent regressions. Learn six progressive canary analysis patterns that compare metrics between control and treatment populations before full rollout."
date: 2026-04-16
difficulty: advanced
category: observability
slug: agent-doesnt-implement-canary-analysis-with-metric-comparison
tags: [canary, deployment, metrics, a/b-testing, rollout, observability]
symptoms:
  - "New model version degrades response quality but nobody notices until 100% rollout"
  - "Prompt changes cause subtle latency increases that accumulate under load"
  - "Error rate spikes go undetected because baseline was never established"
  - "Cost increases from new model tier aren't caught until the billing cycle"
  - "User satisfaction drops silently after agent behavior changes"
---

## The Problem

When AI agents update their underlying model, prompts, or tool configurations, they typically do a hard cutover: one moment the old version serves 100% of traffic, the next the new version does. This is dangerous because LLM behavior is probabilistic and context-dependent — regressions don't always show up in offline evaluations but do emerge under real production traffic patterns.

Without canary analysis, teams discover regressions through user complaints or billing alerts rather than through proactive metric comparison. A proper canary analysis framework routes a small fraction of traffic to the new version, collects comparable metrics from both populations, performs statistical significance testing, and auto-promotes or auto-rolls-back based on configurable thresholds.

```python
# ❌ Hard cutover — no canary analysis
class AgentRouter:
    def __init__(self, model: str):
        self.model = model  # Just switch the variable and pray

    async def handle(self, request):
        return await call_llm(self.model, request)

# ✓ Canary analysis with metric comparison
canary = CanaryAnalyzer(control="claude-3-5-sonnet", treatment="claude-opus-4-6",
                        traffic_fraction=0.05, min_samples=200)
result = await canary.analyze(requests)
if result.should_promote:
    router.promote_treatment()
```

---

## Solution 1: Basic Canary Router with Metric Collection

The simplest canary pattern: deterministic traffic splitting by request hash, metric collection per cohort, and a comparison report.

```python
import hashlib
import time
import statistics
from dataclasses import dataclass, field
from typing import Any
from collections import defaultdict


@dataclass
class RequestMetrics:
    latency_ms: float
    input_tokens: int
    output_tokens: int
    error: bool
    cost_usd: float


@dataclass
class CohortStats:
    latency_p50: float
    latency_p95: float
    latency_p99: float
    error_rate: float
    avg_cost_usd: float
    avg_output_tokens: float
    sample_count: int


class BasicCanaryRouter:
    """Hash-based traffic split with per-cohort metric aggregation."""

    def __init__(
        self,
        control_model: str,
        treatment_model: str,
        treatment_fraction: float = 0.05,
    ):
        self.control_model = control_model
        self.treatment_model = treatment_model
        self.treatment_fraction = treatment_fraction
        self._metrics: dict[str, list[RequestMetrics]] = {
            "control": [],
            "treatment": [],
        }

    def _assign_cohort(self, request_id: str) -> str:
        h = int(hashlib.md5(request_id.encode()).hexdigest(), 16)
        bucket = (h % 10000) / 10000.0
        return "treatment" if bucket < self.treatment_fraction else "control"

    async def handle(self, request_id: str, messages: list[dict]) -> dict:
        cohort = self._assign_cohort(request_id)
        model = self.treatment_model if cohort == "treatment" else self.control_model

        start = time.monotonic()
        error = False
        response = {}
        try:
            import anthropic
            client = anthropic.AsyncAnthropic()
            resp = await client.messages.create(
                model=model,
                max_tokens=1024,
                messages=messages,
            )
            response = {
                "content": resp.content[0].text,
                "model": model,
                "cohort": cohort,
            }
            input_tokens = resp.usage.input_tokens
            output_tokens = resp.usage.output_tokens
        except Exception as e:
            error = True
            response = {"error": str(e), "cohort": cohort}
            input_tokens = output_tokens = 0

        latency_ms = (time.monotonic() - start) * 1000
        cost = self._estimate_cost(model, input_tokens, output_tokens)

        self._metrics[cohort].append(
            RequestMetrics(
                latency_ms=latency_ms,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                error=error,
                cost_usd=cost,
            )
        )
        return response

    def _estimate_cost(self, model: str, input_t: int, output_t: int) -> float:
        prices = {
            "claude-3-5-sonnet-20241022": (0.000003, 0.000015),
            "claude-opus-4-6": (0.000015, 0.000075),
        }
        inp_price, out_price = prices.get(model, (0.000003, 0.000015))
        return inp_price * input_t + out_price * output_t

    def compute_stats(self, cohort: str) -> CohortStats | None:
        metrics = self._metrics[cohort]
        if not metrics:
            return None
        latencies = sorted(m.latency_ms for m in metrics)
        n = len(latencies)
        return CohortStats(
            latency_p50=latencies[int(n * 0.50)],
            latency_p95=latencies[int(n * 0.95)],
            latency_p99=latencies[int(n * 0.99)],
            error_rate=sum(1 for m in metrics if m.error) / n,
            avg_cost_usd=statistics.mean(m.cost_usd for m in metrics),
            avg_output_tokens=statistics.mean(m.output_tokens for m in metrics),
            sample_count=n,
        )

    def comparison_report(self) -> dict:
        ctrl = self.compute_stats("control")
        trt = self.compute_stats("treatment")
        if not ctrl or not trt:
            return {"status": "insufficient_data"}

        def delta_pct(a, b):
            return ((b - a) / a * 100) if a else 0

        return {
            "control": {"model": self.control_model, "samples": ctrl.sample_count},
            "treatment": {"model": self.treatment_model, "samples": trt.sample_count},
            "deltas": {
                "latency_p50_pct": delta_pct(ctrl.latency_p50, trt.latency_p50),
                "latency_p95_pct": delta_pct(ctrl.latency_p95, trt.latency_p95),
                "error_rate_pct": delta_pct(ctrl.error_rate, trt.error_rate),
                "cost_pct": delta_pct(ctrl.avg_cost_usd, trt.avg_cost_usd),
                "output_tokens_pct": delta_pct(ctrl.avg_output_tokens, trt.avg_output_tokens),
            },
        }
```

---

## Solution 2: Statistical Significance Testing with Mann-Whitney U

Metric deltas without statistical significance testing produce false positives. Use Mann-Whitney U (non-parametric, works on skewed latency distributions) to gate promotion decisions.

```python
import math
from scipy import stats


@dataclass
class SignificanceResult:
    metric: str
    control_median: float
    treatment_median: float
    p_value: float
    significant: bool
    regression: bool  # treatment is worse by threshold
    delta_pct: float


class StatisticalCanaryAnalyzer:
    """Canary with Mann-Whitney U significance testing for each metric."""

    REGRESSION_THRESHOLDS = {
        "latency_ms": 0.10,   # 10% latency increase = regression
        "error_rate": 0.005,  # 0.5pp absolute error rate increase = regression
        "cost_usd": 0.15,     # 15% cost increase = regression
    }
    ALPHA = 0.05  # significance level

    def __init__(self, min_samples: int = 100):
        self.min_samples = min_samples
        self._samples: dict[str, dict[str, list[float]]] = {
            "control": defaultdict(list),
            "treatment": defaultdict(list),
        }

    def record(self, cohort: str, latency_ms: float, error: bool, cost_usd: float):
        self._samples[cohort]["latency_ms"].append(latency_ms)
        self._samples[cohort]["error_rate"].append(1.0 if error else 0.0)
        self._samples[cohort]["cost_usd"].append(cost_usd)

    def _mannwhitney(self, ctrl: list[float], trt: list[float]) -> tuple[float, float]:
        """Returns (statistic, p_value)."""
        if len(ctrl) < 2 or len(trt) < 2:
            return 0.0, 1.0
        result = stats.mannwhitneyu(ctrl, trt, alternative="two-sided")
        return result.statistic, result.pvalue

    def analyze(self) -> list[SignificanceResult]:
        results = []
        for metric, threshold in self.REGRESSION_THRESHOLDS.items():
            ctrl = self._samples["control"][metric]
            trt = self._samples["treatment"][metric]

            if len(ctrl) < self.min_samples or len(trt) < self.min_samples:
                continue

            _, p_value = self._mannwhitney(ctrl, trt)
            ctrl_med = statistics.median(ctrl)
            trt_med = statistics.median(trt)
            delta_pct = ((trt_med - ctrl_med) / ctrl_med) if ctrl_med else 0

            significant = p_value < self.ALPHA
            regression = significant and delta_pct > threshold

            results.append(SignificanceResult(
                metric=metric,
                control_median=ctrl_med,
                treatment_median=trt_med,
                p_value=p_value,
                significant=significant,
                regression=regression,
                delta_pct=delta_pct,
            ))
        return results

    def should_rollback(self) -> tuple[bool, list[str]]:
        results = self.analyze()
        regressions = [r.metric for r in results if r.regression]
        return bool(regressions), regressions

    def should_promote(self) -> tuple[bool, str]:
        ctrl_samples = len(self._samples["control"]["latency_ms"])
        trt_samples = len(self._samples["treatment"]["latency_ms"])

        if trt_samples < self.min_samples:
            return False, f"insufficient_samples ({trt_samples}/{self.min_samples})"

        rollback, regressions = self.should_rollback()
        if rollback:
            return False, f"regressions_detected: {regressions}"

        # All metrics passed or not significant — safe to promote
        return True, "no_significant_regressions"
```

---

## Solution 3: Sliding Window Canary with Prometheus Export

For long-running agents, a sliding window canary continuously re-evaluates metric stability rather than computing a single batch result. Exports to Prometheus for dashboard visibility.

```python
import asyncio
import time
from collections import deque


@dataclass
class WindowedSample:
    timestamp: float
    latency_ms: float
    error: bool
    cost_usd: float
    output_tokens: int


class SlidingWindowCanary:
    """Rolling 5-minute window canary with Prometheus metric export."""

    WINDOW_SECONDS = 300  # 5 minutes

    def __init__(self, prometheus_prefix: str = "canary"):
        self._windows: dict[str, deque[WindowedSample]] = {
            "control": deque(),
            "treatment": deque(),
        }
        self._prefix = prometheus_prefix
        self._prometheus_lines: list[str] = []

    def record(self, cohort: str, latency_ms: float, error: bool,
               cost_usd: float, output_tokens: int):
        now = time.time()
        self._windows[cohort].append(WindowedSample(
            timestamp=now, latency_ms=latency_ms, error=error,
            cost_usd=cost_usd, output_tokens=output_tokens,
        ))
        self._evict_old(cohort, now)

    def _evict_old(self, cohort: str, now: float):
        cutoff = now - self.WINDOW_SECONDS
        while self._windows[cohort] and self._windows[cohort][0].timestamp < cutoff:
            self._windows[cohort].popleft()

    def _window_stats(self, cohort: str) -> dict:
        samples = list(self._windows[cohort])
        if not samples:
            return {}
        latencies = sorted(s.latency_ms for s in samples)
        n = len(latencies)
        return {
            "count": n,
            "latency_p50": latencies[int(n * 0.5)],
            "latency_p95": latencies[min(int(n * 0.95), n - 1)],
            "error_rate": sum(1 for s in samples if s.error) / n,
            "avg_cost": sum(s.cost_usd for s in samples) / n,
            "avg_output_tokens": sum(s.output_tokens for s in samples) / n,
        }

    def prometheus_metrics(self) -> str:
        """Returns Prometheus text-format metrics for scraping."""
        lines = []
        for cohort in ("control", "treatment"):
            stats = self._window_stats(cohort)
            if not stats:
                continue
            label = f'cohort="{cohort}"'
            p = self._prefix
            lines += [
                f'{p}_requests_total{{{label}}} {stats["count"]}',
                f'{p}_latency_p50_ms{{{label}}} {stats["latency_p50"]:.2f}',
                f'{p}_latency_p95_ms{{{label}}} {stats["latency_p95"]:.2f}',
                f'{p}_error_rate{{{label}}} {stats["error_rate"]:.4f}',
                f'{p}_avg_cost_usd{{{label}}} {stats["avg_cost"]:.6f}',
                f'{p}_avg_output_tokens{{{label}}} {stats["avg_output_tokens"]:.1f}',
            ]
        return "\n".join(lines)

    def regression_check(self, latency_threshold_pct: float = 0.10,
                         error_rate_threshold: float = 0.005) -> dict:
        ctrl = self._window_stats("control")
        trt = self._window_stats("treatment")
        if not ctrl or not trt:
            return {"status": "insufficient_data"}

        issues = []
        lat_delta = (trt["latency_p95"] - ctrl["latency_p95"]) / max(ctrl["latency_p95"], 1)
        if lat_delta > latency_threshold_pct:
            issues.append(f"latency_p95 +{lat_delta*100:.1f}%")

        err_delta = trt["error_rate"] - ctrl["error_rate"]
        if err_delta > error_rate_threshold:
            issues.append(f"error_rate +{err_delta*100:.2f}pp")

        return {
            "status": "regression" if issues else "healthy",
            "issues": issues,
            "window_seconds": self.WINDOW_SECONDS,
            "control_count": ctrl["count"],
            "treatment_count": trt["count"],
        }

    async def watch_loop(self, check_interval_seconds: int = 30,
                         on_regression=None):
        """Background task: periodically check for regressions."""
        while True:
            await asyncio.sleep(check_interval_seconds)
            result = self.regression_check()
            if result["status"] == "regression" and on_regression:
                await on_regression(result)
```

---

## Solution 4: Multi-Metric Scorecard with Auto-Promote/Rollback

A scorecard aggregates multiple metrics into a single pass/fail decision with configurable weights, enabling automatic promotion when all criteria are met and automatic rollback when regressions are detected.

```python
from dataclasses import dataclass
from enum import Enum
from typing import Callable


class CanaryDecision(Enum):
    PENDING = "pending"
    PROMOTE = "promote"
    ROLLBACK = "rollback"
    INCONCLUSIVE = "inconclusive"


@dataclass
class MetricCriteria:
    name: str
    extract_fn: Callable[[list], float]  # computes scalar from sample list
    max_degradation_pct: float  # positive = treatment can be this much worse
    min_improvement_pct: float = 0.0  # if set, treatment must be this much better
    weight: float = 1.0


class ScorecardCanary:
    """Multi-metric scorecard with weighted pass/fail and auto-decision."""

    DEFAULT_CRITERIA = [
        MetricCriteria("p95_latency", lambda xs: sorted(xs)[int(len(xs)*0.95)],
                       max_degradation_pct=10.0, weight=2.0),
        MetricCriteria("error_rate", lambda xs: sum(xs) / len(xs),
                       max_degradation_pct=50.0, weight=3.0),  # absolute small numbers
        MetricCriteria("avg_cost", lambda xs: sum(xs) / len(xs),
                       max_degradation_pct=20.0, weight=1.5),
        MetricCriteria("avg_output_tokens", lambda xs: sum(xs) / len(xs),
                       max_degradation_pct=30.0, weight=0.5),
    ]

    def __init__(self, min_samples: int = 200,
                 criteria: list[MetricCriteria] | None = None):
        self.min_samples = min_samples
        self.criteria = criteria or self.DEFAULT_CRITERIA
        self._data: dict[str, dict[str, list[float]]] = {
            "control": defaultdict(list),
            "treatment": defaultdict(list),
        }
        self._decision = CanaryDecision.PENDING
        self._decision_reason: list[str] = []

    def record(self, cohort: str, **metric_values: float):
        for k, v in metric_values.items():
            self._data[cohort][k].append(v)

    def evaluate(self) -> tuple[CanaryDecision, list[str]]:
        ctrl_count = len(self._data["control"].get("p95_latency", []))
        trt_count = len(self._data["treatment"].get("p95_latency", []))

        if trt_count < self.min_samples:
            return CanaryDecision.PENDING, [
                f"need {self.min_samples} treatment samples, have {trt_count}"
            ]

        failures = []
        scorecard = []

        for criterion in self.criteria:
            ctrl_vals = self._data["control"].get(criterion.name, [])
            trt_vals = self._data["treatment"].get(criterion.name, [])

            if not ctrl_vals or not trt_vals:
                continue

            ctrl_score = criterion.extract_fn(ctrl_vals)
            trt_score = criterion.extract_fn(trt_vals)

            if ctrl_score == 0:
                delta_pct = 0.0
            else:
                delta_pct = (trt_score - ctrl_score) / ctrl_score * 100

            passed = delta_pct <= criterion.max_degradation_pct
            if not passed:
                failures.append(
                    f"{criterion.name}: +{delta_pct:.1f}% "
                    f"(threshold: +{criterion.max_degradation_pct:.1f}%)"
                )

            scorecard.append({
                "metric": criterion.name,
                "control": ctrl_score,
                "treatment": trt_score,
                "delta_pct": delta_pct,
                "passed": passed,
                "weight": criterion.weight,
            })

        if failures:
            decision = CanaryDecision.ROLLBACK
            reasons = ["regressions_detected"] + failures
        else:
            decision = CanaryDecision.PROMOTE
            reasons = ["all_criteria_passed",
                       f"scorecard: {[s['metric'] + ':' + ('✓' if s['passed'] else '✗') for s in scorecard]}"]

        self._decision = decision
        self._decision_reason = reasons
        return decision, reasons

    async def run_with_auto_action(
        self,
        on_promote: Callable,
        on_rollback: Callable,
        poll_interval_seconds: int = 60,
    ):
        """Poll until enough samples, then auto-act."""
        while True:
            decision, reasons = self.evaluate()
            print(f"[canary] {decision.value}: {reasons}")
            if decision == CanaryDecision.PROMOTE:
                await on_promote(reasons)
                return
            elif decision == CanaryDecision.ROLLBACK:
                await on_rollback(reasons)
                return
            await asyncio.sleep(poll_interval_seconds)
```

---

## Solution 5: Shadow Mode Canary with Quality Judge

For model quality regressions (not just latency/cost), use a shadow mode that runs both models on every request and compares output quality with an LLM judge.

```python
import anthropic
import asyncio


@dataclass
class QualityJudgement:
    request_id: str
    control_wins: bool
    treatment_wins: bool
    tie: bool
    explanation: str
    judge_confidence: float  # 0-1


class ShadowModeQualityCanary:
    """
    Shadow mode: every request goes to both control and treatment.
    An LLM judge (Haiku) scores which response is better.
    Accumulate win rates to make promotion decisions.
    """

    JUDGE_PROMPT = """You are a neutral evaluator comparing two AI responses to the same question.

Request: {request}

Response A: {response_a}

Response B: {response_b}

Which response is better? Consider: accuracy, completeness, clarity, conciseness.
Reply with JSON: {{"winner": "A" | "B" | "tie", "confidence": 0.0-1.0, "reason": "..."}}"""

    def __init__(self, control_model: str, treatment_model: str,
                 judge_model: str = "claude-haiku-4-5-20251001",
                 min_judgements: int = 50):
        self.control_model = control_model
        self.treatment_model = treatment_model
        self.judge_model = judge_model
        self.min_judgements = min_judgements
        self._judgements: list[QualityJudgement] = []
        self._client = anthropic.AsyncAnthropic()

    async def _call_model(self, model: str, messages: list[dict]) -> str:
        resp = await self._client.messages.create(
            model=model, max_tokens=1024, messages=messages
        )
        return resp.content[0].text

    async def _judge(self, request: str, control_resp: str,
                     treatment_resp: str) -> QualityJudgement | None:
        """Use Haiku to judge which response is better (blind to which is which)."""
        import random
        import json

        # Randomly assign A/B to avoid position bias
        flip = random.random() < 0.5
        resp_a = control_resp if not flip else treatment_resp
        resp_b = treatment_resp if not flip else control_resp

        prompt = self.JUDGE_PROMPT.format(
            request=request[:500], response_a=resp_a[:800], response_b=resp_b[:800]
        )
        try:
            judge_resp = await self._client.messages.create(
                model=self.judge_model,
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}],
            )
            data = json.loads(judge_resp.content[0].text)
            winner = data.get("winner", "tie")
            # Unflip
            if flip:
                if winner == "A":
                    winner = "B"
                elif winner == "B":
                    winner = "A"
            return QualityJudgement(
                request_id=str(id(request)),
                control_wins=(winner == "A"),
                treatment_wins=(winner == "B"),
                tie=(winner == "tie"),
                explanation=data.get("reason", ""),
                judge_confidence=float(data.get("confidence", 0.5)),
            )
        except Exception:
            return None

    async def evaluate_request(self, messages: list[dict]) -> dict:
        request_text = str(messages[-1].get("content", ""))
        control_resp, treatment_resp = await asyncio.gather(
            self._call_model(self.control_model, messages),
            self._call_model(self.treatment_model, messages),
        )
        judgement = await self._judge(request_text, control_resp, treatment_resp)
        if judgement:
            self._judgements.append(judgement)
        return {
            "control_response": control_resp,
            "treatment_response": treatment_resp,
            "judgement": judgement,
        }

    def win_rates(self) -> dict:
        if not self._judgements:
            return {}
        n = len(self._judgements)
        ctrl_wins = sum(1 for j in self._judgements if j.control_wins)
        trt_wins = sum(1 for j in self._judgements if j.treatment_wins)
        ties = sum(1 for j in self._judgements if j.tie)
        return {
            "total_judgements": n,
            "control_win_rate": ctrl_wins / n,
            "treatment_win_rate": trt_wins / n,
            "tie_rate": ties / n,
            "treatment_relative_win_rate": trt_wins / (ctrl_wins + trt_wins) if (ctrl_wins + trt_wins) else 0.5,
        }

    def should_promote(self) -> tuple[bool, str]:
        rates = self.win_rates()
        if rates.get("total_judgements", 0) < self.min_judgements:
            return False, "insufficient_judgements"
        # Promote if treatment wins at least 45% of contested (non-tie) judgements
        rel = rates["treatment_relative_win_rate"]
        if rel >= 0.45:
            return True, f"quality_acceptable (rel_win_rate={rel:.2f})"
        return False, f"quality_regression (rel_win_rate={rel:.2f} < 0.45)"
```

---

## Solution 6: Progressive Traffic Ramp with Circuit Breaker

A full-lifecycle canary that starts at 1%, evaluates, then ramps to 5% → 10% → 25% → 50% → 100%, with a circuit breaker that halts and rolls back on any regression at any ramp stage.

```python
import asyncio
import time
from enum import Enum
from dataclasses import dataclass, field


class RampStage(Enum):
    INITIAL = (0.01, "1%")
    STAGE_1 = (0.05, "5%")
    STAGE_2 = (0.10, "10%")
    STAGE_3 = (0.25, "25%")
    STAGE_4 = (0.50, "50%")
    FULL = (1.00, "100%")

    def __init__(self, fraction: float, label: str):
        self.fraction = fraction
        self.label = label

    def next_stage(self) -> "RampStage | None":
        stages = list(RampStage)
        idx = stages.index(self)
        return stages[idx + 1] if idx + 1 < len(stages) else None


@dataclass
class RampResult:
    stage: RampStage
    status: str  # "passed", "failed", "pending"
    metrics: dict = field(default_factory=dict)
    reason: str = ""


class ProgressiveCanaryRamp:
    """
    Multi-stage traffic ramp with circuit breaker.
    Each stage requires min_samples_per_stage requests before evaluation.
    Any regression immediately halts and triggers rollback.
    """

    def __init__(
        self,
        min_samples_per_stage: int = 50,
        soak_seconds_per_stage: int = 120,
        latency_threshold_pct: float = 0.10,
        error_rate_threshold: float = 0.005,
    ):
        self.min_samples_per_stage = min_samples_per_stage
        self.soak_seconds = soak_seconds_per_stage
        self.latency_threshold = latency_threshold_pct
        self.error_threshold = error_rate_threshold

        self._current_stage = RampStage.INITIAL
        self._stage_start = time.time()
        self._stage_data: dict[str, dict[str, list[float]]] = {
            "control": defaultdict(list),
            "treatment": defaultdict(list),
        }
        self._history: list[RampResult] = []
        self._rolled_back = False
        self._fully_promoted = False

    @property
    def current_fraction(self) -> float:
        return self._current_stage.fraction

    def record(self, cohort: str, latency_ms: float, error: bool, cost_usd: float):
        if self._rolled_back:
            return
        self._stage_data[cohort]["latency"].append(latency_ms)
        self._stage_data[cohort]["error"].append(1.0 if error else 0.0)
        self._stage_data[cohort]["cost"].append(cost_usd)

    def _evaluate_stage(self) -> tuple[bool, str]:
        ctrl_lat = self._stage_data["control"]["latency"]
        trt_lat = self._stage_data["treatment"]["latency"]
        ctrl_err = self._stage_data["control"]["error"]
        trt_err = self._stage_data["treatment"]["error"]

        if len(trt_lat) < self.min_samples_per_stage:
            return True, "pending"  # Not enough data yet, continue

        elapsed = time.time() - self._stage_start
        if elapsed < self.soak_seconds:
            return True, "soaking"

        # Latency check
        if ctrl_lat and trt_lat:
            ctrl_p95 = sorted(ctrl_lat)[int(len(ctrl_lat) * 0.95)]
            trt_p95 = sorted(trt_lat)[int(len(trt_lat) * 0.95)]
            if ctrl_p95 > 0:
                lat_delta = (trt_p95 - ctrl_p95) / ctrl_p95
                if lat_delta > self.latency_threshold:
                    return False, f"latency_regression: p95 +{lat_delta*100:.1f}%"

        # Error rate check
        if ctrl_err and trt_err:
            ctrl_err_rate = sum(ctrl_err) / len(ctrl_err)
            trt_err_rate = sum(trt_err) / len(trt_err)
            if trt_err_rate - ctrl_err_rate > self.error_threshold:
                return False, f"error_rate_regression: +{(trt_err_rate - ctrl_err_rate)*100:.2f}pp"

        return True, "passed"

    async def tick(self, on_rollback=None, on_promote=None) -> str:
        """Call periodically. Returns current state."""
        if self._rolled_back:
            return "rolled_back"
        if self._fully_promoted:
            return "fully_promoted"

        passed, reason = self._evaluate_stage()
        if reason == "pending" or reason == "soaking":
            return f"{self._current_stage.label}:{reason}"

        self._history.append(RampResult(
            stage=self._current_stage,
            status="passed" if passed else "failed",
            reason=reason,
        ))

        if not passed:
            self._rolled_back = True
            print(f"[canary] ROLLBACK at {self._current_stage.label}: {reason}")
            if on_rollback:
                await on_rollback(reason, self._current_stage)
            return "rolled_back"

        # Stage passed — advance
        next_stage = self._current_stage.next_stage()
        if next_stage is None:
            self._fully_promoted = True
            print(f"[canary] PROMOTED: all stages passed")
            if on_promote:
                await on_promote(self._history)
            return "fully_promoted"

        print(f"[canary] Stage {self._current_stage.label} passed → advancing to {next_stage.label}")
        self._current_stage = next_stage
        self._stage_start = time.time()
        # Reset per-stage data
        self._stage_data = {"control": defaultdict(list), "treatment": defaultdict(list)}
        return f"advanced_to_{next_stage.label}"
```

---

## Comparison

| Pattern | Latency Detection | Quality Detection | Auto-Decision | Best For |
|---|---|---|---|---|
| Basic metric collection | Yes (p50/p95/p99) | No | No | Manual review dashboards |
| Mann-Whitney significance | Yes (statistically sound) | No | Yes | Noisy traffic with skewed distributions |
| Sliding window + Prometheus | Yes (rolling 5min) | No | Partial (watch loop) | Long-running services with Grafana |
| Weighted scorecard | Yes (multi-metric) | No | Yes (auto promote/rollback) | Multi-criteria deployments with different weights |
| Shadow mode quality judge | Latency (secondary) | Yes (LLM judge) | Yes | Model swaps where output quality matters most |
| Progressive ramp + circuit breaker | Yes (per stage) | No | Yes (full lifecycle) | Production rollouts requiring staged confidence |

**Recommendations:**
- Start with **progressive ramp** (Solution 6) for any model or prompt change going to production.
- Add **shadow quality judge** (Solution 5) when swapping model versions where output quality is the primary concern.
- Use **Mann-Whitney** (Solution 2) when traffic is low and statistical rigor matters more than speed of decision.
- Export **sliding window Prometheus** metrics (Solution 3) to Grafana so on-call engineers can see canary state in real time.
- The **scorecard** (Solution 4) is the right abstraction when different metrics have different business importance (e.g., cost matters 3× more than latency for a batch processing agent).
