---
title: "Agent Doesn't Implement Reasoning Chain Step Count Monitoring"
description: "Agents that execute multi-step reasoning chains without tracking step counts cannot detect runaway loops, unexpectedly shallow reasoning, or regressions in chain depth after prompt changes. Implement reasoning chain step count monitoring that records per-chain step counts, detects depth anomalies, and alerts when chains exceed or fall below expected bounds."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-reasoning-chain-step-count-monitoring
tags: [reasoning-chain, step-count, loop-detection, chain-depth, anomaly-detection, agent-observability]
symptoms:
  - "Agent loops indefinitely — no maximum step count enforced or measured"
  - "Reasoning chain depth drops from 8 steps to 2 after a prompt change with no alert"
  - "No histogram of reasoning chain lengths to establish normal depth distribution"
  - "P99 step count unknown — cannot set a meaningful hard limit"
  - "Runaway chains consume unbounded tokens before timing out"
---

## Why This Happens

Reasoning chains — sequences of think/act/observe cycles — are measured by output tokens and wall-clock latency, but rarely by step count. Step count is a distinct signal: a chain that completes in 2 steps when 8 are expected indicates shallow reasoning or an early exit bug; a chain at 50 steps when 10 is normal indicates a loop or a prompt regression. Without per-chain step count recording, neither condition is detectable from latency or token metrics alone. Step count monitoring requires an explicit counter incremented at each reasoning step, a per-chain record, and an anomaly check against a rolling baseline.

## Solution 1: Reasoning Step Record

```python
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ReasoningStep:
    step_index: int
    step_type: str        # "think" | "act" | "observe" | "plan" | "reflect"
    started_at: float
    ended_at: Optional[float] = None
    tool_called: Optional[str] = None
    token_count: Optional[int] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def duration_ms(self) -> Optional[float]:
        if self.ended_at is None:
            return None
        return round((self.ended_at - self.started_at) * 1000, 2)


@dataclass
class ReasoningChainRecord:
    chain_id: str
    session_id: str
    started_at: float = field(default_factory=time.time)
    ended_at: Optional[float] = None
    steps: List[ReasoningStep] = field(default_factory=list)
    terminated_reason: str = ""   # "complete" | "max_steps" | "error" | "timeout"

    @property
    def step_count(self) -> int:
        return len(self.steps)

    @property
    def duration_ms(self) -> Optional[float]:
        if self.ended_at is None:
            return None
        return round((self.ended_at - self.started_at) * 1000, 2)
```

## Solution 2: Reasoning Chain Step Counter

```python
import time
import uuid
from typing import Optional


class ReasoningChainStepCounter:
    """
    Tracks steps within a single reasoning chain execution.
    Enforces a maximum step limit and records each step's type and duration.
    """

    def __init__(
        self,
        session_id: str,
        max_steps: int = 50,
        chain_id: Optional[str] = None,
    ):
        self._max_steps = max_steps
        self._record = ReasoningChainRecord(
            chain_id=chain_id or str(uuid.uuid4())[:8],
            session_id=session_id,
        )
        self._active_step: Optional[ReasoningStep] = None

    def begin_step(self, step_type: str, tool_called: Optional[str] = None) -> int:
        if self._record.step_count >= self._max_steps:
            raise StepLimitExceededError(
                chain_id=self._record.chain_id,
                limit=self._max_steps,
            )
        step = ReasoningStep(
            step_index=self._record.step_count,
            step_type=step_type,
            started_at=time.time(),
            tool_called=tool_called,
        )
        self._active_step = step
        return step.step_index

    def end_step(self, token_count: Optional[int] = None, **metadata) -> ReasoningStep:
        if self._active_step is None:
            raise RuntimeError("end_step called without begin_step")
        self._active_step.ended_at = time.time()
        self._active_step.token_count = token_count
        self._active_step.metadata.update(metadata)
        self._record.steps.append(self._active_step)
        completed = self._active_step
        self._active_step = None
        return completed

    def finish(self, reason: str = "complete") -> ReasoningChainRecord:
        self._record.ended_at = time.time()
        self._record.terminated_reason = reason
        return self._record

    @property
    def current_step_count(self) -> int:
        return self._record.step_count

    @property
    def chain_id(self) -> str:
        return self._record.chain_id


class StepLimitExceededError(Exception):
    def __init__(self, chain_id: str, limit: int):
        super().__init__(f"Reasoning chain '{chain_id}' exceeded {limit} steps")
        self.chain_id = chain_id
        self.limit = limit
```

