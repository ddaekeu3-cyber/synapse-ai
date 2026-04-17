---
title: "Agent Doesn't Implement Feature Flag Change Impact Tracking"
description: "Agents controlled by feature flags have no observability into the quality and behavioral impact of flag changes: when a flag is toggled, there is no before/after comparison of tool call rates, error rates, latency, or user satisfaction across the flag boundary. Without flag-correlated metrics, toggling a flag is a blind change. Implement feature flag change impact tracking that records which flag state each session ran under and surfaces quality metric deltas at flag transition points."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-feature-flag-change-impact-tracking
tags: [feature-flags, impact-tracking, flag-correlation, before-after-comparison, behavioral-metrics, rollout-observability]
symptoms:
  - "Feature flag toggled in production with no visibility into behavioral impact"
  - "Cannot compare error rates or latency between flag-on and flag-off sessions"
  - "Quality regression traced to a flag change but no flag-correlated metrics exist"
  - "Feature flags are deployed as permanent settings with no rollout tracking"
  - "No record of when flags changed and which sessions ran under which flag state"
---

## Why This Happens

Feature flags are often implemented as environment variables or simple boolean checks inside tool implementations. The flag value is read at call time but never recorded in the session metadata. When a flag is toggled — enabling a new retrieval strategy, switching to a different model, activating a new tool — the sessions immediately after the change run under the new flag state with no comparison baseline. Without recording the flag state per session, there is no way to group sessions by flag state and compare metrics, which is the entire point of feature flags for gradual rollouts.

## Solution 1: Feature Flag Snapshot

```python
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class FeatureFlagSnapshot:
    """
    A point-in-time record of all feature flag values active for a session.
    Attached to every session and metric record for correlation.
    """
    snapshot_id: str
    captured_at: float
    flags: Dict[str, Any]     # flag_name -> value (bool, str, int, float)
    flag_hash: str            # SHA-256[:8] of sorted flags for fast equality check

    @staticmethod
    def capture(flags: Dict[str, Any]) -> "FeatureFlagSnapshot":
        import hashlib, json, uuid
        canonical = json.dumps(flags, sort_keys=True, separators=(",", ":"))
        flag_hash = hashlib.sha256(canonical.encode()).hexdigest()[:8]
        return FeatureFlagSnapshot(
            snapshot_id=uuid.uuid4().hex[:12],
            captured_at=time.time(),
            flags=dict(flags),
            flag_hash=flag_hash,
        )

    def differs_from(self, other: "FeatureFlagSnapshot") -> bool:
        return self.flag_hash != other.flag_hash

    def changed_flags(self, other: "FeatureFlagSnapshot") -> Dict[str, tuple]:
        """Returns {flag_name: (old_value, new_value)} for changed flags."""
        all_keys = set(self.flags) | set(other.flags)
        return {
            k: (self.flags.get(k), other.flags.get(k))
            for k in all_keys
            if self.flags.get(k) != other.flags.get(k)
        }
```

## Solution 2: Feature Flag Registry

```python
import os
from threading import Lock
from typing import Any, Callable, Dict, List, Optional


class FeatureFlagRegistry:
    """
    Central store for all feature flags. Supports env-var-backed flags,
    programmatic overrides, and change callbacks.
    """

    def __init__(self):
        self._lock = Lock()
        self._flags: Dict[str, Any] = {}
        self._defaults: Dict[str, Any] = {}
        self._on_change: List[Callable[[str, Any, Any], None]] = []

    def define(self, name: str, default: Any, env_var: Optional[str] = None) -> None:
        value = default
        if env_var:
            raw = os.environ.get(env_var)
            if raw is not None:
                if isinstance(default, bool):
                    value = raw.lower() in ("1", "true", "yes")
                elif isinstance(default, int):
                    try:
                        value = int(raw)
                    except ValueError:
                        pass
                else:
                    value = raw
        with self._lock:
            self._defaults[name] = default
            self._flags[name] = value

    def get(self, name: str, default: Any = None) -> Any:
        with self._lock:
            return self._flags.get(name, default)

    def set(self, name: str, value: Any) -> None:
        with self._lock:
            old = self._flags.get(name)
            self._flags[name] = value
        for cb in self._on_change:
            try:
                cb(name, old, value)
            except Exception:
                pass

    def on_change(self, callback: Callable[[str, Any, Any], None]) -> None:
        self._on_change.append(callback)

    def snapshot(self) -> FeatureFlagSnapshot:
        with self._lock:
            return FeatureFlagSnapshot.capture(dict(self._flags))

    def all_flags(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._flags)
```

