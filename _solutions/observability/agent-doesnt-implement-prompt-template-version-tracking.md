---
title: "Agent Doesn't Implement Prompt Template Version Tracking"
description: "Agents that modify prompt templates without version tracking cannot correlate quality changes — response length shifts, refusal rate changes, accuracy regressions — with the specific prompt change that caused them. Implement prompt template version tracking that records each template change, tags every LLM call with its template version, and enables before/after quality comparison between versions."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-prompt-template-version-tracking
tags: [prompt-versioning, template-tracking, a-b-testing, prompt-regression, quality-attribution, deployment-observability]
symptoms:
  - "Response quality degraded after last deployment but no record of which prompt changed"
  - "Multiple prompt variants are running simultaneously with no version tag on metric data"
  - "Cannot roll back a prompt change because the previous version is not stored"
  - "A/B prompt experiments produce mixed data in dashboards because calls are not version-tagged"
  - "Engineers debate whether a quality issue predates or postdates a prompt change — no data to resolve it"
---

## Why This Happens

Prompt templates are often stored as code strings or config files and modified without the same rigor as software changes: no changelog, no version number, no impact tracking. When the same metric pipeline collects data from calls using different prompt versions — during a gradual rollout, an A/B test, or after a deployment — the aggregated metrics are uninterpretable. Version tracking requires computing a stable identifier (hash) for each template at load time, tagging every LLM call with that identifier, and maintaining a registry that maps identifiers to template content and deployment metadata.

## Solution 1: Prompt Template Record

```python
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class PromptTemplateRecord:
    template_id: str        # SHA256[:12] of template content
    name: str               # human-readable template name
    content: str            # full template text
    version: int            # monotonic version counter
    created_at: float = field(default_factory=time.time)
    author: str = ""
    description: str = ""
    parent_id: Optional[str] = None     # previous version's template_id
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_content(
        cls,
        name: str,
        content: str,
        version: int = 1,
        **kwargs,
    ) -> "PromptTemplateRecord":
        template_id = hashlib.sha256(content.encode()).hexdigest()[:12]
        return cls(
            template_id=template_id,
            name=name,
            content=content,
            version=version,
            **kwargs,
        )

    def diff_summary(self, other: "PromptTemplateRecord") -> dict:
        old_lines = set(self.content.splitlines())
        new_lines = set(other.content.splitlines())
        added = len(new_lines - old_lines)
        removed = len(old_lines - new_lines)
        return {
            "from_id": self.template_id,
            "to_id": other.template_id,
            "lines_added": added,
            "lines_removed": removed,
            "char_delta": len(other.content) - len(self.content),
        }
```

## Solution 2: Prompt Template Registry

```python
import time
from threading import Lock
from typing import Dict, List, Optional


class PromptTemplateRegistry:
    """
    Stores all known prompt template versions keyed by template_id.
    Supports querying current active version per template name and
    retrieving full version history.
    """

    def __init__(self):
        self._templates: Dict[str, PromptTemplateRecord] = {}
        self._active: Dict[str, str] = {}  # name -> template_id
        self._lock = Lock()

    def register(
        self,
        record: PromptTemplateRecord,
        set_active: bool = True,
    ) -> None:
        with self._lock:
            self._templates[record.template_id] = record
            if set_active:
                self._active[record.name] = record.template_id

    def get_active(self, name: str) -> Optional[PromptTemplateRecord]:
        with self._lock:
            tid = self._active.get(name)
            return self._templates.get(tid) if tid else None

    def get_by_id(self, template_id: str) -> Optional[PromptTemplateRecord]:
        with self._lock:
            return self._templates.get(template_id)

    def history(self, name: str) -> List[PromptTemplateRecord]:
        with self._lock:
            records = [r for r in self._templates.values() if r.name == name]
        return sorted(records, key=lambda r: r.version)

    def all_active(self) -> Dict[str, PromptTemplateRecord]:
        with self._lock:
            return {
                name: self._templates[tid]
                for name, tid in self._active.items()
                if tid in self._templates
            }
```

## Solution 3: Version-Tagged LLM Call Recorder

```python
import time
from collections import defaultdict
from threading import Lock
from typing import Any, Dict, List, Optional


class VersionTaggedCallRecorder:
    """
    Records LLM call outcomes tagged with their prompt template version.
    Enables before/after quality comparison between template versions.
    """

    def __init__(self, max_records: int = 50000):
        self._max = max_records
        self._records: List[dict] = []
        self._lock = Lock()

    def record(
        self,
        template_id: str,
        template_name: str,
        success: bool,
        latency_ms: float,
        response_chars: int = 0,
        session_id: str = "",
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        entry = {
            "ts": time.time(),
            "template_id": template_id,
            "template_name": template_name,
            "success": success,
            "latency_ms": latency_ms,
            "response_chars": response_chars,
            "session_id": session_id,
            **(extra or {}),
        }
        with self._lock:
            self._records.append(entry)
            if len(self._records) > self._max:
                self._records.pop(0)

    def stats_for_version(
        self,
        template_id: str,
        window_seconds: float = 3600.0,
    ) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            records = [
                r for r in self._records
                if r["ts"] >= cutoff and r["template_id"] == template_id
            ]
        if not records:
            return {"template_id": template_id, "calls": 0}
        successes = sum(1 for r in records if r["success"])
        latencies = sorted(r["latency_ms"] for r in records)
        chars = [r["response_chars"] for r in records]
        return {
            "template_id": template_id,
            "calls": len(records),
            "success_rate": round(successes / len(records), 4),
            "p50_latency_ms": latencies[len(latencies) // 2],
            "p95_latency_ms": latencies[min(int(len(latencies) * 0.95), len(latencies) - 1)],
            "mean_response_chars": round(sum(chars) / len(chars), 1),
        }
```

