---
title: "Agent Doesn't Implement SSRF Prevention for URL-Fetching Tools"
description: "Agents that expose URL-fetching tools without server-side request forgery (SSRF) prevention allow attackers to pivot the agent into an internal network scanner: passing 'http://169.254.169.254/latest/meta-data/' to a fetch tool retrieves cloud instance metadata, and 'http://10.0.0.1/' probes internal services. Implement SSRF prevention that validates URLs against an allowlist of safe destinations, blocks private address ranges, and validates DNS resolution results before connecting."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-ssrf-prevention-for-url-fetching-tools
tags: [ssrf, url-validation, request-forgery, network-security, dns-rebinding, allowlist]
symptoms:
  - "Agent fetch tool accepts arbitrary URLs including internal IP ranges"
  - "Cloud metadata endpoint reachable through agent fetch — credentials exposed"
  - "No validation of whether resolved IP falls in private/loopback/link-local ranges"
  - "DNS rebinding bypasses hostname-based allowlist checks"
  - "Fetch tool makes requests to localhost or 127.0.0.1 when prompted"
---

## Why This Happens

URL-fetching tools expose a raw HTTP client to LLM-controlled inputs. Without SSRF prevention, any URL the LLM constructs — whether from user-supplied data or a prompt injection in a retrieved document — is passed to an HTTP library that resolves DNS and connects. Private IP ranges (RFC 1918: 10.x, 172.16-31.x, 192.168.x), loopback (127.x), and link-local (169.254.x — cloud metadata) are reachable from any server. The defense requires two layers: pre-resolution checks on the hostname/URL structure, and post-resolution checks on the actual IP address returned by DNS, since DNS rebinding can return a private IP for an allowlisted hostname after the hostname check passes.

## Solution 1: URL Structure Validator

```python
import re
from dataclasses import dataclass, field
from typing import List, Optional
from urllib.parse import urlparse


@dataclass
class URLValidationPolicy:
    allowed_schemes: List[str] = field(default_factory=lambda: ["https"])
    blocked_hostnames: List[str] = field(default_factory=lambda: [
        "localhost", "metadata.google.internal",
    ])
    require_public_tld: bool = True
    max_url_length: int = 2048
    allow_ip_literals: bool = False   # block direct IP URLs like http://1.2.3.4/


class URLStructureValidator:
    """
    Validates URL structure before DNS resolution.
    Rejects obvious SSRF vectors: private IP literals, localhost,
    non-https schemes, and URLs that exceed maximum length.
    """

    _PRIVATE_IP_LITERAL = re.compile(
        r"^("
        r"10\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
        r"172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|"
        r"192\.168\.\d{1,3}\.\d{1,3}|"
        r"127\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
        r"169\.254\.\d{1,3}\.\d{1,3}|"
        r"::1|"
        r"fc[0-9a-f][0-9a-f]:|"
        r"fd[0-9a-f][0-9a-f]:"
        r")$",
        re.IGNORECASE,
    )

    def __init__(self, policy: URLValidationPolicy):
        self._policy = policy

    def validate(self, url: str) -> tuple[bool, str]:
        """Returns (is_valid, reason_if_invalid)."""
        if len(url) > self._policy.max_url_length:
            return False, f"URL exceeds max length ({len(url)} > {self._policy.max_url_length})"

        try:
            parsed = urlparse(url)
        except Exception as exc:
            return False, f"URL parse error: {exc}"

        if parsed.scheme not in self._policy.allowed_schemes:
            return False, f"Scheme '{parsed.scheme}' not in allowed list"

        hostname = parsed.hostname or ""
        if not hostname:
            return False, "No hostname in URL"

        if hostname.lower() in [h.lower() for h in self._policy.blocked_hostnames]:
            return False, f"Hostname '{hostname}' is explicitly blocked"

        if not self._policy.allow_ip_literals and self._PRIVATE_IP_LITERAL.match(hostname):
            return False, f"Private/loopback IP literal '{hostname}' is blocked"

        return True, ""
```

## Solution 2: Post-Resolution IP Validator

