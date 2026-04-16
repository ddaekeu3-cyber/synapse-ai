---
title: "Agent Doesn't Implement Cross-Origin Request Validation for Webhook Tool Triggers"
description: "Agents that expose webhook endpoints to trigger tool calls without validating the origin, signature, or IP range of incoming requests allow any party to invoke arbitrary tool executions. A forged webhook call can trigger data lookups, external API calls, or state mutations that the attacker could not otherwise initiate. Implement webhook origin validation that verifies HMAC signatures, checks allowlisted IP ranges, and enforces per-source rate limits."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-cross-origin-request-validation-for-webhook-tool-triggers
tags: [webhook-security, origin-validation, hmac-signature, ip-allowlist, webhook-forgery, request-authentication]
symptoms:
  - "Webhook endpoint accepts requests from any IP without signature verification"
  - "Forged webhook calls trigger tool executions that modify agent state or call external APIs"
  - "No HMAC signature validation — any client with the URL can trigger tool calls"
  - "Webhook source IP is logged but not checked against an allowlist"
  - "No rate limit per webhook source — a single attacker can trigger thousands of tool calls"
---

## Why This Happens

Webhook endpoints are URL-addressable triggers. If the URL leaks — through logs, referrer headers, or insider access — anyone with the URL can forge a request. Without signature verification, the agent cannot distinguish a legitimate webhook from an attacker's POST. HMAC-SHA256 signatures solve this: the webhook provider signs the payload with a shared secret, and the receiver verifies the signature before processing. IP allowlisting adds a second layer: even a valid signature from an unexpected IP is rejected. Per-source rate limiting ensures that a compromised secret cannot be used to flood the agent with tool triggers.

## Solution 1: Webhook Source Config

```python
import re
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class WebhookSourceConfig:
    source_name: str
    signing_secret: str                    # shared secret for HMAC verification
    signature_header: str = "X-Signature-256"
    timestamp_header: str = "X-Timestamp"
    max_timestamp_age_seconds: float = 300.0
    allowed_ip_ranges: List[str] = field(default_factory=list)   # CIDR notation
    allowed_tools: List[str] = field(default_factory=list)       # empty = all allowed
    max_requests_per_minute: int = 60
    enabled: bool = True
```

## Solution 2: HMAC Signature Verifier

```python
import hashlib
import hmac
import time
from typing import Dict, Optional


class WebhookHMACVerifier:
    """
    Verifies HMAC-SHA256 signatures on incoming webhook requests.
    Checks timestamp freshness to prevent replay attacks.
    """

    def __init__(self, config: WebhookSourceConfig):
        self._config = config

    def verify(
        self,
        body: bytes,
        headers: Dict[str, str],
    ) -> tuple[bool, str]:
        """Returns (is_valid, reason)."""
        # Timestamp check
        ts_str = headers.get(self._config.timestamp_header, "")
        if not ts_str:
            return False, "missing timestamp header"
        try:
            ts = float(ts_str)
        except ValueError:
            return False, "invalid timestamp format"

        age = abs(time.time() - ts)
        if age > self._config.max_timestamp_age_seconds:
            return False, f"timestamp too old: {age:.0f}s (max {self._config.max_timestamp_age_seconds}s)"

        # Signature check
        sig_header = headers.get(self._config.signature_header, "")
        if not sig_header:
            return False, "missing signature header"

        # Expected format: "sha256=<hex>"
        if sig_header.startswith("sha256="):
            received_sig = sig_header[7:]
        else:
            received_sig = sig_header

        signed_payload = f"{ts_str}.".encode() + body
        expected_sig = hmac.new(
            self._config.signing_secret.encode(),
            signed_payload,
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(expected_sig, received_sig):
            return False, "signature mismatch"

        return True, "ok"
```

## Solution 3: IP Allowlist Validator

```python
import ipaddress
from typing import List, Optional


class WebhookIPAllowlistValidator:
    """
    Validates that the request source IP falls within at least one
    of the configured CIDR ranges. Empty allowlist means all IPs allowed.
    """

    def __init__(self, allowed_cidr_ranges: List[str]):
        self._networks = []
        for cidr in allowed_cidr_ranges:
            try:
                self._networks.append(ipaddress.ip_network(cidr, strict=False))
            except ValueError:
                pass

    def validate(self, source_ip: str) -> tuple[bool, str]:
        if not self._networks:
            return True, "no ip restriction configured"
        try:
            addr = ipaddress.ip_address(source_ip)
        except ValueError:
            return False, f"invalid source IP: {source_ip}"

        for network in self._networks:
            if addr in network:
                return True, f"IP {source_ip} in allowlisted range {network}"

        return False, f"IP {source_ip} not in allowlisted ranges"
```

## Solution 4: Per-Source Rate Limiter

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, Dict


