---
title: "Agent Doesn't Implement SSRF Prevention for URL Fetching Tools"
description: "Agents with URL fetching tools that accept user-supplied URLs without validation are vulnerable to Server-Side Request Forgery: an attacker submits a URL pointing to internal services (169.254.169.254, 10.0.0.0/8, localhost) and the agent fetches it on their behalf, exposing cloud metadata, internal APIs, and private network resources. Implement SSRF prevention that resolves hostnames, validates resolved IPs against allowlists and blocklists, and rejects private/link-local/loopback addresses before making any network request."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-ssrf-prevention-for-url-fetching-tools
tags: [ssrf, url-validation, request-forgery, network-security, ip-allowlist, internal-service-protection]
symptoms:
  - "Agent fetches http://169.254.169.254/latest/meta-data/ when submitted as a 'website to summarize'"
  - "No validation of whether a submitted URL resolves to a private IP range"
  - "Internal APIs accessible via http://10.0.0.1/admin returned when user provides that URL"
  - "localhost and 127.0.0.1 URLs accepted and fetched without restriction"
  - "DNS rebinding attack possible: domain resolves to public IP at validation, private IP at fetch time"
---

## Why This Happens

URL fetching tools receive user-supplied URLs and make HTTP requests on the agent's behalf. Without SSRF prevention, the agent's network access becomes the attacker's network access. The most dangerous targets are cloud metadata services (169.254.169.254 on AWS/GCP/Azure), internal APIs on RFC 1918 ranges (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16), and the loopback interface (127.0.0.1). SSRF prevention requires URL parsing (scheme and hostname extraction), DNS resolution (to catch hostnames that resolve to private IPs), and IP range validation before any network socket is opened.

## Solution 1: IP Range Blocklist

```python
import ipaddress
from typing import List


# IANA special-purpose address ranges that must never be fetched
BLOCKED_NETWORKS: List[ipaddress.IPv4Network | ipaddress.IPv6Network] = [
    # IPv4
    ipaddress.IPv4Network("0.0.0.0/8"),           # "This" network
    ipaddress.IPv4Network("10.0.0.0/8"),           # RFC 1918 private
    ipaddress.IPv4Network("100.64.0.0/10"),        # Shared address space
    ipaddress.IPv4Network("127.0.0.0/8"),          # Loopback
    ipaddress.IPv4Network("169.254.0.0/16"),       # Link-local (metadata services)
    ipaddress.IPv4Network("172.16.0.0/12"),        # RFC 1918 private
    ipaddress.IPv4Network("192.0.0.0/24"),         # IETF Protocol Assignments
    ipaddress.IPv4Network("192.168.0.0/16"),       # RFC 1918 private
    ipaddress.IPv4Network("198.18.0.0/15"),        # Benchmarking
    ipaddress.IPv4Network("198.51.100.0/24"),      # TEST-NET-2
    ipaddress.IPv4Network("203.0.113.0/24"),       # TEST-NET-3
    ipaddress.IPv4Network("224.0.0.0/4"),          # Multicast
    ipaddress.IPv4Network("240.0.0.0/4"),          # Reserved
    ipaddress.IPv4Network("255.255.255.255/32"),   # Broadcast
    # IPv6
    ipaddress.IPv6Network("::1/128"),              # Loopback
    ipaddress.IPv6Network("fc00::/7"),             # Unique local
    ipaddress.IPv6Network("fe80::/10"),            # Link-local
    ipaddress.IPv6Network("ff00::/8"),             # Multicast
]


def is_blocked_ip(ip_str: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip_str)
        for network in BLOCKED_NETWORKS:
            if addr in network:
                return True
        return False
    except ValueError:
        return True  # unparseable IP is blocked by default
```

## Solution 2: URL Validator

