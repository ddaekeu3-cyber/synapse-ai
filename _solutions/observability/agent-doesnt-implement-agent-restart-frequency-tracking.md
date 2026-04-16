---
title: "Agent Doesn't Implement Agent Restart Frequency Tracking"
description: "Agents that do not track their own restart frequency cannot distinguish healthy rolling deployments from crash-loop patterns — an agent restarting 20 times per hour due to OOM kills looks identical to one restarting twice per week for scheduled maintenance. Implement restart frequency tracking with cause classification, crash-loop detection, and deployment-vs-crash discrimination."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-agent-restart-frequency-tracking
tags: [restart-tracking, crash-loop, process-lifecycle, oom-detection, deployment-observability, stability-metrics]
symptoms:
  - "Agent is in a crash loop but no alert fires because individual restarts look like deployments"
  - "No metric distinguishing scheduled restarts from unexpected process exits"
  - "OOM kills are invisible — process just disappears and restarts with no record"
  - "Cannot determine whether a new deployment improved or worsened crash frequency"
  - "On-call engineers discover crash loops only when users report degraded service"
---

## Why This Happens

Process restart metrics are rarely built into application code because the process that crashed is the one that would need to report its own death. Restart tracking requires a persistent state store (a file or external store) that survives process restart, so the newly-started process can read the restart history written by its predecessor. By recording exit cause (OOM, signal, exception, clean shutdown) and exit time at process shutdown, and reading that record on next startup, the agent can detect crash-loop patterns, distinguish crashes from deployments, and alert when restart frequency exceeds a safe threshold.

## Solution 1: Restart Record

```python
import os
import platform
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class RestartCause(str, Enum):
    CLEAN_SHUTDOWN = "clean_shutdown"
    UNHANDLED_EXCEPTION = "unhandled_exception"
    OOM_KILL = "oom_kill"
    SIGNAL_TERM = "signal_term"
    SIGNAL_KILL = "signal_kill"
    DEPLOYMENT = "deployment"
    HEALTH_CHECK_FAILURE = "health_check_failure"
    UNKNOWN = "unknown"


@dataclass
class RestartRecord:
    instance_id: str
    process_id: int
    started_at: float
    exited_at: Optional[float] = None
    exit_cause: RestartCause = RestartCause.UNKNOWN
    exit_code: Optional[int] = None
    uptime_seconds: Optional[float] = None
    deployment_version: str = ""
    environment: str = ""
    error_summary: str = ""

    def finalize(self, cause: RestartCause, exit_code: Optional[int] = None, error: str = "") -> None:
        self.exited_at = time.time()
        self.exit_cause = cause
        self.exit_code = exit_code
        self.uptime_seconds = round(self.exited_at - self.started_at, 2)
        self.error_summary = error[:300]

    @classmethod
    def for_current_process(cls) -> "RestartRecord":
        return cls(
            instance_id=os.getenv("INSTANCE_ID", f"pid-{os.getpid()}"),
            process_id=os.getpid(),
            started_at=time.time(),
            deployment_version=os.getenv("DEPLOYMENT_VERSION", "unknown"),
            environment=os.getenv("AGENT_ENVIRONMENT", "unknown"),
        )
```

## Solution 2: Persistent Restart History Store

```python
import json
import os
import time
from pathlib import Path
from threading import Lock
from typing import List, Optional


class PersistentRestartHistoryStore:
    """
    Writes restart records to a file that survives process restarts.
    The new process reads history written by predecessors on startup.
    """

    def __init__(self, path: str = "/tmp/agent_restart_history.json", max_records: int = 500):
        self._path = Path(path)
        self._max = max_records
        self._lock = Lock()

    def append(self, record: RestartRecord) -> None:
        with self._lock:
            records = self._load()
            records.append(self._serialize(record))
            records = records[-self._max:]
            self._path.write_text(json.dumps(records, indent=2))

    def load_all(self) -> List[dict]:
        with self._lock:
            return self._load()

    def recent(self, window_seconds: float = 3600.0) -> List[dict]:
        cutoff = time.time() - window_seconds
        return [r for r in self.load_all() if r.get("exited_at", 0) >= cutoff]

    def _load(self) -> List[dict]:
        if not self._path.exists():
            return []
        try:
            return json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError):
            return []

    @staticmethod
    def _serialize(record: RestartRecord) -> dict:
        return {
            "instance_id": record.instance_id,
            "process_id": record.process_id,
            "started_at": record.started_at,
            "exited_at": record.exited_at,
            "exit_cause": record.exit_cause.value,
            "exit_code": record.exit_code,
            "uptime_seconds": record.uptime_seconds,
            "deployment_version": record.deployment_version,
            "environment": record.environment,
            "error_summary": record.error_summary,
        }
```

## Solution 3: Crash Loop Detector

```python
import time
from typing import List


class CrashLoopDetector:
    """
    Detects crash loop patterns: N or more restarts within a time window
    where the cause is not a clean shutdown or deployment.
    """

    def __init__(
        self,
        store: PersistentRestartHistoryStore,
        crash_threshold: int = 5,
        window_seconds: float = 600.0,    # 10 minutes
        min_uptime_seconds: float = 60.0,  # restarts after <60s uptime = crash
    ):
        self._store = store
        self._threshold = crash_threshold
        self._window = window_seconds
        self._min_uptime = min_uptime_seconds

    CRASH_CAUSES = {
        RestartCause.UNHANDLED_EXCEPTION,
        RestartCause.OOM_KILL,
        RestartCause.SIGNAL_KILL,
        RestartCause.UNKNOWN,
        RestartCause.HEALTH_CHECK_FAILURE,
    }

    def detect(self) -> dict:
        recent = self._store.recent(self._window)
        crashes = [
            r for r in recent
            if r.get("exit_cause") in {c.value for c in self.CRASH_CAUSES}
            and (r.get("uptime_seconds") or 999) < self._min_uptime
        ]
        is_crash_loop = len(crashes) >= self._threshold
        return {
            "crash_loop_detected": is_crash_loop,
            "crash_count": len(crashes),
            "threshold": self._threshold,
            "window_seconds": self._window,
            "recent_causes": [r["exit_cause"] for r in crashes[-5:]],
        }
```

