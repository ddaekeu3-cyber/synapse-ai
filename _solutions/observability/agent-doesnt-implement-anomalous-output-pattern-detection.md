---
title: "Agent Doesn't Implement Anomalous Output Pattern Detection"
description: "Agents that emit responses without structural validation cannot detect when outputs have drifted into anomalous patterns: JSON responses that stopped being valid JSON, markdown responses that became plain text, structured reports that began including unexpected sections, or code outputs that regressed to prose explanations. Implement anomalous output pattern detection that validates response structure, detects format regressions, and alerts when output patterns deviate from established baselines."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-anomalous-output-pattern-detection
tags: [output-anomaly, format-validation, pattern-detection, response-regression, output-monitoring, format-drift]
symptoms:
  - "JSON output tool silently starts returning prose explanations after a prompt change"
  - "Structured report format breaks on edge-case inputs with no alerting"
  - "Output length distribution shifts dramatically after a model update"
  - "Code generation agent starts returning markdown prose instead of code blocks"
  - "No baseline of expected output format to compare against in production"
---

## Why This Happens

Output format validation is treated as a correctness concern — something to check in tests. In production, the same validation is absent or applied only to hard failures (empty response, JSON parse error). Soft regressions — outputs that are technically non-empty but structurally wrong — pass through undetected. Pattern detection requires a baseline of expected output characteristics (format, length distribution, key section presence) and continuous comparison against that baseline as responses are generated.

## Solution 1: Output Pattern Descriptor

```python
import re
from dataclasses import dataclass, field
from typing import List, Optional, Pattern


@dataclass
class OutputPatternDescriptor:
    name: str
    description: str

    # Format checks
    expected_format: str = "text"        # "text" | "json" | "markdown" | "code"
    required_sections: List[str] = field(default_factory=list)
    forbidden_patterns: List[str] = field(default_factory=list)

    # Length bounds
    min_chars: int = 10
    max_chars: int = 50_000

    # Code-specific
    expected_language: Optional[str] = None   # "python", "javascript", etc.
```

## Solution 2: Output Format Validator

```python
import json
import re
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class FormatValidationResult:
    valid: bool
    format_detected: str
    violations: List[str]
    warnings: List[str]


class OutputFormatValidator:
    """
    Validates a response against an OutputPatternDescriptor.
    Detects format violations, missing sections, and forbidden patterns.
    """

    def validate(
        self,
        response: str,
        descriptor: OutputPatternDescriptor,
    ) -> FormatValidationResult:
        violations = []
        warnings = []

        # Length check
        if len(response) < descriptor.min_chars:
            violations.append(f"too_short: {len(response)} chars < min {descriptor.min_chars}")
        if len(response) > descriptor.max_chars:
            warnings.append(f"too_long: {len(response)} chars > max {descriptor.max_chars}")

        # Format detection
        detected = self._detect_format(response)

        if descriptor.expected_format == "json":
            try:
                json.loads(response)
            except (json.JSONDecodeError, ValueError) as e:
                violations.append(f"invalid_json: {str(e)[:100]}")

        elif descriptor.expected_format == "markdown":
            if not re.search(r"#{1,6}\s|\*\*|__|```|\*[^*]", response):
                warnings.append("no_markdown_formatting_detected")

        elif descriptor.expected_format == "code":
            lang = descriptor.expected_language or ""
            pattern = rf"```{lang}" if lang else r"```"
            if not re.search(pattern, response):
                violations.append(f"no_code_block_found: expected ```{lang}")

        # Required sections
        for section in descriptor.required_sections:
            if section.lower() not in response.lower():
                violations.append(f"missing_section: '{section}'")

        # Forbidden patterns
        for pat in descriptor.forbidden_patterns:
            if re.search(pat, response, re.IGNORECASE):
                violations.append(f"forbidden_pattern_found: '{pat}'")

        return FormatValidationResult(
            valid=len(violations) == 0,
            format_detected=detected,
            violations=violations,
            warnings=warnings,
        )

    @staticmethod
    def _detect_format(text: str) -> str:
        stripped = text.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                json.loads(stripped)
                return "json"
            except Exception:
                pass
        if re.search(r"```", text):
            return "code"
        if re.search(r"#{1,6}\s|\*\*|__", text):
            return "markdown"
        return "text"
```

## Solution 3: Output Length Distribution Tracker

```python
import math
import time
from collections import deque
from threading import Lock
from typing import Deque, Optional, Tuple


class OutputLengthDistributionTracker:
    """
    Tracks response length distribution and detects when current
    lengths deviate significantly from the established baseline.
    Uses z-score to flag statistical outliers.
    """

    def __init__(
        self,
        window_seconds: float = 3600.0,
        z_score_threshold: float = 3.0,
    ):
        self._window = window_seconds
        self._z_threshold = z_score_threshold
        self._samples: Deque[Tuple[float, int]] = deque()
        # (timestamp, char_count)
        self._lock = Lock()

    def record(self, char_count: int) -> Optional[dict]:
        now = time.time()
        with self._lock:
            self._samples.append((now, char_count))
            recent = [(ts, c) for ts, c in self._samples if ts >= now - self._window]

        if len(recent) < 20:
            return None  # Not enough baseline data

        counts = [c for _, c in recent[:-1]]  # exclude current
        mean = sum(counts) / len(counts)
        variance = sum((x - mean) ** 2 for x in counts) / len(counts)
        std = math.sqrt(variance) if variance > 0 else 1.0

        z_score = abs(char_count - mean) / std
        if z_score > self._z_threshold:
            return {
                "anomaly": "length_outlier",
                "char_count": char_count,
                "baseline_mean": round(mean, 1),
                "baseline_std": round(std, 1),
                "z_score": round(z_score, 2),
            }
        return None