## Solution 3: Flag Change Event Recorder

```python
import time
from dataclasses import dataclass
from threading import Lock
from typing import Any, List, Optional


@dataclass
class FlagChangeEvent:
    flag_name: str
    old_value: Any
    new_value: Any
    changed_at: float
    changed_by: str = "system"


class FlagChangeEventRecorder:
    """
    Records every feature flag change with timestamp for
    correlating metric shifts to specific flag transitions.
    """

    def __init__(self):
        self._lock = Lock()
        self._events: List[FlagChangeEvent] = []

    def record(self, flag_name: str, old_value: Any, new_value: Any, changed_by: str = "system") -> None:
        event = FlagChangeEvent(
            flag_name=flag_name,
            old_value=old_value,
            new_value=new_value,
            changed_at=time.time(),
            changed_by=changed_by,
        )
        with self._lock:
            self._events.append(event)

    def events_for_flag(self, flag_name: str) -> List[FlagChangeEvent]:
        with self._lock:
            return [e for e in self._events if e.flag_name == flag_name]

    def recent_changes(
        self, window_seconds: float = 3600.0
    ) -> List[FlagChangeEvent]:
        cutoff = time.time() - window_seconds
        with self._lock:
            return [e for e in self._events if e.changed_at >= cutoff]
```

## Solution 4: Flag-Correlated Session Metrics

```python
import time
from collections import defaultdict
from threading import Lock
from typing import Dict, List, Optional


@dataclass
class FlaggedSessionRecord:
    session_id: str
    flag_hash: str
    flags_snapshot: Dict
    started_at: float
    quality_score: Optional[float] = None
    error_count: int = 0
    tool_call_count: int = 0
    latency_ms: Optional[float] = None
    outcome: Optional[str] = None


class FlagCorrelatedSessionMetrics:
    """
    Stores session outcome metrics tagged with the flag snapshot active
    at session start. Enables before/after comparison across flag changes.
    """

    def __init__(self, max_records: int = 50_000):
        self._lock = Lock()
        self._records: List[FlaggedSessionRecord] = []
        self._max = max_records

    def record_session(self, record: FlaggedSessionRecord) -> None:
        with self._lock:
            if len(self._records) >= self._max:
                self._records.pop(0)
            self._records.append(record)

    def metrics_by_flag_hash(
        self,
        window_seconds: float = 86400.0,
    ) -> Dict[str, dict]:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [r for r in self._records if r.started_at >= cutoff]

        by_hash: Dict[str, List[FlaggedSessionRecord]] = defaultdict(list)
        for r in recent:
            by_hash[r.flag_hash].append(r)

        return {
            flag_hash: {
                "sessions": len(recs),
                "flags": recs[0].flags_snapshot if recs else {},
                "avg_quality": self._avg([r.quality_score for r in recs if r.quality_score is not None]),
                "error_rate": self._avg([r.error_count > 0 for r in recs]),
                "avg_tool_calls": self._avg([r.tool_call_count for r in recs]),
                "avg_latency_ms": self._avg([r.latency_ms for r in recs if r.latency_ms is not None]),
            }
            for flag_hash, recs in by_hash.items()
        }

    @staticmethod
    def _avg(values: list) -> Optional[float]:
        nums = [v for v in values if v is not None]
        return round(sum(nums) / len(nums), 4) if nums else None
```

## Solution 5: Flag Impact Comparator

