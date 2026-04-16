---
title: "Agent Doesn't Implement Rate Limiting on Embedding Requests to Prevent Data Exfiltration"
description: "Agents that accept arbitrary text for embedding without rate limiting are vulnerable to data exfiltration via embedding vectors: an attacker can submit thousands of text snippets from a target corpus, receive embedding vectors for each, and reconstruct the approximate content through nearest-neighbor inversion — all without triggering content filters that only scan the text itself. Implement rate limiting on embedding endpoints with per-session and per-IP quotas, anomaly detection for bulk embedding patterns, and alerts when embedding request volume exceeds plausible interactive use."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-rate-limiting-on-embedding-requests-to-prevent-data-exfiltration
tags: [embedding-rate-limit, data-exfiltration, vector-inversion, bulk-embedding-detection, embedding-security, quota-enforcement]
symptoms:
  - "Single session submits thousands of embedding requests in minutes"
  - "No per-session quota on embedding calls — unlimited volume accepted"
  - "Bulk embedding patterns indistinguishable from interactive use without rate metrics"
  - "Embedding endpoint accepts any text without volume or frequency constraints"
  - "No alert fires when a session consumes 100× the typical embedding volume"
---

## Why This Happens

Embedding APIs encode text as dense vectors. Researchers have demonstrated that embedding vectors can be approximately inverted to recover the original text, particularly when the attacker controls the query texts and can use the returned vectors as an oracle. An agent that exposes embedding capability to users without rate limits allows an attacker to systematically embed every document in a target corpus, build a vector-to-text mapping, and reconstruct proprietary content. Rate limiting on embedding requests is qualitatively different from rate limiting on LLM calls: the attack surface is the volume and pattern of requests, not the content of any single request.

## Solution 1: Embedding Request Fingerprint

```python
import hashlib
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class EmbeddingRequestFingerprint:
    session_id: str
    ip_address: str
    text_hash: str
    text_length: int
    requested_at: float = field(default_factory=time.time)
    model: str = ""

    @classmethod
    def create(
        cls,
        session_id: str,
        ip_address: str,
        text: str,
        model: str = "",
    ) -> "EmbeddingRequestFingerprint":
        return cls(
            session_id=session_id,
            ip_address=ip_address,
            text_hash=hashlib.sha256(text.encode()).hexdigest()[:16],
            text_length=len(text),
            model=model,
        )
```

## Solution 2: Embedding Rate Limiter

```python
import threading
import time
from collections import deque
from typing import Deque, Dict, Optional, Tuple


class EmbeddingRateLimiter:
    """
    Enforces per-session and per-IP rate limits on embedding requests.
    Uses a sliding window counter for each identity dimension.
    """

    def __init__(
        self,
        per_session_per_minute: int = 60,
        per_session_per_hour: int = 500,
        per_ip_per_minute: int = 200,
        per_ip_per_hour: int = 2000,
        global_per_second: float = 50.0,
    ):
        self._sess_min = per_session_per_minute
        self._sess_hr = per_session_per_hour
        self._ip_min = per_ip_per_minute
        self._ip_hr = per_ip_per_hour
        self._global_rate = global_per_second

        self._session_events: Dict[str, Deque[float]] = {}
        self._ip_events: Dict[str, Deque[float]] = {}
        self._global_tokens: float = global_per_second
        self._last_refill: float = time.time()
        self._lock = threading.Lock()
        self._allowed = 0
        self._blocked = 0

    def _clean_window(self, dq: Deque[float], cutoff: float) -> None:
        while dq and dq[0] < cutoff:
            dq.popleft()

    def _check_global(self, now: float) -> bool:
        elapsed = now - self._last_refill
        self._global_tokens = min(
            self._global_rate,
            self._global_tokens + elapsed * self._global_rate,
        )
        self._last_refill = now
        if self._global_tokens >= 1.0:
            self._global_tokens -= 1.0
            return True
        return False

    def check(self, session_id: str, ip_address: str) -> Tuple[bool, str]:
        now = time.time()
        with self._lock:
            if not self._check_global(now):
                self._blocked += 1
                return False, "global_rate_limit"

            sess_dq = self._session_events.setdefault(session_id, deque())
            self._clean_window(sess_dq, now - 3600)
            min_count = sum(1 for ts in sess_dq if ts >= now - 60)
            if min_count >= self._sess_min:
                self._blocked += 1
                return False, "session_per_minute_limit"
            if len(sess_dq) >= self._sess_hr:
                self._blocked += 1
                return False, "session_per_hour_limit"

            ip_dq = self._ip_events.setdefault(ip_address, deque())
            self._clean_window(ip_dq, now - 3600)
            ip_min_count = sum(1 for ts in ip_dq if ts >= now - 60)
            if ip_min_count >= self._ip_min:
                self._blocked += 1
                return False, "ip_per_minute_limit"
            if len(ip_dq) >= self._ip_hr:
                self._blocked += 1
                return False, "ip_per_hour_limit"

            sess_dq.append(now)
            ip_dq.append(now)
            self._allowed += 1
            return True, "ok"

    def stats(self) -> dict:
        total = self._allowed + self._blocked
        return {
            "allowed": self._allowed,
            "blocked": self._blocked,
            "block_rate": round(self._blocked / max(total, 1), 4),
        }
```

