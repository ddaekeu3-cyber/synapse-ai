---
title: "Agent Doesn't Implement Brute Force Protection for Agent Endpoints"
description: "Agent API endpoints that accept credentials, API keys, or session tokens without brute force protection are vulnerable to automated credential stuffing and password spraying attacks. Implement progressive lockout, CAPTCHA escalation, and anomaly-based blocking to stop credential attacks without disrupting legitimate users."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-brute-force-protection-for-agent-endpoints
tags: [brute-force, credential-stuffing, rate-limiting, lockout, security, authentication]
symptoms:
  - "Thousands of failed login attempts per minute with no automatic blocking"
  - "Attacker cycles through credential list without triggering any defense"
  - "Same IP submits 500 API key guesses without a lockout"
  - "Account lockout is permanent — legitimate users can't recover without support"
  - "No distinction between distributed low-rate attacks and single-source floods"
---

## Why This Happens

Authentication endpoints are high-value targets because a successful guess grants full account access. Basic rate limiting by IP is insufficient — credential stuffing attacks distribute requests across thousands of IPs. Effective brute force protection requires tracking failed attempts at multiple levels (IP, username, account), applying progressive delays rather than binary blocks, and using behavioral signals (velocity, device fingerprint, geographic anomaly) to distinguish attacks from legitimate failures.

## Solution 1: Progressive Lockout Tracker

```python
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

@dataclass
class LockoutState:
    attempt_count: int = 0
    consecutive_failures: int = 0
    first_attempt: float = field(default_factory=time.time)
    last_attempt: float = field(default_factory=time.time)
    locked_until: float = 0.0
    total_lockouts: int = 0

class ProgressiveLockoutTracker:
    """
    Tracks failed authentication attempts per (key_type, key_value) pair.
    Key types: "ip", "username", "api_key_prefix".
    Applies exponential lockout durations: 1m → 5m → 15m → 1h → 24h.
    """

    LOCKOUT_SCHEDULE = [60, 300, 900, 3600, 86400]   # seconds

    def __init__(self, window_seconds: float = 900.0, max_attempts: int = 10):
        self._window = window_seconds
        self._max_attempts = max_attempts
        self._states: Dict[str, LockoutState] = {}

    def _key(self, key_type: str, key_value: str) -> str:
        return f"{key_type}:{key_value}"

    def _get_state(self, key_type: str, key_value: str) -> LockoutState:
        k = self._key(key_type, key_value)
        if k not in self._states:
            self._states[k] = LockoutState()
        return self._states[k]

    def is_locked(self, key_type: str, key_value: str) -> tuple[bool, float]:
        """Returns (locked, seconds_remaining)."""
        state = self._get_state(key_type, key_value)
        now = time.time()
        if state.locked_until > now:
            return True, round(state.locked_until - now, 1)
        return False, 0.0

    def record_failure(self, key_type: str, key_value: str) -> dict:
        """Records a failed attempt. Returns lockout info."""
        k = self._key(key_type, key_value)
        state = self._get_state(key_type, key_value)
        now = time.time()

        # Reset window if last attempt was outside the window
        if now - state.last_attempt > self._window:
            state.consecutive_failures = 0

        state.attempt_count += 1
        state.consecutive_failures += 1
        state.last_attempt = now

        if state.consecutive_failures >= self._max_attempts:
            lockout_idx = min(state.total_lockouts, len(self.LOCKOUT_SCHEDULE) - 1)
            duration = self.LOCKOUT_SCHEDULE[lockout_idx]
            state.locked_until = now + duration
            state.total_lockouts += 1
            state.consecutive_failures = 0
            return {
                "locked": True,
                "duration_seconds": duration,
                "lockout_number": state.total_lockouts,
                "unlock_at": state.locked_until,
            }

        remaining = self._max_attempts - state.consecutive_failures
        return {
            "locked": False,
            "attempts_remaining": remaining,
            "consecutive_failures": state.consecutive_failures,
        }

    def record_success(self, key_type: str, key_value: str) -> None:
        """Resets consecutive failure count on successful auth."""
        state = self._get_state(key_type, key_value)
        state.consecutive_failures = 0
        # Don't reset total_lockouts — it affects future lockout duration

    def force_unlock(self, key_type: str, key_value: str) -> None:
        """Admin override to unlock a specific key."""
        k = self._key(key_type, key_value)
        if k in self._states:
            self._states[k].locked_until = 0.0
            self._states[k].consecutive_failures = 0
```

## Solution 2: Distributed Credential Stuffing Detector

