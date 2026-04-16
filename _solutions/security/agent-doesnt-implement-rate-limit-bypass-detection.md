---
title: "Agent Doesn't Implement Rate Limit Bypass Detection"
description: "Rate limits protect agent endpoints from abuse, but determined adversaries bypass them using distributed IPs, rotating API keys, header spoofing, or request fragmentation. Implement bypass detection that correlates requests across identity dimensions — IP, user agent, key fingerprint, and behavioral fingerprint — to detect bypass attempts even when individual identity vectors look clean."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-rate-limit-bypass-detection
tags: [rate-limit-bypass, abuse-detection, distributed-attack, fingerprinting, api-security, anomaly-detection]
symptoms:
  - "Rate limiter allows 10 req/min per IP — attacker uses 100 IPs to send 1000 req/min undetected"
  - "API key rotation: attacker cycles through many keys, each under the per-key limit"
  - "X-Forwarded-For spoofing bypasses IP-based rate limiting"
  - "Request fragmentation: attacker splits expensive queries into many cheap ones"
  - "No correlation between requests sharing the same payload structure despite different identities"
---

## Why This Happens

Per-IP or per-key rate limits work in isolation: each identity vector is checked independently, with no cross-dimension correlation. An attacker who controls 100 IPs or 100 API keys sends one request per identity per minute — each key looks clean, but together they send 100 requests per minute to the same endpoint. Bypass detection adds a correlation layer: it fingerprints requests by behavioral similarity (payload structure, timing pattern, endpoint sequence), groups requests that share fingerprints despite different identity vectors, and rate-limits the behavioral fingerprint group rather than just the individual identity.

## Solution 1: Request Fingerprint

```python
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class RequestFingerprint:
    """
    Multi-dimensional fingerprint for a single request.
    Combines identity vectors (IP, key) with behavioral signals
    (endpoint pattern, payload structure hash, user agent).
    """
    request_id: str
    timestamp: float = field(default_factory=time.time)
    # Identity vectors
    source_ip: str = ""
    api_key_hash: str = ""        # hashed — never store raw keys
    user_agent: str = ""
    forwarded_for: str = ""       # X-Forwarded-For header
    # Behavioral signals
    endpoint: str = ""
    method: str = ""
    payload_structure_hash: str = ""   # hash of field names (not values)
    payload_size_bucket: str = ""      # "tiny" | "small" | "medium" | "large"
    # Composite fingerprints
    identity_fingerprint: str = ""     # hash of identity vectors
    behavioral_fingerprint: str = ""   # hash of behavioral signals

    @classmethod
    def build(
        cls,
        request_id: str,
        source_ip: str,
        api_key: str,
        user_agent: str,
        endpoint: str,
        method: str,
        payload: Dict[str, Any],
        forwarded_for: str = "",
    ) -> "RequestFingerprint":
        def h(s: str) -> str:
            return hashlib.sha256(s.encode()).hexdigest()[:12]

        payload_structure = sorted(payload.keys()) if payload else []
        payload_hash = h(":".join(payload_structure))
        size = len(str(payload))
        size_bucket = (
            "tiny" if size < 100 else
            "small" if size < 1000 else
            "medium" if size < 10000 else "large"
        )
        identity_fp = h(f"{source_ip}:{h(api_key)}:{user_agent}")
        behavioral_fp = h(f"{endpoint}:{method}:{payload_hash}:{size_bucket}")

        return cls(
            request_id=request_id,
            source_ip=source_ip,
            api_key_hash=h(api_key),
            user_agent=user_agent,
            forwarded_for=forwarded_for,
            endpoint=endpoint,
            method=method,
            payload_structure_hash=payload_hash,
            payload_size_bucket=size_bucket,
            identity_fingerprint=identity_fp,
            behavioral_fingerprint=behavioral_fp,
        )
```

## Solution 2: Behavioral Rate Counter

```python
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Tuple


class BehavioralRateCounter:
    """
    Counts requests per behavioral fingerprint in a sliding window.
    Multiple identity vectors can share the same behavioral fingerprint,
    making distributed bypass visible as a single aggregated count.
    """

    def __init__(self, window_seconds: float = 60.0):
        self._window = window_seconds
        # behavioral_fp -> deque of (timestamp, identity_fp)
        self._requests: Dict[str, Deque[Tuple[float, str]]] = defaultdict(deque)
        self._identity_sets: Dict[str, set] = defaultdict(set)

    def record(self, fp: RequestFingerprint) -> None:
        bfp = fp.behavioral_fingerprint
        now = time.time()
        self._requests[bfp].append((now, fp.identity_fingerprint))
        self._identity_sets[bfp].add(fp.identity_fingerprint)
        self._trim(bfp)

    def _trim(self, bfp: str) -> None:
        cutoff = time.time() - self._window
        q = self._requests[bfp]
        while q and q[0][0] < cutoff:
            q.popleft()
        # Rebuild identity set from surviving entries
        self._identity_sets[bfp] = {ifp for _, ifp in q}

    def count(self, behavioral_fp: str) -> int:
        self._trim(behavioral_fp)
        return len(self._requests.get(behavioral_fp, []))

    def unique_identities(self, behavioral_fp: str) -> int:
        self._trim(behavioral_fp)
        return len(self._identity_sets.get(behavioral_fp, set()))

    def top_behavioral_patterns(self, top_n: int = 10) -> list:
        return sorted(
            [
                {
                    "behavioral_fp": bfp,
                    "request_count": self.count(bfp),
                    "unique_identities": self.unique_identities(bfp),
                }
                for bfp in self._requests
            ],
            key=lambda x: -x["request_count"],
        )[:top_n]
```