## Solution 3: Bulk Embedding Anomaly Detector

```python
import time
import threading
from collections import deque, defaultdict
from typing import Deque, Dict, List


class BulkEmbeddingAnomalyDetector:
    """
    Detects bulk embedding patterns that suggest systematic data extraction:
    - Many unique texts from the same session in a short window
    - Systematic length distribution (many similar-length texts)
    - Sequential hash patterns suggesting corpus enumeration
    """

    def __init__(
        self,
        bulk_threshold: int = 100,        # unique texts per session per 10 min
        similarity_threshold: float = 0.8, # fraction of texts with similar length
        window_seconds: float = 600.0,
    ):
        self._bulk = bulk_threshold
        self._sim_threshold = similarity_threshold
        self._window = window_seconds
        self._session_requests: Dict[str, Deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def record(self, fp: EmbeddingRequestFingerprint) -> None:
        with self._lock:
            dq = self._session_requests[fp.session_id]
            dq.append((fp.requested_at, fp.text_hash, fp.text_length))
            cutoff = time.time() - self._window
            while dq and dq[0][0] < cutoff:
                dq.popleft()

    def check_anomaly(self, session_id: str) -> dict:
        now = time.time()
        cutoff = now - self._window
        with self._lock:
            window_requests = [
                (ts, h, l) for ts, h, l in self._session_requests.get(session_id, deque())
                if ts >= cutoff
            ]

        if not window_requests:
            return {"anomaly": False}

        unique_hashes = len(set(h for _, h, _ in window_requests))
        lengths = [l for _, _, l in window_requests]

        is_bulk = unique_hashes >= self._bulk

        # Check for systematic length distribution (similar lengths = corpus extraction)
        if lengths:
            median_len = sorted(lengths)[len(lengths) // 2]
            similar = sum(1 for l in lengths if abs(l - median_len) < median_len * 0.2)
            is_systematic = similar / len(lengths) >= self._sim_threshold and len(lengths) >= 20
        else:
            is_systematic = False

        anomaly = is_bulk or is_systematic
        return {
            "anomaly": anomaly,
            "session_id": session_id,
            "unique_texts_in_window": unique_hashes,
            "window_request_count": len(window_requests),
            "is_bulk": is_bulk,
            "is_systematic_length": is_systematic,
            "severity": "high" if is_bulk and is_systematic else ("medium" if anomaly else "none"),
        }
```

## Solution 4: Protected Embedding Gateway

