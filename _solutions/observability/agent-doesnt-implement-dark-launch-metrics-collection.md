---
title: "Agent Doesn't Implement Dark Launch Metrics Collection for Shadow Traffic Comparison"
description: "AI agents that deploy prompt changes or new model versions without a shadow comparison period risk introducing regressions that only appear at production traffic scale. Dark launch runs the new agent version on a copy of live traffic in parallel with the production version, collecting response quality, latency, and tool call pattern metrics for both, enabling a side-by-side comparison before any traffic is switched over."
date: 2025-02-22
difficulty: advanced
category: observability
slug: agent-doesnt-implement-dark-launch-metrics-collection
tags:
  - dark-launch
  - shadow-traffic
  - a-b-testing
  - deployment
  - observability
  - metrics-comparison
  - canary
symptoms:
  - "New prompt deployed directly to production — first sign of regression is user complaints"
  - "No baseline to compare new model version against current before switching traffic"
  - "Latency difference between old and new agent versions only discovered post-deployment"
  - "Tool call patterns changed significantly with new prompt but went undetected pre-launch"
  - "Quality A/B test requires splitting live user traffic — dark launch would be safer"
---

## Problem

Deploying a new agent version—changed system prompt, different model, updated tool descriptions—without a shadow comparison period means regressions are discovered only after real users are affected. Shadow traffic (dark launch) duplicates every incoming request and sends it to both the current (primary) and new (shadow) agent versions in parallel, discarding the shadow response from the user's perspective but collecting its metrics—latency, token usage, tool call sequence, output length, semantic similarity to primary—for comparison. When shadow metrics show no regression, the traffic switch happens with high confidence. When they show a regression, the new version is rolled back before any user sees it.

---

## Solution 1: ShadowRequestRouter — Duplicate Traffic to Shadow Agent

```python
import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class ShadowResult:
    primary_response: Any
    shadow_response: Optional[Any]
    primary_latency_ms: float
    shadow_latency_ms: Optional[float]
    shadow_error: Optional[str] = None
    request_id: str = ""


class ShadowRequestRouter:
    """
    Routes each request to both a primary and shadow agent handler.
    The primary result is returned to the caller immediately.
    The shadow is executed concurrently with a timeout; its response
    is discarded from the caller's perspective but recorded for metrics.

    Usage:
        router = ShadowRequestRouter(
            primary_fn=current_agent.run,
            shadow_fn=new_agent.run,
            shadow_timeout=30.0,
            sample_rate=1.0,       # 1.0 = shadow all traffic
        )
        result = await router.route(messages, session_id="sess-001")
        # User receives primary_response; shadow metrics collected in background
    """

    def __init__(
        self,
        primary_fn: Callable,
        shadow_fn: Callable,
        shadow_timeout: float = 30.0,
        sample_rate: float = 1.0,
        metrics_sink: Optional[Callable] = None,
    ):
        self._primary = primary_fn
        self._shadow = shadow_fn
        self._timeout = shadow_timeout
        self._sample_rate = sample_rate
        self._metrics_sink = metrics_sink
        self._results: list = []

    def _should_shadow(self) -> bool:
        import random
        return random.random() < self._sample_rate

    async def route(self, *args, request_id: str = "", **kwargs) -> Any:
        if not self._should_shadow():
            return await self._primary(*args, **kwargs) \
                if asyncio.iscoroutinefunction(self._primary) \
                else self._primary(*args, **kwargs)

        async def run_primary():
            t0 = time.monotonic()
            resp = await self._primary(*args, **kwargs) \
                if asyncio.iscoroutinefunction(self._primary) \
                else self._primary(*args, **kwargs)
            return resp, round((time.monotonic() - t0) * 1000, 1)

        async def run_shadow():
            t0 = time.monotonic()
            try:
                resp = await asyncio.wait_for(
                    (self._shadow(*args, **kwargs)
                     if asyncio.iscoroutinefunction(self._shadow)
                     else asyncio.coroutine(self._shadow)(*args, **kwargs)),
                    timeout=self._timeout,
                )
                return resp, round((time.monotonic() - t0) * 1000, 1), None
            except asyncio.TimeoutError:
                return None, round((time.monotonic() - t0) * 1000, 1), "timeout"
            except Exception as exc:
                return None, round((time.monotonic() - t0) * 1000, 1), str(exc)

        (primary_resp, primary_ms), (shadow_resp, shadow_ms, shadow_err) = \
            await asyncio.gather(run_primary(), run_shadow())

        result = ShadowResult(
            primary_response=primary_resp,
            shadow_response=shadow_resp,
            primary_latency_ms=primary_ms,
            shadow_latency_ms=shadow_ms,
            shadow_error=shadow_err,
            request_id=request_id,
        )
        self._results.append(result)
        if self._metrics_sink:
            try:
                await self._metrics_sink(result) \
                    if asyncio.iscoroutinefunction(self._metrics_sink) \
                    else self._metrics_sink(result)
            except Exception:
                pass

        return primary_resp
```

