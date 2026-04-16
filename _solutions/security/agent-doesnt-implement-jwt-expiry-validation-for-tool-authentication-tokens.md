---
title: "Agent Doesn't Implement JWT Expiry Validation for Tool Authentication Tokens"
description: "Agents that accept JWTs for tool authentication without validating expiry allow indefinitely-lived tokens: a token issued during initial setup that was never rotated remains valid years later, and a stolen token provides permanent access. Implement strict JWT expiry validation with clock skew tolerance, token blacklisting for revoked credentials, and proactive refresh before expiry to prevent gaps in tool access."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-jwt-expiry-validation-for-tool-authentication-tokens
tags: [jwt, token-expiry, authentication, credential-rotation, token-blacklist, clock-skew]
symptoms:
  - "JWTs decoded and used without checking the 'exp' claim"
  - "Expired tokens accepted because validation only checks signature, not claims"
  - "No token refresh mechanism — agents use the same token until manual rotation"
  - "Revoked tokens remain usable until they naturally expire"
  - "Clock differences between issuer and validator cause valid tokens to be rejected"
---

## Why This Happens

JWT libraries decode and verify signatures by default but do not always enforce claim validation — `exp`, `nbf`, and `iat` checks are often opt-in. A developer who calls `jwt.decode(token, key)` without passing `options={"verify_exp": True}` (or equivalent) has a signature-verified but temporally unvalidated token. The token is cryptographically authentic but may have expired months ago. Expiry validation must be explicit, clock skew tolerance must be bounded, and revocation must be handled out-of-band since JWTs are stateless by design.

## Solution 1: JWT Claims Validator

```python
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class JWTValidationConfig:
    clock_skew_seconds: float = 30.0     # tolerate up to 30s clock drift
    max_token_lifetime_seconds: float = 86400.0  # reject tokens valid >24h
    required_claims: List[str] = None    # claims that must be present
    allowed_algorithms: List[str] = None

    def __post_init__(self) -> None:
        if self.required_claims is None:
            self.required_claims = ["exp", "iat", "sub"]
        if self.allowed_algorithms is None:
            self.allowed_algorithms = ["RS256", "ES256"]


@dataclass
class JWTValidationResult:
    valid: bool
    claims: Dict[str, Any]
    error: str = ""
    expires_in_seconds: float = 0.0
    issued_seconds_ago: float = 0.0


class JWTClaimsValidator:
    """
    Validates JWT claims independently of signature verification.
    Enforces expiry, not-before, issuance time, and required claims.
    """

    def __init__(self, config: JWTValidationConfig):
        self._config = config

    def validate(self, claims: Dict[str, Any]) -> JWTValidationResult:
        now = time.time()
        skew = self._config.clock_skew_seconds

        # Check required claims
        for claim in self._config.required_claims:
            if claim not in claims:
                return JWTValidationResult(
                    valid=False, claims=claims,
                    error=f"missing required claim: '{claim}'"
                )

        exp = claims.get("exp", 0)
        iat = claims.get("iat", now)
        nbf = claims.get("nbf", 0)

        if now > exp + skew:
            return JWTValidationResult(
                valid=False, claims=claims,
                error=f"token expired at {exp}, now={now:.0f}"
            )

        if nbf and now < nbf - skew:
            return JWTValidationResult(
                valid=False, claims=claims,
                error=f"token not yet valid (nbf={nbf})"
            )

        lifetime = exp - iat
        if lifetime > self._config.max_token_lifetime_seconds:
            return JWTValidationResult(
                valid=False, claims=claims,
                error=f"token lifetime {lifetime:.0f}s exceeds maximum {self._config.max_token_lifetime_seconds:.0f}s"
            )

        return JWTValidationResult(
            valid=True,
            claims=claims,
            expires_in_seconds=round(exp - now, 1),
            issued_seconds_ago=round(now - iat, 1),
        )
```

## Solution 2: Token Blacklist