```python
import time
from typing import Any, Callable, Optional


class ProtectedEmbeddingGateway:
    """
    Enforces rate limits and anomaly detection before passing
    embedding requests to the underlying model.
    """

    def __init__(
        self,
        rate_limiter: EmbeddingRateLimiter,
        anomaly_detector: BulkEmbeddingAnomalyDetector,
        audit_fn: Optional[Callable[[dict], None]] = None,
        block_on_anomaly: bool = True,
    ):
        self._limiter = rate_limiter
        self._detector = anomaly_detector
        self._audit = audit_fn or (lambda ev: None)
        self._block_anomaly = block_on_anomaly

    async def embed(
        self,
        text: str,
        embed_fn: Callable,
        session_id: str = "",
        ip_address: str = "",
        model: str = "",
    ) -> Any:
        fp = EmbeddingRequestFingerprint.create(session_id, ip_address, text, model)
        self._detector.record(fp)

        allowed, reason = self._limiter.check(session_id, ip_address)
        if not allowed:
            self._audit({
                "event": "embedding_rate_limited",
                "session_id": session_id,
                "ip_address": ip_address,
                "reason": reason,
                "timestamp": time.time(),
            })
            raise EmbeddingRateLimitError(session_id, reason)

        anomaly = self._detector.check_anomaly(session_id)
        if anomaly["anomaly"]:
            self._audit({
                "event": "embedding_anomaly_detected",
                "session_id": session_id,
                "ip_address": ip_address,
                "anomaly": anomaly,
                "timestamp": time.time(),
            })
            if self._block_anomaly and anomaly["severity"] == "high":
                raise EmbeddingAnomalyError(session_id, anomaly)

        return await embed_fn(text, model=model)


class EmbeddingRateLimitError(Exception):
    def __init__(self, session_id: str, reason: str):
        super().__init__(f"embedding rate limit exceeded for session '{session_id}': {reason}")
        self.session_id = session_id
        self.reason = reason


class EmbeddingAnomalyError(Exception):
    def __init__(self, session_id: str, anomaly: dict):
        super().__init__(f"embedding anomaly detected for session '{session_id}'")
        self.session_id = session_id
        self.anomaly = anomaly
```

## Solution 5: Session Embedding Profile

```python
import time
import threading
from typing import Dict


class SessionEmbeddingProfiler:
    """
    Maintains per-session embedding usage profiles for audit and analysis.
    """

    def __init__(self):
        self._profiles: Dict[str, dict] = {}
        self._lock = threading.Lock()

    def record(self, session_id: str, text_length: int, blocked: bool = False) -> None:
        now = time.time()
        with self._lock:
            if session_id not in self._profiles:
                self._profiles[session_id] = {
                    "first_seen": now,
                    "last_seen": now,
                    "total_requests": 0,
                    "blocked_requests": 0,
                    "total_chars": 0,
                }
            p = self._profiles[session_id]
            p["last_seen"] = now
            p["total_requests"] += 1
            p["total_chars"] += text_length
            if blocked:
                p["blocked_requests"] += 1

    def get_profile(self, session_id: str) -> dict:
        with self._lock:
            return dict(self._profiles.get(session_id, {}))

    def high_volume_sessions(self, threshold: int = 200) -> list:
        with self._lock:
            return [
                {"session_id": sid, **p}
                for sid, p in self._profiles.items()
                if p["total_requests"] >= threshold
            ]
```

## Solution 6: Embedding Security Dashboard

```python
import time


class EmbeddingSecurityDashboard:
    """
    Combines rate limiter stats, anomaly counts, and high-volume
    session profiles into a security monitoring view.
    """

    def __init__(
        self,
        gateway: ProtectedEmbeddingGateway,
        profiler: SessionEmbeddingProfiler,
        anomaly_detector: BulkEmbeddingAnomalyDetector,
    ):
        self._gateway = gateway
        self._profiler = profiler
        self._detector = anomaly_detector

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "rate_limiter_stats": self._gateway._limiter.stats(),
            "high_volume_sessions": self._profiler.high_volume_sessions(threshold=200),
        }
```

## Comparison

| Approach | Per-Session Limit | Per-IP Limit | Bulk Detection | Systematic Pattern | Audit |
|---|---|---|---|---|---|
| EmbeddingRateLimiter | Yes (min+hr) | Yes (min+hr) | No | No | No |
| BulkEmbeddingAnomalyDetector | No | No | Yes (unique count) | Yes (length dist) | No |
| ProtectedEmbeddingGateway | Via limiter | Via limiter | Via detector | Via detector | Yes |
| SessionEmbeddingProfiler | No | No | No | No | Yes (profile) |
| EmbeddingSecurityDashboard | No | No | No | No | Yes (combined) |

**Best for production**: Set `per_session_per_hour=500` as the default — legitimate interactive use rarely requires more than a few hundred embeddings per session, while corpus extraction attacks typically require thousands. Use `BulkEmbeddingAnomalyDetector` with `bulk_threshold=100` in a 10-minute window as the trip wire: 100 unique texts in 10 minutes is plausible for a document processing workflow but implausible for a conversational agent. Alert and require manual review for high-severity anomalies rather than silently blocking — some bulk patterns (document ingestion workflows) are legitimate and need a separate high-volume API key with explicit authorization.
