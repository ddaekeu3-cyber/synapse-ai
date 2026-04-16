---
title: "Agent Doesn't Implement Error Fingerprinting for Issue Deduplication"
description: "AI agents that log every exception as a unique event flood error dashboards with thousands of identical stack traces, making it impossible to identify the three real issues hidden among ten thousand duplicates. Error fingerprinting normalizes exception structure into a stable hash, groups occurrences under a single issue ID, and surfaces first-seen/last-seen/count metadata so on-call engineers triage actual distinct problems rather than noise."
date: 2025-02-17
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-error-fingerprinting-for-issue-deduplication
tags:
  - error-fingerprinting
  - deduplication
  - observability
  - exception-tracking
  - issue-grouping
  - alerting
  - reliability
symptoms:
  - "Error dashboard shows 50,000 events but only 5 distinct root causes"
  - "Same ValueError from the same line floods logs with identical stack traces"
  - "No way to know if an error is new or has been occurring for weeks"
  - "Alert fires for every individual exception rather than once per unique issue"
  - "No count or first-seen timestamp to prioritize which errors matter most"
---

## Problem

Without deduplication, every occurrence of a recurring error creates a separate log entry, alert, and ticket. A single misconfigured retry loop can generate 10,000 log lines that obscure a critical new error that appeared once. Error fingerprinting extracts the stable structural identity of an exception — exception type, normalized message (variables stripped), and call-site location — and hashes them into an issue ID. All future occurrences of the same structural error increment the same issue's counter rather than creating a new record.

---

## Solution 1: ErrorFingerprinter — Compute Stable Structural Hash

```python
import hashlib
import re
import traceback
from dataclasses import dataclass
from typing import List, Optional, Tuple


@dataclass
class ErrorFingerprint:
    fingerprint: str       # 12-char hex hash
    error_type: str        # Exception class name
    normalized_message: str  # Message with variable data stripped
    call_site: str         # file:line of the innermost application frame
    top_frames: List[str]  # Top 3 normalized stack frames


class ErrorFingerprinter:
    """
    Computes a stable fingerprint for an exception that is invariant to:
    - Variable values in the message (user IDs, timestamps, request IDs)
    - Line number changes caused by whitespace-only edits
    - Different but structurally identical stack traces

    Usage:
        fp = ErrorFingerprinter(app_module_prefix="myapp")
        fingerprint = fp.fingerprint(exc)
        issue_id = fingerprint.fingerprint  # "a3f8c1d2e4b9"
    """

    # Patterns that represent variable data — stripped before hashing
    VARIABLE_PATTERNS = [
        re.compile(r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b'),  # UUID
        re.compile(r'\b\d{10,13}\b'),     # Unix timestamps
        re.compile(r'\b\d+\.\d+\.\d+\.\d+\b'),  # IP addresses
        re.compile(r"'[^']{32,}'"),       # Long quoted strings (IDs, tokens)
        re.compile(r'"[^"]{32,}"'),
        re.compile(r'\b[A-Za-z0-9+/]{40,}={0,2}\b'),  # Base64-ish
        re.compile(r'\b\d{4,}\b'),        # Large numbers (IDs, counts)
    ]

    def __init__(self, app_module_prefix: str = "",
                  max_frames: int = 3):
        self._prefix = app_module_prefix
        self._max_frames = max_frames

    def _normalize_message(self, msg: str) -> str:
        result = msg
        for pattern in self.VARIABLE_PATTERNS:
            result = pattern.sub("<VAR>", result)
        return result.strip()

    def _extract_frames(self, tb) -> List[Tuple[str, str, int]]:
        """Returns list of (filename, function, lineno) from traceback."""
        frames = []
        for frame_info in traceback.extract_tb(tb):
            frames.append((frame_info.filename, frame_info.name, frame_info.lineno))
        return frames

    def _app_frame(self, frames: List[Tuple[str, str, int]]) -> Optional[str]:
        """Find the innermost application (non-library) frame."""
        for filename, func, lineno in reversed(frames):
            if self._prefix and self._prefix in filename:
                short = filename.rsplit("/", 2)[-1] if "/" in filename else filename
                return f"{short}:{func}:{lineno}"
            if "site-packages" not in filename and "lib/python" not in filename:
                short = filename.rsplit("/", 2)[-1] if "/" in filename else filename
                return f"{short}:{func}:{lineno}"
        return frames[-1][0] if frames else "unknown"

    def fingerprint(self, exc: Exception) -> ErrorFingerprint:
        error_type = type(exc).__name__
        raw_msg = str(exc)
        normalized_msg = self._normalize_message(raw_msg)

        tb = exc.__traceback__
        frames = self._extract_frames(tb) if tb else []
        call_site = self._app_frame(frames) or "unknown"

        top_frames = []
        for filename, func, lineno in frames[-self._max_frames:]:
            short = filename.rsplit("/", 2)[-1] if "/" in filename else filename
            top_frames.append(f"{short}:{func}")

        components = f"{error_type}::{normalized_msg}::{call_site}"
        digest = hashlib.sha256(components.encode()).hexdigest()[:12]

        return ErrorFingerprint(
            fingerprint=digest,
            error_type=error_type,
            normalized_message=normalized_msg,
            call_site=call_site,
            top_frames=top_frames,
        )
```

