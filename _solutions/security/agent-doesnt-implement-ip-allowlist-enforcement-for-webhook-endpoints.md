---
title: "Agent Doesn't Implement IP Allowlist Enforcement for Webhook Endpoints"
description: "Agent webhook endpoints that accept callbacks from external services — Slack, Telegram, Stripe, GitHub — are exposed to the public internet with no IP-level restriction. Any host can send arbitrary payloads to these endpoints, bypassing application-layer authentication through forged or replayed requests. Implement IP allowlist enforcement that validates the source IP of incoming webhook requests against provider-published CIDR ranges before any payload processing occurs."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-ip-allowlist-enforcement-for-webhook-endpoints
tags: [ip-allowlist, webhook-security, cidr-validation, network-security, source-ip, ingress-control]
symptoms:
  - "Webhook endpoint accepts requests from any IP address on the internet"
  - "No IP-based check before HMAC signature verification runs"
  - "Attacker can probe webhook endpoint with crafted payloads from any host"
  - "No logging of source IPs for webhook requests — no forensic trail"
  - "Provider CIDR ranges never loaded — all IPs implicitly trusted equally"
---

## Why This Happens

Webhook endpoints are typically implemented as plain HTTP POST handlers that verify an HMAC signature and then process the payload. The signature check provides application-layer authentication, but it still exposes the endpoint to probing, replay amplification, and denial-of-service from arbitrary IP addresses. IP allowlisting is a network-layer defense that rejects requests before any application logic runs: if the source IP is not in the provider's published CIDR range, the connection is rejected with a 403 before the HMAC is even computed.

## Solution 1: CIDR Range Allowlist

```python
import ipaddress
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class WebhookSourceAllowlist:
    provider_name: str
    cidr_ranges: List[str]
    description: str = ""
    _networks: List[ipaddress.IPv4Network | ipaddress.IPv6Network] = field(
        default_factory=list, repr=False
    )

    def __post_init__(self) -> None:
        self._networks = []
        for cidr in self.cidr_ranges:
            try:
                self._networks.append(ipaddress.ip_network(cidr, strict=False))
            except ValueError:
                pass

    def contains(self, ip_str: str) -> bool:
        try:
            addr = ipaddress.ip_address(ip_str)
        except ValueError:
            return False
        return any(addr in network for network in self._networks)


# Known provider CIDR ranges (update periodically from provider documentation)
PROVIDER_ALLOWLISTS = {
    "slack": WebhookSourceAllowlist(
        provider_name="slack",
        cidr_ranges=["54.80.0.0/12", "54.144.0.0/12", "3.0.0.0/8"],
        description="Slack webhook source IPs",
    ),
    "github": WebhookSourceAllowlist(
        provider_name="github",
        cidr_ranges=["192.30.252.0/22", "185.199.108.0/22", "140.82.112.0/20"],
        description="GitHub webhook source IPs",
    ),
    "stripe": WebhookSourceAllowlist(
        provider_name="stripe",
        cidr_ranges=["3.18.12.63/32", "3.130.192.231/32", "13.235.14.237/32",
                     "13.235.122.149/32", "18.211.135.69/32", "35.154.171.200/32",
                     "52.15.183.38/32", "54.187.174.169/32", "54.187.205.235/32",
                     "54.187.216.72/32"],
        description="Stripe webhook source IPs",
    ),
}
```

## Solution 2: IP Allowlist Registry

```python
from threading import Lock
from typing import Dict, List, Optional


class WebhookIPAllowlistRegistry:
    """
    Manages allowlists for multiple webhook providers.
    Supports dynamic CIDR updates without restart.
    """

    def __init__(self):
        self._lock = Lock()
        self._allowlists: Dict[str, WebhookSourceAllowlist] = {}

    def register(self, allowlist: WebhookSourceAllowlist) -> None:
        with self._lock:
            self._allowlists[allowlist.provider_name] = allowlist

    def register_all(self, allowlists: Dict[str, WebhookSourceAllowlist]) -> None:
        with self._lock:
            self._allowlists.update(allowlists)

    def update_cidrs(self, provider_name: str, cidr_ranges: List[str]) -> None:
        with self._lock:
            existing = self._allowlists.get(provider_name)
            if existing:
                updated = WebhookSourceAllowlist(
                    provider_name=provider_name,
                    cidr_ranges=cidr_ranges,
                    description=existing.description,
                )
                self._allowlists[provider_name] = updated

    def is_allowed(self, provider_name: str, ip_address: str) -> bool:
        with self._lock:
            allowlist = self._allowlists.get(provider_name)
        if allowlist is None:
            return False   # deny by default if provider not registered
        return allowlist.contains(ip_address)

    def registered_providers(self) -> List[str]:
        with self._lock:
            return list(self._allowlists.keys())
```

## Solution 3: IP Extractor

```python
from typing import Any, Optional


class WebhookSourceIPExtractor:
    """
    Extracts the true client IP from HTTP request headers,
    handling X-Forwarded-For and X-Real-IP proxying.
    """

    def __init__(
        self,
        trusted_proxy_header: str = "X-Forwarded-For",
        trust_proxy: bool = True,
    ):
        self._header = trusted_proxy_header
        self._trust_proxy = trust_proxy

    def extract(self, request: Any) -> Optional[str]:
        """
        Accepts a request-like object with .headers dict and .remote_addr.
        Returns the source IP string, or None if not determinable.
        """
        if self._trust_proxy:
            forwarded = None
            if hasattr(request, "headers"):
                forwarded = (
                    request.headers.get("X-Forwarded-For")
                    or request.headers.get("X-Real-IP")
                )
            if forwarded:
                # X-Forwarded-For may be "client, proxy1, proxy2"
                # The leftmost non-private IP is the real client
                for ip_str in forwarded.split(","):
                    ip_str = ip_str.strip()
                    if self._is_public(ip_str):
                        return ip_str
                # Fall through to remote_addr if all forwarded IPs are private
        return getattr(request, "remote_addr", None)

    @staticmethod
    def _is_public(ip_str: str) -> bool:
        import ipaddress
        try:
            addr = ipaddress.ip_address(ip_str)
            return not addr.is_private and not addr.is_loopback
        except ValueError:
            return False
```

