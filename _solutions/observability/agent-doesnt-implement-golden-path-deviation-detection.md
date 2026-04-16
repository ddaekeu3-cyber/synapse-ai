---
title: "Agent Doesn't Implement Golden Path Deviation Detection"
description: "Agents without a reference model for expected execution paths can't distinguish normal variability from meaningful deviations — unexpected tool call sequences, unusual response lengths, out-of-order stage completions. Implement golden path deviation detection to capture baseline execution fingerprints from successful runs, then alert when live executions diverge structurally from the expected pattern."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-golden-path-deviation-detection
tags: [golden-path, deviation-detection, execution-tracing, behavioral-baseline, anomaly-detection, observability]
symptoms:
  - "Agent starts calling unexpected tools mid-session but no alert fires until user complains"
  - "Tool call order changed after a prompt update but nobody noticed until accuracy dropped"
  - "A new code path produces correct-looking output but takes 10× more tool calls than usual"
  - "No way to distinguish a legitimate new agent behavior from a regression in tool usage patterns"
  - "Post-incident review: the deviation was visible in traces for hours before anyone noticed"
---

## Why This Happens

Without a baseline of expected execution behavior, every trace looks equally normal — there is no reference point for "something changed." Golden path deviation detection builds a structural fingerprint from known-good executions: which tools are called, in what order, with roughly how many tokens, and how long each step takes. New executions are compared against this fingerprint; deviations beyond a configurable threshold trigger alerts. This is analogous to canary analysis for deployments but applied to individual execution paths rather than service-level metrics.

## Solution 1: Execution Trace Fingerprint

```python
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

@dataclass
class ToolCallRecord:
    tool_name: str
    position: int          # call order within the execution
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    success: bool = True
    metadata: Dict[str, Any] = field(default_factory=dict)

@dataclass
class ExecutionFingerprint:
    """
    Structural summary of an agent execution.
    Used as both a baseline (golden path) and a probe (live execution).
    """
    session_id: str
    task_type: str           # groups executions for comparison
    tool_sequence: List[str] # ordered list of tool names called
    tool_call_count: int
    total_input_tokens: int
    total_output_tokens: int
    total_latency_ms: float
    unique_tools: List[str]  # sorted unique tool names
    stage_count: int = 0     # number of distinct stages (if tracked)
    success: bool = True
    timestamp: float = field(default_factory=time.time)

    def sequence_hash(self) -> str:
        """Hash of the tool call sequence for structural comparison."""
        seq = ",".join(self.tool_sequence)
        return hashlib.sha256(seq.encode()).hexdigest()[:12]

    def tool_set_hash(self) -> str:
        """Hash of the unique tool set (order-independent)."""
        tools = ",".join(sorted(self.unique_tools))
        return hashlib.sha256(tools.encode()).hexdigest()[:12]
```

## Solution 2: Execution Fingerprint Builder

```python
import time
from typing import List, Optional

class ExecutionFingerprintBuilder:
    """
    Builds an ExecutionFingerprint from a live execution trace.
    Records tool calls as they happen; call finalize() at execution end.
    """

    def __init__(self, session_id: str, task_type: str):
        self._session_id = session_id
        self._task_type = task_type
        self._records: List[ToolCallRecord] = []
        self._start_time = time.monotonic()

    def record_tool_call(
        self,
        tool_name: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        latency_ms: float = 0.0,
        success: bool = True,
    ) -> None:
        self._records.append(ToolCallRecord(
            tool_name=tool_name,
            position=len(self._records),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
            success=success,
        ))

    def finalize(self, overall_success: bool = True) -> ExecutionFingerprint:
        elapsed_ms = (time.monotonic() - self._start_time) * 1000
        tool_sequence = [r.tool_name for r in self._records]
        return ExecutionFingerprint(
            session_id=self._session_id,
            task_type=self._task_type,
            tool_sequence=tool_sequence,
            tool_call_count=len(self._records),
            total_input_tokens=sum(r.input_tokens for r in self._records),
            total_output_tokens=sum(r.output_tokens for r in self._records),
            total_latency_ms=elapsed_ms,
            unique_tools=sorted(set(tool_sequence)),
            success=overall_success,
        )
```

