---
title: "Agent Doesn't Implement DNS Rebinding Protection for Tool HTTP Calls"
description: "Agents that make HTTP requests on behalf of user-provided URLs are vulnerable to DNS rebinding attacks: a malicious host resolves to a legitimate IP during validation but rebinds to an internal IP (e.g., 169.254.169.254) when the actual request is made, causing the agent to exfiltrate cloud metadata or probe internal services. Implement DNS rebinding protection that resolves the target hostname before connecting and rejects requests where the resolved IP falls in private or link-local address ranges."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-dns-rebinding-protection-for-tool-http-calls
tags: [dns-rebinding, ssrf, private-ip-protection, cloud-metadata, url-validation, network-security]
symptoms:
  - "Tool HTTP calls to user-supplied URLs can reach cloud metadata endpoint 169.254.169.254"
  - "URL validation passes for external hostnames that later resolve to internal IPs"
  - "No IP-level check after DNS resolution — only scheme/hostname validation"
  - "Agent can be tricked into proxying requests to internal services via hostname rebinding"
  - "Cloud provider IMDS endpoints accessible through user-controlled URL parameters"
---

## Why This Happens

URL validation that operates on the hostname string alone is insufficient. A hostname like `evil.attacker.com` passes all string-based checks. When the HTTP client resolves the hostname at connection time, the attacker's DNS server returns `169.254.169.254` (AWS/GCP/Azure IMDS) or a private RFC-1918 address. The resolution and the connection happen inside the HTTP client, bypassing any pre-flight hostname check. Protection requires intercepting the DNS resolution result before the TCP connection is established and rejecting IPs that fall within private, loopback, link-local, or reserved ranges.

## Solution 1: IP Range Classifier

```python
import ipaddress
from enum import Enum
from typing import List


class IPRangeCategory(str, Enum):
    PUBLIC = "public"
    LOOPBACK = "loopback"
    PRIVATE_RFC1918 = "private_rfc1918"
    LINK_LOCAL = "link_local"           # 169.254.0.0/16 — IMDS range
    MULTICAST = "multicast"
    RESERVED = "reserved"
    DOCUMENTATION = "documentation"


_BLOCKED_RANGES = [
    (ipaddress.ip_network("127.0.0.0/8"), IPRangeCategory.LOOPBACK),
    (ipaddress.ip_network("10.0.0.0/8"), IPRangeCategory.PRIVATE_RFC1918),
    (ipaddress.ip_network("172.16.0.0/12"), IPRangeCategory.PRIVATE_RFC1918),
    (ipaddress.ip_network("192.168.0.0/16"), IPRangeCategory.PRIVATE_RFC1918),
    (ipaddress.ip_network("169.254.0.0/16"), IPRangeCategory.LINK_LOCAL),
    (ipaddress.ip_network("::1/128"), IPRangeCategory.LOOPBACK),
    (ipaddress.ip_network("fc00::/7"), IPRangeCategory.PRIVATE_RFC1918),
    (ipaddress.ip_network("fe80::/10"), IPRangeCategory.LINK_LOCAL),
    (ipaddress.ip_network("224.0.0.0/4"), IPRangeCategory.MULTICAST),
    (ipaddress.ip_network("240.0.0.0/4"), IPRangeCategory.RESERVED),
    (ipaddress.ip_network("100.64.0.0/10"), IPRangeCategory.RESERVED),  # carrier-grade NAT
    (ipaddress.ip_network("0.0.0.0/8"), IPRangeCategory.RESERVED),
]


class IPRangeClassifier:
    """
    Classifies an IP address into a range category.
    Returns PUBLIC only if no blocked range matches.
    """

    def classify(self, ip_str: str) -> IPRangeCategory:
        try:
            addr = ipaddress.ip_address(ip_str)
        except ValueError:
            return IPRangeCategory.RESERVED

        for network, category in _BLOCKED_RANGES:
            if addr in network:
                return category
        return IPRangeCategory.PUBLIC

    def is_allowed(self, ip_str: str) -> bool:
        return self.classify(ip_str) == IPRangeCategory.PUBLIC
```

## Solution 2: DNS Resolver with IP Validation

