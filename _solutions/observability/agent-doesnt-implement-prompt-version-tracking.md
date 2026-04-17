---
title: "Agent Doesn't Implement Prompt Version Tracking"
description: "Agents that deploy prompt changes without version tracking cannot correlate quality regressions to specific prompt edits: when answer quality drops, there is no record of what changed, when it changed, or which sessions ran under which prompt version. Implement prompt version tracking that fingerprints prompt templates, records which version each session used, and surfaces version-correlated quality metrics."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-prompt-version-tracking
tags: [prompt-versioning, prompt-management, quality-correlation, regression-tracking, prompt-audit, deployment-tracking]
symptoms:
  - "Quality regression traced to a prompt change but no record of when or what changed"
  - "Multiple prompt variants deployed simultaneously with no tracking of which is active"
  - "Cannot compare quality metrics between prompt version A and version B"
  - "Rollback requires manual reconstruction of the previous prompt from git history"
  - "No audit trail of who changed what in the system prompt and when"
---

## Why This Happens

Prompt templates are often stored as string constants in code or configuration files. When a developer edits the system prompt to improve one behavior, no version record is created, no fingerprint is computed, and no correlation is made between the change and subsequent quality metrics. The prompt is just a string — it has no identity. Prompt version tracking assigns each unique prompt content a stable fingerprint, records that fingerprint alongside every session and quality metric, and enables before/after comparisons when a regression is suspected.

## Solution 1: Prompt Version Record

```python
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class PromptVersion:
    version_id: str              # SHA-256[:12] of normalized content
    template_name: str
    content: str
    author: str
    created_at: float
    description: str = ""        # human-readable change description
    parent_version_id: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def from_content(
        template_name: str,
        content: str,
        author: str,
        description: str = "",
        parent_version_id: Optional[str] = None,
    ) -> "PromptVersion":
        normalized = content.strip()
        version_id = hashlib.sha256(
            f"{template_name}::{normalized}".encode()
        ).hexdigest()[:12]
        return PromptVersion(
            version_id=version_id,
            template_name=template_name,
            content=normalized,
            author=author,
            created_at=time.time(),
            description=description,
            parent_version_id=parent_version_id,
        )

    def fingerprint(self) -> str:
        return self.version_id
```

## Solution 2: Prompt Version Registry

```python
import json
from pathlib import Path
from threading import Lock
from typing import Dict, List, Optional


class PromptVersionRegistry:
    """
    Stores the full history of prompt versions per template name.
    Persists to a JSON file for durability across restarts.
    """

    def __init__(self, store_path: str = "/tmp/prompt_versions.json"):
        self._path = Path(store_path)
        self._lock = Lock()
        self._versions: Dict[str, List[dict]] = {}
        self._active: Dict[str, str] = {}   # template_name -> active version_id
        self._load()

    def register(self, version: PromptVersion) -> None:
        with self._lock:
            if version.template_name not in self._versions:
                self._versions[version.template_name] = []
            # Avoid duplicate registration
            existing_ids = {v["version_id"] for v in self._versions[version.template_name]}
            if version.version_id not in existing_ids:
                self._versions[version.template_name].append({
                    "version_id": version.version_id,
                    "author": version.author,
                    "created_at": version.created_at,
                    "description": version.description,
                    "parent_version_id": version.parent_version_id,
                    "content_preview": version.content[:200],
                })
            self._active[version.template_name] = version.version_id
            self._save()

    def active_version_id(self, template_name: str) -> Optional[str]:
        with self._lock:
            return self._active.get(template_name)

    def history(self, template_name: str) -> List[dict]:
        with self._lock:
            return list(self._versions.get(template_name, []))

    def rollback(self, template_name: str, version_id: str) -> bool:
        with self._lock:
            versions = self._versions.get(template_name, [])
            if any(v["version_id"] == version_id for v in versions):
                self._active[template_name] = version_id
                self._save()
                return True
        return False

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text())
            self._versions = data.get("versions", {})
            self._active = data.get("active", {})
        except (json.JSONDecodeError, OSError):
            pass

    def _save(self) -> None:
        data = {"versions": self._versions, "active": self._active}
        self._path.write_text(json.dumps(data, indent=2))
```

## Solution 3: Session Prompt Version Tracker

```python
import time
from threading import Lock
from typing import Dict, List, Optional


@dataclass
class SessionVersionRecord:
    session_id: str
    template_name: str
    version_id: str
    started_at: float
    quality_score: Optional[float] = None
    outcome: Optional[str] = None   # "success" | "failure" | "abandoned"


class SessionPromptVersionTracker:
    """
    Records which prompt version each session ran under.
    Allows quality metrics to be correlated with specific prompt versions.
    """

    def __init__(self, max_records: int = 100_000):
        self._lock = Lock()
        self._records: List[SessionVersionRecord] = []
        self._max = max_records

    def record_session_start(
        self, session_id: str, template_name: str, version_id: str
    ) -> None:
        with self._lock:
            if len(self._records) >= self._max:
                self._records.pop(0)
            self._records.append(SessionVersionRecord(
                session_id=session_id,
                template_name=template_name,
                version_id=version_id,
                started_at=time.time(),
            ))

    def update_outcome(
        self,
        session_id: str,
        quality_score: Optional[float] = None,
        outcome: Optional[str] = None,
    ) -> None:
        with self._lock:
            for r in reversed(self._records):
                if r.session_id == session_id:
                    r.quality_score = quality_score
                    r.outcome = outcome
                    break

    def quality_by_version(
        self,
        template_name: str,
        window_seconds: float = 86400.0,
    ) -> Dict[str, dict]:
        cutoff = time.time() - window_seconds
        with self._lock:
            relevant = [
                r for r in self._records
                if r.template_name == template_name
                and r.started_at >= cutoff
                and r.quality_score is not None
            ]

        by_version: Dict[str, List[float]] = {}
        for r in relevant:
            if r.version_id not in by_version:
                by_version[r.version_id] = []
            by_version[r.version_id].append(r.quality_score)

        return {
            vid: {
                "sessions": len(scores),
                "avg_quality": round(sum(scores) / len(scores), 4),
                "min_quality": round(min(scores), 4),
                "max_quality": round(max(scores), 4),
            }
            for vid, scores in by_version.items()
        }
```