## Solution 3: Golden Path Registry

```python
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

@dataclass
class GoldenPathProfile:
    task_type: str
    sample_count: int = 0

    # Tool sequence statistics
    common_sequences: Dict[str, int] = field(default_factory=dict)  # hash -> count
    most_common_sequence: List[str] = field(default_factory=list)

    # Numeric statistics
    avg_tool_calls: float = 0.0
    stddev_tool_calls: float = 0.0
    avg_input_tokens: float = 0.0
    avg_output_tokens: float = 0.0
    avg_latency_ms: float = 0.0
    stddev_latency_ms: float = 0.0

    # Tool set
    expected_tool_set: List[str] = field(default_factory=list)  # tools seen in >50% of samples
    updated_at: float = field(default_factory=time.time)

class GoldenPathRegistry:
    """
    Builds and maintains baseline profiles from known-good executions.
    Profiles are updated incrementally as new successful executions are recorded.
    """

    def __init__(self, min_samples_for_baseline: int = 10):
        self._min_samples = min_samples_for_baseline
        self._profiles: Dict[str, GoldenPathProfile] = {}
        self._raw_samples: Dict[str, List[ExecutionFingerprint]] = defaultdict(list)

    def record_golden(self, fingerprint: ExecutionFingerprint) -> None:
        """Record a known-good execution to build the baseline."""
        if not fingerprint.success:
            return
        self._raw_samples[fingerprint.task_type].append(fingerprint)
        self._rebuild_profile(fingerprint.task_type)

    def _rebuild_profile(self, task_type: str) -> None:
        samples = self._raw_samples[task_type]
        if len(samples) < self._min_samples:
            return

        profile = GoldenPathProfile(task_type=task_type, sample_count=len(samples))

        # Sequence statistics
        seq_counts: Dict[str, int] = defaultdict(int)
        for s in samples:
            seq_counts[s.sequence_hash()] += 1
        profile.common_sequences = dict(seq_counts)
        most_common_hash = max(seq_counts, key=seq_counts.get)
        for s in samples:
            if s.sequence_hash() == most_common_hash:
                profile.most_common_sequence = s.tool_sequence
                break

        # Numeric statistics
        tool_counts = [s.tool_call_count for s in samples]
        latencies = [s.total_latency_ms for s in samples]
        profile.avg_tool_calls = statistics.mean(tool_counts)
        profile.stddev_tool_calls = statistics.stdev(tool_counts) if len(tool_counts) > 1 else 0
        profile.avg_input_tokens = statistics.mean(s.total_input_tokens for s in samples)
        profile.avg_output_tokens = statistics.mean(s.total_output_tokens for s in samples)
        profile.avg_latency_ms = statistics.mean(latencies)
        profile.stddev_latency_ms = statistics.stdev(latencies) if len(latencies) > 1 else 0

        # Expected tool set: tools appearing in >50% of samples
        tool_freq: Dict[str, int] = defaultdict(int)
        for s in samples:
            for tool in s.unique_tools:
                tool_freq[tool] += 1
        profile.expected_tool_set = sorted(
            t for t, count in tool_freq.items() if count / len(samples) > 0.5
        )
        profile.updated_at = time.time()
        self._profiles[task_type] = profile

    def get_profile(self, task_type: str) -> Optional[GoldenPathProfile]:
        return self._profiles.get(task_type)

    def has_baseline(self, task_type: str) -> bool:
        return task_type in self._profiles
```

## Solution 4: Deviation Scorer

