---
title: "Agent Doesn't Implement Timing-Attack Resistant Comparison for Secrets"
description: "AI agents that compare API keys, tokens, or HMACs with Python's == operator leak secret length and value through timing side-channels: a correct prefix returns slightly faster than a wrong one. Constant-time comparison eliminates this channel by ensuring every comparison takes identical wall-clock time regardless of where bytes diverge."
date: 2025-02-14
difficulty: intermediate
category: security
slug: agent-doesnt-implement-timing-attack-resistant-comparison-for-secrets
tags:
  - timing-attack
  - hmac
  - constant-time
  - secrets
  - api-key
  - side-channel
  - security
symptoms:
  - "Agent compares webhook signatures with == or str comparison"
  - "API key validation uses early-exit string equality"
  - "HMAC tag verification done with bytes == bytes"
  - "Token comparison returns faster for correct prefixes — measurable with 10k requests"
  - "Security audit flags secret comparison as timing-vulnerable"
---

## Problem

Python's `==` operator short-circuits: it returns `False` as soon as the first differing byte is found. An attacker who can make millions of requests can measure the average response time for each candidate byte, progressively recovering a secret one byte at a time. `hmac.compare_digest` (Python ≥ 3.3) and equivalent constant-time implementations eliminate this by always iterating every byte, XOR-accumulating differences, and returning only after a full scan. Every agent that validates tokens, webhook signatures, or HMAC-tagged tool responses must use constant-time comparison.

---

## Solution 1: ConstantTimeComparator — Core Comparison Primitives

```python
import hashlib
import hmac
import os
import secrets
import time
from typing import Union


class ConstantTimeComparator:
    """
    Drop-in constant-time comparison primitives for all secret comparisons
    in agent tool handlers and middleware.

    Usage:
        cmp = ConstantTimeComparator()

        # Webhook HMAC validation:
        ok = cmp.compare_hmac(
            body=request_body,
            signature=request.headers["X-Hub-Signature-256"],
            secret=WEBHOOK_SECRET,
        )

        # API key check:
        ok = cmp.compare_token(received_key, EXPECTED_KEY)
    """

    @staticmethod
    def compare_bytes(a: bytes, b: bytes) -> bool:
        """Constant-time bytes comparison (always compares len(b) bytes)."""
        return hmac.compare_digest(a, b)

    @staticmethod
    def compare_str(a: str, b: str) -> bool:
        """Constant-time string comparison via hmac.compare_digest."""
        return hmac.compare_digest(
            a.encode("utf-8"), b.encode("utf-8")
        )

    @staticmethod
    def compare_token(received: Union[str, bytes],
                      expected: Union[str, bytes]) -> bool:
        """
        Compare bearer tokens or API keys in constant time.
        Normalises both to bytes before comparison.
        """
        if isinstance(received, str):
            received = received.encode("utf-8")
        if isinstance(expected, str):
            expected = expected.encode("utf-8")
        # Length mismatch is still O(1) via compare_digest
        return hmac.compare_digest(received, expected)

    @staticmethod
    def compute_hmac(payload: bytes, secret: bytes,
                     algorithm: str = "sha256") -> str:
        """Compute HMAC-SHA256 hex digest for payload."""
        mac = hmac.new(secret, payload, algorithm)
        return mac.hexdigest()

    @staticmethod
    def compare_hmac(body: bytes, signature: str,
                     secret: Union[str, bytes],
                     prefix: str = "sha256=") -> bool:
        """
        Validate an HMAC-SHA256 webhook signature in constant time.
        Strips optional prefix (e.g. 'sha256=') before comparing.
        """
        if isinstance(secret, str):
            secret = secret.encode("utf-8")
        expected = hmac.new(secret, body, "sha256").hexdigest()
        received = signature.removeprefix(prefix)
        return hmac.compare_digest(expected, received)
```

---

## Solution 2: SecretValidator — Request Authentication Middleware