## Solution 3: Chain Depth Distribution Tracker

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, List, Optional, Tuple


class ChainDepthDistributionTracker:
    """
    Accumulates step counts from completed reasoning chains.
    Supports percentile queries to establish normal depth baselines.
    """

    def __init__(self, max_records: int = 10000):
        self._records: Deque[Tuple[float, int, str]] = deque()
        # (recorded_at, step_count, terminated_reason)
        self._max = max_records
        self._lock = Lock()

    def record(self, chain: ReasoningChainRecord) -> None:
        with self._lock:
            self._records.append((
                time.time(),
                chain.step_count,
                chain.terminated_reason,
            ))
            if len(self._records) > self._max:
                self._records.popleft()

    def percentile(self, pct: float, window_seconds: float = 3600.0) -> Optional[float]:
        cutoff = time.time() - window_seconds
        with self._lock:
            values = sorted(
                count for ts, count, _ in self._records if ts >= cutoff
            )
        if not values:
            return None
        idx = min(int(len(values) * pct / 100.0), len(values) - 1)
        return float(values[idx])

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [(ts, c, r) for ts, c, r in self._records if ts >= cutoff]

        if not recent:
            return {"window_seconds": window_seconds, "chains": 0}

        counts = [c for _, c, _ in recent]
        reasons = {}
        for _, _, r in recent:
            reasons[r] = reasons.get(r, 0) + 1

        return {
            "window_seconds": window_seconds,
            "chains": len(recent),
            "mean_steps": round(sum(counts) / len(counts), 2),
            "min_steps": min(counts),
            "max_steps": max(counts),
            "p50_steps": self.percentile(50, window_seconds),
            "p95_steps": self.percentile(95, window_seconds),
            "p99_steps": self.percentile(99, window_seconds),
            "termination_reasons": reasons,
        }
```

## Solution 4: Chain Depth Anomaly Detector

```python
from typing import Optional


class ChainDepthAnomalyDetector:
    """
    Compares a completed chain's step count against the rolling baseline
    (P5 and P95) to detect unusually shallow or deep chains.
    """

    def __init__(
        self,
        tracker: ChainDepthDistributionTracker,
        shallow_threshold_pct: float = 5.0,   # below P5 = too shallow
        deep_threshold_pct: float = 95.0,     # above P95 = too deep
        min_samples: int = 20,
    ):
        self._tracker = tracker
        self._shallow_pct = shallow_threshold_pct
        self._deep_pct = deep_threshold_pct
        self._min_samples = min_samples

    def check(self, chain: ReasoningChainRecord) -> dict:
        summary = self._tracker.summary()
        chain_count = summary.get("chains", 0)

        if chain_count < self._min_samples:
            return {
                "anomaly": False,
                "reason": "insufficient_baseline",
                "chain_id": chain.chain_id,
                "step_count": chain.step_count,
            }

        p_shallow = self._tracker.percentile(self._shallow_pct)
        p_deep = self._tracker.percentile(self._deep_pct)
        steps = chain.step_count

        if p_shallow is not None and steps < p_shallow:
            return {
                "anomaly": True,
                "anomaly_type": "shallow",
                "chain_id": chain.chain_id,
                "step_count": steps,
                "threshold": p_shallow,
                "message": f"Chain completed in {steps} steps — below P{self._shallow_pct} ({p_shallow})",
            }
        if p_deep is not None and steps > p_deep:
            return {
                "anomaly": True,
                "anomaly_type": "deep",
                "chain_id": chain.chain_id,
                "step_count": steps,
                "threshold": p_deep,
                "message": f"Chain completed in {steps} steps — above P{self._deep_pct} ({p_deep})",
            }
        return {
            "anomaly": False,
            "chain_id": chain.chain_id,
            "step_count": steps,
        }
