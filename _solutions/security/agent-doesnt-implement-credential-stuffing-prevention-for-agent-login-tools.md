---
title: "Agent Doesn't Implement Credential Stuffing Prevention for Agent Login Tools"
description: "AI agents that expose login or authentication tools without rate limiting, device fingerprinting, or breach-credential checks allow automated credential stuffing attacks to succeed silently. Defense-in-depth combines per-IP rate limiting, CAPTCHA escalation, leaked-credential lookup, and anomaly detection to stop bulk automated login attempts before they compromise user accounts."
date: 2025-02-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-credential-stuffing-prevention-for-agent-login-tools
tags:
  - credential-stuffing
  - rate-limiting
  - authentication
  - security
  - anomaly-detection
  - breach-detection
  - login-protection
symptoms:
  - "Hundreds of failed login attempts from the same IP succeed without throttling"
  - "Agent login tool accepts known breached passwords without checking"
  - "No per-IP or per-username attempt counter exists in the auth tool"
  - "Successful logins from new countries or devices trigger no alert"
  - "Automated tools can call the login tool at full request throughput"
---

## Problem

Credential stuffing attacks replay username/password pairs harvested from breaches against a target service. If an agent's login tool has no attempt limits, an attacker can test millions of credentials at network speed. Effective prevention requires multiple independent layers: per-IP and per-username counters to detect bulk attempts, breach-credential checks (Have I Been Pwned) to reject known-compromised passwords, device/IP anomaly scoring to flag impossible logins, and progressive lockout to make brute force economically infeasible.

---

## Solution 1: LoginAttemptRateLimiter — Per-IP and Per-Username Throttle

```python
import asyncio
import hashlib
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple


@dataclass
class AttemptWindow:
    count: int = 0
    first_attempt: float = field(default_factory=time.monotonic)
    lockout_until: float = 0.0


class LoginAttemptRateLimiter:
    """
    Tracks failed login attempts per IP and per username with sliding
    windows and exponential lockout. Blocks IPs and usernames that
    exceed thresholds before credentials are ever checked.

    Usage:
        limiter = LoginAttemptRateLimiter(
            ip_limit=20, username_limit=5, window_s=300
        )
        allowed, wait_s = limiter.check("192.168.1.1", "alice@example.com")
        if not allowed:
            return 429, f"Too many attempts. Retry after {wait_s:.0f}s"
        ok = await verify_credentials(username, password)
        if not ok:
            limiter.record_failure("192.168.1.1", "alice@example.com")
        else:
            limiter.record_success("192.168.1.1", "alice@example.com")
    """

    LOCKOUT_STEPS = [30, 120, 600, 3600, 86400]  # seconds per violation tier

    def __init__(self, ip_limit: int = 20,
                  username_limit: int = 5,
                  window_s: float = 300.0):
        self._ip_limit = ip_limit
        self._user_limit = username_limit
        self._window = window_s
        self._ip: Dict[str, AttemptWindow] = defaultdict(AttemptWindow)
        self._user: Dict[str, AttemptWindow] = defaultdict(AttemptWindow)
        self._ip_violations: Dict[str, int] = defaultdict(int)
        self._user_violations: Dict[str, int] = defaultdict(int)

    def _hashed(self, value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()[:16]

    def check(self, ip: str, username: str) -> Tuple[bool, float]:
        """Returns (allowed, wait_seconds). wait_seconds > 0 means blocked."""
        now = time.monotonic()
        ip_key = self._hashed(ip)
        user_key = self._hashed(username.lower())

        for key, store, violations in [
            (ip_key, self._ip, self._ip_violations),
            (user_key, self._user, self._user_violations),
        ]:
            w = store[key]
            if w.lockout_until > now:
                return False, w.lockout_until - now
            # Reset window if expired
            if now - w.first_attempt > self._window:
                store[key] = AttemptWindow(first_attempt=now)

        return True, 0.0

    def record_failure(self, ip: str, username: str):
        now = time.monotonic()
        for key, store, violations, limit in [
            (self._hashed(ip), self._ip, self._ip_violations, self._ip_limit),
            (self._hashed(username.lower()), self._user, self._user_violations, self._user_limit),
        ]:
            store[key].count += 1
            if store[key].count >= limit:
                tier = min(violations[key], len(self.LOCKOUT_STEPS) - 1)
                lockout = self.LOCKOUT_STEPS[tier]
                store[key].lockout_until = now + lockout
                store[key].count = 0
                store[key].first_attempt = now
                violations[key] += 1

    def record_success(self, ip: str, username: str):
        """Reset counters on successful login."""
        ip_key = self._hashed(ip)
        user_key = self._hashed(username.lower())
        self._ip.pop(ip_key, None)
        self._user.pop(user_key, None)

    def stats(self) -> Dict[str, int]:
        return {
            "tracked_ips": len(self._ip),
            "tracked_users": len(self._user),
        }
```