## Solution 4: Prompt Version Change Detector

```python
import time
from typing import Optional


class PromptVersionChangeDetector:
    """
    Detects when the active prompt version changes and emits a structured
    change event. Useful for correlating deployment times with metric shifts.
    """

    def __init__(self, registry: PromptVersionRegistry):
        self._registry = registry
        self._last_seen: Dict[str, str] = {}
        self._change_events: list = []

    def check(self, template_name: str) -> Optional[dict]:
        current = self._registry.active_version_id(template_name)
        if current is None:
            return None

        previous = self._last_seen.get(template_name)
        if previous != current:
            event = {
                "ts": time.time(),
                "template_name": template_name,
                "previous_version_id": previous,
                "new_version_id": current,
            }
            self._last_seen[template_name] = current
            self._change_events.append(event)
            return event
        return None

    def recent_changes(self, last_n: int = 20) -> list:
        return self._change_events[-last_n:]
```

## Solution 5: Version Quality Comparator

```python
from typing import Optional


class PromptVersionQualityComparator:
    """
    Compares quality metrics between two prompt versions to surface
    regressions or improvements introduced by a specific change.
    """

    def __init__(self, tracker: SessionPromptVersionTracker):
        self._tracker = tracker

    def compare(
        self,
        template_name: str,
        version_a: str,
        version_b: str,
        window_seconds: float = 86400.0,
    ) -> dict:
        by_version = self._tracker.quality_by_version(template_name, window_seconds)

        stats_a = by_version.get(version_a)
        stats_b = by_version.get(version_b)

        if not stats_a or not stats_b:
            return {
                "status": "insufficient_data",
                "version_a": version_a,
                "version_b": version_b,
                "stats_a": stats_a,
                "stats_b": stats_b,
            }

        delta = stats_b["avg_quality"] - stats_a["avg_quality"]
        pct_change = delta / max(abs(stats_a["avg_quality"]), 0.001) * 100

        return {
            "status": "regression" if delta < -0.05 else "improvement" if delta > 0.05 else "neutral",
            "version_a": version_a,
            "version_b": version_b,
            "avg_quality_a": stats_a["avg_quality"],
            "avg_quality_b": stats_b["avg_quality"],
            "delta": round(delta, 4),
            "pct_change": round(pct_change, 1),
            "sessions_a": stats_a["sessions"],
            "sessions_b": stats_b["sessions"],
        }
```

## Solution 6: Prompt Version Dashboard

```python
import time


class PromptVersionDashboard:
    """
    Combines version history, active versions, quality breakdown,
    and recent change events into a single engineering view.
    """

    def __init__(
        self,
        registry: PromptVersionRegistry,
        tracker: SessionPromptVersionTracker,
        change_detector: PromptVersionChangeDetector,
    ):
        self._registry = registry
        self._tracker = tracker
        self._detector = change_detector

    def render(self, template_name: str) -> dict:
        active_id = self._registry.active_version_id(template_name)
        history = self._registry.history(template_name)
        quality = self._tracker.quality_by_version(template_name, window_seconds=86400.0)
        changes = self._detector.recent_changes()

        return {
            "generated_at": time.time(),
            "template_name": template_name,
            "active_version_id": active_id,
            "version_count": len(history),
            "history": history[-10:],   # last 10 versions
            "quality_last_24h": quality,
            "recent_changes": [c for c in changes if c["template_name"] == template_name],
        }
```

## Comparison

| Approach | Content Fingerprint | Version History | Session Correlation | Quality Comparison | Change Detection |
|---|---|---|---|---|---|
| PromptVersionRegistry | Yes (SHA-256[:12]) | Yes (per template) | No | No | No |
| SessionPromptVersionTracker | No | No | Yes | Yes (by version) | No |
| PromptVersionChangeDetector | No | No | No | No | Yes |
| PromptVersionQualityComparator | No | No | Via tracker | Yes (A vs B) | No |
| PromptVersionDashboard | No | Via registry | Via tracker | Via tracker | Via detector |

**Best for production**: Compute `PromptVersion.from_content()` at agent startup and register it — the SHA-256 fingerprint is deterministic so restarting with the same prompt does not create a new version. Store `version_id` as a tag on every LLM API call metric so your observability platform can group quality metrics by prompt version without any post-processing. When a quality regression is detected, use `PromptVersionQualityComparator.compare()` between the version before and after the last deployment: a `status == "regression"` with sufficient session counts is evidence enough to trigger a rollback via `PromptVersionRegistry.rollback()`.