```python
import hashlib
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional, Set

@dataclass
class AttackSignal:
    attack_type: str    # "ip_flood" | "username_spray" | "distributed_stuffing"
    confidence: float
    evidence: dict
    detected_at: float

class CredentialStuffingDetector:
    """
    Detects distributed credential stuffing by tracking failure patterns
    across multiple dimensions: IP/username velocity, known-bad IP lists,
    and anomalous request fingerprints (user-agent uniformity).
    """

    def __init__(
        self,
        ip_failure_threshold: int = 20,
        username_failure_threshold: int = 5,
        spray_window_seconds: float = 60.0,
        stuffing_user_ratio: float = 0.8,
    ):
        self._ip_threshold = ip_failure_threshold
        self._user_threshold = username_failure_threshold
        self._window = spray_window_seconds
        self._stuffing_ratio = stuffing_user_ratio

        # Sliding window counters
        self._ip_failures: Dict[str, List[float]] = defaultdict(list)
        self._username_failures: Dict[str, List[float]] = defaultdict(list)
        self._user_agents: Dict[str, Set[str]] = defaultdict(set)
        self._known_bad_ips: Set[str] = set()

    def _prune_window(self, timestamps: List[float], now: float) -> List[float]:
        return [t for t in timestamps if now - t < self._window]

    def record_failure(
        self,
        ip: str,
        username: str,
        user_agent: str,
    ) -> Optional[AttackSignal]:
        now = time.time()

        # Update counters
        self._ip_failures[ip].append(now)
        self._ip_failures[ip] = self._prune_window(self._ip_failures[ip], now)
        self._username_failures[username].append(now)
        self._username_failures[username] = self._prune_window(self._username_failures[username], now)
        self._user_agents[ip].add(user_agent[:50])

        # IP flood detection
        if len(self._ip_failures[ip]) >= self._ip_threshold:
            return AttackSignal(
                attack_type="ip_flood",
                confidence=0.95,
                evidence={
                    "ip": ip,
                    "failures_in_window": len(self._ip_failures[ip]),
                    "threshold": self._ip_threshold,
                },
                detected_at=now,
            )

        # Username spraying: many IPs targeting one username
        if len(self._username_failures[username]) >= self._user_threshold:
            unique_ips_for_user = len(set(
                ip for ip, ts_list in self._ip_failures.items()
                for t in ts_list if now - t < self._window
            ))
            if unique_ips_for_user > 3:
                return AttackSignal(
                    attack_type="username_spray",
                    confidence=0.88,
                    evidence={
                        "username": username,
                        "failures_in_window": len(self._username_failures[username]),
                        "unique_ips": unique_ips_for_user,
                    },
                    detected_at=now,
                )

        # Uniform user-agent from IP (bot pattern)
        if len(self._user_agents[ip]) == 1 and len(self._ip_failures[ip]) > 5:
            return AttackSignal(
                attack_type="distributed_stuffing",
                confidence=0.75,
                evidence={
                    "ip": ip,
                    "uniform_user_agent": True,
                    "failures": len(self._ip_failures[ip]),
                },
                detected_at=now,
            )

        return None

    def add_known_bad_ip(self, ip: str) -> None:
        self._known_bad_ips.add(ip)

    def is_known_bad(self, ip: str) -> bool:
        return ip in self._known_bad_ips
```

## Solution 3: CAPTCHA Escalation Gate

```python
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

@dataclass
class CaptchaRequirement:
    required: bool
    difficulty: str     # "easy" | "medium" | "hard"
    reason: str
    expires_at: float

class CaptchaEscalationGate:
    """
    Escalates CAPTCHA difficulty based on failure history.
    Returns CAPTCHA requirement decision per session/IP.
    Integrates with reCAPTCHA v3 score thresholds.
    """

    def __init__(self, min_captcha_score: float = 0.5):
        self._requirements: Dict[str, CaptchaRequirement] = {}
        self._failure_counts: Dict[str, int] = {}
        self._min_score = min_captcha_score

    def evaluate(self, session_key: str, failure_count: int) -> CaptchaRequirement:
        """Returns CAPTCHA requirement based on failure count."""
        now = time.time()

        if failure_count == 0:
            req = CaptchaRequirement(
                required=False, difficulty="none",
                reason="no_failures", expires_at=now + 300,
            )
        elif failure_count <= 2:
            req = CaptchaRequirement(
                required=False, difficulty="none",
                reason="low_failure_count", expires_at=now + 120,
            )
        elif failure_count <= 5:
            req = CaptchaRequirement(
                required=True, difficulty="easy",
                reason="moderate_failure_count", expires_at=now + 300,
            )
        elif failure_count <= 10:
            req = CaptchaRequirement(
                required=True, difficulty="medium",
                reason="high_failure_count", expires_at=now + 600,
            )
        else:
            req = CaptchaRequirement(
                required=True, difficulty="hard",
                reason="suspected_brute_force", expires_at=now + 1800,
            )

        self._requirements[session_key] = req
        return req

    def validate_score(self, recaptcha_score: float, difficulty: str) -> bool:
        """Validate reCAPTCHA v3 score against difficulty threshold."""
        thresholds = {"easy": 0.3, "medium": 0.5, "hard": 0.7}
        return recaptcha_score >= thresholds.get(difficulty, self._min_score)

    def is_required(self, session_key: str) -> bool:
        req = self._requirements.get(session_key)
        if not req:
            return False
        if time.time() > req.expires_at:
            return False
        return req.required
```