## Solution 3: Bypass Signal Detector

```python
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
import time


@dataclass
class BypassSignal:
    signal_type: str     # "distributed_ip" | "key_rotation" | "header_spoof" | "fragmentation"
    behavioral_fp: str
    request_count: int
    unique_identities: int
    severity: str        # "low" | "medium" | "high" | "critical"
    detail: str
    detected_at: float = field(default_factory=time.time)


class BypassSignalDetector:
    """
    Analyzes behavioral rate counts to detect specific bypass patterns.
    """

    def __init__(
        self,
        counter: BehavioralRateCounter,
        max_requests_per_pattern: int = 100,
        max_identities_per_pattern: int = 5,
        fragmentation_request_ratio: float = 10.0,
    ):
        self._counter = counter
        self._max_requests = max_requests_per_pattern
        self._max_identities = max_identities_per_pattern
        self._frag_ratio = fragmentation_request_ratio

    def detect(self, fp: RequestFingerprint) -> List[BypassSignal]:
        bfp = fp.behavioral_fingerprint
        count = self._counter.count(bfp)
        unique = self._counter.unique_identities(bfp)
        signals = []

        # Distributed identity bypass: many identities, same behavior
        if unique > self._max_identities and count > self._max_requests:
            severity = "critical" if unique > self._max_identities * 5 else "high"
            signals.append(BypassSignal(
                signal_type="distributed_bypass",
                behavioral_fp=bfp,
                request_count=count,
                unique_identities=unique,
                severity=severity,
                detail=(
                    f"{unique} different identities sharing behavioral pattern; "
                    f"{count} total requests in window"
                ),
            ))

        # Header spoofing: forwarded_for differs from source_ip pattern
        if (
            fp.forwarded_for
            and fp.forwarded_for != fp.source_ip
            and "," in fp.forwarded_for
        ):
            signals.append(BypassSignal(
                signal_type="header_spoof",
                behavioral_fp=bfp,
                request_count=count,
                unique_identities=unique,
                severity="medium",
                detail=f"X-Forwarded-For chain '{fp.forwarded_for[:50]}' differs from source IP",
            ))

        # Request fragmentation: many tiny requests with same structure
        if fp.payload_size_bucket == "tiny" and count > self._max_requests * self._frag_ratio:
            signals.append(BypassSignal(
                signal_type="fragmentation",
                behavioral_fp=bfp,
                request_count=count,
                unique_identities=unique,
                severity="medium",
                detail=f"high volume of tiny requests ({count}/window) with identical structure",
            ))

        return signals
```

## Solution 4: Bypass Incident Log

```python
import time
from typing import Dict, List


class BypassIncidentLog:
    """
    Append-only log of rate-limit bypass detection events.
    Deduplicates identical signals within a cooldown window to prevent
    alert storms from a single ongoing attack.
    """

    def __init__(
        self,
        max_entries: int = 5_000,
        dedup_window_seconds: float = 300.0,
    ):
        self._log: List[BypassSignal] = []
        self._max = max_entries
        self._dedup_window = dedup_window_seconds
        self._last_seen: Dict[str, float] = {}   # (type, bfp) -> timestamp

    def record(self, signal: BypassSignal) -> bool:
        """Returns True if the signal was newly recorded (not a duplicate)."""
        key = f"{signal.signal_type}:{signal.behavioral_fp}"
        last = self._last_seen.get(key, 0)
        if time.time() - last < self._dedup_window:
            return False   # deduplicated

        self._last_seen[key] = time.time()
        if len(self._log) >= self._max:
            self._log.pop(0)
        self._log.append(signal)
        return True

    def recent(self, hours: float = 1.0) -> List[BypassSignal]:
        cutoff = time.time() - hours * 3600
        return [s for s in self._log if s.detected_at >= cutoff]

    def summary(self) -> dict:
        recent = self.recent(1.0)
        by_type: Dict[str, int] = {}
        for s in recent:
            by_type[s.signal_type] = by_type.get(s.signal_type, 0) + 1
        return {
            "incidents_last_hour": len(recent),
            "by_type": by_type,
            "critical_count": sum(1 for s in recent if s.severity == "critical"),
            "high_count": sum(1 for s in recent if s.severity == "high"),
        }
```

## Solution 5: Adaptive Block List