## Solution 4: Deployment vs Crash Discriminator

```python
from typing import List


class DeploymentVsCrashDiscriminator:
    """
    Separates restart history into deployment-driven restarts and
    unexpected crashes, enabling per-deployment stability comparison.
    """

    def __init__(self, store: PersistentRestartHistoryStore):
        self._store = store

    DEPLOYMENT_CAUSES = {
        RestartCause.CLEAN_SHUTDOWN.value,
        RestartCause.DEPLOYMENT.value,
        RestartCause.SIGNAL_TERM.value,
    }

    def classify(self, window_seconds: float = 86400.0) -> dict:
        recent = self._store.recent(window_seconds)
        deployments = [r for r in recent if r.get("exit_cause") in self.DEPLOYMENT_CAUSES]
        crashes = [r for r in recent if r.get("exit_cause") not in self.DEPLOYMENT_CAUSES]

        by_version: dict = {}
        for r in recent:
            ver = r.get("deployment_version", "unknown")
            if ver not in by_version:
                by_version[ver] = {"deployments": 0, "crashes": 0}
            if r.get("exit_cause") in self.DEPLOYMENT_CAUSES:
                by_version[ver]["deployments"] += 1
            else:
                by_version[ver]["crashes"] += 1

        return {
            "window_seconds": window_seconds,
            "total_restarts": len(recent),
            "deployment_restarts": len(deployments),
            "crash_restarts": len(crashes),
            "crash_rate": round(len(crashes) / max(len(recent), 1), 4),
            "by_deployment_version": by_version,
        }
```

## Solution 5: Restart Frequency Alerter

```python
import time
from typing import List


class RestartFrequencyAlerter:
    """
    Fires alerts when restart rate exceeds thresholds.
    Separates crash-type alert (crash loop) from high-frequency alert (many restarts).
    """

    def __init__(
        self,
        store: PersistentRestartHistoryStore,
        crash_loop_detector: CrashLoopDetector,
        max_restarts_per_hour: int = 10,
    ):
        self._store = store
        self._detector = crash_loop_detector
        self._max_per_hour = max_restarts_per_hour

    def evaluate(self) -> List[dict]:
        alerts = []
        recent_hour = self._store.recent(3600.0)

        if len(recent_hour) >= self._max_per_hour:
            alerts.append({
                "type": "high_restart_frequency",
                "severity": "warn",
                "restart_count": len(recent_hour),
                "threshold": self._max_per_hour,
                "window_seconds": 3600.0,
                "ts": time.time(),
            })

        crash_loop = self._detector.detect()
        if crash_loop["crash_loop_detected"]:
            alerts.append({
                "type": "crash_loop",
                "severity": "critical",
                **crash_loop,
                "ts": time.time(),
            })

        return alerts
```

## Solution 6: Restart Frequency Dashboard

```python
import time


class AgentRestartFrequencyDashboard:
    """
    Combines restart history, crash loop detection, deployment
    discrimination, and active alerts into an agent stability report.
    """

    def __init__(
        self,
        store: PersistentRestartHistoryStore,
        crash_detector: CrashLoopDetector,
        discriminator: DeploymentVsCrashDiscriminator,
        alerter: RestartFrequencyAlerter,
        current_record: RestartRecord,
    ):
        self._store = store
        self._detector = crash_detector
        self._discriminator = discriminator
        self._alerter = alerter
        self._current = current_record

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "current_instance": {
                "instance_id": self._current.instance_id,
                "uptime_seconds": round(time.time() - self._current.started_at, 1),
                "deployment_version": self._current.deployment_version,
            },
            "crash_loop": self._detector.detect(),
            "classification_24h": self._discriminator.classify(86400.0),
            "active_alerts": self._alerter.evaluate(),
            "recent_restarts_1h": len(self._store.recent(3600.0)),
        }
```

## Comparison

| Approach | Persistent History | Crash Loop Detection | Deployment Discrimination | Frequency Alerting | Dashboard |
|---|---|---|---|---|---|
| PersistentRestartHistoryStore | Yes (file) | No | No | No | No |
| CrashLoopDetector | Via store | Yes (threshold) | No | No | No |
| DeploymentVsCrashDiscriminator | Via store | No | Yes (per version) | No | No |
| RestartFrequencyAlerter | Via store | Via detector | No | Yes | No |
| AgentRestartFrequencyDashboard | No | No | No | No | Yes |

**Best for production**: Write the restart record at process startup (not shutdown — the process may be killed before shutdown hooks run) with `exit_cause=UNKNOWN`, then update it with the actual cause during a clean shutdown handler. This way, a crash that prevents the shutdown hook from running leaves an `UNKNOWN` record — which the `CrashLoopDetector` correctly counts as a crash. Store the history file on a volume that persists across container restarts (not a tmpfs) so the crash loop detector can see history from previous container incarnations. Emit `restart_cause` as a structured log field on every startup — this feeds external log-based alerting without requiring the dashboard endpoint to be polled.