```python
import hashlib
import hmac
import logging
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    valid: bool
    reason: str
    elapsed_ms: float


class SecretValidator:
    """
    Middleware-style validator for inbound tool call authentication.
    Validates API keys, HMAC signatures, and bearer tokens using
    constant-time comparison. Logs validation outcomes without
    leaking secret values.

    Usage:
        validator = SecretValidator(
            api_key=os.environ["AGENT_API_KEY"],
            webhook_secret=os.environ["WEBHOOK_SECRET"],
        )

        result = validator.validate_api_key(request.headers.get("X-Api-Key"))
        if not result.valid:
            return 401, result.reason
    """

    def __init__(self, api_key: str = "",
                 webhook_secret: str = ""):
        self._api_key = api_key.encode("utf-8") if api_key else b""
        self._webhook_secret = (
            webhook_secret.encode("utf-8") if webhook_secret else b""
        )

    def validate_api_key(self, received: Optional[str]) -> ValidationResult:
        t0 = time.monotonic()
        if not received:
            # Still compute a fake comparison to avoid early return
            hmac.compare_digest(b"x", self._api_key or b"x")
            return ValidationResult(False, "missing_api_key",
                                    (time.monotonic() - t0) * 1000)
        ok = hmac.compare_digest(
            received.encode("utf-8"), self._api_key
        )
        elapsed = (time.monotonic() - t0) * 1000
        if not ok:
            logger.warning("api_key_rejected elapsed_ms=%.2f", elapsed)
        return ValidationResult(ok, "ok" if ok else "invalid_api_key", elapsed)

    def validate_webhook(self, body: bytes, signature: str,
                         prefix: str = "sha256=") -> ValidationResult:
        t0 = time.monotonic()
        received = signature.removeprefix(prefix)
        expected = hmac.new(self._webhook_secret, body, "sha256").hexdigest()
        ok = hmac.compare_digest(expected, received)
        elapsed = (time.monotonic() - t0) * 1000
        if not ok:
            logger.warning("webhook_sig_rejected elapsed_ms=%.2f", elapsed)
        return ValidationResult(ok, "ok" if ok else "invalid_signature", elapsed)

    def validate_bearer(self, auth_header: Optional[str],
                        expected_token: str) -> ValidationResult:
        t0 = time.monotonic()
        if not auth_header or not auth_header.startswith("Bearer "):
            hmac.compare_digest(b"x", expected_token.encode("utf-8"))
            return ValidationResult(False, "missing_bearer",
                                    (time.monotonic() - t0) * 1000)
        token = auth_header[7:]
        ok = hmac.compare_digest(
            token.encode("utf-8"), expected_token.encode("utf-8")
        )
        elapsed = (time.monotonic() - t0) * 1000
        return ValidationResult(ok, "ok" if ok else "invalid_bearer", elapsed)
```

---

## Solution 3: HMACToolSigner — Sign and Verify Tool Responses

```python
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple


@dataclass
class SignedToolResponse:
    payload: Dict[str, Any]
    signature: str          # hex HMAC-SHA256
    signed_at: float


class HMACToolSigner:
    """
    Signs outbound tool responses and verifies inbound ones using HMAC-SHA256.
    Prevents a compromised tool from injecting unsigned responses into the
    agent's reasoning loop without detection.

    Usage:
        signer = HMACToolSigner(secret=os.environ["TOOL_SIGNING_SECRET"])

        # Tool produces result:
        signed = signer.sign({"rows": [...], "count": 42})

        # Agent verifies before trusting:
        payload, ok = signer.verify(signed)
        if not ok:
            raise SecurityError("Tool response signature invalid")
    """

    def __init__(self, secret: str, algorithm: str = "sha256"):
        self._secret = secret.encode("utf-8")
        self._algorithm = algorithm

    def _canonical(self, payload: Dict[str, Any]) -> bytes:
        return json.dumps(payload, sort_keys=True,
                          separators=(",", ":")).encode("utf-8")

    def sign(self, payload: Dict[str, Any]) -> SignedToolResponse:
        canonical = self._canonical(payload)
        sig = hmac.new(self._secret, canonical, self._algorithm).hexdigest()
        return SignedToolResponse(
            payload=payload,
            signature=sig,
            signed_at=time.time(),
        )

    def verify(self, signed: SignedToolResponse,
               max_age_s: float = 60.0) -> Tuple[Dict[str, Any], bool]:
        """
        Returns (payload, True) if signature valid and not expired.
        Returns (payload, False) otherwise. Always constant-time.
        """
        age = time.time() - signed.signed_at
        canonical = self._canonical(signed.payload)
        expected = hmac.new(self._secret, canonical, self._algorithm).hexdigest()
        sig_ok = hmac.compare_digest(expected, signed.signature)
        age_ok = age <= max_age_s
        return signed.payload, sig_ok and age_ok

    def verify_raw(self, body: bytes, signature: str) -> bool:
        """Verify a raw bytes payload against a hex signature."""
        expected = hmac.new(self._secret, body, self._algorithm).hexdigest()
        return hmac.compare_digest(expected, signature)
```

