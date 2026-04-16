---
title: "Agent Doesn't Implement DNS over HTTPS for External Tool Calls"
description: "Agents resolving external tool endpoints via plaintext DNS expose resolution queries to interception and cache poisoning attacks. Implement DNS over HTTPS (DoH) for all external endpoint resolution to prevent DNS spoofing, eavesdropping on which APIs the agent uses, and cache poisoning that could redirect tool calls to attacker-controlled hosts."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-dns-over-https-for-external-tool-calls
tags: [dns-over-https, doh, dns-security, cache-poisoning, tool-security, network-security]
symptoms:
  - "Agent tool call DNS queries visible in plaintext on the network"
  - "DNS cache poisoning redirected tool endpoint to attacker IP without TLS error (if cert also compromised)"
  - "ISP or network operator can enumerate which external APIs the agent accesses"
  - "No validation that resolved IP matches expected geographic region for the service"
  - "Agent trusts system resolver which is controlled by untrusted network infrastructure"
---

## Why This Happens

Standard DNS resolution uses UDP port 53 in plaintext. In cloud or enterprise environments, the system resolver may be intercepted, compromised, or configured to serve poisoned responses. DNS over HTTPS wraps resolution in TLS, preventing eavesdropping and making spoofing significantly harder. For agents making tool calls to external APIs (OpenAI, Stripe, GitHub, etc.), DoH ensures that the resolved IP addresses come from a trusted resolver, not a poisoned cache or malicious DHCP-provided resolver.

## Solution 1: DoH Resolver Client

```python
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

@dataclass
class DNSRecord:
    name: str
    record_type: str    # "A" | "AAAA" | "CNAME"
    value: str
    ttl: int
    resolved_at: float = field(default_factory=time.time)

    @property
    def expires_at(self) -> float:
        return self.resolved_at + self.ttl

    @property
    def is_expired(self) -> bool:
        return time.time() > self.expires_at

class DoHResolverClient:
    """
    Resolves DNS queries via DNS over HTTPS (RFC 8484).
    Supports Cloudflare (1.1.1.1) and Google (8.8.8.8) DoH endpoints.
    Falls back to secondary resolver on primary failure.
    """

    PRIMARY_DOH = "https://cloudflare-dns.com/dns-query"
    SECONDARY_DOH = "https://dns.google/dns-query"

    def __init__(self, http_client, timeout_seconds: float = 5.0):
        self._http = http_client
        self._timeout = timeout_seconds
        self._cache: Dict[str, List[DNSRecord]] = {}

    async def resolve(self, hostname: str, record_type: str = "A") -> List[DNSRecord]:
        cache_key = f"{hostname}:{record_type}"

        # Return cached records if still valid
        cached = self._cache.get(cache_key, [])
        valid = [r for r in cached if not r.is_expired]
        if valid:
            return valid

        # Try primary, fall back to secondary
        for doh_url in [self.PRIMARY_DOH, self.SECONDARY_DOH]:
            try:
                records = await self._query(doh_url, hostname, record_type)
                if records:
                    self._cache[cache_key] = records
                    return records
            except Exception as exc:
                print(f"[doh] {doh_url} failed for {hostname}: {exc}")
                continue

        raise RuntimeError(f"DoH resolution failed for {hostname}")

    async def _query(
        self, doh_url: str, hostname: str, record_type: str
    ) -> List[DNSRecord]:
        """RFC 8484 DNS wire format over HTTPS GET with dns= parameter."""
        import base64

        # Build minimal DNS query wire format
        wire = self._build_dns_query(hostname, record_type)
        dns_param = base64.urlsafe_b64encode(wire).rstrip(b"=").decode()

        url = f"{doh_url}?dns={dns_param}"
        headers = {"Accept": "application/dns-message"}

        response = await self._http.get(url, headers=headers, timeout=self._timeout)
        if response.status_code != 200:
            raise RuntimeError(f"DoH returned {response.status_code}")

        return self._parse_dns_response(response.content, hostname, record_type)

    def _build_dns_query(self, hostname: str, record_type: str) -> bytes:
        """Build a minimal DNS query packet (wire format)."""
        qtype = {"A": 1, "AAAA": 28, "CNAME": 5}.get(record_type, 1)
        # Transaction ID: 2 bytes, Flags: standard query
        header = b"\x00\x01\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
        # Encode hostname as DNS labels
        labels = b""
        for part in hostname.split("."):
            encoded = part.encode("ascii")
            labels += bytes([len(encoded)]) + encoded
        labels += b"\x00"
        question = labels + qtype.to_bytes(2, "big") + b"\x00\x01"
        return header + question

    def _parse_dns_response(
        self, data: bytes, hostname: str, record_type: str
    ) -> List[DNSRecord]:
        """Minimal DNS response parser — extracts A/AAAA records."""
        records = []
        # Use struct to parse header
        import socket
        try:
            # Skip header (12 bytes) + question section
            pos = 12
            # Skip question labels
            while pos < len(data) and data[pos] != 0:
                length = data[pos]
                pos += length + 1
            pos += 5  # null byte + qtype + qclass

            # Parse answer records
            answer_count = int.from_bytes(data[6:8], "big")
            for _ in range(answer_count):
                # Skip name (may be pointer)
                if pos < len(data) and data[pos] & 0xC0 == 0xC0:
                    pos += 2
                else:
                    while pos < len(data) and data[pos] != 0:
                        pos += data[pos] + 1
                    pos += 1
                if pos + 10 > len(data):
                    break
                rtype = int.from_bytes(data[pos:pos+2], "big")
                ttl = int.from_bytes(data[pos+4:pos+8], "big")
                rdlength = int.from_bytes(data[pos+8:pos+10], "big")
                rdata = data[pos+10:pos+10+rdlength]
                pos += 10 + rdlength

                if rtype == 1 and len(rdata) == 4:   # A record
                    ip = socket.inet_ntoa(rdata)
                    records.append(DNSRecord(
                        name=hostname, record_type="A", value=ip, ttl=ttl
                    ))
                elif rtype == 28 and len(rdata) == 16:  # AAAA record
                    ip = socket.inet_ntop(socket.AF_INET6, rdata)
                    records.append(DNSRecord(
                        name=hostname, record_type="AAAA", value=ip, ttl=ttl
                    ))
        except Exception:
            pass
        return records
```

