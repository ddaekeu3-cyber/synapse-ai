---
title: "Agent Doesn't Implement SSRF Prevention for Agent URL Fetching"
description: "AI agents that fetch URLs from user input or tool parameters without validation are vulnerable to Server-Side Request Forgery (SSRF). An attacker supplies an internal URL — cloud metadata endpoints, localhost admin panels, internal databases — and the agent fetches and leaks the response. SSRF prevention requires allowlist-based URL validation, DNS rebinding protection, and blocking reserved IP ranges before any HTTP connection is opened."
date: 2025-02-11
difficulty: intermediate
category: security
slug: agent-doesnt-implement-ssrf-prevention-for-agent-url-fetching
tags:
  - ssrf
  - server-side-request-forgery
  - url-validation
  - allowlist
  - dns-rebinding
  - network-security
  - input-validation
symptoms:
  - "Agent fetches URLs provided by users without validating the destination"
  - "web_search or fetch_page tool can be directed to http://169.254.169.254/ (AWS metadata)"
  - "Agent can be made to probe internal network services via tool parameters"
  - "No blocklist for RFC-1918 and link-local IP ranges in the URL fetch path"
  - "DNS rebinding attack bypasses hostname checks by resolving to private IP after validation"
---

## Problem

SSRF lets an attacker use the agent as a proxy to reach internal services that are not publicly accessible. The cloud metadata endpoint (`169.254.169.254`), Kubernetes API server (`10.x.x.x:6443`), and internal admin panels are all reachable from within the agent's network. The agent returns the response to the attacker — credentials, tokens, configuration. Prevention requires validating URL structure, resolving the hostname, and checking the resolved IP against blocked ranges — in that order, with the IP check happening as close to the connection as possible.

---

## Solution 1: SSRFGuard — URL Validation and IP Blocklist

```python
import ipaddress
import socket
import urllib.parse
from dataclasses import dataclass
from typing import FrozenSet, List, Optional, Set


# SSRF-dangerous IP ranges (RFC 1918, link-local, loopback, multicast)
_BLOCKED_NETWORKS = [
    ipaddress.ip_network(n) for n in [
        "127.0.0.0/8",       # loopback
        "10.0.0.0/8",        # private class A
        "172.16.0.0/12",     # private class B
        "192.168.0.0/16",    # private class C
        "169.254.0.0/16",    # link-local (AWS/GCP/Azure metadata)
        "100.64.0.0/10",     # shared address space (RFC 6598)
        "::1/128",           # IPv6 loopback
        "fc00::/7",          # IPv6 unique local
        "fe80::/10",         # IPv6 link-local
        "0.0.0.0/8",         # this network
        "240.0.0.0/4",       # reserved
        "255.255.255.255/32",# broadcast
    ]
]

_BLOCKED_HOSTS = frozenset([
    "metadata.google.internal",
    "metadata.goog",
])


@dataclass
class SSRFCheckResult:
    allowed: bool
    url: str
    reason: Optional[str] = None
    resolved_ip: Optional[str] = None


class SSRFGuard:
    """
    Validates URLs before the agent fetches them.
    Blocks private IPs, loopback, link-local, and known metadata endpoints.

    Usage:
        guard = SSRFGuard(allowed_schemes={"https"}, allowed_ports={443, 80})
        result = guard.check("https://169.254.169.254/latest/meta-data/")
        if not result.allowed:
            raise SSRFAttempt(result.reason)
        response = await http_client.get(result.url)
    """

    def __init__(self,
                 allowed_schemes: Optional[Set[str]] = None,
                 allowed_ports: Optional[Set[int]] = None,
                 domain_allowlist: Optional[FrozenSet[str]] = None,
                 resolve_dns: bool = True):
        self._schemes = allowed_schemes or {"https", "http"}
        self._ports = allowed_ports or {80, 443, 8080, 8443}
        self._allowlist = domain_allowlist
        self._resolve = resolve_dns

    def check(self, url: str) -> SSRFCheckResult:
        try:
            parsed = urllib.parse.urlparse(url)
        except Exception as exc:
            return SSRFCheckResult(False, url, f"URL parse error: {exc}")

        # Scheme check
        if parsed.scheme not in self._schemes:
            return SSRFCheckResult(
                False, url, f"Scheme '{parsed.scheme}' not allowed"
            )

        # Port check
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if port not in self._ports:
            return SSRFCheckResult(False, url, f"Port {port} not in allowlist")

        hostname = parsed.hostname or ""
        if not hostname:
            return SSRFCheckResult(False, url, "Missing hostname")

        # Blocked hostnames
        if hostname.lower() in _BLOCKED_HOSTS:
            return SSRFCheckResult(False, url, f"Blocked hostname: {hostname}")

        # Domain allowlist
        if self._allowlist is not None:
            if not any(hostname == d or hostname.endswith("." + d)
                       for d in self._allowlist):
                return SSRFCheckResult(
                    False, url, f"Host '{hostname}' not in domain allowlist"
                )

        # DNS resolution + IP check
        if self._resolve:
            try:
                infos = socket.getaddrinfo(hostname, None)
                for info in infos:
                    ip_str = info[4][0]
                    ip = ipaddress.ip_address(ip_str)
                    for blocked in _BLOCKED_NETWORKS:
                        if ip in blocked:
                            return SSRFCheckResult(
                                False, url,
                                f"Resolved IP {ip_str} is in blocked range {blocked}",
                                resolved_ip=ip_str,
                            )
            except socket.gaierror as exc:
                return SSRFCheckResult(False, url, f"DNS resolution failed: {exc}")

        return SSRFCheckResult(True, url)


class SSRFAttempt(SecurityError if False else ValueError):
    pass
```