---

## Solution 2: DarkLaunchMetricsCollector — Per-Request Comparison Recording

```python
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ComparisonRecord:
    request_id: str
    ts: float
    primary_latency_ms: float
    shadow_latency_ms: Optional[float]
    primary_token_count: int
    shadow_token_count: int
    primary_tool_count: int
    shadow_tool_count: int
    output_length_delta: int      # shadow - primary (positive = shadow longer)
    semantic_similarity: float    # -1 if not computed
    shadow_error: Optional[str]


class DarkLaunchMetricsCollector:
    """
    Records per-request comparison metrics between primary and shadow
    agent responses. Computes semantic similarity between outputs using
    a lightweight embedding model to catch qualitative regressions.

    Usage:
        collector = DarkLaunchMetricsCollector(
            compute_similarity=True,
            max_records=10_000,
        )
        await collector.record(shadow_result, primary_meta, shadow_meta)
        report = collector.comparison_report()
    """

    def __init__(self, compute_similarity: bool = False, max_records: int = 10_000):
        self._compute_sim = compute_similarity
        self._max = max_records
        self._records: List[ComparisonRecord] = []

    def _similarity(self, text_a: str, text_b: str) -> float:
        if not self._compute_sim:
            return -1.0
        try:
            from sentence_transformers import SentenceTransformer
            import numpy as np
            model = SentenceTransformer("all-MiniLM-L6-v2")
            vecs = model.encode([text_a[:512], text_b[:512]])
            return float(np.dot(vecs[0], vecs[1]) /
                          (np.linalg.norm(vecs[0]) * np.linalg.norm(vecs[1]) + 1e-9))
        except Exception:
            return -1.0

    def record(
        self,
        result: ShadowResult,
        primary_tokens: int = 0,
        shadow_tokens: int = 0,
        primary_tools: int = 0,
        shadow_tools: int = 0,
    ):
        p_text = str(result.primary_response or "")
        s_text = str(result.shadow_response or "")
        sim = self._similarity(p_text, s_text) if not result.shadow_error else -1.0

        rec = ComparisonRecord(
            request_id=result.request_id,
            ts=time.time(),
            primary_latency_ms=result.primary_latency_ms,
            shadow_latency_ms=result.shadow_latency_ms,
            primary_token_count=primary_tokens,
            shadow_token_count=shadow_tokens,
            primary_tool_count=primary_tools,
            shadow_tool_count=shadow_tools,
            output_length_delta=len(s_text) - len(p_text),
            semantic_similarity=sim,
            shadow_error=result.shadow_error,
        )
        if len(self._records) >= self._max:
            self._records.pop(0)
        self._records.append(rec)
        logger.info(
            "dark_launch_record request_id=%s p_ms=%.0f s_ms=%s sim=%.3f error=%s",
            result.request_id, result.primary_latency_ms,
            f"{result.shadow_latency_ms:.0f}" if result.shadow_latency_ms else "N/A",
            sim, result.shadow_error or "none",
        )

    def comparison_report(self) -> Dict[str, Any]:
        records = self._records
        if not records:
            return {"total": 0}

        valid = [r for r in records if r.shadow_latency_ms is not None and not r.shadow_error]
        error_count = sum(1 for r in records if r.shadow_error)

        def mean(vals):
            return round(sum(vals) / len(vals), 2) if vals else 0

        def pct_delta(primary_vals, shadow_vals):
            p = mean(primary_vals)
            s = mean(shadow_vals)
            return round((s - p) / max(abs(p), 1) * 100, 1) if p else 0

        p_latencies = [r.primary_latency_ms for r in valid]
        s_latencies = [r.shadow_latency_ms for r in valid]
        sims = [r.semantic_similarity for r in valid if r.semantic_similarity >= 0]

        return {
            "total_requests": len(records),
            "valid_comparisons": len(valid),
            "shadow_error_count": error_count,
            "shadow_error_rate_pct": round(error_count / max(len(records), 1) * 100, 1),
            "latency": {
                "primary_mean_ms": mean(p_latencies),
                "shadow_mean_ms": mean(s_latencies),
                "delta_pct": pct_delta(p_latencies, s_latencies),
            },
            "tokens": {
                "primary_mean": mean([r.primary_token_count for r in valid]),
                "shadow_mean": mean([r.shadow_token_count for r in valid]),
            },
            "tools": {
                "primary_mean": mean([r.primary_tool_count for r in valid]),
                "shadow_mean": mean([r.shadow_tool_count for r in valid]),
            },
            "semantic_similarity": {
                "mean": mean(sims),
                "min": round(min(sims), 3) if sims else -1,
            },
            "output_length_delta_mean": mean([r.output_length_delta for r in valid]),
        }
```