```

## Solution 5: Instrumented Reasoning Chain Executor

```python
import time
from typing import Any, AsyncIterator, Callable, Optional


class InstrumentedReasoningChainExecutor:
    """
    Wraps a reasoning loop with step counting, anomaly detection,
    and chain record emission on completion.
    """

    def __init__(
        self,
        tracker: ChainDepthDistributionTracker,
        anomaly_detector: ChainDepthAnomalyDetector,
        max_steps: int = 50,
        on_complete: Optional[Callable[[ReasoningChainRecord, dict], None]] = None,
    ):
        self._tracker = tracker
        self._anomaly = anomaly_detector
        self._max_steps = max_steps
        self._on_complete = on_complete

    async def run(
        self,
        session_id: str,
        step_generator: AsyncIterator,  # yields (step_type, step_fn, tool_name)
    ) -> ReasoningChainRecord:
        counter = ReasoningChainStepCounter(
            session_id=session_id,
            max_steps=self._max_steps,
        )
        terminated_reason = "complete"

        try:
            async for step_type, step_fn, tool_name in step_generator:
                counter.begin_step(step_type, tool_called=tool_name)
                try:
                    token_count = await step_fn()
                except Exception:
                    counter.end_step()
                    terminated_reason = "error"
                    raise
                counter.end_step(token_count=token_count)
        except StepLimitExceededError:
            terminated_reason = "max_steps"

        chain = counter.finish(reason=terminated_reason)
        self._tracker.record(chain)
        anomaly = self._anomaly.check(chain)

        if self._on_complete:
            self._on_complete(chain, anomaly)

        return chain
```

## Solution 6: Reasoning Chain Step Count Dashboard

```python
import time


class ReasoningChainStepCountDashboard:
    """
    Combines chain depth distribution, anomaly rates, and termination
    reasons into a single operational snapshot.
    """

    def __init__(
        self,
        tracker: ChainDepthDistributionTracker,
        anomaly_detector: ChainDepthAnomalyDetector,
    ):
        self._tracker = tracker
        self._anomaly = anomaly_detector

    def render(self, window_seconds: float = 3600.0) -> dict:
        summary = self._tracker.summary(window_seconds)
        return {
            "generated_at": time.time(),
            "window_seconds": window_seconds,
            "depth_distribution": summary,
            "thresholds": {
                "max_steps_hard_limit": 50,
                "shallow_alert_below_p": self._anomaly._shallow_pct,
                "deep_alert_above_p": self._anomaly._deep_pct,
            },
        }
```

## Comparison

| Approach | Per-Step Timing | Step Limit | Depth Distribution | Anomaly Detection | Dashboard |
|---|---|---|---|---|---|
| ReasoningChainStepCounter | Yes | Yes (hard stop) | No | No | No |
| ChainDepthDistributionTracker | No | No | Yes (P5–P99) | No | No |
| ChainDepthAnomalyDetector | No | No | Via tracker | Yes (shallow/deep) | No |
| InstrumentedReasoningChainExecutor | Via counter | Via counter | Via tracker | Via detector | No |
| ReasoningChainStepCountDashboard | No | No | Via tracker | Via detector | Yes |

**Best for production**: Set `max_steps=50` as a hard limit and enforce it via `StepLimitExceededError` — a chain that reaches 50 steps is either looping or has a prompt defect, not a legitimate deep reasoner. Record every completed chain in `ChainDepthDistributionTracker` and alert when `termination_reasons["max_steps"]` exceeds 1% of total chains — that rate indicates a systematic prompt regression. Monitor P5 step count alongside P95: a drop in P5 (chains completing in 1–2 steps) after a prompt deployment signals that the agent is short-circuiting instead of reasoning. Use `ReasoningStep.step_type` to break down step counts by type — a spike in "observe" steps with no corresponding "act" steps identifies tool call failures that the agent is silently retrying.