## Solution 2: DNS Resolution Cache with Negative Caching

```python
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

@dataclass
class NegativeCacheEntry:
    hostname: str
    reason: str
    cached_at: float = field(default_factory=time.time)
    ttl: int = 300   # cache NXDOMAIN for 5 minutes

    @property
    def is_expired(self) -> bool:
        return time.time() > self.cached_at + self.ttl

class DoHResolutionCache:
    """
    Combined positive and negative DNS cache.
    Negative caching prevents hammering the DoH resolver for known-bad hostnames.
    """

    def __init__(self, max_entries: int = 1000):
        self._positive: Dict[str, List[DNSRecord]] = {}
        self._negative: Dict[str, NegativeCacheEntry] = {}
        self._max = max_entries
        self._hits = 0
        self._misses = 0

    def get(self, hostname: str, record_type: str = "A") -> Optional[List[DNSRecord]]:
        key = f"{hostname}:{record_type}"

        # Check negative cache
        neg = self._negative.get(hostname)
        if neg and not neg.is_expired:
            self._hits += 1
            return []   # cached NXDOMAIN

        # Check positive cache
        records = self._positive.get(key, [])
        valid = [r for r in records if not r.is_expired]
        if valid:
            self._hits += 1
            return valid

        self._misses += 1
        return None

    def put(self, hostname: str, record_type: str, records: List[DNSRecord]) -> None:
        if not records:
            self._negative[hostname] = NegativeCacheEntry(
                hostname=hostname, reason="nxdomain"
            )
            return
        key = f"{hostname}:{record_type}"
        if len(self._positive) >= self._max:
            oldest = next(iter(self._positive))
            del self._positive[oldest]
        self._positive[key] = records

    def put_failure(self, hostname: str, reason: str) -> None:
        self._negative[hostname] = NegativeCacheEntry(hostname=hostname, reason=reason)

    @property
    def hit_rate(self) -> float:
        total = self._hits + self._misses
        return self._hits / total if total else 0.0
```

## Solution 3: Resolved IP Validator