```

## Solution 4: Pattern Regression Detector

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, Dict, List


class PatternRegressionDetector:
    """
    Tracks format validation pass rates over time and detects
    when the pass rate drops below a baseline — indicating a
    prompt or model regression that is producing wrong formats.
    """

    def __init__(
        self,
        window_seconds: float = 600.0,
        regression_threshold_pct: float = 20.0,
    ):
        self._window = window_seconds
        self._threshold = regression_threshold_pct / 100.0
        self._records: Deque[dict] = deque()
        self._lock = Lock()

    def record(self, descriptor_name: str, valid: bool) -> None:
        with self._lock:
            self._records.append({
                "ts": time.time(),
                "name": descriptor_name,
                "valid": valid,
            })

    def _pass_rate(self, name: str, from_ts: float) -> Optional[float]:
        with self._lock:
            relevant = [
                r for r in self._records
                if r["name"] == name and r["ts"] >= from_ts
            ]
        if not relevant:
            return None
        return sum(1 for r in relevant if r["valid"]) / len(relevant)

    def detect_regressions(self) -> List[dict]:
        now = time.time()
        baseline_cutoff = now - self._window * 6   # 6× window for baseline
        recent_cutoff = now - self._window

        names: set = set()
        with self._lock:
            names = {r["name"] for r in self._records}

        regressions = []
        for name in names:
            baseline_rate = self._pass_rate(name, baseline_cutoff)
            recent_rate = self._pass_rate(name, recent_cutoff)
            if baseline_rate is None or recent_rate is None:
                continue
            drop = baseline_rate - recent_rate
            if drop > self._threshold:
                regressions.append({
                    "descriptor": name,
                    "baseline_pass_rate": round(baseline_rate, 4),
                    "recent_pass_rate": round(recent_rate, 4),
                    "drop_pct": round(drop * 100, 1),
                })
        return regressions
```

## Solution 5: Anomalous Output Gate

```python
import time
from typing import Dict, Optional


class AnomalousOutputGate:
    """
    Combines format validation, length anomaly detection, and
    regression detection into a single gate that runs on every response.
    """

    def __init__(
        self,
        validator: OutputFormatValidator,
        length_tracker: OutputLengthDistributionTracker,
        regression_detector: PatternRegressionDetector,
        descriptors: Dict[str, OutputPatternDescriptor],
    ):
        self._validator = validator
        self._length = length_tracker
        self._regression = regression_detector
        self._descriptors = descriptors

    def evaluate(
        self,
        response: str,
        descriptor_name: str,
    ) -> dict:
        descriptor = self._descriptors.get(descriptor_name)
        validation = (
            self._validator.validate(response, descriptor)
            if descriptor else None
        )
        length_anomaly = self._length.record(len(response))

        if validation:
            self._regression.record(descriptor_name, validation.valid)

        return {
            "ts": time.time(),
            "descriptor_name": descriptor_name,
            "format_valid": validation.valid if validation else None,
            "violations": validation.violations if validation else [],
            "warnings": validation.warnings if validation else [],
            "length_anomaly": length_anomaly,
            "regressions": self._regression.detect_regressions(),
        }
```

## Solution 6: Output Anomaly Dashboard

```python
import time
from typing import Optional


class OutputAnomalyDashboard:
    """
    Renders format validation rates, length distribution statistics,
    and active regressions for output quality monitoring.
    """

    def __init__(
        self,
        gate: AnomalousOutputGate,
    ):
        self._gate = gate

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "active_regressions": self._gate._regression.detect_regressions(),
            "registered_descriptors": list(self._gate._descriptors.keys()),
        }
```

## Comparison

| Approach | Format Validation | Length Anomaly | Regression Detection | Per-Response Gate | Dashboard |
|---|---|---|---|---|---|
| OutputFormatValidator | Yes (JSON/MD/code) | No | No | No | No |
| OutputLengthDistributionTracker | No | Yes (z-score) | No | No | No |
| PatternRegressionDetector | No | No | Yes (pass-rate drop) | No | No |
| AnomalousOutputGate | Via validator | Via tracker | Via detector | Yes | No |
| OutputAnomalyDashboard | No | No | No | No | Yes |

**Best for production**: Register an `OutputPatternDescriptor` for each distinct response type (JSON API response, markdown report, Python code block) and run `AnomalousOutputGate.evaluate()` on every response. Log violations as structured events with severity: format violations are errors (block or alert), warnings are informational (monitor). Set `regression_threshold_pct=20` — a 20% drop in pass rate within a 10-minute window almost always indicates a prompt change or model update that broke the format contract. Alert on-call when regressions are detected and any violation count exceeds 5 per minute.