```python
import socket
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class DNSResolutionResult:
    hostname: str
    resolved_ips: List[str]
    allowed_ips: List[str]
    blocked_ips: List[str]
    blocked_categories: List[str]
    allowed: bool


class ValidatingDNSResolver:
    """
    Resolves a hostname and validates all returned IPs against the
    IP range classifier. Blocks resolution if any IP falls in a
    disallowed range — conservative approach prevents partial rebinding.
    """

    def __init__(self, classifier: IPRangeClassifier):
        self._classifier = classifier

    def resolve_and_validate(self, hostname: str) -> DNSResolutionResult:
        try:
            addr_infos = socket.getaddrinfo(hostname, None)
        except socket.gaierror as e:
            return DNSResolutionResult(
                hostname=hostname,
                resolved_ips=[],
                allowed_ips=[],
                blocked_ips=[],
                blocked_categories=[f"resolution_failed: {e}"],
                allowed=False,
            )

        resolved = list({info[4][0] for info in addr_infos})
        allowed_ips = []
        blocked_ips = []
        blocked_categories = []

        for ip in resolved:
            category = self._classifier.classify(ip)
            if category == IPRangeCategory.PUBLIC:
                allowed_ips.append(ip)
            else:
                blocked_ips.append(ip)
                blocked_categories.append(f"{ip}:{category.value}")

        # Block if ANY resolved IP is non-public
        allowed = len(blocked_ips) == 0 and len(resolved) > 0

        return DNSResolutionResult(
            hostname=hostname,
            resolved_ips=resolved,
            allowed_ips=allowed_ips,
            blocked_ips=blocked_ips,
            blocked_categories=blocked_categories,
            allowed=allowed,
        )
```

## Solution 3: URL Pre-Flight Validator

```python
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse


@dataclass
class URLPreFlightResult:
    url: str
    allowed: bool
    block_reason: Optional[str]
    resolved_hostname: Optional[str] = None
    resolved_ips: Optional[list] = None


_ALLOWED_SCHEMES = {"https", "http"}
_BLOCKED_HOSTNAME_PATTERNS = [
    re.compile(r"^localhost$", re.IGNORECASE),
    re.compile(r"^.*\.internal$", re.IGNORECASE),
    re.compile(r"^.*\.local$", re.IGNORECASE),
]


class URLPreFlightValidator:
    """
    Validates a URL before an HTTP tool call:
    1. Scheme must be http or https
    2. Hostname must not match known-internal patterns
    3. DNS resolution must return only public IPs
    """

    def __init__(self, resolver: ValidatingDNSResolver):
        self._resolver = resolver

    def validate(self, url: str) -> URLPreFlightResult:
        parsed = urlparse(url)

        if parsed.scheme not in _ALLOWED_SCHEMES:
            return URLPreFlightResult(url=url, allowed=False,
                                      block_reason=f"scheme_not_allowed: {parsed.scheme}")

        hostname = parsed.hostname or ""
        if not hostname:
            return URLPreFlightResult(url=url, allowed=False, block_reason="missing_hostname")

        for pattern in _BLOCKED_HOSTNAME_PATTERNS:
            if pattern.match(hostname):
                return URLPreFlightResult(url=url, allowed=False,
                                          block_reason=f"blocked_hostname_pattern: {hostname}")

        dns_result = self._resolver.resolve_and_validate(hostname)
        if not dns_result.allowed:
            return URLPreFlightResult(
                url=url,
                allowed=False,
                block_reason=f"dns_rebinding_blocked: {', '.join(dns_result.blocked_categories)}",
                resolved_hostname=hostname,
                resolved_ips=dns_result.resolved_ips,
            )

        return URLPreFlightResult(
            url=url,
            allowed=True,
            block_reason=None,
            resolved_hostname=hostname,
            resolved_ips=dns_result.allowed_ips,
        )
```

## Solution 4: DNS-Safe HTTP Tool Wrapper