## Solution 4: Version Comparison Analyzer

```python
from typing import Optional


class PromptVersionComparisonAnalyzer:
    """
    Compares quality metrics between two template versions.
    Used to evaluate whether a prompt change improved or regressed quality.
    """

    def __init__(
        self,
        recorder: VersionTaggedCallRecorder,
        registry: PromptTemplateRegistry,
    ):
        self._recorder = recorder
        self._registry = registry

    def compare(
        self,
        template_name: str,
        version_a_id: str,
        version_b_id: str,
        window_seconds: float = 86400.0,
    ) -> dict:
        stats_a = self._recorder.stats_for_version(version_a_id, window_seconds)
        stats_b = self._recorder.stats_for_version(version_b_id, window_seconds)
        record_a = self._registry.get_by_id(version_a_id)
        record_b = self._registry.get_by_id(version_b_id)

        def delta(key: str) -> Optional[float]:
            va = stats_a.get(key)
            vb = stats_b.get(key)
            if va is None or vb is None:
                return None
            return round(vb - va, 4)

        diff = record_a.diff_summary(record_b) if record_a and record_b else {}

        return {
            "template_name": template_name,
            "version_a": {"id": version_a_id, "version": record_a.version if record_a else None, "stats": stats_a},
            "version_b": {"id": version_b_id, "version": record_b.version if record_b else None, "stats": stats_b},
            "deltas": {
                "success_rate_delta": delta("success_rate"),
                "p50_latency_delta_ms": delta("p50_latency_ms"),
                "mean_response_chars_delta": delta("mean_response_chars"),
            },
            "template_diff": diff,
        }
```

## Solution 5: Active Version Monitor

```python
import time
from typing import List


class ActiveVersionMonitor:
    """
    Detects when multiple versions of the same template are receiving
    traffic simultaneously — signalling a partial rollout or A/B test
    that may be contaminating aggregated metrics.
    """

    def __init__(
        self,
        recorder: VersionTaggedCallRecorder,
        registry: PromptTemplateRegistry,
        window_seconds: float = 1800.0,
    ):
        self._recorder = recorder
        self._registry = registry
        self._window = window_seconds

    def active_versions(self) -> dict:
        cutoff = time.time() - self._window
        with self._recorder._lock:
            recent = [r for r in self._recorder._records if r["ts"] >= cutoff]

        by_name: dict = {}
        for r in recent:
            name = r["template_name"]
            tid = r["template_id"]
            if name not in by_name:
                by_name[name] = {}
            by_name[name][tid] = by_name[name].get(tid, 0) + 1

        result = {}
        for name, versions in by_name.items():
            result[name] = {
                "active_version_count": len(versions),
                "mixed_traffic": len(versions) > 1,
                "versions": [
                    {"template_id": tid, "call_count": count}
                    for tid, count in sorted(versions.items(), key=lambda x: -x[1])
                ],
            }
        return result
```

## Solution 6: Prompt Template Version Dashboard

```python
import time


class PromptTemplateVersionDashboard:
    """
    Combines registry contents, active version monitor, and per-version
    quality stats into a single prompt versioning health report.
    """

    def __init__(
        self,
        registry: PromptTemplateRegistry,
        recorder: VersionTaggedCallRecorder,
        monitor: ActiveVersionMonitor,
    ):
        self._registry = registry
        self._recorder = recorder
        self._monitor = monitor

    def render(self) -> dict:
        active = self._registry.all_active()
        return {
            "generated_at": time.time(),
            "registered_templates": len(active),
            "active_versions": {
                name: {
                    "template_id": rec.template_id,
                    "version": rec.version,
                    "stats_1h": self._recorder.stats_for_version(rec.template_id, 3600.0),
                }
                for name, rec in active.items()
            },
            "mixed_traffic_alerts": {
                name: info
                for name, info in self._monitor.active_versions().items()
                if info["mixed_traffic"]
            },
        }
```

## Comparison

| Approach | Content Hashing | Version History | Quality Tagging | Version Comparison | Mixed Traffic Detection |
|---|---|---|---|---|---|
| PromptTemplateRecord | Yes (SHA256[:12]) | No | No | No | No |
| PromptTemplateRegistry | Via record | Yes (by name) | No | No | No |
| VersionTaggedCallRecorder | No | No | Yes | No | No |
| PromptVersionComparisonAnalyzer | No | Via registry | Via recorder | Yes | No |
| ActiveVersionMonitor | No | No | Via recorder | No | Yes |
| PromptTemplateVersionDashboard | No | No | No | No | Yes |

**Best for production**: Compute `template_id` from content hash at load time — this automatically detects when the same template name has different content in different environments (config drift). Tag every LLM call with both `template_id` and `template_name` as metric dimensions so dashboards can filter by either. Use `ActiveVersionMonitor.mixed_traffic_alerts` as a deployment health signal: mixed traffic across two versions for more than 30 minutes after a deployment indicates a partial rollout that is skewing your quality metrics. Store the full template content in the registry (not just the hash) so you can reconstruct exactly what prompt produced a specific set of calls weeks after the fact.