---

## Solution 2: SafeHTTPClient — SSRF-Protected Async HTTP

Wraps `aiohttp` to validate every URL via SSRFGuard before opening a connection. Also handles DNS rebinding by validating the resolved IP at connection time.

```python
import asyncio
import ipaddress
import socket
from typing import Any, Dict, Optional


class SSRFProtectedConnector:
    """
    Async HTTP client wrapper that validates URLs against SSRF rules
    before each request. Validates at the TCP connection level to prevent
    DNS rebinding (where the DNS answer changes between the check and connect).

    Usage:
        client = SSRFProtectedConnector(guard=SSRFGuard())
        response = await client.get("https://api.example.com/data")
        data = await response.json()
    """

    def __init__(self, guard: SSRFGuard,
                 timeout_s: float = 10.0,
                 max_response_bytes: int = 10 * 1024 * 1024):
        self._guard = guard
        self._timeout = timeout_s
        self._max_bytes = max_response_bytes

    async def _validate_connection(self, host: str, port: int):
        """Second validation at connection time to catch DNS rebinding."""
        loop = asyncio.get_event_loop()
        infos = await loop.getaddrinfo(host, port)
        for info in infos:
            ip_str = info[4][0]
            ip = ipaddress.ip_address(ip_str)
            for blocked in _BLOCKED_NETWORKS:
                if ip in blocked:
                    raise SSRFAttempt(
                        f"DNS rebinding detected: {host} resolved to "
                        f"blocked IP {ip_str} at connection time"
                    )

    async def get(self, url: str,
                  headers: Optional[Dict[str, str]] = None) -> Any:
        result = self._guard.check(url)
        if not result.allowed:
            raise SSRFAttempt(f"SSRF blocked: {result.reason}")

        import aiohttp
        import urllib.parse
        parsed = urllib.parse.urlparse(url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        await self._validate_connection(parsed.hostname, port)

        timeout = aiohttp.ClientTimeout(total=self._timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers or {}) as resp:
                content = await resp.read()
                if len(content) > self._max_bytes:
                    raise ValueError(
                        f"Response size {len(content)} exceeds limit {self._max_bytes}"
                    )
                return {
                    "status": resp.status,
                    "headers": dict(resp.headers),
                    "body": content,
                }

    async def post(self, url: str, json_body: Any,
                   headers: Optional[Dict[str, str]] = None) -> Any:
        result = self._guard.check(url)
        if not result.allowed:
            raise SSRFAttempt(f"SSRF blocked: {result.reason}")
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=self._timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=json_body,
                                     headers=headers or {}) as resp:
                return {"status": resp.status, "body": await resp.read()}
```

---

## Solution 3: URLSchemeNormaliser — Canonicalise Before Checking

Attackers use URL encoding, Unicode homoglyphs, and alternate forms to bypass naive string checks. Normalise before validating.