```python
import ipaddress
from dataclasses import dataclass
from typing import List, Optional, Set

@dataclass
class IPValidationResult:
    allowed: bool
    ip: str
    reason: str

class ResolvedIPValidator:
    """
    Validates resolved IP addresses against allowlists and blocklists.
    Prevents DoH-resolved IPs from pointing to private/reserved ranges
    (SSRF prevention) or known malicious ranges.
    """

    # RFC 1918 and other reserved ranges
    PRIVATE_RANGES = [
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
        ipaddress.ip_network("127.0.0.0/8"),
        ipaddress.ip_network("169.254.0.0/16"),   # link-local
        ipaddress.ip_network("100.64.0.0/10"),    # shared address space
        ipaddress.ip_network("::1/128"),           # IPv6 loopback
        ipaddress.ip_network("fc00::/7"),          # IPv6 ULA
    ]

    def __init__(self, allowed_cidrs: Optional[List[str]] = None):
        self._allowed_cidrs = [
            ipaddress.ip_network(c) for c in (allowed_cidrs or [])
        ]
        self._blocked_ips: Set[str] = set()

    def validate(self, ip: str) -> IPValidationResult:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return IPValidationResult(allowed=False, ip=ip, reason="invalid_ip")

        # Block known-bad IPs
        if ip in self._blocked_ips:
            return IPValidationResult(allowed=False, ip=ip, reason="blocked_ip")

        # Block private/reserved ranges (SSRF prevention)
        for network in self.PRIVATE_RANGES:
            if addr in network:
                return IPValidationResult(
                    allowed=False, ip=ip,
                    reason=f"private_range:{network}"
                )

        # If allowlist is configured, enforce it
        if self._allowed_cidrs:
            for network in self._allowed_cidrs:
                if addr in network:
                    return IPValidationResult(allowed=True, ip=ip, reason="in_allowlist")
            return IPValidationResult(allowed=False, ip=ip, reason="not_in_allowlist")

        return IPValidationResult(allowed=True, ip=ip, reason="ok")

    def block_ip(self, ip: str) -> None:
        self._blocked_ips.add(ip)
```

## Solution 4: DoH-Aware HTTP Client

```python
import asyncio
from typing import Optional

class DoHAwareHTTPClient:
    """
    HTTP client wrapper that resolves hostnames via DoH before connecting.
    Passes the resolved IP directly to the underlying HTTP client,
    bypassing the system resolver entirely.
    """

    def __init__(
        self,
        doh_resolver: DoHResolverClient,
        cache: DoHResolutionCache,
        validator: ResolvedIPValidator,
        base_http_client,
    ):
        self._resolver = doh_resolver
        self._cache = cache
        self._validator = validator
        self._base = base_http_client

    async def _resolve_host(self, hostname: str) -> str:
        cached = self._cache.get(hostname)
        if cached is not None:
            if not cached:
                raise RuntimeError(f"Cached NXDOMAIN for {hostname}")
            for record in cached:
                result = self._validator.validate(record.value)
                if result.allowed:
                    return record.value
            raise RuntimeError(f"No valid IPs for {hostname} (all blocked)")

        records = await self._resolver.resolve(hostname)
        self._cache.put(hostname, "A", records)

        for record in records:
            result = self._validator.validate(record.value)
            if result.allowed:
                return record.value

        raise RuntimeError(f"No valid IPs for {hostname}")

    async def get(self, url: str, **kwargs):
        from urllib.parse import urlparse
        parsed = urlparse(url)
        hostname = parsed.hostname
        ip = await self._resolve_host(hostname)
        # Replace hostname with resolved IP, preserve Host header
        resolved_url = url.replace(hostname, ip, 1)
        headers = kwargs.pop("headers", {})
        headers["Host"] = hostname
        return await self._base.get(resolved_url, headers=headers, **kwargs)

    async def post(self, url: str, **kwargs):
        from urllib.parse import urlparse
        parsed = urlparse(url)
        hostname = parsed.hostname
        ip = await self._resolve_host(hostname)
        resolved_url = url.replace(hostname, ip, 1)
        headers = kwargs.pop("headers", {})
        headers["Host"] = hostname
        return await self._base.post(resolved_url, headers=headers, **kwargs)
```

## Solution 5: DoH Health Monitor