---

## Solution 4: ConstantTimeKeyStore — Multi-Key Lookup Without Timing Leak

```python
import hmac
import secrets
import time
from typing import Dict, List, Optional, Tuple


class ConstantTimeKeyStore:
    """
    Stores multiple API keys (e.g., per-client) and validates inbound
    keys in constant time regardless of how many keys are stored.
    Prevents timing oracle attacks that enumerate valid key prefixes.

    Usage:
        store = ConstantTimeKeyStore()
        store.add_key("client-a", "sk-abc123...")
        store.add_key("client-b", "sk-xyz789...")

        client_id, ok = store.lookup(request.headers["X-Api-Key"])
        if not ok:
            return 401
    """

    def __init__(self):
        # Store (client_id, key_bytes) pairs — never a dict keyed by key
        self._keys: List[Tuple[str, bytes]] = []

    def add_key(self, client_id: str, api_key: str):
        self._keys.append((client_id, api_key.encode("utf-8")))

    def remove_key(self, client_id: str):
        self._keys = [(cid, k) for cid, k in self._keys if cid != client_id]

    def lookup(self, received: str) -> Tuple[Optional[str], bool]:
        """
        Validates `received` against all stored keys in constant time.
        Always iterates every key to prevent early-exit timing leaks.
        Returns (client_id, True) on first match, (None, False) otherwise.
        """
        received_bytes = received.encode("utf-8")
        matched_client: Optional[str] = None
        matched = False

        for client_id, key_bytes in self._keys:
            # Pad shorter key to same length as received before compare
            # (compare_digest handles unequal lengths safely)
            ok = hmac.compare_digest(received_bytes, key_bytes)
            if ok and not matched:
                matched_client = client_id
                matched = True
            # Always iterate remaining keys even after match

        return (matched_client, True) if matched else (None, False)

    def rotate_key(self, client_id: str, new_key: str):
        """Replace an existing key without a window where neither key works."""
        new_bytes = new_key.encode("utf-8")
        updated = False
        new_list = []
        for cid, kb in self._keys:
            if cid == client_id:
                new_list.append((cid, new_bytes))
                updated = True
            else:
                new_list.append((cid, kb))
        if not updated:
            new_list.append((client_id, new_bytes))
        self._keys = new_list

    def key_count(self) -> int:
        return len(self._keys)
```

---

## Solution 5: TimingAwareAuditLogger — Log Anomalous Comparison Times

```python
import logging
import statistics
import time
from collections import deque
from typing import Deque, Optional

logger = logging.getLogger(__name__)


class TimingAwareAuditLogger:
    """
    Records validation timing and alerts when comparisons take
    anomalously long — which can indicate a side-channel probe
    or a degraded comparison path that reintroduced timing variance.

    Usage:
        audit = TimingAwareAuditLogger(window=200, alert_sigma=4.0)

        t0 = time.monotonic()
        ok = hmac.compare_digest(a, b)
        audit.record("api_key_check", (time.monotonic() - t0) * 1e6, ok)
    """

    def __init__(self, window: int = 200, alert_sigma: float = 4.0):
        self._window = window
        self._sigma = alert_sigma
        self._samples: Deque[float] = deque(maxlen=window)
        self._failures = 0

    def record(self, operation: str, elapsed_us: float, success: bool):
        self._samples.append(elapsed_us)
        if not success:
            self._failures += 1

        if len(self._samples) >= 20:
            mean = statistics.mean(self._samples)
            stdev = statistics.stdev(self._samples)
            if stdev > 0 and (elapsed_us - mean) > self._sigma * stdev:
                logger.warning(
                    "timing_anomaly op=%s elapsed_us=%.1f mean=%.1f "
                    "sigma=%.1f threshold=%.1f — possible side-channel probe",
                    operation, elapsed_us, mean, stdev,
                    mean + self._sigma * stdev,
                )

    def stats(self) -> dict:
        if not self._samples:
            return {}
        return {
            "samples": len(self._samples),
            "mean_us": round(statistics.mean(self._samples), 2),
            "stdev_us": round(statistics.stdev(self._samples), 2)
            if len(self._samples) > 1 else 0.0,
            "max_us": round(max(self._samples), 2),
            "failure_count": self._failures,
        }
```