```python
import re
import unicodedata
import urllib.parse
from typing import Optional


class URLSchemeNormaliser:
    """
    Canonicalises URLs before SSRF checking.
    Handles: percent-encoding tricks, Unicode homoglyphs, IPv6 bypass,
    octal/hex IP notation, and embedded credentials.

    Usage:
        norm = URLSchemeNormaliser()
        clean = norm.normalise("https://169%2E254%2E169%2E254/metadata")
        # clean == "https://169.254.169.254/metadata"
        guard.check(clean)
    """

    _HOMOGLYPHS = str.maketrans({
        "\u2024": ".",  # one dot leader
        "\uff0e": ".",  # fullwidth full stop
        "\u3002": ".",  # ideographic full stop
        "\uff0f": "/",  # fullwidth solidus
        "\u2215": "/",  # division slash
    })

    def normalise(self, url: str) -> str:
        # Strip leading/trailing whitespace including zero-width chars
        url = url.strip().strip("\u200b\u200c\u200d\ufeff")
        # Translate homoglyphs
        url = url.translate(self._HOMOGLYPHS)
        # NFKC normalise
        url = unicodedata.normalize("NFKC", url)
        # Percent-decode once
        url = urllib.parse.unquote(url)
        # Parse and re-serialise to normalise
        parsed = urllib.parse.urlparse(url)
        # Remove embedded credentials (user:pass@host)
        netloc = parsed.hostname or ""
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
        normalised = urllib.parse.urlunparse((
            parsed.scheme.lower(),
            netloc,
            parsed.path,
            parsed.params,
            parsed.query,
            "",  # strip fragment
        ))
        return normalised

    def extract_ips(self, hostname: str) -> list:
        """Detect hex/octal/decimal IP notation (0x7f000001, 0177.0.0.1)."""
        import ipaddress
        ips = []
        # Try decimal integer (http://2130706433 == 127.0.0.1)
        try:
            packed = int(hostname)
            ips.append(str(ipaddress.IPv4Address(packed)))
        except (ValueError, ipaddress.AddressValueError):
            pass
        return ips
```

---

## Solution 4: AgentWebFetchTool — SSRF-Safe fetch_page Tool

A complete `fetch_page` tool implementation with SSRF prevention, response size limits, and content-type validation.

```python
import asyncio
from dataclasses import dataclass
from typing import Optional


@dataclass
class FetchResult:
    url: str
    status_code: int
    content_type: str
    body_text: str
    body_bytes: int
    ssrf_checked: bool = True


class AgentWebFetchTool:
    """
    SSRF-protected web fetch tool for agents.
    Validates URLs, limits response sizes, and restricts content types.

    Usage:
        tool = AgentWebFetchTool(
            allowed_domains=frozenset(["wikipedia.org", "arxiv.org"]),
            max_response_kb=512,
        )
        result = await tool.fetch("https://en.wikipedia.org/wiki/SSRF")
        print(result.body_text[:2000])
    """

    SAFE_CONTENT_TYPES = {
        "text/html", "text/plain", "application/json",
        "application/xml", "text/xml",
    }

    def __init__(self,
                 allowed_domains: Optional[frozenset] = None,
                 max_response_kb: int = 1024,
                 timeout_s: float = 15.0):
        self._guard = SSRFGuard(
            allowed_schemes={"https"},
            allowed_ports={443},
            domain_allowlist=allowed_domains,
        )
        self._client = SSRFProtectedConnector(
            guard=self._guard,
            timeout_s=timeout_s,
            max_response_bytes=max_response_kb * 1024,
        )

    async def fetch(self, url: str) -> FetchResult:
        norm = URLSchemeNormaliser()
        url = norm.normalise(url)
        resp = await self._client.get(url)
        ct = resp["headers"].get("content-type", "").split(";")[0].strip().lower()
        if ct and ct not in self.SAFE_CONTENT_TYPES:
            raise ValueError(
                f"Unsafe content type '{ct}'. Only text/data responses allowed."
            )
        body = resp["body"].decode("utf-8", errors="replace")
        return FetchResult(
            url=url,
            status_code=resp["status"],
            content_type=ct,
            body_text=body,
            body_bytes=len(resp["body"]),
        )
```

---

## Solution 5: SSRFAuditLogger — Detection and Alerting

Log all SSRF attempts with attacker-supplied URL, resolved IP, and request context. Alert on repeated attempts.