```python
import re
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import urlparse


ALLOWED_SCHEMES = {"http", "https"}
BLOCKED_HOSTNAME_PATTERNS = [
    re.compile(r"^localhost$", re.IGNORECASE),
    re.compile(r"^.*\.local$", re.IGNORECASE),
    re.compile(r"^.*\.internal$", re.IGNORECASE),
    re.compile(r"^0\.0\.0\.0$"),
]


@dataclass
class URLValidationResult:
    url: str
    valid: bool
    reason: str = ""
    scheme: str = ""
    hostname: str = ""
    port: Optional[int] = None


class SSRFURLValidator:
    """
    Validates a URL for SSRF risk before any network operation.
    Checks scheme, hostname patterns, and explicit IP address ranges.
    DNS resolution is handled separately by SSRFDNSResolver.
    """

    def __init__(
        self,
        allowed_schemes: set = None,
        allowed_hostname_suffix: List[str] = None,
        max_url_length: int = 2048,
    ):
        self._schemes = allowed_schemes or ALLOWED_SCHEMES
        self._allowed_suffixes = allowed_hostname_suffix or []
        self._max_len = max_url_length

    def validate(self, url: str) -> URLValidationResult:
        if len(url) > self._max_len:
            return URLValidationResult(url=url, valid=False, reason="url_too_long")

        try:
            parsed = urlparse(url)
        except Exception:
            return URLValidationResult(url=url, valid=False, reason="url_parse_error")

        if parsed.scheme not in self._schemes:
            return URLValidationResult(
                url=url, valid=False, reason=f"scheme_not_allowed:{parsed.scheme}"
            )

        hostname = parsed.hostname or ""
        if not hostname:
            return URLValidationResult(url=url, valid=False, reason="missing_hostname")

        # Block explicit IP addresses in blocked ranges
        try:
            import ipaddress
            ipaddress.ip_address(hostname)
            if is_blocked_ip(hostname):
                return URLValidationResult(
                    url=url, valid=False, reason=f"blocked_ip:{hostname}"
                )
        except ValueError:
            pass  # Not a bare IP — will be resolved later

        # Block known internal hostnames
        for pattern in BLOCKED_HOSTNAME_PATTERNS:
            if pattern.match(hostname):
                return URLValidationResult(
                    url=url, valid=False, reason=f"blocked_hostname:{hostname}"
                )

        # Allowlist check (if configured)
        if self._allowed_suffixes:
            allowed = any(hostname.endswith(suffix) for suffix in self._allowed_suffixes)
            if not allowed:
                return URLValidationResult(
                    url=url, valid=False, reason=f"hostname_not_in_allowlist:{hostname}"
                )

        return URLValidationResult(
            url=url, valid=True, scheme=parsed.scheme,
            hostname=hostname, port=parsed.port
        )
```

## Solution 3: DNS Resolver with SSRF Check

```python
import socket
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class DNSResolutionResult:
    hostname: str
    resolved_ips: List[str]
    blocked_ips: List[str]
    safe: bool
    reason: str = ""


class SSRFDNSResolver:
    """
    Resolves a hostname to its IP addresses and validates each
    resolved IP against the blocked network list.
    Catches DNS rebinding by resolving immediately before each fetch.
    """

    def resolve_and_validate(self, hostname: str) -> DNSResolutionResult:
        try:
            addr_infos = socket.getaddrinfo(hostname, None)
        except socket.gaierror as exc:
            return DNSResolutionResult(
                hostname=hostname,
                resolved_ips=[],
                blocked_ips=[],
                safe=False,
                reason=f"dns_resolution_failed:{exc}",
            )

        resolved_ips = list({info[4][0] for info in addr_infos})
        blocked_ips = [ip for ip in resolved_ips if is_blocked_ip(ip)]

        if blocked_ips:
            return DNSResolutionResult(
                hostname=hostname,
                resolved_ips=resolved_ips,
                blocked_ips=blocked_ips,
                safe=False,
                reason=f"resolves_to_blocked_ip:{','.join(blocked_ips)}",
            )

        return DNSResolutionResult(
            hostname=hostname,
            resolved_ips=resolved_ips,
            blocked_ips=[],
            safe=True,
        )
```

## Solution 4: SSRF-Safe HTTP Client

