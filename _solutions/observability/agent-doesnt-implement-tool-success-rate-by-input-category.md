---
title: "Agent Doesn't Implement Tool Success Rate by Input Category"
description: "Agents that report a single aggregate success rate for each tool miss category-level failures: a search tool may succeed 98% of the time for English queries but fail 40% of the time for non-ASCII queries, or a date parser may succeed on ISO formats but fail on ambiguous locale-specific dates. Implement tool success rate tracking segmented by input category to surface systematic failure patterns invisible in aggregate metrics."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-tool-success-rate-by-input-category
tags: [success-rate, input-segmentation, categorical-metrics, failure-patterns, tool-quality, slice-based-monitoring]
symptoms:
  - "Tool aggregate success rate is 95% but a specific input category fails 60% of the time"
  - "No breakdown of tool outcomes by query language, input length, or data type"
  - "Failure pattern only discovered when a specific user segment complains"
  - "Cannot determine if a fix improved success rate for the affected input category"
  - "Single success-rate metric obscures category-specific regressions after model upgrades"
---

## Why This Happens

Aggregate metrics hide slice-level failures. A tool that fails 40% of the time on one category of inputs but succeeds 99% on all others will report a 95%+ aggregate success rate — the 40% failure is invisible. Category-based success rate tracking requires classifying each tool call's input into one or more categories, recording success/failure per category, and computing per-category success rates. The categories can be structural (input length buckets, data type, format) or semantic (language, domain, query intent).

## Solution 1: Input Category Classifier

```python
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class InputCategory:
    name: str
    dimensions: Dict[str, str] = field(default_factory=dict)
    # e.g. {"length": "medium", "language": "non-ascii", "format": "iso-date"}


class InputCategoryClassifier:
    """
    Classifies tool call inputs into categorical dimensions.
    Each dimension is a named partition of the input space.
    """

    def classify(self, tool_name: str, args: Dict[str, Any]) -> InputCategory:
        dimensions = {}
        text_arg = self._extract_primary_text(args)

        if text_arg:
            dimensions["length"] = self._length_bucket(text_arg)
            dimensions["encoding"] = "non_ascii" if not text_arg.isascii() else "ascii"
            dimensions["has_numbers"] = "yes" if re.search(r"\d", text_arg) else "no"

        dimensions["arg_count"] = str(len(args))
        dimensions["tool"] = tool_name

        return InputCategory(name=self._category_name(dimensions), dimensions=dimensions)

    @staticmethod
    def _extract_primary_text(args: Dict[str, Any]) -> Optional[str]:
        for key in ("query", "text", "content", "message", "input", "prompt"):
            val = args.get(key)
            if isinstance(val, str):
                return val
        # Fallback: first string-valued arg
        for val in args.values():
            if isinstance(val, str):
                return val
        return None

    @staticmethod
    def _length_bucket(text: str) -> str:
        n = len(text)
        if n < 50:
            return "short"
        if n < 500:
            return "medium"
        return "long"

    @staticmethod
    def _category_name(dimensions: Dict[str, str]) -> str:
        parts = [f"{k}={v}" for k, v in sorted(dimensions.items()) if k != "tool"]
        return "|".join(parts[:3])
```

## Solution 2: Per-Category Success Rate Counter