```python
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class SSRFAttemptRecord:
    timestamp: float
    url: str
    resolved_ip: Optional[str]
    reason: str
    session_id: str
    user_id: Optional[str]


class SSRFAuditLogger:
    """
    Logs SSRF attempts and raises a high-severity alert when a session
    exceeds an attempt threshold (indicating active probing).

    Usage:
        audit = SSRFAuditLogger(alert_threshold=3, alert_window_s=60)
        guard = SSRFGuard()

        result = guard.check(user_supplied_url)
        if not result.allowed:
            audit.record(result, session_id="s1", user_id="u42")
            raise SSRFAttempt(result.reason)
    """

    def __init__(self, alert_threshold: int = 3,
                 alert_window_s: float = 60.0):
        self._threshold = alert_threshold
        self._window = alert_window_s
        self._records: List[SSRFAttemptRecord] = []
        self._counts: Dict[str, List[float]] = defaultdict(list)

    def record(self, result: SSRFCheckResult,
               session_id: str,
               user_id: Optional[str] = None):
        rec = SSRFAttemptRecord(
            timestamp=time.time(),
            url=result.url,
            resolved_ip=result.resolved_ip,
            reason=result.reason or "unknown",
            session_id=session_id,
            user_id=user_id,
        )
        self._records.append(rec)
        self._counts[session_id].append(rec.timestamp)
        logger.warning(
            "SSRF attempt blocked",
            extra={
                "url": result.url,
                "resolved_ip": result.resolved_ip,
                "reason": result.reason,
                "session_id": session_id,
                "user_id": user_id,
            },
        )
        self._check_alert(session_id)

    def _check_alert(self, session_id: str):
        now = time.time()
        recent = [t for t in self._counts[session_id]
                  if now - t <= self._window]
        self._counts[session_id] = recent
        if len(recent) >= self._threshold:
            logger.critical(
                "SSRF PROBE DETECTED: session %s made %d attempts in %ds",
                session_id, len(recent), self._window,
            )

    def summary(self) -> Dict:
        return {
            "total_attempts": len(self._records),
            "unique_sessions": len(self._counts),
            "top_urls": [r.url for r in self._records[-10:]],
        }
```

---

## Solution 6: SSRFPolicy — Composable Validation Rules

A policy engine that chains multiple SSRF checks as composable rules, making it easy to add or remove restrictions without touching the core guard.

```python
from abc import ABC, abstractmethod
from typing import List


class SSRFRule(ABC):
    @abstractmethod
    def check(self, url: str) -> SSRFCheckResult:
        ...


class SchemeRule(SSRFRule):
    def __init__(self, allowed: frozenset = frozenset({"https"})):
        self._allowed = allowed

    def check(self, url: str) -> SSRFCheckResult:
        import urllib.parse
        scheme = urllib.parse.urlparse(url).scheme.lower()
        if scheme not in self._allowed:
            return SSRFCheckResult(False, url, f"Scheme '{scheme}' blocked")
        return SSRFCheckResult(True, url)


class PrivateIPRule(SSRFRule):
    def check(self, url: str) -> SSRFCheckResult:
        guard = SSRFGuard(resolve_dns=True)
        return guard.check(url)


class DomainAllowlistRule(SSRFRule):
    def __init__(self, allowlist: frozenset):
        self._allowlist = allowlist

    def check(self, url: str) -> SSRFCheckResult:
        import urllib.parse
        host = urllib.parse.urlparse(url).hostname or ""
        if any(host == d or host.endswith("." + d) for d in self._allowlist):
            return SSRFCheckResult(True, url)
        return SSRFCheckResult(False, url, f"Host '{host}' not in allowlist")


class SSRFPolicy:
    """
    Chains SSRFRules; first failing rule blocks the request.

    Usage:
        policy = SSRFPolicy([
            SchemeRule(frozenset({"https"})),
            DomainAllowlistRule(frozenset({"api.openai.com", "arxiv.org"})),
            PrivateIPRule(),
        ])
        result = policy.evaluate("https://169.254.169.254/meta-data/")
        assert not result.allowed
    """

    def __init__(self, rules: List[SSRFRule]):
        self._rules = rules

    def evaluate(self, url: str) -> SSRFCheckResult:
        norm = URLSchemeNormaliser()
        url = norm.normalise(url)
        for rule in self._rules:
            result = rule.check(url)
            if not result.allowed:
                return result
        return SSRFCheckResult(True, url)
```

---

## Comparison

| Approach | Blocks Private IPs | DNS Rebinding | Homoglyph Bypass | Alerting | Composable |
|---|---|---|---|---|---|
| **SSRFGuard** | Yes | Yes (resolve) | No | No | No |
| **SSRFProtectedConnector** | Yes | Yes (at connect) | No | No | No |
| **URLSchemeNormaliser** | No (pre-step) | No | Yes | No | No |
| **AgentWebFetchTool** | Yes | Yes | Yes | No | No |
| **SSRFAuditLogger** | No (records) | No | No | Yes | No |
| **SSRFPolicy** | Yes | Yes | Yes (via norm) | No | Yes |

**Key insight**: validate at two points — before DNS resolution (scheme, port, hostname allowlist) and again after DNS resolution (IP range check). The second check is mandatory to defeat DNS rebinding. Always normalise URLs through `URLSchemeNormaliser` before any check to defeat encoding and homoglyph bypasses.