class WebhookPerSourceRateLimiter:
    """
    Sliding-window rate limiter per webhook source name.
    Rejects requests that exceed max_requests_per_minute.
    """

    def __init__(self):
        self._windows: Dict[str, Deque[float]] = {}
        self._lock = Lock()

    def check(self, source_name: str, max_per_minute: int) -> tuple[bool, str]:
        now = time.time()
        cutoff = now - 60.0
        with self._lock:
            window = self._windows.setdefault(source_name, deque())
            while window and window[0] < cutoff:
                window.popleft()
            if len(window) >= max_per_minute:
                return False, f"rate limit exceeded: {len(window)}/{max_per_minute} requests/min"
            window.append(now)
            return True, f"{len(window)}/{max_per_minute} requests/min"
```

## Solution 5: Webhook Validation Gate

```python
import time
from typing import Any, Callable, Dict, List, Optional


class WebhookValidationGate:
    """
    Combines HMAC verification, IP allowlist, rate limiting, and
    tool allowlist into a single validation pass for incoming webhooks.
    """

    def __init__(
        self,
        configs: List[WebhookSourceConfig],
        rate_limiter: WebhookPerSourceRateLimiter,
    ):
        self._configs = {c.source_name: c for c in configs}
        self._rate_limiter = rate_limiter
        self._blocked = 0
        self._allowed = 0

    def validate(
        self,
        source_name: str,
        body: bytes,
        headers: Dict[str, str],
        source_ip: str,
        requested_tool: Optional[str] = None,
    ) -> dict:
        config = self._configs.get(source_name)
        if config is None:
            self._blocked += 1
            return {"allowed": False, "reason": f"unknown webhook source '{source_name}'"}

        if not config.enabled:
            self._blocked += 1
            return {"allowed": False, "reason": f"webhook source '{source_name}' is disabled"}

        # HMAC signature
        verifier = WebhookHMACVerifier(config)
        sig_ok, sig_reason = verifier.verify(body, headers)
        if not sig_ok:
            self._blocked += 1
            return {"allowed": False, "reason": f"signature invalid: {sig_reason}"}

        # IP allowlist
        ip_validator = WebhookIPAllowlistValidator(config.allowed_ip_ranges)
        ip_ok, ip_reason = ip_validator.validate(source_ip)
        if not ip_ok:
            self._blocked += 1
            return {"allowed": False, "reason": ip_reason}

        # Rate limit
        rate_ok, rate_reason = self._rate_limiter.check(source_name, config.max_requests_per_minute)
        if not rate_ok:
            self._blocked += 1
            return {"allowed": False, "reason": rate_reason}

        # Tool allowlist
        if requested_tool and config.allowed_tools and requested_tool not in config.allowed_tools:
            self._blocked += 1
            return {
                "allowed": False,
                "reason": f"tool '{requested_tool}' not in allowlist for source '{source_name}'",
            }

        self._allowed += 1
        return {"allowed": True, "source_name": source_name, "source_ip": source_ip}

    def stats(self) -> dict:
        total = self._allowed + self._blocked
        return {
            "total_requests": total,
            "allowed": self._allowed,
            "blocked": self._blocked,
            "block_rate": round(self._blocked / max(total, 1), 4),
        }
```

## Solution 6: Webhook Security Dashboard

```python
import time


class WebhookSecurityDashboard:
    """
    Combines gate stats with source config audit for operational
    security visibility of webhook endpoints.
    """

    def __init__(
        self,
        gate: WebhookValidationGate,
        rate_limiter: WebhookPerSourceRateLimiter,
    ):
        self._gate = gate
        self._rate_limiter = rate_limiter

    def render(self) -> dict:
        stats = self._gate.stats()
        return {
            "generated_at": time.time(),
            "gate_stats": stats,
            "registered_sources": list(self._gate._configs.keys()),
            "health": {
                "block_rate": stats["block_rate"],
                "sources_configured": len(self._gate._configs),
            },
        }
```

## Comparison

| Approach | HMAC Verification | Timestamp Replay Prevention | IP Allowlist | Per-Source Rate Limit | Tool Allowlist |
|---|---|---|---|---|---|
| WebhookHMACVerifier | Yes (SHA-256) | Yes (max age) | No | No | No |
| WebhookIPAllowlistValidator | No | No | Yes (CIDR) | No | No |
| WebhookPerSourceRateLimiter | No | No | No | Yes (sliding window) | No |
| WebhookValidationGate | Via verifier | Via verifier | Via validator | Via limiter | Yes |
| WebhookSecurityDashboard | No | No | No | No | No |

**Best for production**: Always verify HMAC signatures — IP allowlisting alone is bypassable if an attacker has access to a machine in the allowed range. Use `max_timestamp_age_seconds=300` (5 minutes) to tolerate clock skew between webhook provider and agent while still blocking replayed requests. Store signing secrets in a secrets manager (AWS Secrets Manager, Vault) rather than environment variables — they must be rotatable without redeployment. Set `allowed_tools` per source to the minimum set that source legitimately triggers: a GitHub webhook source should only be allowed to trigger `fetch_pr_diff` and `post_comment`, not `send_email` or `delete_record`.
