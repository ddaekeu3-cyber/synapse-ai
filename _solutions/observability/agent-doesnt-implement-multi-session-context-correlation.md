---
title: "Agent Doesn't Implement Multi-Session Context Correlation"
description: "Agents that treat every session as isolated miss cross-session patterns: a user who failed to complete a task three times in different sessions, a common tool failure sequence that spans multiple conversations, or a model quality regression that affects all sessions started after a deployment. Implement multi-session context correlation to link sessions by user, topic, tool sequence, and error pattern — enabling cross-session analytics and proactive support."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-multi-session-context-correlation
tags: [session-correlation, cross-session-analytics, user-journey, pattern-detection, multi-session, behavioral-analysis]
symptoms:
  - "Same user fails three times across different sessions — no alert fires because each session looks normal"
  - "Common tool failure sequence detected per session but never aggregated across sessions"
  - "Model quality regression visible only in aggregate — impossible to tell which sessions were affected"
  - "User journey analysis impossible because sessions are not linked by user identity"
  - "No way to know if a current session's problem was also seen by other users recently"
---

## Why This Happens

Session-level observability records what happened in one conversation. Cross-session correlation requires a secondary index that groups sessions by shared attributes — same user, same error fingerprint, same tool sequence, same time window around a deployment. Most observability implementations stop at per-session metrics and never build this index. Adding correlation requires a session store that supports multi-attribute queries and a correlation engine that runs after each session closes.

## Solution 1: Session Summary Record

```python
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class SessionSummaryRecord:
    """
    Compact summary of a completed session, stored for cross-session correlation.
    """
    session_id: str
    user_id: str
    agent_id: str
    started_at: float
    ended_at: float
    turn_count: int
    tool_sequence: List[str]          # ordered list of tool names called
    error_codes: List[str]            # error type codes encountered
    total_tokens: int
    completion_status: str            # "completed" | "abandoned" | "error" | "timeout"
    topic_fingerprint: str            # hash of first user message
    deployment_version: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def duration_seconds(self) -> float:
        return self.ended_at - self.started_at

    @property
    def had_error(self) -> bool:
        return bool(self.error_codes)

    @property
    def tool_sequence_key(self) -> str:
        return ":".join(self.tool_sequence[:5])   # first 5 tools as signature
```

## Solution 2: Cross-Session Index

```python
import time
from collections import defaultdict
from typing import Dict, List, Optional, Set


class CrossSessionIndex:
    """
    Secondary index over session summaries supporting lookups by:
    - user_id: all sessions for a user
    - tool_sequence_key: sessions sharing the same tool call pattern
    - error_code: sessions that hit a specific error
    - deployment_version: sessions started on a specific deployment
    - time_window: sessions in a given time range
    """

    def __init__(self, max_sessions: int = 100_000):
        self._sessions: Dict[str, SessionSummaryRecord] = {}
        self._by_user: Dict[str, List[str]] = defaultdict(list)
        self._by_tool_seq: Dict[str, List[str]] = defaultdict(list)
        self._by_error: Dict[str, List[str]] = defaultdict(list)
        self._by_version: Dict[str, List[str]] = defaultdict(list)
        self._max = max_sessions

    def index(self, record: SessionSummaryRecord) -> None:
        if len(self._sessions) >= self._max:
            # Evict oldest session
            oldest = min(self._sessions.values(), key=lambda r: r.started_at)
            self._remove(oldest.session_id)

        self._sessions[record.session_id] = record
        self._by_user[record.user_id].append(record.session_id)
        self._by_tool_seq[record.tool_sequence_key].append(record.session_id)
        for err in record.error_codes:
            self._by_error[err].append(record.session_id)
        if record.deployment_version:
            self._by_version[record.deployment_version].append(record.session_id)

    def _remove(self, session_id: str) -> None:
        record = self._sessions.pop(session_id, None)
        if not record:
            return
        self._by_user[record.user_id] = [
            s for s in self._by_user[record.user_id] if s != session_id
        ]
        self._by_tool_seq[record.tool_sequence_key] = [
            s for s in self._by_tool_seq[record.tool_sequence_key] if s != session_id
        ]

    def sessions_for_user(
        self, user_id: str, limit: int = 50
    ) -> List[SessionSummaryRecord]:
        ids = self._by_user.get(user_id, [])[-limit:]
        return [self._sessions[s] for s in ids if s in self._sessions]

    def sessions_with_tool_sequence(
        self, sequence_key: str
    ) -> List[SessionSummaryRecord]:
        ids = self._by_tool_seq.get(sequence_key, [])
        return [self._sessions[s] for s in ids if s in self._sessions]

    def sessions_with_error(
        self, error_code: str, since_seconds: float = 3600.0
    ) -> List[SessionSummaryRecord]:
        cutoff = time.time() - since_seconds
        ids = self._by_error.get(error_code, [])
        return [
            self._sessions[s]
            for s in ids
            if s in self._sessions and self._sessions[s].started_at >= cutoff
        ]

    def sessions_in_window(
        self, since: float, until: Optional[float] = None
    ) -> List[SessionSummaryRecord]:
        until = until or time.time()
        return [
            r for r in self._sessions.values()
            if since <= r.started_at <= until
        ]

    def stats(self) -> dict:
        return {
            "indexed_sessions": len(self._sessions),
            "unique_users": len(self._by_user),
            "unique_tool_sequences": len(self._by_tool_seq),
            "unique_errors": len(self._by_error),
            "unique_versions": len(self._by_version),
        }
```