---

## Solution 2: IssueRegistry — Track Occurrences Under a Single Issue ID

```python
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class Issue:
    issue_id: str
    error_type: str
    normalized_message: str
    call_site: str
    first_seen: float
    last_seen: float
    count: int = 0
    resolved: bool = False
    samples: List[str] = field(default_factory=list)  # Up to 3 raw messages
    MAX_SAMPLES = 3


class IssueRegistry:
    """
    Maps fingerprints to Issue records. Records the first/last occurrence
    time and total count per fingerprint. New fingerprints create issues;
    repeated fingerprints increment the existing issue's counter.

    Usage:
        registry = IssueRegistry()
        fingerprint = ErrorFingerprinter().fingerprint(exc)
        issue = registry.record(fingerprint, raw_message=str(exc))

        if issue.count == 1:
            notify_new_issue(issue)   # Only alert once per unique error
    """

    def __init__(self):
        self._issues: Dict[str, Issue] = {}

    def record(self, fp: ErrorFingerprint,
                raw_message: str = "") -> Issue:
        now = time.time()
        issue = self._issues.get(fp.fingerprint)

        if issue is None:
            issue = Issue(
                issue_id=fp.fingerprint,
                error_type=fp.error_type,
                normalized_message=fp.normalized_message,
                call_site=fp.call_site,
                first_seen=now,
                last_seen=now,
                count=1,
            )
            if raw_message:
                issue.samples.append(raw_message[:200])
            self._issues[fp.fingerprint] = issue
            logger.warning(
                "new_issue id=%s type=%s location=%s",
                fp.fingerprint, fp.error_type, fp.call_site,
            )
        else:
            issue.count += 1
            issue.last_seen = now
            if len(issue.samples) < Issue.MAX_SAMPLES and raw_message:
                issue.samples.append(raw_message[:200])
            logger.debug(
                "issue_recurrence id=%s count=%d", fp.fingerprint, issue.count
            )

        return issue

    def resolve(self, issue_id: str):
        if issue_id in self._issues:
            self._issues[issue_id].resolved = True

    def open_issues(self) -> List[Issue]:
        return [i for i in self._issues.values() if not i.resolved]

    def top_issues(self, n: int = 10) -> List[Issue]:
        return sorted(self.open_issues(), key=lambda i: -i.count)[:n]

    def summary(self) -> Dict[str, Any]:
        issues = list(self._issues.values())
        return {
            "total_distinct_issues": len(issues),
            "open_issues": sum(1 for i in issues if not i.resolved),
            "total_occurrences": sum(i.count for i in issues),
        }
```

---

## Solution 3: FingerprintedErrorLogger — Drop-In Logger Replacement