```python
from typing import Optional


class FeatureFlagImpactComparator:
    """
    Compares quality and reliability metrics between two flag states
    (identified by flag_hash) to quantify the impact of a flag change.
    """

    def __init__(self, metrics: FlagCorrelatedSessionMetrics):
        self._metrics = metrics

    def compare(
        self,
        hash_before: str,
        hash_after: str,
        window_seconds: float = 86400.0,
    ) -> dict:
        by_hash = self._metrics.metrics_by_flag_hash(window_seconds)
        before = by_hash.get(hash_before)
        after = by_hash.get(hash_after)

        if not before or not after:
            return {
                "status": "insufficient_data",
                "hash_before": hash_before,
                "hash_after": hash_after,
                "before_found": before is not None,
                "after_found": after is not None,
            }

        def delta(key: str) -> Optional[float]:
            a = before.get(key)
            b = after.get(key)
            if a is None or b is None:
                return None
            return round(b - a, 4)

        quality_delta = delta("avg_quality")
        error_delta = delta("error_rate")
        latency_delta = delta("avg_latency_ms")

        verdict = "neutral"
        if quality_delta is not None and quality_delta < -0.05:
            verdict = "regression"
        elif quality_delta is not None and quality_delta > 0.05:
            verdict = "improvement"
        if error_delta is not None and error_delta > 0.05:
            verdict = "regression"

        return {
            "status": verdict,
            "hash_before": hash_before,
            "hash_after": hash_after,
            "sessions_before": before["sessions"],
            "sessions_after": after["sessions"],
            "quality_delta": quality_delta,
            "error_rate_delta": error_delta,
            "latency_delta_ms": latency_delta,
            "tool_call_delta": delta("avg_tool_calls"),
        }
```

## Solution 6: Flag Impact Dashboard

```python
import time


class FeatureFlagImpactDashboard:
    """
    Combines flag change history, per-hash metrics, and recent flag state
    into a single rollout observability view.
    """

    def __init__(
        self,
        registry: FeatureFlagRegistry,
        recorder: FlagChangeEventRecorder,
        metrics: FlagCorrelatedSessionMetrics,
        comparator: FeatureFlagImpactComparator,
    ):
        self._registry = registry
        self._recorder = recorder
        self._metrics = metrics
        self._comparator = comparator

    def render(self) -> dict:
        recent_changes = self._recorder.recent_changes(window_seconds=3600.0)
        by_hash = self._metrics.metrics_by_flag_hash(window_seconds=86400.0)

        return {
            "generated_at": time.time(),
            "current_flags": self._registry.all_flags(),
            "current_flag_hash": self._registry.snapshot().flag_hash,
            "recent_flag_changes": [
                {
                    "flag_name": e.flag_name,
                    "old_value": e.old_value,
                    "new_value": e.new_value,
                    "changed_at": e.changed_at,
                }
                for e in recent_changes
            ],
            "metrics_by_flag_state": {
                h: {
                    "sessions": m["sessions"],
                    "avg_quality": m["avg_quality"],
                    "error_rate": m["error_rate"],
                }
                for h, m in by_hash.items()
            },
        }
```

## Comparison

| Approach | Flag Snapshot | Change Recording | Session Correlation | Impact Comparison | Dashboard |
|---|---|---|---|---|---|
| FeatureFlagRegistry | Yes (capture) | Via callback | No | No | No |
| FlagChangeEventRecorder | No | Yes | No | No | No |
| FlagCorrelatedSessionMetrics | No | No | Yes (by hash) | No | No |
| FeatureFlagImpactComparator | No | No | Via metrics | Yes | No |
| FeatureFlagImpactDashboard | Via registry | Via recorder | Via metrics | Via comparator | Yes |

**Best for production**: Capture the flag snapshot at session start — not at each request — so the session's entire metric trajectory is attributed to the flag state it began with. Use `flag_hash` as an index key rather than storing the full flag map per session: hash collisions are negligible for a small flag set and the hash makes grouping O(1). Automate impact comparison after every flag toggle: have the flag change callback schedule a `FeatureFlagImpactComparator.compare()` run 15 minutes after the change, and alert if `verdict == "regression"` with more than 50 sessions in the after group.