## Solution 3: Repeated Failure Detector

```python
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class RepeatedFailureAlert:
    user_id: str
    failure_count: int
    recent_sessions: List[str]
    common_error_codes: List[str]
    common_tool_sequences: List[str]
    recommendation: str


class RepeatedFailureDetector:
    """
    Identifies users who have failed repeatedly across multiple sessions.
    Fires alerts when a user's failure rate exceeds a threshold,
    suggesting a systematic problem rather than a one-off issue.
    """

    def __init__(
        self,
        index: CrossSessionIndex,
        min_sessions: int = 3,
        failure_rate_threshold: float = 0.67,
        lookback_seconds: float = 86400.0,
    ):
        self._index = index
        self._min_sessions = min_sessions
        self._threshold = failure_rate_threshold
        self._lookback = lookback_seconds

    def check_user(self, user_id: str) -> Optional[RepeatedFailureAlert]:
        sessions = self._index.sessions_for_user(user_id)
        if len(sessions) < self._min_sessions:
            return None

        cutoff = __import__("time").time() - self._lookback
        recent = [s for s in sessions if s.started_at >= cutoff]
        if len(recent) < self._min_sessions:
            return None

        failed = [s for s in recent if s.had_error or s.completion_status != "completed"]
        rate = len(failed) / len(recent)
        if rate < self._threshold:
            return None

        # Common error codes across failed sessions
        error_counts: dict = {}
        for s in failed:
            for e in s.error_codes:
                error_counts[e] = error_counts.get(e, 0) + 1
        common_errors = sorted(error_counts, key=error_counts.get, reverse=True)[:3]

        # Common tool sequences
        seq_counts: dict = {}
        for s in failed:
            key = s.tool_sequence_key
            seq_counts[key] = seq_counts.get(key, 0) + 1
        common_seqs = sorted(seq_counts, key=seq_counts.get, reverse=True)[:2]

        return RepeatedFailureAlert(
            user_id=user_id,
            failure_count=len(failed),
            recent_sessions=[s.session_id for s in failed[-5:]],
            common_error_codes=common_errors,
            common_tool_sequences=common_seqs,
            recommendation=(
                f"user {user_id} has {len(failed)}/{len(recent)} failed sessions "
                f"(rate={rate:.0%}); review errors: {common_errors}"
            ),
        )
```

## Solution 4: Deployment Impact Analyzer

```python
from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class DeploymentImpactReport:
    deployment_version: str
    session_count: int
    error_rate: float
    avg_tokens: float
    avg_duration_seconds: float
    top_errors: List[str]
    comparison_to_previous: Optional[dict]


class DeploymentImpactAnalyzer:
    """
    Compares session quality metrics across deployment versions.
    Detects regressions introduced by a new deployment by comparing
    error rates, token usage, and session duration against the prior version.
    """

    def __init__(self, index: CrossSessionIndex):
        self._index = index

    def analyze(
        self,
        version: str,
        baseline_version: Optional[str] = None,
    ) -> DeploymentImpactReport:
        sessions = [
            self._index._sessions[sid]
            for sid in self._index._by_version.get(version, [])
            if sid in self._index._sessions
        ]
        if not sessions:
            return DeploymentImpactReport(
                deployment_version=version,
                session_count=0,
                error_rate=0.0,
                avg_tokens=0.0,
                avg_duration_seconds=0.0,
                top_errors=[],
                comparison_to_previous=None,
            )

        errors = [s for s in sessions if s.had_error]
        error_rate = len(errors) / len(sessions)
        avg_tokens = sum(s.total_tokens for s in sessions) / len(sessions)
        avg_duration = sum(s.duration_seconds for s in sessions) / len(sessions)

        err_counts: dict = {}
        for s in errors:
            for e in s.error_codes:
                err_counts[e] = err_counts.get(e, 0) + 1
        top_errors = sorted(err_counts, key=err_counts.get, reverse=True)[:5]

        comparison = None
        if baseline_version:
            baseline = self.analyze(baseline_version)
            if baseline.session_count > 0:
                comparison = {
                    "error_rate_delta": round(error_rate - baseline.error_rate, 4),
                    "token_delta_pct": round(
                        (avg_tokens - baseline.avg_tokens) / max(baseline.avg_tokens, 1), 4
                    ),
                    "duration_delta_pct": round(
                        (avg_duration - baseline.avg_duration_seconds)
                        / max(baseline.avg_duration_seconds, 1), 4
                    ),
                    "regression": error_rate > baseline.error_rate * 1.20,
                }

        return DeploymentImpactReport(
            deployment_version=version,
            session_count=len(sessions),
            error_rate=round(error_rate, 4),
            avg_tokens=round(avg_tokens, 1),
            avg_duration_seconds=round(avg_duration, 1),
            top_errors=top_errors,
            comparison_to_previous=comparison,
        )
```