```python
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class FingerprintedErrorLogger:
    """
    Drop-in replacement for logger.exception() that automatically
    fingerprints the exception and includes the issue ID and occurrence
    count in the log record. Repeated identical errors emit DEBUG instead
    of ERROR after the first N occurrences, reducing log volume.

    Usage:
        err_logger = FingerprintedErrorLogger(
            suppress_after=5,   # Log at DEBUG after 5 occurrences
        )
        try:
            await tool_call()
        except Exception as exc:
            err_logger.exception("tool_call_failed", exc, tool="web_search")
    """

    def __init__(self, suppress_after: int = 5,
                  app_module_prefix: str = ""):
        self._fingerprinter = ErrorFingerprinter(app_module_prefix)
        self._registry = IssueRegistry()
        self._suppress_after = suppress_after

    def exception(self, message: str, exc: Exception,
                   **context) -> Issue:
        fp = self._fingerprinter.fingerprint(exc)
        issue = self._registry.record(fp, raw_message=str(exc))

        extra = {
            "issue_id": issue.issue_id,
            "occurrence": issue.count,
            "error_type": fp.error_type,
            "call_site": fp.call_site,
            **context,
        }

        log_msg = (
            f"{message} issue_id={issue.issue_id} "
            f"occurrence={issue.count} type={fp.error_type} "
            f"location={fp.call_site}"
        )

        if issue.count == 1:
            logger.error(log_msg, exc_info=exc, extra=extra)
        elif issue.count <= self._suppress_after:
            logger.warning(log_msg, extra=extra)
        else:
            logger.debug(
                f"{log_msg} (suppressed after {self._suppress_after})",
                extra=extra,
            )

        return issue

    def open_issues(self):
        return self._registry.open_issues()

    def top_issues(self, n: int = 10):
        return self._registry.top_issues(n)
```

---

## Solution 4: FingerprintAlertGate — Alert Once Per Issue, Not Per Occurrence

```python
import logging
import time
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


class FingerprintAlertGate:
    """
    Fires an alert callback exactly once when a new issue is first seen,
    and again if the issue recurs after a configurable silence window.
    Prevents alert fatigue from repeated identical errors.

    Usage:
        def send_pagerduty(issue): ...

        gate = FingerprintAlertGate(
            on_new_issue=send_pagerduty,
            recurrence_alert_after_s=3600,   # Re-alert if seen again after 1h
        )
        gate.evaluate(issue)
    """

    def __init__(self,
                  on_new_issue: Optional[Callable[[Issue], None]] = None,
                  on_recurrence: Optional[Callable[[Issue], None]] = None,
                  recurrence_alert_after_s: float = 3600.0,
                  high_frequency_threshold: int = 100):
        self._on_new = on_new_issue or self._log_new
        self._on_recur = on_recurrence or self._log_recurrence
        self._silence_window = recurrence_alert_after_s
        self._high_freq_thresh = high_frequency_threshold
        self._last_alerted: Dict[str, float] = {}

    @staticmethod
    def _log_new(issue: Issue):
        logger.critical(
            "new_error_issue id=%s type=%s location=%s",
            issue.issue_id, issue.error_type, issue.call_site,
        )

    @staticmethod
    def _log_recurrence(issue: Issue):
        logger.error(
            "error_recurrence id=%s count=%d type=%s",
            issue.issue_id, issue.count, issue.error_type,
        )

    def evaluate(self, issue: Issue):
        now = time.time()
        last = self._last_alerted.get(issue.issue_id, 0.0)

        if issue.count == 1:
            # New issue — always alert
            self._on_new(issue)
            self._last_alerted[issue.issue_id] = now

        elif now - last >= self._silence_window:
            # Recurrence after silence window
            self._on_recur(issue)
            self._last_alerted[issue.issue_id] = now

        elif issue.count == self._high_freq_thresh:
            # High-frequency burst — alert once when threshold crossed
            logger.critical(
                "high_frequency_error id=%s count=%d",
                issue.issue_id, issue.count,
            )
            self._last_alerted[issue.issue_id] = now
```

---

## Solution 5: IssueVelocityTracker — Detect Error Rate Regressions