---

## Solution 3: DarkLaunchDecisionGate — Auto-Promote or Rollback Based on Metrics

```python
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class PromotionThresholds:
    max_latency_increase_pct: float = 15.0      # shadow can be ≤15% slower
    min_semantic_similarity: float = 0.85        # outputs must be ≥85% similar
    max_shadow_error_rate_pct: float = 2.0       # ≤2% shadow errors
    max_token_increase_pct: float = 20.0         # shadow can use ≤20% more tokens
    min_valid_comparisons: int = 100             # need ≥100 samples before deciding


@dataclass
class GateDecision:
    promote: bool
    reasons: List[str]
    report: Dict

    def __str__(self):
        action = "PROMOTE" if self.promote else "ROLLBACK"
        return f"{action}: " + "; ".join(self.reasons) if self.reasons else action


class DarkLaunchDecisionGate:
    """
    Evaluates a DarkLaunchMetricsCollector report against configured
    promotion thresholds and returns a promote/rollback decision with
    reasons. Designed for use in CI/CD deployment pipelines or as a
    periodic background check during the dark launch window.

    Usage:
        gate = DarkLaunchDecisionGate(PromotionThresholds(min_semantic_similarity=0.90))
        decision = gate.evaluate(collector.comparison_report())
        if not decision.promote:
            rollback_shadow_deployment()
    """

    def __init__(self, thresholds: Optional[PromotionThresholds] = None):
        self._t = thresholds or PromotionThresholds()

    def evaluate(self, report: Dict) -> GateDecision:
        reasons = []
        promote = True

        total = report.get("total_requests", 0)
        valid = report.get("valid_comparisons", 0)

        if valid < self._t.min_valid_comparisons:
            return GateDecision(
                promote=False,
                reasons=[f"Insufficient data: {valid} comparisons (need {self._t.min_valid_comparisons})"],
                report=report,
            )

        error_rate = report.get("shadow_error_rate_pct", 0)
        if error_rate > self._t.max_shadow_error_rate_pct:
            reasons.append(f"Shadow error rate {error_rate}% > {self._t.max_shadow_error_rate_pct}%")
            promote = False

        lat = report.get("latency", {})
        lat_delta = lat.get("delta_pct", 0)
        if lat_delta > self._t.max_latency_increase_pct:
            reasons.append(f"Shadow latency +{lat_delta}% > {self._t.max_latency_increase_pct}%")
            promote = False

        sim = report.get("semantic_similarity", {}).get("mean", -1)
        if 0 <= sim < self._t.min_semantic_similarity:
            reasons.append(f"Semantic similarity {sim:.3f} < {self._t.min_semantic_similarity}")
            promote = False

        tok = report.get("tokens", {})
        p_tok = tok.get("primary_mean", 0)
        s_tok = tok.get("shadow_mean", 0)
        if p_tok > 0:
            tok_delta_pct = (s_tok - p_tok) / p_tok * 100
            if tok_delta_pct > self._t.max_token_increase_pct:
                reasons.append(f"Shadow tokens +{tok_delta_pct:.1f}% > {self._t.max_token_increase_pct}%")
                promote = False

        if promote:
            reasons.append(f"All {valid} comparisons within thresholds")

        decision = GateDecision(promote=promote, reasons=reasons, report=report)
        logger.info("dark_launch_decision promote=%s reasons=%s", promote, reasons)
        return decision
```

---

## Solution 4: TrafficSplitController — Graduated Shadow → Canary → Full Rollout