## Solution 4: Multi-Layer Auth Guard

```python
import asyncio
import time
from dataclasses import dataclass
from typing import Optional

@dataclass
class AuthAttempt:
    ip: str
    username: str
    user_agent: str
    session_id: str
    recaptcha_score: Optional[float] = None

@dataclass
class AuthDecision:
    allowed: bool
    reason: str
    lockout_seconds: float = 0.0
    captcha_required: bool = False
    captcha_difficulty: str = "none"

class MultiLayerAuthGuard:
    """
    Orchestrates all brute-force defenses into a single pre-auth check.
    Call check() before validating credentials. Record success/failure after.
    """

    def __init__(
        self,
        lockout_tracker: ProgressiveLockoutTracker,
        stuffing_detector: CredentialStuffingDetector,
        captcha_gate: CaptchaEscalationGate,
    ):
        self._lockout = lockout_tracker
        self._detector = stuffing_detector
        self._captcha = captcha_gate

    def check(self, attempt: AuthAttempt) -> AuthDecision:
        # Known bad IP — immediate block
        if self._detector.is_known_bad(attempt.ip):
            return AuthDecision(allowed=False, reason="known_bad_ip")

        # IP lockout check
        ip_locked, ip_remaining = self._lockout.is_locked("ip", attempt.ip)
        if ip_locked:
            return AuthDecision(
                allowed=False,
                reason="ip_locked",
                lockout_seconds=ip_remaining,
            )

        # Username lockout check
        user_locked, user_remaining = self._lockout.is_locked("username", attempt.username)
        if user_locked:
            return AuthDecision(
                allowed=False,
                reason="username_locked",
                lockout_seconds=user_remaining,
            )

        # CAPTCHA check
        failure_count = self._lockout._get_state("username", attempt.username).attempt_count
        captcha_req = self._captcha.evaluate(attempt.session_id, failure_count)

        if captcha_req.required:
            if attempt.recaptcha_score is None:
                return AuthDecision(
                    allowed=False,
                    reason="captcha_required",
                    captcha_required=True,
                    captcha_difficulty=captcha_req.difficulty,
                )
            if not self._captcha.validate_score(attempt.recaptcha_score, captcha_req.difficulty):
                return AuthDecision(
                    allowed=False,
                    reason="captcha_failed",
                    captcha_required=True,
                    captcha_difficulty=captcha_req.difficulty,
                )

        return AuthDecision(allowed=True, reason="ok")

    def record_failure(self, attempt: AuthAttempt) -> None:
        self._lockout.record_failure("ip", attempt.ip)
        self._lockout.record_failure("username", attempt.username)
        signal = self._detector.record_failure(attempt.ip, attempt.username, attempt.user_agent)
        if signal and signal.confidence >= 0.9:
            self._detector.add_known_bad_ip(attempt.ip)
            print(f"[auth_guard] blocked IP {attempt.ip}: {signal.attack_type}")

    def record_success(self, attempt: AuthAttempt) -> None:
        self._lockout.record_success("ip", attempt.ip)
        self._lockout.record_success("username", attempt.username)
```

## Solution 5: Velocity Anomaly Detector