```python
import time
from threading import Lock
from typing import Dict, Optional, Set


class TokenBlacklist:
    """
    Maintains a set of revoked JWT IDs (jti claims).
    Automatically evicts entries after their original expiry time.
    """

    def __init__(self):
        self._revoked: Dict[str, float] = {}   # jti -> expiry time
        self._lock = Lock()

    def revoke(self, jti: str, expires_at: float) -> None:
        with self._lock:
            self._revoked[jti] = expires_at

    def is_revoked(self, jti: str) -> bool:
        with self._lock:
            self._evict()
            return jti in self._revoked

    def _evict(self) -> None:
        now = time.time()
        expired = [jti for jti, exp in self._revoked.items() if now > exp + 3600]
        for jti in expired:
            del self._revoked[jti]

    def revoked_count(self) -> int:
        with self._lock:
            self._evict()
            return len(self._revoked)
```

## Solution 3: JWT Tool Auth Verifier

```python
import hashlib
import hmac
import json
import base64
import time
from typing import Any, Dict, Optional


class JWTToolAuthVerifier:
    """
    Full JWT verification pipeline: decode, verify signature (HMAC-SHA256
    for simplicity — replace with RS256/ES256 for production), validate claims,
    and check blacklist.
    """

    def __init__(
        self,
        secret_key: bytes,
        claims_validator: JWTClaimsValidator,
        blacklist: TokenBlacklist,
    ):
        self._key = secret_key
        self._validator = claims_validator
        self._blacklist = blacklist

    def _b64decode(self, s: str) -> bytes:
        padding = 4 - len(s) % 4
        return base64.urlsafe_b64decode(s + "=" * padding)

    def verify(self, token: str) -> JWTValidationResult:
        parts = token.split(".")
        if len(parts) != 3:
            return JWTValidationResult(valid=False, claims={}, error="malformed JWT structure")

        header_b64, payload_b64, sig_b64 = parts
        signing_input = f"{header_b64}.{payload_b64}".encode()

        # Verify signature
        expected_sig = hmac.new(self._key, signing_input, hashlib.sha256).digest()
        provided_sig = self._b64decode(sig_b64)
        if not hmac.compare_digest(expected_sig, provided_sig):
            return JWTValidationResult(valid=False, claims={}, error="invalid signature")

        # Decode claims
        try:
            claims: Dict[str, Any] = json.loads(self._b64decode(payload_b64))
        except Exception as exc:
            return JWTValidationResult(valid=False, claims={}, error=f"payload decode error: {exc}")

        # Check blacklist
        jti = claims.get("jti", "")
        if jti and self._blacklist.is_revoked(jti):
            return JWTValidationResult(valid=False, claims=claims, error=f"token jti={jti} is revoked")

        # Validate claims
        return self._validator.validate(claims)
```

## Solution 4: Proactive Token Refresher

```python
import asyncio
import time
from typing import Callable, Optional


class ProactiveTokenRefresher:
    """
    Monitors a JWT's remaining lifetime and triggers a refresh callback
    before expiry, preventing gaps in tool authentication.
    """

    def __init__(
        self,
        refresh_before_expiry_seconds: float = 300.0,  # refresh 5 min before expiry
        check_interval_seconds: float = 60.0,
    ):
        self._refresh_before = refresh_before_expiry_seconds
        self._check_interval = check_interval_seconds
        self._current_expiry: Optional[float] = None
        self._refresh_count = 0
        self._running = False

    def update_expiry(self, expires_at: float) -> None:
        self._current_expiry = expires_at

    def needs_refresh(self) -> bool:
        if self._current_expiry is None:
            return True
        return time.time() >= self._current_expiry - self._refresh_before

    async def run(self, refresh_fn: Callable) -> None:
        self._running = True
        while self._running:
            if self.needs_refresh():
                try:
                    new_token = await refresh_fn()
                    self._refresh_count += 1
                except Exception:
                    pass  # caller should handle refresh failures
            await asyncio.sleep(self._check_interval)

    def stop(self) -> None:
        self._running = False

    def stats(self) -> dict:
        return {
            "refresh_count": self._refresh_count,
            "current_expiry": self._current_expiry,
            "seconds_until_expiry": round(
                (self._current_expiry or 0) - time.time(), 1
            ) if self._current_expiry else None,
            "needs_refresh": self.needs_refresh(),
        }
```