```python
from dataclasses import dataclass, field
from typing import List, Optional

@dataclass
class DeviationReport:
    session_id: str
    task_type: str
    overall_deviation_score: float   # 0.0 = perfect match, 1.0 = completely different
    deviations: List[dict]
    is_anomalous: bool
    anomaly_threshold: float

class DeviationScorer:
    """
    Compares a live execution fingerprint against the golden path profile.
    Scores each dimension of deviation and computes a composite anomaly score.
    Anomalous when composite score exceeds the configured threshold.
    """

    def __init__(
        self,
        registry: GoldenPathRegistry,
        anomaly_threshold: float = 0.4,
    ):
        self._registry = registry
        self._threshold = anomaly_threshold

    def score(self, fingerprint: ExecutionFingerprint) -> DeviationReport:
        profile = self._registry.get_profile(fingerprint.task_type)
        if not profile:
            return DeviationReport(
                session_id=fingerprint.session_id,
                task_type=fingerprint.task_type,
                overall_deviation_score=0.0,
                deviations=[{"type": "no_baseline", "note": "insufficient samples for comparison"}],
                is_anomalous=False,
                anomaly_threshold=self._threshold,
            )

        deviations = []
        scores = []

        # Tool call count deviation
        if profile.stddev_tool_calls > 0:
            z = abs(fingerprint.tool_call_count - profile.avg_tool_calls) / profile.stddev_tool_calls
            if z > 2.0:
                scores.append(min(1.0, z / 5.0))
                deviations.append({
                    "type": "tool_count_anomaly",
                    "expected_avg": round(profile.avg_tool_calls, 1),
                    "observed": fingerprint.tool_call_count,
                    "z_score": round(z, 2),
                })

        # Sequence deviation: check if sequence hash matches any common pattern
        seq_hash = fingerprint.sequence_hash()
        total_samples = sum(profile.common_sequences.values())
        seq_frequency = profile.common_sequences.get(seq_hash, 0) / max(total_samples, 1)
        if seq_frequency < 0.1:   # seen in less than 10% of golden samples
            seq_score = 1.0 - seq_frequency * 10
            scores.append(seq_score)
            deviations.append({
                "type": "unusual_sequence",
                "sequence_frequency": round(seq_frequency, 4),
                "observed_sequence": fingerprint.tool_sequence[:5],
            })

        # Tool set deviation: unexpected or missing tools
        observed_tools = set(fingerprint.unique_tools)
        expected_tools = set(profile.expected_tool_set)
        unexpected = observed_tools - expected_tools
        missing = expected_tools - observed_tools
        if unexpected or missing:
            tool_deviation = (len(unexpected) + len(missing)) / max(len(expected_tools), 1)
            scores.append(min(1.0, tool_deviation))
            deviations.append({
                "type": "tool_set_deviation",
                "unexpected_tools": sorted(unexpected),
                "missing_tools": sorted(missing),
            })

        # Latency deviation
        if profile.stddev_latency_ms > 0:
            z = abs(fingerprint.total_latency_ms - profile.avg_latency_ms) / profile.stddev_latency_ms
            if z > 3.0:
                scores.append(min(1.0, z / 6.0))
                deviations.append({
                    "type": "latency_anomaly",
                    "expected_avg_ms": round(profile.avg_latency_ms, 1),
                    "observed_ms": round(fingerprint.total_latency_ms, 1),
                    "z_score": round(z, 2),
                })

        composite = sum(scores) / max(len(scores), 1) if scores else 0.0
        return DeviationReport(
            session_id=fingerprint.session_id,
            task_type=fingerprint.task_type,
            overall_deviation_score=round(composite, 4),
            deviations=deviations,
            is_anomalous=composite >= self._threshold,
            anomaly_threshold=self._threshold,
        )
```

## Solution 5: Deviation Alert Manager