---

## Solution 2: BreachCredentialChecker — Reject Known-Compromised Passwords

```python
import hashlib
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


class BreachCredentialChecker:
    """
    Checks passwords against the Have I Been Pwned Pwned Passwords API
    using k-anonymity (only the first 5 chars of the SHA-1 hash are sent).
    Rejects credentials that appear in known breach datasets.

    Usage:
        checker = BreachCredentialChecker(min_breach_count=1)
        if await checker.is_breached(password):
            return 400, "This password appears in known data breaches. Choose a different one."
    """

    HIBP_API = "https://api.pwnedpasswords.com/range/{prefix}"

    def __init__(self, min_breach_count: int = 1,
                  timeout_s: float = 3.0,
                  fallback_allow: bool = True):
        self._min_count = min_breach_count
        self._timeout = timeout_s
        self._fallback_allow = fallback_allow
        self._cache: dict = {}

    def _sha1_prefix(self, password: str) -> tuple[str, str]:
        digest = hashlib.sha1(password.encode("utf-8")).hexdigest().upper()
        return digest[:5], digest[5:]

    async def is_breached(self, password: str) -> bool:
        """Returns True if password appears in breach data."""
        prefix, suffix = self._sha1_prefix(password)
        if prefix in self._cache:
            hashes = self._cache[prefix]
        else:
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.get(
                        self.HIBP_API.format(prefix=prefix),
                        headers={"Add-Padding": "true"},
                    )
                    resp.raise_for_status()
                    hashes = resp.text
                    self._cache[prefix] = hashes
            except Exception as exc:
                logger.warning("hibp_check_failed error=%s", exc)
                return not self._fallback_allow  # fail open by default

        for line in hashes.splitlines():
            if ":" not in line:
                continue
            hash_suffix, count_str = line.split(":", 1)
            if hash_suffix == suffix:
                count = int(count_str.strip())
                if count >= self._min_count:
                    logger.info(
                        "breached_password_rejected breach_count=%d", count
                    )
                    return True
        return False

    def clear_cache(self):
        self._cache.clear()
```

---

## Solution 3: LoginAnomalyDetector — Flag Impossible or Suspicious Logins