## Solution 4: IP Enforcement Middleware

```python
import time
from typing import Any, Callable, Optional


class WebhookIPEnforcementError(Exception):
    def __init__(self, provider: str, ip: str):
        super().__init__(f"Request from '{ip}' not in allowlist for provider '{provider}'")
        self.provider = provider
        self.ip = ip


class WebhookIPEnforcementMiddleware:
    """
    Rejects webhook requests whose source IP is not in the provider's allowlist.
    Must run before HMAC verification and payload processing.
    """

    def __init__(
        self,
        registry: WebhookIPAllowlistRegistry,
        extractor: WebhookSourceIPExtractor,
        audit_logger: Optional[object] = None,
    ):
        self._registry = registry
        self._extractor = extractor
        self._logger = audit_logger
        self._rejections: list = []
        self._allowed: int = 0

    def enforce(self, provider_name: str, request: Any) -> str:
        """
        Returns the source IP if allowed.
        Raises WebhookIPEnforcementError if blocked.
        """
        ip = self._extractor.extract(request) or "unknown"

        if not self._registry.is_allowed(provider_name, ip):
            self._rejections.append({
                "ts": time.time(),
                "provider": provider_name,
                "ip": ip,
            })
            raise WebhookIPEnforcementError(provider_name, ip)

        self._allowed += 1
        return ip

    def rejection_summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._rejections if r["ts"] >= cutoff]
        from collections import Counter
        return {
            "window_seconds": window_seconds,
            "rejections": len(recent),
            "allowed": self._allowed,
            "top_blocked_ips": Counter(r["ip"] for r in recent).most_common(5),
            "by_provider": Counter(r["provider"] for r in recent),
        }
```

## Solution 5: CIDR Range Updater

```python
import time
from typing import Callable, Dict, List, Optional


class ProviderCIDRRangeUpdater:
    """
    Fetches updated CIDR ranges from provider-published endpoints
    and refreshes the registry. Run on a schedule or at startup.
    """

    def __init__(
        self,
        registry: WebhookIPAllowlistRegistry,
        fetch_fns: Dict[str, Callable[[], List[str]]],
    ):
        self._registry = registry
        self._fetch_fns = fetch_fns
        self._last_updated: Dict[str, float] = {}

    def refresh(self, provider_name: str) -> dict:
        fetch_fn = self._fetch_fns.get(provider_name)
        if not fetch_fn:
            return {"status": "no_fetch_fn", "provider": provider_name}

        try:
            cidrs = fetch_fn()
            self._registry.update_cidrs(provider_name, cidrs)
            self._last_updated[provider_name] = time.time()
            return {
                "status": "updated",
                "provider": provider_name,
                "cidr_count": len(cidrs),
            }
        except Exception as exc:
            return {"status": "error", "provider": provider_name, "error": str(exc)}

    def refresh_all(self) -> list:
        return [self.refresh(p) for p in self._fetch_fns]
```

## Solution 6: IP Allowlist Dashboard

```python
import time


class WebhookIPAllowlistDashboard:
    """
    Combines allowlist registry state, enforcement statistics,
    and CIDR update history into a single security view.
    """

    def __init__(
        self,
        registry: WebhookIPAllowlistRegistry,
        middleware: WebhookIPEnforcementMiddleware,
        updater: ProviderCIDRRangeUpdater,
    ):
        self._registry = registry
        self._middleware = middleware
        self._updater = updater

    def render(self) -> dict:
        providers = self._registry.registered_providers()
        return {
            "generated_at": time.time(),
            "registered_providers": providers,
            "enforcement_last_hour": self._middleware.rejection_summary(window_seconds=3600.0),
            "last_cidr_update": self._updater._last_updated,
        }
```

## Comparison

| Approach | CIDR Matching | Proxy Header Support | Dynamic Update | Rejection Logging | Dashboard |
|---|---|---|---|---|---|
| WebhookSourceAllowlist | Yes (ipaddress) | No | No | No | No |
| WebhookIPAllowlistRegistry | Via allowlist | No | Yes (update_cidrs) | No | No |
| WebhookSourceIPExtractor | No | Yes (XFF/X-Real-IP) | No | No | No |
| WebhookIPEnforcementMiddleware | Via registry | Via extractor | No | Yes | No |
| WebhookIPAllowlistDashboard | No | No | No | No | Yes |

**Best for production**: Place IP enforcement as the first check in the webhook handler — before signature verification, before JSON parsing, before any business logic. Return HTTP 403 (not 401) for blocked IPs: 401 implies credentials could fix the problem, which is misleading for an IP block. Schedule `ProviderCIDRRangeUpdater.refresh_all()` weekly since provider IP ranges change infrequently but do change. Set a fallback behavior when the allowlist is empty (no CIDRs loaded): deny all requests until the list is populated rather than allowing all, to prevent a misconfiguration from opening the endpoint to the world.