```python
import time
from typing import Any, Callable, Optional


class SSRFBlockedError(Exception):
    def __init__(self, url: str, reason: str):
        super().__init__(f"SSRF/DNS-rebinding blocked for '{url}': {reason}")
        self.url = url
        self.reason = reason


class DNSSafeHTTPToolWrapper:
    """
    Wraps any HTTP tool call with DNS rebinding pre-flight validation.
    Raises SSRFBlockedError before the connection is attempted.
    """

    def __init__(
        self,
        validator: URLPreFlightValidator,
        audit_fn: Optional[Callable[[dict], None]] = None,
    ):
        self._validator = validator
        self._audit = audit_fn or (lambda _: None)

    async def call(
        self,
        url: str,
        http_fn: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        result = self._validator.validate(url)

        self._audit({
            "ts": time.time(),
            "url": url,
            "allowed": result.allowed,
            "block_reason": result.block_reason,
            "resolved_ips": result.resolved_ips,
        })

        if not result.allowed:
            raise SSRFBlockedError(url, result.block_reason or "unknown")

        return await http_fn(url, *args, **kwargs)
```

## Solution 5: SSRF Attempt Tracker

```python
import time
from collections import deque
from threading import Lock
from typing import Deque


class SSRFAttemptTracker:
    """
    Records blocked DNS rebinding / SSRF attempts for security audit.
    Detects sessions with repeated SSRF attempts indicating active probing.
    """

    def __init__(self, max_records: int = 10_000):
        self._max = max_records
        self._records: Deque[dict] = deque()
        self._lock = Lock()

    def record(self, audit_event: dict, session_id: str = "") -> None:
        if audit_event.get("allowed", True):
            return
        with self._lock:
            self._records.append({
                **audit_event,
                "session_id": session_id,
            })
            if len(self._records) > self._max:
                self._records.popleft()

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [r for r in self._records if r.get("ts", 0) >= cutoff]
        return {
            "window_seconds": window_seconds,
            "blocked_attempts": len(recent),
            "unique_sessions": len({r.get("session_id") for r in recent}),
            "unique_urls": len({r.get("url") for r in recent}),
            "top_block_reasons": self._top_reasons(recent),
        }

    @staticmethod
    def _top_reasons(records: list) -> list:
        counts: dict = {}
        for r in records:
            reason = (r.get("block_reason") or "unknown").split(":")[0]
            counts[reason] = counts.get(reason, 0) + 1
        return sorted(
            [{"reason": k, "count": v} for k, v in counts.items()],
            key=lambda x: -x["count"],
        )[:5]
```

## Solution 6: DNS Rebinding Protection Dashboard

```python
import time
from typing import Optional


class DNSRebindingProtectionDashboard:
    """
    Renders validation configuration, blocked attempt statistics,
    and classifier coverage for operational and security review.
    """

    def __init__(
        self,
        validator: URLPreFlightValidator,
        tracker: SSRFAttemptTracker,
    ):
        self._validator = validator
        self._tracker = tracker

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "protection": {
                "blocked_ranges_count": len(_BLOCKED_RANGES),
                "blocked_hostname_patterns": len(_BLOCKED_HOSTNAME_PATTERNS),
                "allowed_schemes": list(_ALLOWED_SCHEMES),
            },
            "attempt_summary_1h": self._tracker.summary(3600.0),
            "attempt_summary_24h": self._tracker.summary(86400.0),
        }
```

## Comparison

| Approach | IP Range Blocking | DNS Resolution | URL Pre-flight | Request Blocking | SSRF Audit |
|---|---|---|---|---|---|
| IPRangeClassifier | Yes (12 ranges) | No | No | No | No |
| ValidatingDNSResolver | Via classifier | Yes | No | No | No |
| URLPreFlightValidator | Via resolver | Via resolver | Yes | No | No |
| DNSSafeHTTPToolWrapper | Via validator | Via validator | Via validator | Yes | Via audit_fn |
| SSRFAttemptTracker | No | No | No | No | Yes |

**Best for production**: Block if ANY resolved IP is non-public — not just the first. Attackers use multi-answer DNS responses where one IP is public (passing validation) and another is internal (used for the actual connection). Run DNS resolution synchronously before constructing the HTTP request object; do not cache the result — a DNS TTL of 1 second is enough to rebind between a cached validation and an actual request. Log every blocked attempt with the resolved IPs to `SSRFAttemptTracker`; more than 3 blocked attempts from a single session within a minute is active probing and should trigger session termination.