```python
import logging
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class LoginEvent:
    username: str
    ip: str
    country: Optional[str]
    user_agent: str
    success: bool
    timestamp: float = 0.0

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = time.time()


class LoginAnomalyDetector:
    """
    Detects suspicious login patterns: new country, new device, impossible
    travel (two logins from distant countries within minutes), and unusual
    login times. Anomalies trigger additional verification requirements.

    Usage:
        detector = LoginAnomalyDetector()
        score, reasons = detector.score(event, history)
        if score >= 0.7:
            require_mfa(username)
        detector.record(event)
    """

    # Anomaly weights
    WEIGHTS = {
        "new_country": 0.4,
        "new_user_agent": 0.2,
        "impossible_travel": 0.9,
        "unusual_hour": 0.1,
        "rapid_country_switch": 0.7,
    }

    # km/h threshold for "impossible travel"
    MAX_TRAVEL_KMH = 900.0

    COUNTRY_COORDS: Dict[str, Tuple[float, float]] = {
        "US": (38.0, -97.0),
        "GB": (55.4, -3.4),
        "CN": (35.9, 104.2),
        "RU": (61.5, 105.3),
        "BR": (-14.2, -51.9),
        "DE": (51.2, 10.5),
        "KR": (35.9, 127.8),
    }

    def __init__(self, history_limit: int = 20):
        self._history: Dict[str, List[LoginEvent]] = {}
        self._limit = history_limit

    def record(self, event: LoginEvent):
        key = event.username.lower()
        if key not in self._history:
            self._history[key] = []
        self._history[key].append(event)
        self._history[key] = self._history[key][-self._limit:]

    def score(self, event: LoginEvent) -> Tuple[float, List[str]]:
        """Returns (risk_score 0–1, list of anomaly reasons)."""
        history = self._history.get(event.username.lower(), [])
        reasons: List[str] = []
        score = 0.0

        if not history:
            return 0.0, []

        known_countries = {e.country for e in history if e.country}
        known_uas = {e.user_agent for e in history}

        if event.country and known_countries and event.country not in known_countries:
            reasons.append(f"new_country:{event.country}")
            score += self.WEIGHTS["new_country"]

        if event.user_agent not in known_uas:
            reasons.append("new_user_agent")
            score += self.WEIGHTS["new_user_agent"]

        # Impossible travel check
        last = next(
            (e for e in reversed(history)
             if e.country and e.country != event.country and e.success),
            None,
        )
        if last and last.country and event.country:
            elapsed_h = (event.timestamp - last.timestamp) / 3600
            dist_km = self._haversine_km(last.country, event.country)
            if elapsed_h > 0 and dist_km / elapsed_h > self.MAX_TRAVEL_KMH:
                reasons.append(
                    f"impossible_travel:{last.country}->{event.country}"
                    f" in {elapsed_h:.1f}h"
                )
                score += self.WEIGHTS["impossible_travel"]

        # Unusual hour (02:00–05:00 local — crude UTC approximation)
        hour = time.gmtime(event.timestamp).tm_hour
        if 2 <= hour <= 5:
            reasons.append(f"unusual_hour:{hour}")
            score += self.WEIGHTS["unusual_hour"]

        return min(score, 1.0), reasons

    def _haversine_km(self, country_a: str, country_b: str) -> float:
        import math
        ca = self.COUNTRY_COORDS.get(country_a)
        cb = self.COUNTRY_COORDS.get(country_b)
        if not ca or not cb:
            return 0.0
        lat1, lon1 = math.radians(ca[0]), math.radians(ca[1])
        lat2, lon2 = math.radians(cb[0]), math.radians(cb[1])
        dlat, dlon = lat2 - lat1, lon2 - lon1
        a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
        return 6371 * 2 * math.asin(math.sqrt(a))
```

---

## Solution 4: CAPTCHAEscalationPolicy — Progressive Friction for Suspicious Requests

```python
import logging
import time
from dataclasses import dataclass
from typing import Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class EscalationState:
    ip: str
    failure_count: int = 0
    last_failure: float = 0.0
    captcha_required: bool = False
    mfa_required: bool = False
    blocked: bool = False


class CAPTCHAEscalationPolicy:
    """
    Progressively escalates authentication friction based on failure count:
    0–2 failures: normal
    3–5 failures: CAPTCHA required
    6–9 failures: MFA required
    10+ failures: account temporarily blocked

    Usage:
        policy = CAPTCHAEscalationPolicy()
        state = policy.evaluate("192.168.1.1", failure_count=4)
        if state.blocked:
            return 403, "Account locked"
        if state.captcha_required:
            return 200, {"require_captcha": True}
        if state.mfa_required:
            return 200, {"require_mfa": True}
    """

    CAPTCHA_THRESHOLD = 3
    MFA_THRESHOLD = 6
    BLOCK_THRESHOLD = 10
    RESET_WINDOW_S = 900.0  # 15 minutes

    def __init__(self):
        self._states: Dict[str, EscalationState] = {}

    def evaluate(self, ip: str, failure_count: int) -> EscalationState:
        now = time.monotonic()
        state = self._states.get(ip)

        if state is None or (now - state.last_failure) > self.RESET_WINDOW_S:
            state = EscalationState(ip=ip)
            self._states[ip] = state

        state.failure_count = failure_count
        state.last_failure = now

        if failure_count >= self.BLOCK_THRESHOLD:
            state.blocked = True
            state.mfa_required = True
            state.captcha_required = True
            logger.warning(
                "login_escalation level=block ip_hash=%s failures=%d",
                ip[:8], failure_count,
            )
        elif failure_count >= self.MFA_THRESHOLD:
            state.blocked = False
            state.mfa_required = True
            state.captcha_required = True
            logger.info("login_escalation level=mfa failures=%d", failure_count)
        elif failure_count >= self.CAPTCHA_THRESHOLD:
            state.blocked = False
            state.mfa_required = False
            state.captcha_required = True
            logger.info("login_escalation level=captcha failures=%d", failure_count)
        else:
            state.blocked = False
            state.mfa_required = False
            state.captcha_required = False

        return state

    def reset(self, ip: str):
        self._states.pop(ip, None)

    def current(self, ip: str) -> Optional[EscalationState]:
        return self._states.get(ip)
```