```python
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Callable, Deque, Dict, List, Optional

class DeviationAlertManager:
    """
    Receives deviation reports and fires alerts for anomalous executions.
    Tracks alert rates per task_type to detect systematic regressions
    (e.g., a prompt change that causes deviation in all executions of a type).
    """

    def __init__(
        self,
        alert_threshold: float = 0.4,
        systematic_threshold: float = 0.3,   # alert if >30% of executions deviate
        window_seconds: float = 600.0,
    ):
        self._alert_threshold = alert_threshold
        self._systematic_threshold = systematic_threshold
        self._window = window_seconds
        self._reports: Dict[str, Deque[DeviationReport]] = defaultdict(
            lambda: deque(maxlen=500)
        )
        self._alert_handlers: List[Callable] = []
        self._alerts_fired = 0

    def add_alert_handler(self, handler: Callable) -> None:
        self._alert_handlers.append(handler)

    def ingest(self, report: DeviationReport) -> None:
        self._reports[report.task_type].append(report)
        if report.is_anomalous:
            self._fire("execution_deviation", report)
        self._check_systematic(report.task_type)

    def _check_systematic(self, task_type: str) -> None:
        cutoff = time.time() - self._window
        recent = [
            r for r in self._reports[task_type]
            if r.is_anomalous  # use timestamp if available
        ]
        all_recent = list(self._reports[task_type])[-50:]
        if len(all_recent) < 5:
            return
        anomaly_rate = sum(1 for r in all_recent if r.is_anomalous) / len(all_recent)
        if anomaly_rate >= self._systematic_threshold:
            self._fire("systematic_deviation", {
                "task_type": task_type,
                "anomaly_rate": round(anomaly_rate, 3),
                "window_seconds": self._window,
            })

    def _fire(self, alert_type: str, payload) -> None:
        self._alerts_fired += 1
        for handler in self._alert_handlers:
            try:
                handler(alert_type, payload)
            except Exception as exc:
                print(f"[deviation_alert] handler error: {exc}")

    def anomaly_rate(self, task_type: str) -> float:
        reports = list(self._reports.get(task_type, []))
        if not reports:
            return 0.0
        return round(sum(1 for r in reports if r.is_anomalous) / len(reports), 4)
```

## Solution 6: Golden Path Dashboard

```python
import time
from typing import Dict, List, Optional

class GoldenPathDashboard:
    """
    Summarizes golden path baseline coverage and live deviation rates.
    """

    def __init__(
        self,
        registry: GoldenPathRegistry,
        scorer: DeviationScorer,
        alert_manager: DeviationAlertManager,
    ):
        self._registry = registry
        self._scorer = scorer
        self._alerts = alert_manager

    def render(self) -> dict:
        task_types = list(self._registry._profiles.keys())
        profiles = []
        for tt in task_types:
            profile = self._registry.get_profile(tt)
            if profile:
                profiles.append({
                    "task_type": tt,
                    "sample_count": profile.sample_count,
                    "avg_tool_calls": round(profile.avg_tool_calls, 1),
                    "expected_tools": profile.expected_tool_set,
                    "anomaly_rate": self._alerts.anomaly_rate(tt),
                    "baseline_age_seconds": round(time.time() - profile.updated_at, 0),
                })

        return {
            "generated_at": time.time(),
            "task_types_with_baseline": len(task_types),
            "total_alerts_fired": self._alerts._alerts_fired,
            "profiles": sorted(profiles, key=lambda p: p["anomaly_rate"], reverse=True),
            "high_deviation_types": [
                p for p in profiles if p["anomaly_rate"] > 0.2
            ],
        }
```

## Comparison

| Approach | Baseline Building | Sequence Comparison | Numeric Comparison | Systematic Detection |
|---|---|---|---|---|
| ExecutionFingerprint | N/A (data model) | Via hash | Via fields | No |
| GoldenPathRegistry | Yes (incremental) | Via hash frequency | Via statistics | No |
| DeviationScorer | No | Yes (Z-score + hash) | Yes (Z-score) | No |
| DeviationAlertManager | No | No | No | Yes (rate-based) |
| GoldenPathDashboard | No | No | No | Via alert manager |

**Best for production**: Record all successful executions to `GoldenPathRegistry` — wait for 20–30 samples before activating deviation scoring (set `min_samples_for_baseline=20`). After each live execution, call `DeviationScorer.score()` and feed the result to `DeviationAlertManager`. Set `anomaly_threshold=0.4` initially and tune down after seeing false positive rates. Alert on `systematic_deviation` events — these indicate a code or prompt change that structurally altered agent behavior across all executions of a task type. Review `GoldenPathDashboard` after every deployment to validate that golden path profiles remain stable.