```python
import math
import time
from collections import deque
from typing import Deque, Dict, Optional

class VelocityAnomalyDetector:
    """
    Flags authentication attempts with anomalous velocity patterns:
    - Superhuman typing speed (< 100ms between attempts from same session)
    - Geographic impossibility (same account from two distant IPs in < 5 min)
    - Session reuse anomaly (session ID reused across different IPs)
    """

    def __init__(self):
        self._attempt_times: Dict[str, Deque[float]] = {}   # session_id -> timestamps
        self._account_ips: Dict[str, list] = {}             # username -> [(ip, time)]
        self._session_ips: Dict[str, str] = {}              # session_id -> first_ip

    def check_velocity(self, session_id: str, username: str, ip: str) -> Optional[dict]:
        now = time.time()

        # Init
        if session_id not in self._attempt_times:
            self._attempt_times[session_id] = deque(maxlen=50)
        if username not in self._account_ips:
            self._account_ips[username] = []

        times = self._attempt_times[session_id]

        # Superhuman typing speed
        if times and (now - times[-1]) < 0.1:
            return {
                "anomaly": "superhuman_velocity",
                "interval_ms": round((now - times[-1]) * 1000, 1),
                "session_id": session_id,
            }

        times.append(now)

        # Session IP mismatch
        first_ip = self._session_ips.get(session_id)
        if first_ip and first_ip != ip:
            return {
                "anomaly": "session_ip_mismatch",
                "original_ip": first_ip,
                "current_ip": ip,
                "session_id": session_id,
            }
        if not first_ip:
            self._session_ips[session_id] = ip

        # Geographic impossibility (simplified: different /16 subnets within 5 min)
        recent_ips = [
            (prev_ip, t) for prev_ip, t in self._account_ips[username]
            if now - t < 300
        ]
        if recent_ips:
            last_ip, _ = recent_ips[-1]
            last_subnet = ".".join(last_ip.split(".")[:2])
            curr_subnet = ".".join(ip.split(".")[:2])
            if last_subnet != curr_subnet:
                return {
                    "anomaly": "geographic_impossibility",
                    "previous_subnet": last_subnet,
                    "current_subnet": curr_subnet,
                    "username": username,
                }

        self._account_ips[username].append((ip, now))
        # Keep last 20 entries per username
        if len(self._account_ips[username]) > 20:
            self._account_ips[username].pop(0)

        return None
```

## Solution 6: Brute Force Audit Logger

```python
import json
import time
from dataclasses import asdict, dataclass, field
from typing import List

@dataclass
class BruteForceEvent:
    event_type: str     # "failure" | "lockout" | "attack_detected" | "unlock"
    ip: str
    username: str
    session_id: str
    details: dict
    timestamp: float = field(default_factory=time.time)
    severity: str = "low"   # "low" | "medium" | "high" | "critical"

class BruteForceAuditLogger:
    """
    Structured audit log for all brute-force-related events.
    Used for forensic analysis, SOC alerting, and compliance reports.
    """

    def __init__(self, storage_backend=None):
        self._backend = storage_backend
        self._buffer: List[BruteForceEvent] = []

    def log(self, event: BruteForceEvent) -> None:
        self._buffer.append(event)
        entry = {
            "event_type": event.event_type,
            "ip": event.ip,
            "username": event.username,
            "session_id": event.session_id,
            "severity": event.severity,
            "details": event.details,
            "timestamp": event.timestamp,
        }
        # Emit as structured JSON for SIEM ingestion
        print(f"[brute_force_audit] {json.dumps(entry)}")
        if self._backend:
            self._backend.append(entry)

    def log_lockout(self, ip: str, username: str, session_id: str, duration_s: float) -> None:
        self.log(BruteForceEvent(
            event_type="lockout",
            ip=ip, username=username, session_id=session_id,
            details={"lockout_duration_seconds": duration_s},
            severity="medium",
        ))

    def log_attack_detected(self, ip: str, username: str, session_id: str, signal: dict) -> None:
        self.log(BruteForceEvent(
            event_type="attack_detected",
            ip=ip, username=username, session_id=session_id,
            details=signal,
            severity="critical",
        ))

    def summary_report(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [e for e in self._buffer if e.timestamp >= cutoff]
        return {
            "window_seconds": window_seconds,
            "total_events": len(recent),
            "lockouts": sum(1 for e in recent if e.event_type == "lockout"),
            "attacks_detected": sum(1 for e in recent if e.event_type == "attack_detected"),
            "unique_ips": len({e.ip for e in recent}),
            "unique_usernames": len({e.username for e in recent}),
            "critical_events": sum(1 for e in recent if e.severity == "critical"),
        }
```

## Comparison

| Approach | IP-Level | User-Level | Distributed Attacks | Human Verification | Forensics |
|---|---|---|---|---|---|
| ProgressiveLockoutTracker | Yes | Yes | No | No | No |
| CredentialStuffingDetector | Yes | Yes | Yes | No | Partial |
| CaptchaEscalationGate | No | Via session | No | Yes | No |
| MultiLayerAuthGuard | Yes | Yes | Via detector | Yes | No |
| VelocityAnomalyDetector | Yes | Yes | Partial | No | No |
| BruteForceAuditLogger | N/A | N/A | N/A | N/A | Yes |

**Best for production**: Layer all components: `MultiLayerAuthGuard` as the entry point, backed by `ProgressiveLockoutTracker` for lockouts, `CredentialStuffingDetector` for distributed patterns, `CaptchaEscalationGate` for human verification at high failure counts, and `VelocityAnomalyDetector` for behavioral signals. Log every event through `BruteForceAuditLogger` for SOC visibility and post-incident forensics. Set lockout thresholds conservatively (10 attempts per 15-minute window) and test recovery flows to avoid locking out legitimate users permanently.