```python
import time
from typing import Any, Callable, Optional


class SSRFSafeHTTPClient:
    """
    Wraps an HTTP client with SSRF prevention applied before every request.
    Validates the URL, resolves DNS, and re-validates the resolved IP
    immediately before opening a socket connection.
    """

    def __init__(
        self,
        url_validator: SSRFURLValidator,
        dns_resolver: SSRFDNSResolver,
        http_fn: Callable[[str], Any],   # underlying fetch function
        max_response_bytes: int = 5 * 1024 * 1024,
    ):
        self._validator = url_validator
        self._resolver = dns_resolver
        self._http = http_fn
        self._max_bytes = max_response_bytes
        self._blocked_requests = 0
        self._allowed_requests = 0

    async def fetch(self, url: str) -> dict:
        # Step 1: URL structural validation
        url_result = self._validator.validate(url)
        if not url_result.valid:
            self._blocked_requests += 1
            return {
                "success": False,
                "blocked": True,
                "reason": url_result.reason,
                "url": url,
            }

        # Step 2: DNS resolution and IP validation
        if url_result.hostname:
            dns_result = self._resolver.resolve_and_validate(url_result.hostname)
            if not dns_result.safe:
                self._blocked_requests += 1
                return {
                    "success": False,
                    "blocked": True,
                    "reason": dns_result.reason,
                    "url": url,
                    "resolved_ips": dns_result.resolved_ips,
                }

        # Step 3: Fetch with size limit
        self._allowed_requests += 1
        try:
            response = await self._http(url)
            return {"success": True, "blocked": False, "response": response}
        except Exception as exc:
            return {"success": False, "blocked": False, "error": str(exc)}

    def stats(self) -> dict:
        total = self._blocked_requests + self._allowed_requests
        return {
            "total_requests": total,
            "blocked": self._blocked_requests,
            "allowed": self._allowed_requests,
            "block_rate": round(self._blocked_requests / max(total, 1), 4),
        }
```

## Solution 5: SSRF Attempt Logger

```python
import json
import time
from pathlib import Path
from threading import Lock


class SSRFAttemptLogger:
    """
    Logs SSRF-blocked requests for security audit and attacker pattern analysis.
    """

    def __init__(self, log_path: str = "/tmp/ssrf_attempts.jsonl"):
        self._path = Path(log_path)
        self._lock = Lock()
        self._total = 0

    def log(
        self,
        url: str,
        reason: str,
        session_id: str = "",
        user_id: str = "",
        resolved_ips: list = None,
    ) -> None:
        record = {
            "ts": time.time(),
            "url": url[:512],
            "reason": reason,
            "session_id": session_id,
            "user_id": user_id,
            "resolved_ips": resolved_ips or [],
        }
        with self._lock:
            with self._path.open("a") as f:
                f.write(json.dumps(record) + "\n")
            self._total += 1

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        records = []
        if not self._path.exists():
            return {"window_seconds": window_seconds, "attempts": 0}
        with self._lock:
            for line in self._path.read_text().splitlines():
                try:
                    r = json.loads(line)
                    if r.get("ts", 0) >= cutoff:
                        records.append(r)
                except json.JSONDecodeError:
                    continue
        by_reason: dict = {}
        for r in records:
            reason = r.get("reason", "unknown").split(":")[0]
            by_reason[reason] = by_reason.get(reason, 0) + 1
        return {
            "window_seconds": window_seconds,
            "attempts": len(records),
            "by_reason": by_reason,
            "unique_sessions": len({r.get("session_id") for r in records}),
        }
```

## Solution 6: SSRF Prevention Dashboard

```python
import time


class SSRFPreventionDashboard:
    """
    Combines HTTP client stats and audit log summary into a
    single operational view for security monitoring.
    """

    def __init__(
        self,
        client: SSRFSafeHTTPClient,
        logger: SSRFAttemptLogger,
    ):
        self._client = client
        self._logger = logger

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "request_stats": self._client.stats(),
            "ssrf_attempts_1h": self._logger.summary(3600.0),
        }
```

## Comparison

| Approach | Scheme Validation | IP Blocklist | DNS Resolution Check | Request Blocking | Audit Logging |
|---|---|---|---|---|---|
| SSRFURLValidator | Yes | Yes (explicit IPs) | No | Signal only | No |
| SSRFDNSResolver | No | Yes (resolved IPs) | Yes | Signal only | No |
| SSRFSafeHTTPClient | Via validator | Via resolver | Via resolver | Yes | No |
| SSRFAttemptLogger | No | No | No | No | Yes (JSONL) |
| SSRFPreventionDashboard | No | No | No | No | Yes |

**Best for production**: Always resolve DNS immediately before opening the socket — not at validation time — to defeat DNS rebinding attacks where a domain resolves to a public IP during validation but a private IP during the actual fetch. Block the entire `169.254.0.0/16` range unconditionally; this covers AWS instance metadata (169.254.169.254), Azure IMDS (169.254.169.254), GCP metadata (169.254.169.254), and link-local addresses used by container orchestration. If you need to allow fetching from a curated set of domains, use `allowed_hostname_suffix` to enforce an explicit allowlist rather than relying solely on the blocklist. Log every blocked request with the resolved IPs — a sequence of requests to `*.attacker.com` domains that all resolve to 10.x.x.x addresses is a DNS rebinding campaign in progress.