## Solution 5: Cross-Session Error Clustering

```python
from typing import Dict, List


class CrossSessionErrorClusterer:
    """
    Groups sessions by shared error code sequences to identify
    systemic failures vs random one-off errors.
    A cluster of 50 sessions all hitting the same error sequence
    indicates a systemic bug, not random noise.
    """

    def __init__(self, index: CrossSessionIndex, min_cluster_size: int = 5):
        self._index = index
        self._min_size = min_cluster_size

    def cluster_by_error(
        self, since_seconds: float = 3600.0
    ) -> List[dict]:
        cutoff = __import__("time").time() - since_seconds
        recent = [
            r for r in self._index._sessions.values()
            if r.started_at >= cutoff and r.had_error
        ]

        by_error_sig: Dict[str, List[SessionSummaryRecord]] = {}
        for s in recent:
            sig = ":".join(sorted(s.error_codes))
            by_error_sig.setdefault(sig, []).append(s)

        clusters = []
        for sig, sessions in by_error_sig.items():
            if len(sessions) >= self._min_size:
                affected_users = len({s.user_id for s in sessions})
                clusters.append({
                    "error_signature": sig,
                    "session_count": len(sessions),
                    "affected_users": affected_users,
                    "example_session_ids": [s.session_id for s in sessions[:3]],
                    "systemic": affected_users >= 3,
                })

        return sorted(clusters, key=lambda x: -x["session_count"])
```

## Solution 6: Multi-Session Correlation Dashboard

```python
import time
from typing import List, Optional


class MultiSessionCorrelationDashboard:
    """
    Unified cross-session observability view: user journey health,
    deployment impact, systemic error clusters, and repeated failure alerts.
    """

    def __init__(
        self,
        index: CrossSessionIndex,
        failure_detector: RepeatedFailureDetector,
        deployment_analyzer: DeploymentImpactAnalyzer,
        error_clusterer: CrossSessionErrorClusterer,
    ):
        self._index = index
        self._failure = failure_detector
        self._deployment = deployment_analyzer
        self._clusterer = error_clusterer

    def render(
        self,
        current_version: Optional[str] = None,
        baseline_version: Optional[str] = None,
    ) -> dict:
        error_clusters = self._clusterer.cluster_by_error(since_seconds=3600.0)
        systemic = [c for c in error_clusters if c["systemic"]]

        deployment_report = None
        if current_version:
            report = self._deployment.analyze(current_version, baseline_version)
            deployment_report = {
                "version": report.deployment_version,
                "sessions": report.session_count,
                "error_rate": report.error_rate,
                "regression": (
                    report.comparison_to_previous.get("regression", False)
                    if report.comparison_to_previous else False
                ),
                "top_errors": report.top_errors,
            }

        alerts = []
        for cluster in systemic[:3]:
            alerts.append(
                f"systemic error: '{cluster['error_signature']}' "
                f"in {cluster['session_count']} sessions, {cluster['affected_users']} users"
            )
        if deployment_report and deployment_report.get("regression"):
            alerts.append(
                f"deployment regression detected in version {current_version}: "
                f"error_rate={deployment_report['error_rate']:.1%}"
            )

        return {
            "generated_at": time.time(),
            "index_stats": self._index.stats(),
            "error_clusters_1h": len(error_clusters),
            "systemic_error_clusters": systemic[:5],
            "deployment_impact": deployment_report,
            "alerts": alerts,
            "healthy": len(alerts) == 0,
        }
```

## Comparison

| Approach | User Journey | Tool Sequence | Error Clustering | Deployment Impact |
|---|---|---|---|---|
| CrossSessionIndex | Yes (by user) | Yes (by seq key) | Via error code | Yes (by version) |
| RepeatedFailureDetector | Yes | No | No | No |
| DeploymentImpactAnalyzer | No | No | No | Yes (regression) |
| CrossSessionErrorClusterer | No | No | Yes (by error sig) | No |
| MultiSessionCorrelationDashboard | Via detector | Via index | Via clusterer | Via analyzer |

**Best for production**: Index every session summary in `CrossSessionIndex` when the session ends. Run `CrossSessionErrorClusterer.cluster_by_error()` every 15 minutes — a cluster of 10+ sessions with the same error signature in one hour is a P1 incident, not random noise. Use `DeploymentImpactAnalyzer` with the previous version as baseline after every deployment: a 20%+ increase in error rate is a rollback trigger. Wire `RepeatedFailureDetector` to a support ticketing system — proactively surfacing users with 3+ failures converts reactive support into proactive resolution.