```python
import time
from typing import Dict, Set


class AdaptiveBlockList:
    """
    Maintains a temporary block list of behavioral fingerprints and
    identity vectors detected in bypass attacks.
    Blocks expire after a configurable TTL.
    """

    def __init__(self, default_block_ttl_seconds: float = 3600.0):
        self._blocked_behavioral: Dict[str, float] = {}   # fp -> expires_at
        self._blocked_identity: Dict[str, float] = {}
        self._default_ttl = default_block_ttl_seconds

    def block_behavioral(
        self, behavioral_fp: str, ttl_seconds: Optional[float] = None
    ) -> None:
        ttl = ttl_seconds or self._default_ttl
        self._blocked_behavioral[behavioral_fp] = time.time() + ttl

    def block_identity(
        self, identity_fp: str, ttl_seconds: Optional[float] = None
    ) -> None:
        ttl = ttl_seconds or self._default_ttl
        self._blocked_identity[identity_fp] = time.time() + ttl

    def is_blocked(self, fp: RequestFingerprint) -> bool:
        now = time.time()
        bfp_expires = self._blocked_behavioral.get(fp.behavioral_fingerprint, 0)
        ifp_expires = self._blocked_identity.get(fp.identity_fingerprint, 0)
        return now < bfp_expires or now < ifp_expires

    def purge_expired(self) -> int:
        now = time.time()
        before = len(self._blocked_behavioral) + len(self._blocked_identity)
        self._blocked_behavioral = {
            k: v for k, v in self._blocked_behavioral.items() if v > now
        }
        self._blocked_identity = {
            k: v for k, v in self._blocked_identity.items() if v > now
        }
        return before - (len(self._blocked_behavioral) + len(self._blocked_identity))

    def stats(self) -> dict:
        now = time.time()
        return {
            "blocked_behavioral_patterns": sum(
                1 for v in self._blocked_behavioral.values() if v > now
            ),
            "blocked_identities": sum(
                1 for v in self._blocked_identity.values() if v > now
            ),
        }


from typing import Optional
```

## Solution 6: Bypass Detection Gateway

```python
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class BypassCheckResult:
    allowed: bool
    blocked: bool
    signals: List[BypassSignal]
    block_reason: str = ""


class BypassDetectionGateway:
    """
    Entry point for all incoming requests: fingerprints, records,
    detects bypass signals, updates block list, and returns an allow/block decision.
    """

    def __init__(
        self,
        counter: BehavioralRateCounter,
        detector: BypassSignalDetector,
        incident_log: BypassIncidentLog,
        block_list: AdaptiveBlockList,
        auto_block_on_critical: bool = True,
        auto_block_on_high: bool = False,
    ):
        self._counter = counter
        self._detector = detector
        self._log = incident_log
        self._block_list = block_list
        self._auto_block_critical = auto_block_on_critical
        self._auto_block_high = auto_block_on_high

    def check(self, fp: RequestFingerprint) -> BypassCheckResult:
        # Check existing blocks first
        if self._block_list.is_blocked(fp):
            return BypassCheckResult(
                allowed=False,
                blocked=True,
                signals=[],
                block_reason="behavioral_or_identity_blocked",
            )

        # Record and detect
        self._counter.record(fp)
        signals = self._detector.detect(fp)
        new_incidents = []
        for sig in signals:
            if self._log.record(sig):
                new_incidents.append(sig)

        # Auto-block on severe signals
        for sig in new_incidents:
            if sig.severity == "critical" and self._auto_block_critical:
                self._block_list.block_behavioral(sig.behavioral_fp)
            elif sig.severity == "high" and self._auto_block_high:
                self._block_list.block_behavioral(sig.behavioral_fp, 1800.0)

        critical = [s for s in signals if s.severity == "critical"]
        if critical and self._auto_block_critical:
            return BypassCheckResult(
                allowed=False,
                blocked=True,
                signals=signals,
                block_reason=critical[0].detail[:100],
            )

        return BypassCheckResult(
            allowed=True,
            blocked=False,
            signals=signals,
        )
```

## Comparison

| Approach | Behavioral Fingerprint | Cross-Identity Correlation | Auto Block | Incident Log |
|---|---|---|---|---|
| RequestFingerprint | Yes | No | No | No |
| BehavioralRateCounter | Via fingerprint | Yes (per behavioral FP) | No | No |
| BypassSignalDetector | Via counter | Yes | No | No |
| AdaptiveBlockList | No | No | Yes | No |
| BypassDetectionGateway | Via fingerprint | Via counter | Yes | Yes |

**Best for production**: Set `max_identities_per_pattern=5` for endpoints that should receive traffic from a narrow set of clients. For public endpoints, increase to 20–50. Set `auto_block_on_critical=True` with a 1-hour block TTL — this stops the attack immediately while minimizing false-positive impact. Review `BypassIncidentLog.summary()` daily; repeated `distributed_bypass` signals on the same behavioral fingerprint indicate a sustained campaign, not a one-off burst.