```python
import asyncio
import logging
import time
from enum import Enum
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


class RolloutStage(str, Enum):
    DARK = "dark"           # 0% real traffic; 100% shadow
    CANARY_1 = "canary_1"   # 1% real traffic
    CANARY_10 = "canary_10" # 10% real traffic
    FULL = "full"           # 100% real traffic


class TrafficSplitController:
    """
    Controls graduated rollout from dark launch to full traffic.
    Starts in DARK mode (shadow only), then advances through canary
    stages based on operator approval or automatic metric gate checks.

    Usage:
        controller = TrafficSplitController(
            primary_fn=v1_agent.run,
            shadow_fn=v2_agent.run,
            gate=DarkLaunchDecisionGate(),
            collector=metrics_collector,
        )
        controller.start()  # DARK stage
        # After sufficient shadow data:
        controller.advance()  # -> CANARY_1 if gate passes
    """

    STAGE_WEIGHTS = {
        RolloutStage.DARK: 0.0,
        RolloutStage.CANARY_1: 0.01,
        RolloutStage.CANARY_10: 0.10,
        RolloutStage.FULL: 1.0,
    }

    def __init__(
        self,
        primary_fn: Callable,
        shadow_fn: Callable,
        gate: Optional[DarkLaunchDecisionGate] = None,
        collector: Optional[DarkLaunchMetricsCollector] = None,
    ):
        self._primary = primary_fn
        self._shadow = shadow_fn
        self._gate = gate
        self._collector = collector
        self._stage = RolloutStage.DARK
        self._stage_started = time.time()

    def start(self):
        self._stage = RolloutStage.DARK
        self._stage_started = time.time()
        logger.info("rollout_started stage=%s", self._stage.value)

    def advance(self, force: bool = False) -> bool:
        if self._stage == RolloutStage.FULL:
            return True

        if not force and self._gate and self._collector:
            report = self._collector.comparison_report()
            decision = self._gate.evaluate(report)
            if not decision.promote:
                logger.warning("rollout_advance_blocked reason=%s", decision.reasons)
                return False

        stages = list(RolloutStage)
        idx = stages.index(self._stage)
        if idx + 1 < len(stages):
            self._stage = stages[idx + 1]
            self._stage_started = time.time()
            logger.info("rollout_advanced stage=%s", self._stage.value)
        return True

    def rollback(self):
        self._stage = RolloutStage.DARK
        self._stage_started = time.time()
        logger.warning("rollout_rolled_back stage=%s", self._stage.value)

    async def route(self, *args, request_id: str = "", **kwargs) -> Any:
        import random
        weight = self.STAGE_WEIGHTS[self._stage]
        use_shadow = random.random() < weight

        if self._stage == RolloutStage.DARK or not use_shadow:
            # Run primary; also run shadow for metrics collection
            router = ShadowRequestRouter(
                primary_fn=self._primary,
                shadow_fn=self._shadow,
                sample_rate=1.0 if self._stage == RolloutStage.DARK else 0.0,
                metrics_sink=self._collector.record if self._collector else None,
            )
            return await router.route(*args, request_id=request_id, **kwargs)
        else:
            # Send this request to shadow (v2) as real traffic
            return await self._shadow(*args, **kwargs) \
                if asyncio.iscoroutinefunction(self._shadow) \
                else self._shadow(*args, **kwargs)

    @property
    def status(self) -> Dict:
        return {
            "stage": self._stage.value,
            "shadow_traffic_weight": self.STAGE_WEIGHTS[self._stage],
            "stage_age_seconds": round(time.time() - self._stage_started, 0),
        }
```

---

## Solution 5: ToolCallPatternComparator — Detect Behavioral Changes in Tool Usage