## Solution 5: Token Audit Logger

```python
import time
from typing import List


class TokenAuditLogger:
    """
    Records JWT validation outcomes for security monitoring.
    Surfaces repeated failures from the same subject for alerting.
    """

    def __init__(self, max_records: int = 10000):
        self._records: List[dict] = []
        self._max = max_records

    def record(self, result: JWTValidationResult, tool_name: str = "", source_ip: str = "") -> None:
        if len(self._records) >= self._max:
            self._records.pop(0)
        claims = result.claims
        self._records.append({
            "ts": time.time(),
            "valid": result.valid,
            "error": result.error,
            "sub": claims.get("sub", ""),
            "jti": claims.get("jti", ""),
            "tool_name": tool_name,
            "source_ip": source_ip,
            "expires_in": result.expires_in_seconds if result.valid else None,
        })

    def repeated_failures(self, window_seconds: float = 300.0, threshold: int = 5) -> List[str]:
        cutoff = time.time() - window_seconds
        failures: dict = {}
        for r in self._records:
            if r["ts"] < cutoff or r["valid"]:
                continue
            sub = r.get("sub", "unknown")
            failures[sub] = failures.get(sub, 0) + 1
        return [sub for sub, count in failures.items() if count >= threshold]

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        return {
            "window_seconds": window_seconds,
            "total_validations": len(recent),
            "valid": sum(1 for r in recent if r["valid"]),
            "invalid": sum(1 for r in recent if not r["valid"]),
            "expired_tokens": sum(1 for r in recent if "expired" in r.get("error", "")),
            "revoked_tokens": sum(1 for r in recent if "revoked" in r.get("error", "")),
            "repeated_failure_subjects": self.repeated_failures(window_seconds),
        }
```

## Solution 6: JWT Auth Security Dashboard

```python
import time


class JWTAuthSecurityDashboard:
    """
    Combines verifier status, refresh state, blacklist size, and
    audit summary into a single security snapshot.
    """

    def __init__(
        self,
        refresher: ProactiveTokenRefresher,
        blacklist: TokenBlacklist,
        audit_logger: TokenAuditLogger,
    ):
        self._refresher = refresher
        self._blacklist = blacklist
        self._audit = audit_logger

    def render(self) -> dict:
        audit = self._audit.summary(window_seconds=3600.0)
        refresh = self._refresher.stats()
        failures = self._audit.repeated_failures(window_seconds=300.0)

        return {
            "generated_at": time.time(),
            "token_refresh": refresh,
            "blacklisted_tokens": self._blacklist.revoked_count(),
            "audit_1h": audit,
            "repeated_failure_subjects_5m": failures,
            "alert": len(failures) > 0 or audit["revoked_tokens"] > 0,
        }
```

## Comparison

| Approach | Expiry Validation | Clock Skew | Blacklist | Proactive Refresh | Audit |
|---|---|---|---|---|---|
| JWTClaimsValidator | Yes (exp+nbf+iat) | Yes (configurable) | No | No | No |
| TokenBlacklist | No | No | Yes (jti-based) | No | No |
| JWTToolAuthVerifier | Via validator | Via validator | Via blacklist | No | No |
| ProactiveTokenRefresher | No | No | No | Yes | No |
| TokenAuditLogger | No | No | No | No | Yes |
| JWTAuthSecurityDashboard | No | No | Via blacklist | Via refresher | Via logger |

**Best for production**: Always set `required_claims=["exp", "iat", "sub", "jti"]` — the `jti` claim enables revocation without which you cannot invalidate a stolen token before expiry. Set `max_token_lifetime_seconds=3600` for tool tokens — tools do not need 24-hour tokens and shorter lifetimes limit the blast radius of theft. Set `refresh_before_expiry_seconds=300` (5 minutes) in `ProactiveTokenRefresher` so there is a 5-minute window to handle transient refresh failures before the current token expires. Alert immediately on any `revoked_tokens` count in the audit summary — a revoked token being presented means either a replay attack or a client that has not rotated.