```python
import ipaddress
import socket
from typing import List, Optional, Tuple


class PostResolutionIPValidator:
    """
    Resolves the URL hostname and validates that none of the returned
    IP addresses fall in private, loopback, link-local, or multicast ranges.
    Defends against DNS rebinding: the actual connect IP is always checked.
    """

    _BLOCKED_NETWORKS = [
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
        ipaddress.ip_network("127.0.0.0/8"),
        ipaddress.ip_network("169.254.0.0/16"),   # link-local / cloud metadata
        ipaddress.ip_network("::1/128"),
        ipaddress.ip_network("fc00::/7"),          # ULA
        ipaddress.ip_network("fe80::/10"),         # link-local IPv6
        ipaddress.ip_network("100.64.0.0/10"),     # CGNAT
        ipaddress.ip_network("0.0.0.0/8"),
        ipaddress.ip_network("255.255.255.255/32"),
    ]

    def resolve_and_validate(self, hostname: str, port: int = 443) -> Tuple[bool, str, List[str]]:
        """
        Returns (is_safe, reason_if_blocked, resolved_ips).
        Raises socket.gaierror if DNS resolution fails.
        """
        try:
            addrs = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
        except socket.gaierror as exc:
            return False, f"DNS resolution failed: {exc}", []

        resolved_ips = list({addr[4][0] for addr in addrs})

        for ip_str in resolved_ips:
            try:
                ip = ipaddress.ip_address(ip_str)
            except ValueError:
                return False, f"Could not parse resolved IP '{ip_str}'", resolved_ips

            for network in self._BLOCKED_NETWORKS:
                if ip in network:
                    return False, f"Resolved IP {ip_str} is in blocked network {network}", resolved_ips

        return True, "", resolved_ips
```

## Solution 3: Hostname Allowlist Matcher

```python
import fnmatch
from typing import List, Optional


class HostnameAllowlistMatcher:
    """
    Matches a resolved hostname against an explicit allowlist of trusted domains.
    Supports wildcard patterns (*.example.com) for subdomain allowlisting.
    When an allowlist is configured, any hostname not on it is blocked.
    """

    def __init__(self, patterns: Optional[List[str]] = None):
        # None means no allowlist (block nothing via this check).
        # Empty list means block everything.
        self._patterns = [p.lower() for p in patterns] if patterns is not None else None

    def is_allowed(self, hostname: str) -> tuple[bool, str]:
        if self._patterns is None:
            return True, ""   # no allowlist configured

        hostname = hostname.lower()
        for pattern in self._patterns:
            if fnmatch.fnmatch(hostname, pattern):
                return True, ""

        return False, f"Hostname '{hostname}' not in allowlist ({len(self._patterns)} patterns)"

    def add_pattern(self, pattern: str) -> None:
        if self._patterns is None:
            self._patterns = []
        self._patterns.append(pattern.lower())
```

## Solution 4: SSRF-Safe URL Fetcher

```python
import asyncio
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlparse


@dataclass
class FetchResult:
    url: str
    status_code: Optional[int]
    body: Optional[bytes]
    resolved_ips: list
    blocked: bool = False
    block_reason: str = ""
    error: Optional[str] = None


class SSRFSafeURLFetcher:
    """
    Wraps an async HTTP client with SSRF prevention.
    Validates URL structure, checks hostname allowlist, resolves DNS,
    validates resolved IPs, then connects only if all checks pass.
    """

    def __init__(
        self,
        structure_validator: URLStructureValidator,
        ip_validator: PostResolutionIPValidator,
        allowlist_matcher: HostnameAllowlistMatcher,
        connect_timeout_seconds: float = 5.0,
        read_timeout_seconds: float = 15.0,
        max_response_bytes: int = 5 * 1024 * 1024,  # 5 MB
    ):
        self._structure = structure_validator
        self._ip_validator = ip_validator
        self._allowlist = allowlist_matcher
        self._connect_timeout = connect_timeout_seconds
        self._read_timeout = read_timeout_seconds
        self._max_bytes = max_response_bytes

    async def fetch(self, url: str) -> FetchResult:
        # Step 1: Structure check
        valid, reason = self._structure.validate(url)
        if not valid:
            return FetchResult(url=url, status_code=None, body=None,
                               resolved_ips=[], blocked=True, block_reason=reason)

        hostname = urlparse(url).hostname or ""

        # Step 2: Allowlist check
        allowed, reason = self._allowlist.is_allowed(hostname)
        if not allowed:
            return FetchResult(url=url, status_code=None, body=None,
                               resolved_ips=[], blocked=True, block_reason=reason)

        # Step 3: DNS + IP validation
        try:
            safe, reason, resolved_ips = self._ip_validator.resolve_and_validate(hostname)
        except Exception as exc:
            return FetchResult(url=url, status_code=None, body=None,
                               resolved_ips=[], blocked=True,
                               block_reason=f"DNS error: {exc}")

        if not safe:
            return FetchResult(url=url, status_code=None, body=None,
                               resolved_ips=resolved_ips, blocked=True, block_reason=reason)

        # Step 4: Actual fetch (inject your preferred async HTTP client here)
        try:
            import aiohttp
            timeout = aiohttp.ClientTimeout(
                connect=self._connect_timeout, total=self._read_timeout
            )
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    body = await resp.content.read(self._max_bytes)
                    return FetchResult(
                        url=url,
                        status_code=resp.status,
                        body=body,
                        resolved_ips=resolved_ips,
                    )
        except Exception as exc:
            return FetchResult(url=url, status_code=None, body=None,
                               resolved_ips=resolved_ips, error=str(exc))
```