```python
import logging
from collections import Counter
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ToolCallPatternComparator:
    """
    Compares the sequence and frequency of tool calls between primary
    and shadow agent responses. A new agent version that calls different
    tools—or calls them in a different order—may indicate behavioral
    regression even if the final text output looks similar.

    Usage:
        comparator = ToolCallPatternComparator()
        comparator.record(
            primary_tools=["web_search", "summarize"],
            shadow_tools=["web_search", "web_search", "summarize"],
        )
        print(comparator.divergence_report())
    """

    def __init__(self):
        self._primary_sequences: List[List[str]] = []
        self._shadow_sequences: List[List[str]] = []

    def record(self, primary_tools: List[str], shadow_tools: List[str]):
        self._primary_sequences.append(primary_tools)
        self._shadow_sequences.append(shadow_tools)

    def divergence_report(self) -> Dict[str, Any]:
        if not self._primary_sequences:
            return {"total": 0}

        p_counts = Counter(t for seq in self._primary_sequences for t in seq)
        s_counts = Counter(t for seq in self._shadow_sequences for t in seq)
        all_tools = set(p_counts) | set(s_counts)

        p_total = sum(p_counts.values())
        s_total = sum(s_counts.values())

        tool_deltas = {}
        for tool in all_tools:
            p_freq = p_counts.get(tool, 0) / max(len(self._primary_sequences), 1)
            s_freq = s_counts.get(tool, 0) / max(len(self._shadow_sequences), 1)
            delta = s_freq - p_freq
            if abs(delta) > 0.05:  # report only significant deltas
                tool_deltas[tool] = {
                    "primary_freq": round(p_freq, 3),
                    "shadow_freq": round(s_freq, 3),
                    "delta": round(delta, 3),
                }

        p_len = sum(len(s) for s in self._primary_sequences) / max(len(self._primary_sequences), 1)
        s_len = sum(len(s) for s in self._shadow_sequences) / max(len(self._shadow_sequences), 1)

        return {
            "total_comparisons": len(self._primary_sequences),
            "mean_primary_tools_per_turn": round(p_len, 2),
            "mean_shadow_tools_per_turn": round(s_len, 2),
            "tool_frequency_deltas": tool_deltas,
            "significant_divergences": len(tool_deltas),
        }
```

---

## Solution 6: DarkLaunchDashboard — Operator View of Shadow vs. Primary

```python
import time
from typing import Any, Dict, Optional


class DarkLaunchDashboard:
    """
    Aggregates all dark launch observability components into a single
    dashboard payload suitable for a health endpoint or Grafana JSON
    datasource. Provides an at-a-glance view of whether the shadow
    version is safe to promote.

    Usage:
        dashboard = DarkLaunchDashboard(
            controller=traffic_controller,
            collector=metrics_collector,
            gate=decision_gate,
            comparator=tool_comparator,
        )
        data = dashboard.render()
        # Serve at: GET /internal/dark-launch-status
    """

    def __init__(
        self,
        controller: TrafficSplitController,
        collector: DarkLaunchMetricsCollector,
        gate: DarkLaunchDecisionGate,
        comparator: Optional[ToolCallPatternComparator] = None,
    ):
        self._controller = controller
        self._collector = collector
        self._gate = gate
        self._comparator = comparator

    def render(self) -> Dict[str, Any]:
        report = self._collector.comparison_report()
        decision = self._gate.evaluate(report)
        tool_div = self._comparator.divergence_report() if self._comparator else {}

        return {
            "generated_at": time.time(),
            "rollout_status": self._controller.status,
            "gate_decision": {
                "promote": decision.promote,
                "reasons": decision.reasons,
            },
            "metrics_comparison": report,
            "tool_divergence": tool_div,
            "recommendation": (
                "Safe to advance rollout stage"
                if decision.promote
                else "Hold — investigate regressions before advancing"
            ),
        }
```

---

## Comparison

| Approach | Shadow Routing | Metric Collection | Gate Decision | Graduated Rollout | Tool Pattern | Dashboard |
|---|---|---|---|---|---|---|
| **ShadowRequestRouter** | Yes | No | No | No | No | No |
| **DarkLaunchMetricsCollector** | No | Yes | No | No | No | No |
| **DarkLaunchDecisionGate** | No | No | Yes | No | No | No |
| **TrafficSplitController** | Yes | Via collector | Via gate | Yes | No | No |
| **ToolCallPatternComparator** | No | Yes (tools) | No | No | Yes | No |
| **DarkLaunchDashboard** | No | No | No | No | No | Yes |

**Key insight**: the minimum viable dark launch is `ShadowRequestRouter` wrapping the agent entrypoint with `sample_rate=0.1` (10% shadow) for 24 hours before any deployment. This gives 10% of traffic as shadow data with zero user impact. Monitor `DarkLaunchMetricsCollector.comparison_report()` for the three critical signals: latency delta >15%, semantic similarity <0.85, and shadow error rate >2%—any of these warrants a rollback. Use `ToolCallPatternComparator` to catch behavioral regressions where the final text looks similar but the tool call sequence changed dramatically, which often indicates prompt drift or capability degradation in the new model version.