---

## Solution 6: SecureAgentAuthMiddleware — Full Auth Stack

```python
import hmac
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class AuthContext:
    authenticated: bool
    client_id: Optional[str]
    method: str           # "api_key" | "hmac_webhook" | "bearer"
    elapsed_ms: float


class SecureAgentAuthMiddleware:
    """
    Full authentication middleware for agent HTTP endpoints.
    Routes to the appropriate constant-time validator based on
    the request path and available headers.

    Usage:
        auth = SecureAgentAuthMiddleware(
            api_key=os.environ["AGENT_API_KEY"],
            webhook_secret=os.environ["WEBHOOK_SECRET"],
        )

        @app.post("/tool")
        async def handle_tool(request):
            ctx = auth.authenticate(
                headers=dict(request.headers),
                body=await request.body(),
            )
            if not ctx.authenticated:
                return Response(status_code=401)
            ...
    """

    def __init__(self, api_key: str = "",
                 webhook_secret: str = "",
                 bearer_token: str = ""):
        self._validator = SecretValidator(
            api_key=api_key,
            webhook_secret=webhook_secret,
        )
        self._bearer = bearer_token
        self._audit = TimingAwareAuditLogger()

    def authenticate(self, headers: Dict[str, str],
                     body: bytes = b"") -> AuthContext:
        t0 = time.monotonic()

        # Prefer webhook signature if present
        sig = headers.get("X-Hub-Signature-256") or headers.get("X-Signature-256")
        if sig:
            result = self._validator.validate_webhook(body, sig)
            elapsed = (time.monotonic() - t0) * 1000
            self._audit.record("webhook_hmac", elapsed * 1000, result.valid)
            return AuthContext(result.valid, None, "hmac_webhook", elapsed)

        # Bearer token
        auth_header = headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer ") and self._bearer:
            result = self._validator.validate_bearer(auth_header, self._bearer)
            elapsed = (time.monotonic() - t0) * 1000
            self._audit.record("bearer", elapsed * 1000, result.valid)
            return AuthContext(result.valid, None, "bearer", elapsed)

        # API key
        api_key = headers.get("X-Api-Key")
        result = self._validator.validate_api_key(api_key)
        elapsed = (time.monotonic() - t0) * 1000
        self._audit.record("api_key", elapsed * 1000, result.valid)
        return AuthContext(result.valid, None, "api_key", elapsed)

    def timing_report(self) -> dict:
        return self._audit.stats()
```

---

## Comparison

| Approach | Scope | Constant-Time | HMAC | Multi-Key | Audit |
|---|---|---|---|---|---|
| **ConstantTimeComparator** | Primitives | Yes | Yes | No | No |
| **SecretValidator** | Request auth | Yes | Yes | No | Partial |
| **HMACToolSigner** | Tool responses | Yes | Yes | No | No |
| **ConstantTimeKeyStore** | Multi-client | Yes | No | Yes | No |
| **TimingAwareAuditLogger** | Monitoring | N/A | N/A | N/A | Yes |
| **SecureAgentAuthMiddleware** | Full stack | Yes | Yes | No | Yes |

**Key insight**: never use `==` or `!=` to compare secrets. Use `hmac.compare_digest` for every token, key, and HMAC tag validation. For webhook signatures, always compute the expected HMAC server-side and compare the result — never compare the incoming bytes directly against a stored hash. Pair with `TimingAwareAuditLogger` to detect if a code change accidentally reintroduces timing variance.