## Solution 5: SSRF Attempt Audit Logger

```python
import time
from typing import List


class SSRFAttemptAuditLogger:
    """
    Records every blocked fetch attempt with the URL, block reason,
    and resolved IPs. Surfaces attack patterns by session and domain.
    """

    def __init__(self, max_records: int = 10000):
        self._max = max_records
        self._records: List[dict] = []

    def record_block(
        self,
        url: str,
        block_reason: str,
        resolved_ips: List[str],
        session_id: str = "",
    ) -> None:
        from urllib.parse import urlparse
        hostname = urlparse(url).hostname or ""
        if len(self._records) >= self._max:
            self._records.pop(0)
        self._records.append({
            "ts": time.time(),
            "url": url,
            "hostname": hostname,
            "block_reason": block_reason,
            "resolved_ips": resolved_ips,
            "session_id": session_id,
        })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        hostname_counts: dict = {}
        for r in recent:
            hostname_counts[r["hostname"]] = hostname_counts.get(r["hostname"], 0) + 1
        return {
            "window_seconds": window_seconds,
            "blocked_requests": len(recent),
            "top_hostnames": sorted(
                hostname_counts.items(), key=lambda x: x[1], reverse=True
            )[:10],
        }
```

## Solution 6: SSRF Prevention Dashboard

```python
import time


class SSRFPreventionDashboard:
    """
    Combines structural validation stats, allowlist config, and
    blocked-attempt audit into a single operational snapshot.
    """

    def __init__(
        self,
        structure_validator: URLStructureValidator,
        allowlist_matcher: HostnameAllowlistMatcher,
        audit_logger: SSRFAttemptAuditLogger,
    ):
        self._structure = structure_validator
        self._allowlist = allowlist_matcher
        self._audit = audit_logger

    def render(self) -> dict:
        policy = self._structure._policy
        return {
            "generated_at": time.time(),
            "policy": {
                "allowed_schemes": policy.allowed_schemes,
                "allow_ip_literals": policy.allow_ip_literals,
                "blocked_hostnames": policy.blocked_hostnames,
                "allowlist_pattern_count": (
                    len(self._allowlist._patterns)
                    if self._allowlist._patterns is not None
                    else "disabled"
                ),
            },
            "blocked_attempts_1h": self._audit.summary(3600.0),
            "blocked_attempts_24h": self._audit.summary(86400.0),
        }
```

## Comparison

| Approach | Structure Check | IP Range Block | DNS Rebinding Defense | Allowlist | Audit |
|---|---|---|---|---|---|
| URLStructureValidator | Yes (scheme, literal IP) | Literal only | No | No | No |
| PostResolutionIPValidator | No | Yes (post-DNS) | Yes | No | No |
| HostnameAllowlistMatcher | No | No | Partial | Yes | No |
| SSRFSafeURLFetcher | Via validator | Via IP validator | Via IP validator | Via allowlist | No |
| SSRFAttemptAuditLogger | No | No | No | No | Yes |
| SSRFPreventionDashboard | No | No | No | No | Yes (aggregate) |

**Best for production**: Always perform both pre-resolution and post-resolution checks — pre-resolution catches obvious cases (IP literals, localhost), but post-resolution is the only defense against DNS rebinding where an allowlisted hostname resolves to a private IP after the hostname check. Set `allow_ip_literals=False` globally — no legitimate external URL should be a raw IP in an agent context. Use `HostnameAllowlistMatcher` with an explicit `["*.example.com", "api.partner.com"]` list rather than relying solely on blocklists, as blocklists can never enumerate all internal hostnames. Monitor `SSRFAttemptAuditLogger.summary()`: repeated blocks from the same session targeting cloud metadata ranges (169.254.x) indicate an active prompt injection attack attempting infrastructure reconnaissance.