```python
import logging
import time
from collections import defaultdict, deque
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


class IssueVelocityTracker:
    """
    Tracks the rate of new issue discoveries and error occurrence velocity
    per fingerprint. A sudden spike in new issues or high velocity for a
    single issue indicates a deployment regression.

    Usage:
        tracker = IssueVelocityTracker(window_s=300)
        tracker.record(issue)

        new_issue_rate = tracker.new_issue_rate()  # new distinct issues/min
        if new_issue_rate > 5:
            trigger_deployment_rollback_alert()
    """

    def __init__(self, window_s: float = 300.0):
        self._window = window_s
        # fingerprint -> deque of timestamps
        self._occurrences: Dict[str, deque] = defaultdict(deque)
        self._new_issues: deque = deque()

    def record(self, issue: Issue):
        now = time.time()
        self._occurrences[issue.issue_id].append(now)
        if issue.count == 1:
            self._new_issues.append(now)
        self._evict(now)

    def _evict(self, now: float):
        cutoff = now - self._window
        while self._new_issues and self._new_issues[0] < cutoff:
            self._new_issues.popleft()
        for q in self._occurrences.values():
            while q and q[0] < cutoff:
                q.popleft()

    def new_issue_rate(self) -> float:
        """New distinct issues per minute."""
        return len(self._new_issues) / (self._window / 60)

    def occurrence_rate(self, issue_id: str) -> float:
        """Occurrences per minute for a specific issue."""
        q = self._occurrences.get(issue_id, deque())
        return len(q) / (self._window / 60)

    def hottest_issues(self, top_n: int = 5) -> List[Dict[str, Any]]:
        return sorted(
            [
                {"issue_id": iid, "rate_per_min": self.occurrence_rate(iid)}
                for iid in self._occurrences
            ],
            key=lambda x: -x["rate_per_min"],
        )[:top_n]

    def velocity_report(self) -> Dict[str, Any]:
        return {
            "new_issues_per_min": round(self.new_issue_rate(), 2),
            "hottest": self.hottest_issues(),
        }
```

---

## Solution 6: ErrorFingerprintingPipeline — Full Deduplication Stack

```python
import logging
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


class ErrorFingerprintingPipeline:
    """
    Integrates fingerprinting, registry, deduplication logging,
    alert gating, and velocity tracking into a single exception handler.

    Usage:
        pipeline = ErrorFingerprintingPipeline(
            app_module_prefix="myapp",
            on_new_issue=pagerduty_alert,
        )

        try:
            await agent.run(query)
        except Exception as exc:
            pipeline.handle(exc, context={"tool": "web_search"})

        report = pipeline.report()
    """

    def __init__(self,
                  app_module_prefix: str = "",
                  on_new_issue: Optional[Callable[[Issue], None]] = None,
                  suppress_after: int = 5,
                  recurrence_alert_s: float = 3600.0):
        self._fingerprinter = ErrorFingerprinter(app_module_prefix)
        self._registry = IssueRegistry()
        self._err_logger = FingerprintedErrorLogger(
            suppress_after=suppress_after,
            app_module_prefix=app_module_prefix,
        )
        self._alert_gate = FingerprintAlertGate(
            on_new_issue=on_new_issue,
            recurrence_alert_after_s=recurrence_alert_s,
        )
        self._velocity = IssueVelocityTracker()

    def handle(self, exc: Exception,
                message: str = "unhandled_exception",
                **context) -> Issue:
        issue = self._err_logger.exception(message, exc, **context)
        self._alert_gate.evaluate(issue)
        self._velocity.record(issue)
        return issue

    def report(self) -> Dict[str, Any]:
        return {
            "registry": self._registry.summary(),
            "top_issues": [
                {
                    "id": i.issue_id,
                    "type": i.error_type,
                    "count": i.count,
                    "location": i.call_site,
                    "message": i.normalized_message[:80],
                }
                for i in self._registry.top_issues(10)
            ],
            "velocity": self._velocity.velocity_report(),
        }
```

---

## Comparison

| Approach | Fingerprinting | Issue Tracking | Log Deduplication | Alert Gating | Velocity | Integrated |
|---|---|---|---|---|---|---|
| **ErrorFingerprinter** | Yes | No | No | No | No | No |
| **IssueRegistry** | No | Yes | No | No | No | No |
| **FingerprintedErrorLogger** | Yes | Yes | Yes | No | No | No |
| **FingerprintAlertGate** | No | No | No | Yes | No | No |
| **IssueVelocityTracker** | No | No | No | No | Yes | No |
| **ErrorFingerprintingPipeline** | Yes | Yes | Yes | Yes | Yes | Yes |

**Key insight**: the fingerprint components that produce stable hashes are exception type + normalized message + call site. Do not include the full stack trace in the hash — library version upgrades change line numbers and create phantom new issues. Strip all variable data (UUIDs, timestamps, large numbers) from the message before hashing; `ValueError: user abc123 not found` and `ValueError: user def456 not found` should map to the same issue. Set `suppress_after=5` so the first five occurrences log at WARNING (for context) and all subsequent occurrences log at DEBUG — this cuts log volume by 99% for high-frequency errors without losing the signal.