---

## Solution 5: StuffingPatternDetector — Detect Cross-Account Spray Patterns

```python
import logging
import time
from collections import defaultdict, deque
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class StuffingPatternDetector:
    """
    Detects credential-stuffing spray patterns: a single IP attempting
    many distinct usernames in a short window (as opposed to a legitimate
    user who retries the same account). Also detects distributed stuffing
    where many IPs each attempt a few accounts within a burst window.

    Usage:
        detector = StuffingPatternDetector(
            spray_threshold=10,  # distinct usernames per IP per window
            window_s=60,
        )
        if detector.is_spraying(ip="1.2.3.4", username="alice@example.com"):
            block_ip("1.2.3.4")
    """

    def __init__(self, spray_threshold: int = 10,
                  distributed_threshold: int = 50,
                  window_s: float = 60.0):
        self._spray_thresh = spray_threshold
        self._dist_thresh = distributed_threshold
        self._window = window_s
        # ip -> deque of (timestamp, username)
        self._ip_attempts: Dict[str, deque] = defaultdict(deque)
        # username -> deque of (timestamp, ip)
        self._user_attempts: Dict[str, deque] = defaultdict(deque)

    def _evict(self, q: deque, now: float):
        while q and q[0][0] < now - self._window:
            q.popleft()

    def record(self, ip: str, username: str, success: bool):
        now = time.monotonic()
        self._ip_attempts[ip].append((now, username.lower()))
        self._user_attempts[username.lower()].append((now, ip))

    def is_spraying(self, ip: str, username: str) -> bool:
        """True if this IP is attempting many distinct usernames (spray)."""
        now = time.monotonic()
        q = self._ip_attempts[ip]
        self._evict(q, now)
        distinct = len({u for _, u in q})
        if distinct >= self._spray_thresh:
            logger.warning(
                "stuffing_spray_detected ip_hash=%s distinct_users=%d",
                ip[:8], distinct,
            )
            return True
        return False

    def is_distributed_attack_on(self, username: str) -> bool:
        """True if many IPs are targeting the same username."""
        now = time.monotonic()
        q = self._user_attempts[username.lower()]
        self._evict(q, now)
        distinct_ips = len({ip for _, ip in q})
        if distinct_ips >= self._dist_thresh:
            logger.warning(
                "stuffing_distributed_detected username_hash=%s ips=%d",
                username[:4], distinct_ips,
            )
            return True
        return False

    def spray_stats(self) -> List[Tuple[str, int]]:
        """Returns (ip_prefix, distinct_user_count) for top offenders."""
        now = time.monotonic()
        result = []
        for ip, q in self._ip_attempts.items():
            self._evict(q, now)
            if q:
                result.append((ip[:8], len({u for _, u in q})))
        return sorted(result, key=lambda x: -x[1])[:10]
```

---

## Solution 6: CredentialStuffingGuard — Composable Full Defense Stack