```python
import asyncio
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

@dataclass
class DoHHealthSample:
    resolver_url: str
    latency_ms: float
    success: bool
    timestamp: float

class DoHHealthMonitor:
    """
    Periodically probes DoH resolvers and selects the fastest healthy one.
    Fails over automatically when primary resolver degrades.
    """

    PROBE_HOSTNAME = "example.com"

    def __init__(self, resolver: DoHResolverClient, probe_interval: float = 30.0):
        self._resolver = resolver
        self._interval = probe_interval
        self._samples: Dict[str, List[DoHHealthSample]] = {
            DoHResolverClient.PRIMARY_DOH: [],
            DoHResolverClient.SECONDARY_DOH: [],
        }
        self._preferred_resolver: Optional[str] = None

    async def start(self) -> None:
        while True:
            await self._probe_all()
            await asyncio.sleep(self._interval)

    async def _probe(self, doh_url: str) -> DoHHealthSample:
        t0 = time.monotonic()
        try:
            records = await self._resolver._query(doh_url, self.PROBE_HOSTNAME, "A")
            success = len(records) > 0
        except Exception:
            success = False
        latency_ms = (time.monotonic() - t0) * 1000
        return DoHHealthSample(
            resolver_url=doh_url,
            latency_ms=round(latency_ms, 1),
            success=success,
            timestamp=time.time(),
        )

    async def _probe_all(self) -> None:
        results = await asyncio.gather(
            self._probe(DoHResolverClient.PRIMARY_DOH),
            self._probe(DoHResolverClient.SECONDARY_DOH),
            return_exceptions=True,
        )
        for sample in results:
            if isinstance(sample, DoHHealthSample):
                self._samples[sample.resolver_url].append(sample)
                if len(self._samples[sample.resolver_url]) > 20:
                    self._samples[sample.resolver_url].pop(0)

        self._update_preferred()

    def _update_preferred(self) -> None:
        best = None
        best_latency = float("inf")
        for url, samples in self._samples.items():
            recent = samples[-5:]
            if not recent:
                continue
            success_rate = sum(1 for s in recent if s.success) / len(recent)
            if success_rate < 0.6:
                continue
            avg_latency = sum(s.latency_ms for s in recent) / len(recent)
            if avg_latency < best_latency:
                best_latency = avg_latency
                best = url
        self._preferred_resolver = best

    def preferred(self) -> Optional[str]:
        return self._preferred_resolver or DoHResolverClient.PRIMARY_DOH

    def health_report(self) -> dict:
        report = {}
        for url, samples in self._samples.items():
            if not samples:
                continue
            recent = samples[-10:]
            report[url] = {
                "success_rate": round(sum(1 for s in recent if s.success) / len(recent), 2),
                "avg_latency_ms": round(sum(s.latency_ms for s in recent) / len(recent), 1),
                "is_preferred": url == self._preferred_resolver,
            }
        return report
```

## Solution 6: DoH Audit Logger

```python
import json
import time
from dataclasses import dataclass, field
from typing import List

@dataclass
class DoHResolutionEvent:
    hostname: str
    resolved_ips: List[str]
    resolver_used: str
    cache_hit: bool
    validation_passed: bool
    latency_ms: float
    timestamp: float = field(default_factory=time.time)

class DoHAuditLogger:
    def __init__(self):
        self._events: List[DoHResolutionEvent] = []

    def log(self, event: DoHResolutionEvent) -> None:
        self._events.append(event)
        record = {
            "hostname": event.hostname,
            "ips": event.resolved_ips,
            "resolver": event.resolver_used,
            "cache_hit": event.cache_hit,
            "valid": event.validation_passed,
            "ms": event.latency_ms,
            "ts": event.timestamp,
        }
        print(f"[doh_audit] {json.dumps(record)}")

    def blocked_summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [e for e in self._events if e.timestamp >= cutoff]
        blocked = [e for e in recent if not e.validation_passed]
        return {
            "total_resolutions": len(recent),
            "blocked": len(blocked),
            "cache_hit_rate": round(
                sum(1 for e in recent if e.cache_hit) / max(len(recent), 1), 3
            ),
            "blocked_hostnames": list({e.hostname for e in blocked}),
        }
```

## Comparison

| Approach | Prevents Eavesdropping | Prevents Spoofing | SSRF Prevention | Failover |
|---|---|---|---|---|
| DoHResolverClient | Yes (TLS) | Yes (trusted resolver) | No | Yes (secondary) |
| DoHResolutionCache | N/A | Via cached records | No | No |
| ResolvedIPValidator | No | No | Yes (private ranges) | No |
| DoHAwareHTTPClient | Yes | Yes | Yes | Via resolver |
| DoHHealthMonitor | N/A | N/A | No | Yes (adaptive) |
| DoHAuditLogger | N/A | N/A | N/A | No |

**Best for production**: Wrap all outbound tool HTTP calls through `DoHAwareHTTPClient`. Use `ResolvedIPValidator` to block resolution to private ranges (SSRF defense in depth). Monitor both Cloudflare and Google DoH with `DoHHealthMonitor` and auto-failover on latency degradation. Log all resolution events via `DoHAuditLogger` to detect attempts to resolve internal hostnames or hostnames that fail IP validation — these are strong signals of prompt injection or SSRF attempts.