```python
import time
from collections import defaultdict
from dataclasses import dataclass, field
from threading import Lock
from typing import Dict, List, Optional, Tuple


@dataclass
class CategorySuccessRecord:
    successes: int = 0
    failures: int = 0
    last_updated: float = field(default_factory=time.time)

    def rate(self) -> float:
        total = self.successes + self.failures
        return round(self.successes / total, 4) if total else 0.0

    def total(self) -> int:
        return self.successes + self.failures


class PerCategorySuccessRateCounter:
    """
    Tracks success and failure counts per (tool_name, category) pair.
    Provides success rates and identifies lowest-performing categories.
    """

    def __init__(self):
        self._counts: Dict[Tuple[str, str], CategorySuccessRecord] = defaultdict(CategorySuccessRecord)
        self._lock = Lock()

    def record(
        self,
        tool_name: str,
        category: InputCategory,
        success: bool,
    ) -> None:
        key = (tool_name, category.name)
        with self._lock:
            record = self._counts[key]
            if success:
                record.successes += 1
            else:
                record.failures += 1
            record.last_updated = time.time()

    def success_rate(self, tool_name: str, category_name: str) -> Optional[float]:
        key = (tool_name, category_name)
        with self._lock:
            record = self._counts.get(key)
        return record.rate() if record else None

    def all_rates(self, tool_name: Optional[str] = None) -> List[dict]:
        with self._lock:
            items = list(self._counts.items())
        result = []
        for (tname, cat), record in items:
            if tool_name and tname != tool_name:
                continue
            result.append({
                "tool": tname,
                "category": cat,
                "success_rate": record.rate(),
                "total": record.total(),
                "successes": record.successes,
                "failures": record.failures,
            })
        return sorted(result, key=lambda x: x["success_rate"])

    def worst_categories(self, tool_name: str, n: int = 5, min_samples: int = 10) -> List[dict]:
        rates = self.all_rates(tool_name)
        return [r for r in rates if r["total"] >= min_samples][:n]
```

## Solution 3: Category Success Rate Anomaly Detector

```python
from typing import List, Optional


class CategorySuccessAnomalyDetector:
    """
    Flags categories whose success rate is significantly below the
    aggregate tool success rate, indicating a systematic failure pattern.
    """

    def __init__(
        self,
        counter: PerCategorySuccessRateCounter,
        min_rate_gap: float = 0.15,   # 15 percentage points below aggregate
        min_samples: int = 20,
    ):
        self._counter = counter
        self._min_gap = min_rate_gap
        self._min_samples = min_samples

    def _aggregate_rate(self, tool_name: str) -> Optional[float]:
        rates = self._counter.all_rates(tool_name)
        filtered = [r for r in rates if r["total"] >= 5]
        if not filtered:
            return None
        total_calls = sum(r["total"] for r in filtered)
        total_success = sum(r["successes"] for r in filtered)
        return total_success / total_calls if total_calls else None

    def detect(self, tool_name: str) -> List[dict]:
        aggregate = self._aggregate_rate(tool_name)
        if aggregate is None:
            return []

        anomalies = []
        for rate_info in self._counter.all_rates(tool_name):
            if rate_info["total"] < self._min_samples:
                continue
            gap = aggregate - rate_info["success_rate"]
            if gap >= self._min_gap:
                anomalies.append({
                    "tool": tool_name,
                    "category": rate_info["category"],
                    "category_rate": rate_info["success_rate"],
                    "aggregate_rate": round(aggregate, 4),
                    "gap": round(gap, 4),
                    "total_calls": rate_info["total"],
                })
        return sorted(anomalies, key=lambda x: -x["gap"])
```

## Solution 4: Instrumented Category-Aware Tool Executor

```python
from typing import Any, Callable, Dict, Optional


class CategoryAwareToolExecutor:
    """
    Wraps tool calls with input classification and per-category success recording.
    """

    def __init__(
        self,
        classifier: InputCategoryClassifier,
        counter: PerCategorySuccessRateCounter,
        anomaly_detector: Optional[CategorySuccessAnomalyDetector] = None,
        anomaly_check_every_n: int = 100,
        alert_fn=None,
    ):
        self._classifier = classifier
        self._counter = counter
        self._detector = anomaly_detector
        self._check_every = anomaly_check_every_n
        self._alert_fn = alert_fn
        self._call_count = 0

    async def execute(
        self,
        tool_name: str,
        tool_fn: Callable,
        **kwargs: Any,
    ) -> Any:
        category = self._classifier.classify(tool_name, kwargs)
        try:
            result = await tool_fn(**kwargs)
            self._counter.record(tool_name, category, success=True)
            return result
        except Exception:
            self._counter.record(tool_name, category, success=False)
            raise
        finally:
            self._call_count += 1
            if (
                self._detector
                and self._alert_fn
                and self._call_count % self._check_every == 0
            ):
                anomalies = self._detector.detect(tool_name)
                if anomalies:
                    self._alert_fn(anomalies)
```