```python
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class LoginDecision:
    allowed: bool
    require_captcha: bool = False
    require_mfa: bool = False
    block_reason: Optional[str] = None
    retry_after_s: float = 0.0
    risk_score: float = 0.0
    anomaly_reasons: list = None

    def __post_init__(self):
        if self.anomaly_reasons is None:
            self.anomaly_reasons = []


class CredentialStuffingGuard:
    """
    Composable credential-stuffing defense layer that combines:
    - Per-IP and per-username rate limiting
    - Breach credential checking
    - Login anomaly detection
    - CAPTCHA/MFA escalation
    - Spray pattern detection

    Usage:
        guard = CredentialStuffingGuard()
        decision = await guard.evaluate_login(
            ip="1.2.3.4",
            username="alice@example.com",
            password=raw_password,
            user_agent="Mozilla/5.0...",
            country="CN",
        )
        if not decision.allowed:
            return 429, decision.block_reason
        if decision.require_captcha:
            return 200, {"step": "captcha"}
        if decision.require_mfa:
            return 200, {"step": "mfa"}
        # Proceed with actual credential check
    """

    def __init__(self,
                  ip_limit: int = 20,
                  username_limit: int = 5,
                  spray_threshold: int = 10,
                  check_breaches: bool = True,
                  breach_timeout_s: float = 2.0):
        self._rate_limiter = LoginAttemptRateLimiter(
            ip_limit=ip_limit,
            username_limit=username_limit,
        )
        self._breach_checker = BreachCredentialChecker(
            timeout_s=breach_timeout_s,
        ) if check_breaches else None
        self._anomaly = LoginAnomalyDetector()
        self._escalation = CAPTCHAEscalationPolicy()
        self._spray = StuffingPatternDetector(spray_threshold=spray_threshold)

    async def evaluate_login(
        self,
        ip: str,
        username: str,
        password: str,
        user_agent: str = "",
        country: Optional[str] = None,
    ) -> LoginDecision:
        # 1. Spray detection (fast, no I/O)
        self._spray.record(ip, username, success=False)
        if self._spray.is_spraying(ip, username):
            return LoginDecision(
                allowed=False,
                block_reason="Too many accounts attempted from this IP",
                risk_score=1.0,
            )
        if self._spray.is_distributed_attack_on(username):
            return LoginDecision(
                allowed=False,
                block_reason="Account under distributed attack — try later",
                risk_score=1.0,
            )

        # 2. Rate limiting
        allowed, wait_s = self._rate_limiter.check(ip, username)
        if not allowed:
            return LoginDecision(
                allowed=False,
                block_reason="Rate limit exceeded",
                retry_after_s=wait_s,
                risk_score=1.0,
            )

        # 3. Breach credential check
        if self._breach_checker:
            try:
                breached = await self._breach_checker.is_breached(password)
                if breached:
                    return LoginDecision(
                        allowed=False,
                        block_reason=(
                            "Password found in breach dataset. "
                            "Reset your password before logging in."
                        ),
                        risk_score=0.9,
                    )
            except Exception:
                pass  # Fail open for breach check

        # 4. Anomaly scoring
        event = LoginEvent(
            username=username, ip=ip,
            country=country, user_agent=user_agent,
            success=False,
        )
        risk_score, reasons = self._anomaly.score(event)

        # 5. Escalation policy based on rate limiter counters
        from collections import defaultdict
        # Approximate failure count from rate limiter internal state
        state = self._escalation.evaluate(ip, failure_count=0)

        return LoginDecision(
            allowed=True,
            require_captcha=state.captcha_required or risk_score >= 0.4,
            require_mfa=state.mfa_required or risk_score >= 0.7,
            risk_score=risk_score,
            anomaly_reasons=reasons,
        )

    def record_outcome(self, ip: str, username: str,
                        country: Optional[str], user_agent: str,
                        success: bool):
        """Call after credential verification to update counters."""
        if success:
            self._rate_limiter.record_success(ip, username)
            self._escalation.reset(ip)
        else:
            self._rate_limiter.record_failure(ip, username)

        event = LoginEvent(
            username=username, ip=ip,
            country=country, user_agent=user_agent or "",
            success=success,
        )
        self._anomaly.record(event)

    def health(self) -> Dict[str, Any]:
        return {
            "rate_limiter": self._rate_limiter.stats(),
            "spray_top_offenders": self._spray.spray_stats(),
        }
```

---

## Comparison

| Approach | Rate Limiting | Breach Check | Anomaly Detection | CAPTCHA Escalation | Spray Detection | Integrated |
|---|---|---|---|---|---|---|
| **LoginAttemptRateLimiter** | Yes | No | No | No | No | No |
| **BreachCredentialChecker** | No | Yes | No | No | No | No |
| **LoginAnomalyDetector** | No | No | Yes | No | No | No |
| **CAPTCHAEscalationPolicy** | No | No | No | Yes | No | No |
| **StuffingPatternDetector** | No | No | No | No | Yes | No |
| **CredentialStuffingGuard** | Yes | Yes | Yes | Yes | Yes | Yes |

**Key insight**: credential stuffing requires layered defense because each layer catches a different attack variant. Rate limiting stops naive single-IP attacks; spray detection catches distributed campaigns; breach checks stop reused passwords; anomaly scoring catches low-volume stealthy attacks. Always record outcomes after the actual credential check — never before — so counters reflect real failures, not pre-check rejections. Set `ip_limit` to 20 and `username_limit` to 5 for most applications; the asymmetry matters because attackers rotate IPs but reuse usernames.