## Solution 5: Category Success Rate Regression Detector

```python
import time
from collections import defaultdict
from threading import Lock
from typing import Dict, List, Tuple


class CategorySuccessRegressionDetector:
    """
    Detects regressions in per-category success rates after deployments.
    Compares recent rates against a stored baseline snapshot.
    """

    def __init__(self, counter: PerCategorySuccessRateCounter):
        self._counter = counter
        self._baseline: Dict[Tuple[str, str], float] = {}
        self._lock = Lock()

    def capture_baseline(self, tool_name: str) -> int:
        rates = self._counter.all_rates(tool_name)
        with self._lock:
            for r in rates:
                self._baseline[(r["tool"], r["category"])] = r["success_rate"]
        return len(rates)

    def check_regressions(self, tool_name: str, threshold: float = 0.10) -> List[dict]:
        rates = self._counter.all_rates(tool_name)
        regressions = []
        with self._lock:
            for r in rates:
                baseline = self._baseline.get((r["tool"], r["category"]))
                if baseline is None or r["total"] < 10:
                    continue
                drop = baseline - r["success_rate"]
                if drop >= threshold:
                    regressions.append({
                        "tool": r["tool"],
                        "category": r["category"],
                        "baseline_rate": baseline,
                        "current_rate": r["success_rate"],
                        "drop": round(drop, 4),
                    })
        return sorted(regressions, key=lambda x: -x["drop"])
```

## Solution 6: Tool Category Success Dashboard

```python
import time


class ToolCategorySuccessDashboard:
    """
    Renders per-category success rates, anomalies, and regressions per tool.
    """

    def __init__(
        self,
        counter: PerCategorySuccessRateCounter,
        anomaly_detector: CategorySuccessAnomalyDetector,
        regression_detector: CategorySuccessRegressionDetector,
        tools: list,
    ):
        self._counter = counter
        self._anomaly = anomaly_detector
        self._regression = regression_detector
        self._tools = tools

    def render(self) -> dict:
        report = {"generated_at": time.time(), "tools": {}}
        for tool in self._tools:
            report["tools"][tool] = {
                "worst_categories": self._counter.worst_categories(tool),
                "anomalies": self._anomaly.detect(tool),
                "regressions": self._regression.check_regressions(tool),
            }
        return report
```

## Comparison

| Approach | Input Classification | Per-Category Rates | Anomaly Detection | Regression Detection | Dashboard |
|---|---|---|---|---|---|
| InputCategoryClassifier | Yes (multi-dim) | No | No | No | No |
| PerCategorySuccessRateCounter | Via classifier | Yes | No | No | No |
| CategorySuccessAnomalyDetector | No | Via counter | Yes (gap-based) | No | No |
| CategoryAwareToolExecutor | Via classifier | Via counter | Via detector | No | No |
| CategorySuccessRegressionDetector | No | Via counter | No | Yes | No |
| ToolCategorySuccessDashboard | No | No | No | No | Yes |

**Best for production**: Add domain-specific dimensions to `InputCategoryClassifier` for your tool's input space — a search tool should classify by query language and query length; a date parser should classify by date format; an API tool should classify by endpoint path. Set `min_rate_gap=0.15` for anomaly detection: a category 15 percentage points below aggregate is a clear signal, not noise. Capture a baseline with `CategorySuccessRegressionDetector.capture_baseline()` after each deployment and check regressions 1 hour later — this catches input-category-specific regressions that aggregate metrics miss.
