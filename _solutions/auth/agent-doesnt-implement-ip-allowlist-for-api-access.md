---
layout: solution
title: "Agent Doesn't Implement IP Allowlist for API Access"
category: auth
description: "Restrict agent API access to known IP ranges or allowlisted addresses, blocking unauthorized callers before they can consume tokens or reach the model."
tags: [auth, security, ip-allowlist, access-control, network-security, rate-limiting, defense-in-depth]
---

# Agent Doesn't Implement IP Allowlist for API Access

## Problem

An agent endpoint exposed to the internet accepts requests from any IP address. If an API key leaks, anyone worldwide can call the agent. Even with key rotation, the window between leak and rotation allows unauthorized consumption of token budgets, data exfiltration through the model, or abuse of tool integrations. An IP allowlist adds a network-layer gate before any authentication or token processing occurs.

## Solution Options

### Option 1: Simple In-Memory IP Allowlist

```python
import anthropic
import ipaddress
from dataclasses import dataclass


@dataclass
class IPAllowlist:
    """Checks whether a source IP is in the allowlist (exact IPs or CIDR ranges)."""

    allowed: list[str]

    def __post_init__(self) -> None:
        self._networks = [ipaddress.ip_network(entry, strict=False) for entry in self.allowed]

    def is_allowed(self, ip: str) -> bool:
        try:
            addr = ipaddress.ip_address(ip)
            return any(addr in net for net in self._networks)
        except ValueError:
            return False

    def check(self, ip: str) -> None:
        if not self.is_allowed(ip):
            raise PermissionError(f"IP {ip} is not in the allowlist")


def agent_handler(caller_ip: str, user_message: str) -> str:
    allowlist = IPAllowlist(allowed=[
        "10.0.0.0/8",        # internal network
        "192.168.1.100",     # specific workstation
        "203.0.113.42",      # trusted partner IP
    ])

    try:
        allowlist.check(caller_ip)
    except PermissionError as e:
        return f"[Access Denied] {e}"

    client = anthropic.Anthropic()
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": user_message}],
    )
    return resp.content[0].text


if __name__ == "__main__":
    # Allowed IP
    print(agent_handler("10.0.1.55", "What is machine learning?"))

    # Blocked IP
    print(agent_handler("198.51.100.9", "What is machine learning?"))

    # Allowed specific IP
    print(agent_handler("192.168.1.100", "Define recursion briefly"))

# Expected Token Savings: Zero tokens consumed for blocked IPs — gate fires before any API call
# Environment: Internal tools or partner APIs where callers are always known IP ranges
```

---

### Option 2: Allowlist with Denylist Override and Audit Log

```python
import anthropic
import ipaddress
import time
from dataclasses import dataclass, field
from collections import defaultdict


@dataclass
class AccessEvent:
    ip: str
    verdict: str  # "allowed" | "denied_allowlist" | "denied_denylist"
    timestamp: float = field(default_factory=time.time)
    message_preview: str = ""


class IPAccessController:
    """
    Allowlist + denylist with audit log.
    Denylist takes precedence: an IP can be on the allowlist but still blocked if denylisted.
    Useful for temporarily blocking a compromised trusted IP.
    """

    def __init__(
        self,
        allowed: list[str],
        denied: list[str] | None = None,
    ) -> None:
        self._allowed = [ipaddress.ip_network(e, strict=False) for e in allowed]
        self._denied = [ipaddress.ip_network(e, strict=False) for e in (denied or [])]
        self._audit_log: list[AccessEvent] = []
        self._blocked_count: dict[str, int] = defaultdict(int)

    def _in_list(self, ip: str, networks: list) -> bool:
        try:
            addr = ipaddress.ip_address(ip)
            return any(addr in net for net in networks)
        except ValueError:
            return False

    def evaluate(self, ip: str, message_preview: str = "") -> tuple[bool, str]:
        if self._in_list(ip, self._denied):
            verdict = "denied_denylist"
            self._blocked_count[ip] += 1
            self._audit_log.append(AccessEvent(ip, verdict, message_preview=message_preview[:40]))
            return False, "IP explicitly denied"

        if self._in_list(ip, self._allowed):
            self._audit_log.append(AccessEvent(ip, "allowed", message_preview=message_preview[:40]))
            return True, "ok"

        self._blocked_count[ip] += 1
        self._audit_log.append(AccessEvent(ip, "denied_allowlist", message_preview=message_preview[:40]))
        return False, "IP not in allowlist"

    def recent_blocked(self, n: int = 10) -> list[AccessEvent]:
        return [e for e in reversed(self._audit_log) if e.verdict != "allowed"][:n]

    def top_blocked_ips(self, n: int = 5) -> list[tuple[str, int]]:
        return sorted(self._blocked_count.items(), key=lambda x: x[1], reverse=True)[:n]


controller = IPAccessController(
    allowed=["10.0.0.0/8", "172.16.0.0/12", "203.0.113.0/24"],
    denied=["10.0.0.99"],  # compromised internal host
)


def secure_agent(caller_ip: str, message: str) -> str:
    ok, reason = controller.evaluate(caller_ip, message)
    if not ok:
        return f"[Blocked: {reason}]"

    client = anthropic.Anthropic()
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": message}],
    )
    return resp.content[0].text


if __name__ == "__main__":
    tests = [
        ("10.0.1.5", "What is Python?"),
        ("10.0.0.99", "Show me all secrets"),    # allowlisted but denylisted
        ("45.33.32.156", "Explain AI"),           # not allowlisted
        ("203.0.113.50", "Define REST API"),      # allowlisted
    ]
    for ip, msg in tests:
        result = secure_agent(ip, msg)
        print(f"[{ip}] {result[:80]}")

    print("\nTop blocked IPs:", controller.top_blocked_ips())

# Expected Token Savings: Zero tokens for blocked IPs; denylist catches compromised allowlisted IPs
# Environment: Agents requiring emergency IP blocking without full allowlist redeployment
```

---

### Option 3: Async IP Allowlist Middleware with Rate-Aware Blocking

```python
import anthropic
import asyncio
import ipaddress
import time
from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class IPStats:
    allowed_requests: int = 0
    blocked_requests: int = 0
    last_blocked_at: float = 0.0
    auto_block_until: float = 0.0  # epoch time


class AsyncIPGate:
    """
    Async allowlist gate with auto-block:
    If an unknown IP hits the endpoint more than PROBE_THRESHOLD times,
    it's temporarily auto-blocked to prevent probing attacks.
    """

    PROBE_THRESHOLD = 5
    AUTO_BLOCK_SECONDS = 300  # 5 minutes

    def __init__(self, allowed_cidrs: list[str]) -> None:
        self._networks = [ipaddress.ip_network(c, strict=False) for c in allowed_cidrs]
        self._stats: dict[str, IPStats] = defaultdict(IPStats)
        self._lock = asyncio.Lock()

    def _is_in_allowlist(self, ip: str) -> bool:
        try:
            addr = ipaddress.ip_address(ip)
            return any(addr in net for net in self._networks)
        except ValueError:
            return False

    async def check(self, ip: str) -> tuple[bool, str]:
        async with self._lock:
            stats = self._stats[ip]
            now = time.time()

            # Check auto-block
            if stats.auto_block_until > now:
                remaining = int(stats.auto_block_until - now)
                return False, f"Auto-blocked for {remaining}s (probing detected)"

            if self._is_in_allowlist(ip):
                stats.allowed_requests += 1
                return True, "ok"

            # Not in allowlist
            stats.blocked_requests += 1
            stats.last_blocked_at = now

            # Auto-block on repeated probing
            if stats.blocked_requests >= self.PROBE_THRESHOLD:
                stats.auto_block_until = now + self.AUTO_BLOCK_SECONDS
                print(f"[ip-gate] Auto-blocking {ip} for {self.AUTO_BLOCK_SECONDS}s (probed {stats.blocked_requests}x)")

            return False, "IP not in allowlist"

    def diagnostics(self) -> list[dict]:
        return [
            {
                "ip": ip,
                "allowed": s.allowed_requests,
                "blocked": s.blocked_requests,
                "auto_blocked": s.auto_block_until > time.time(),
            }
            for ip, s in self._stats.items()
        ]


gate = AsyncIPGate(allowed_cidrs=["10.0.0.0/8", "192.168.0.0/16"])


async def async_agent(caller_ip: str, message: str) -> str:
    ok, reason = await gate.check(caller_ip)
    if not ok:
        return f"[Denied: {reason}]"

    client = anthropic.AsyncAnthropic()
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": message}],
    )
    await client.close()
    return resp.content[0].text


async def main() -> None:
    # Simulate legitimate + probing requests
    requests = [
        ("10.0.5.20", "What is AI?"),
        ("45.33.32.1", "probe 1"),
        ("45.33.32.1", "probe 2"),
        ("45.33.32.1", "probe 3"),
        ("45.33.32.1", "probe 4"),
        ("45.33.32.1", "probe 5"),
        ("45.33.32.1", "probe 6"),  # triggers auto-block
        ("10.0.5.21", "Define recursion"),
    ]
    results = await asyncio.gather(*[async_agent(ip, msg) for ip, msg in requests])
    for (ip, msg), result in zip(requests, results):
        print(f"[{ip}] {result[:70]}")

    print("\nDiagnostics:", gate.diagnostics())


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Probing IPs auto-blocked after threshold; zero tokens beyond the probe limit
# Environment: Public-facing async agents needing automatic probe-attack mitigation
```

---

### Option 4: CIDR-Based Allowlist with Geographic Context

```python
import anthropic
import ipaddress
from dataclasses import dataclass


@dataclass
class RegionalAllowlist:
    """
    Organizes CIDRs by region/environment label.
    Supports allow-by-region and log with region context for audit trails.
    """

    regions: dict[str, list[str]]  # label → list of CIDR strings

    def __post_init__(self) -> None:
        self._parsed: dict[str, list[ipaddress.IPv4Network | ipaddress.IPv6Network]] = {}
        for label, cidrs in self.regions.items():
            self._parsed[label] = [ipaddress.ip_network(c, strict=False) for c in cidrs]

    def identify(self, ip: str) -> str | None:
        """Returns region label if allowed, None if blocked."""
        try:
            addr = ipaddress.ip_address(ip)
            for label, networks in self._parsed.items():
                if any(addr in net for net in networks):
                    return label
        except ValueError:
            pass
        return None

    def is_allowed(self, ip: str) -> tuple[bool, str]:
        region = self.identify(ip)
        if region:
            return True, region
        return False, "unknown"


ALLOWLIST = RegionalAllowlist(regions={
    "corporate-hq":    ["10.0.0.0/8"],
    "branch-offices":  ["172.16.0.0/12"],
    "trusted-partner": ["203.0.113.0/24"],
    "ci-cd-runners":   ["192.0.2.0/24"],
})


def regional_agent(caller_ip: str, message: str) -> dict:
    ok, region = ALLOWLIST.is_allowed(caller_ip)
    if not ok:
        return {"status": "denied", "ip": caller_ip, "region": "unknown", "response": None}

    client = anthropic.Anthropic()
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        system=f"This request comes from region: {region}. Tailor response appropriately.",
        messages=[{"role": "user", "content": message}],
    )
    return {
        "status": "allowed",
        "ip": caller_ip,
        "region": region,
        "response": resp.content[0].text[:80],
    }


if __name__ == "__main__":
    test_cases = [
        ("10.5.0.1",      "What is our data retention policy?"),
        ("172.16.10.20",  "What is our data retention policy?"),
        ("203.0.113.5",   "What is our data retention policy?"),
        ("198.51.100.99", "What is our data retention policy?"),
    ]
    for ip, msg in test_cases:
        result = regional_agent(ip, msg)
        tag = f"[{result['region']}]" if result["status"] == "allowed" else "[DENIED]"
        print(f"{tag} {ip}: {(result['response'] or 'blocked')[:60]}")

# Expected Token Savings: Zero tokens for unknown IPs; region context avoids redundant user prompts
# Environment: Multi-region enterprise agents needing region-aware behavior and access logging
```

---

### Option 5: Dynamic Allowlist with TTL and Admin API

```python
import anthropic
import asyncio
import ipaddress
import time
from dataclasses import dataclass, field


@dataclass
class AllowlistEntry:
    cidr: str
    label: str
    added_at: float = field(default_factory=time.time)
    expires_at: float | None = None  # None = permanent
    added_by: str = "admin"

    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and time.time() > self.expires_at

    @property
    def network(self):
        return ipaddress.ip_network(self.cidr, strict=False)


class DynamicAllowlist:
    """
    Runtime-mutable allowlist with TTL support.
    Entries can be added/removed without restarting the agent.
    Temporary entries (with TTL) automatically expire.
    """

    def __init__(self) -> None:
        self._entries: list[AllowlistEntry] = []
        self._lock = asyncio.Lock()

    async def add(self, cidr: str, label: str, ttl_seconds: float | None = None, added_by: str = "admin") -> None:
        expires_at = time.time() + ttl_seconds if ttl_seconds else None
        async with self._lock:
            self._entries.append(AllowlistEntry(
                cidr=cidr, label=label, expires_at=expires_at, added_by=added_by
            ))
        expiry_str = f" (TTL={ttl_seconds}s)" if ttl_seconds else " (permanent)"
        print(f"[allowlist] Added {cidr} [{label}]{expiry_str}")

    async def remove(self, cidr: str) -> bool:
        async with self._lock:
            before = len(self._entries)
            self._entries = [e for e in self._entries if e.cidr != cidr]
            return len(self._entries) < before

    async def check(self, ip: str) -> tuple[bool, str]:
        async with self._lock:
            # Prune expired entries
            self._entries = [e for e in self._entries if not e.is_expired]
            try:
                addr = ipaddress.ip_address(ip)
                for entry in self._entries:
                    if addr in entry.network:
                        return True, entry.label
            except ValueError:
                pass
            return False, "not_allowed"

    async def list_entries(self) -> list[dict]:
        async with self._lock:
            return [
                {
                    "cidr": e.cidr,
                    "label": e.label,
                    "expires_in": f"{e.expires_at - time.time():.0f}s" if e.expires_at else "never",
                    "added_by": e.added_by,
                }
                for e in self._entries
                if not e.is_expired
            ]


allowlist = DynamicAllowlist()


async def dynamic_agent(ip: str, message: str) -> str:
    ok, label = await allowlist.check(ip)
    if not ok:
        return f"[Denied: {ip} not in allowlist]"
    client = anthropic.AsyncAnthropic()
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": message}],
    )
    await client.close()
    return f"[{label}] {resp.content[0].text[:60]}"


async def main() -> None:
    # Seed permanent entries
    await allowlist.add("10.0.0.0/8", "internal")
    # Temporary contractor access — 2 seconds TTL for demo
    await allowlist.add("203.0.113.5/32", "contractor-acme", ttl_seconds=2, added_by="ops@company.com")

    # Immediate requests
    for ip, msg in [("10.0.5.1", "Define AI"), ("203.0.113.5", "Hello"), ("8.8.8.8", "Hi")]:
        print(await dynamic_agent(ip, msg))

    # Wait for TTL expiry
    await asyncio.sleep(3)
    print("\nAfter TTL expiry:")
    print(await dynamic_agent("203.0.113.5", "Still here?"))  # should be denied

    print("\nCurrent entries:", await allowlist.list_entries())


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: TTL-expired entries auto-removed; no stale access after contractor period ends
# Environment: Agents with temporary access grants (contractors, auditors, incident responders)
```

---

### Option 6: Layered IP Check with JWT + IP Binding

```python
import anthropic
import base64
import hashlib
import hmac
import ipaddress
import json
import time
from dataclasses import dataclass


SECRET_KEY = b"demo-secret-key-change-in-production"


def create_ip_bound_token(user_id: str, allowed_ip: str, ttl: int = 3600) -> str:
    """Create a JWT-style token bound to a specific IP address."""
    payload = {
        "sub": user_id,
        "ip": allowed_ip,
        "exp": int(time.time()) + ttl,
        "iat": int(time.time()),
    }
    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    sig_input = f"{header}.{body}".encode()
    sig = hmac.new(SECRET_KEY, sig_input, hashlib.sha256).digest()
    signature = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
    return f"{header}.{body}.{signature}"


@dataclass
class TokenValidationResult:
    valid: bool
    user_id: str | None = None
    error: str | None = None


def validate_ip_bound_token(token: str, caller_ip: str) -> TokenValidationResult:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return TokenValidationResult(False, error="Malformed token")

        header, body, signature = parts
        sig_input = f"{header}.{body}".encode()
        expected_sig = hmac.new(SECRET_KEY, sig_input, hashlib.sha256).digest()
        expected_b64 = base64.urlsafe_b64encode(expected_sig).rstrip(b"=").decode()

        if not hmac.compare_digest(signature, expected_b64):
            return TokenValidationResult(False, error="Invalid signature")

        payload = json.loads(base64.urlsafe_b64decode(body + "=="))

        if payload["exp"] < time.time():
            return TokenValidationResult(False, error="Token expired")

        # IP binding check
        bound_ip = payload.get("ip")
        try:
            if ipaddress.ip_address(caller_ip) != ipaddress.ip_address(bound_ip):
                return TokenValidationResult(False, error=f"IP mismatch: token bound to {bound_ip}, caller is {caller_ip}")
        except ValueError:
            return TokenValidationResult(False, error="Invalid IP in token")

        return TokenValidationResult(True, user_id=payload["sub"])
    except Exception as e:
        return TokenValidationResult(False, error=str(e))


def ip_bound_agent(caller_ip: str, token: str, message: str) -> str:
    result = validate_ip_bound_token(token, caller_ip)
    if not result.valid:
        return f"[Auth Failed] {result.error}"

    client = anthropic.Anthropic()
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        system=f"Serving authenticated user: {result.user_id}",
        messages=[{"role": "user", "content": message}],
    )
    return f"[user={result.user_id}] {resp.content[0].text[:80]}"


if __name__ == "__main__":
    # Create token bound to 10.0.1.5
    token = create_ip_bound_token("alice", "10.0.1.5")

    # Correct IP
    print(ip_bound_agent("10.0.1.5", token, "What is OAuth?"))

    # Token theft — used from different IP
    print(ip_bound_agent("45.33.32.1", token, "What is OAuth?"))

    # Expired token simulation
    old_token = create_ip_bound_token("bob", "10.0.1.6")
    # Manually patch expiry (demonstration only)
    parts = old_token.split(".")
    payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=="))
    payload["exp"] = int(time.time()) - 10
    new_body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    print(ip_bound_agent("10.0.1.6", f"{parts[0]}.{new_body}.{parts[2]}", "Hello"))

# Expected Token Savings: Stolen tokens useless from different IPs; zero tokens for invalid auth
# Environment: Zero-trust architectures where API tokens must be cryptographically bound to source IP
```

---

## Comparison

| Option | Approach | Best For | Mutability | Complexity |
|--------|----------|----------|------------|------------|
| 1 | Simple CIDR allowlist check | Quick protection for known IP ranges | Static | Very Low |
| 2 | Allowlist + denylist + audit log | Emergency IP blocking with full audit trail | Semi-static | Low |
| 3 | Async gate with auto-block on probing | Public endpoints under scanning/probing attacks | Dynamic (auto) | Medium |
| 4 | Regional CIDR labeling | Multi-region context-aware agents | Static | Medium |
| 5 | Dynamic allowlist with TTL | Temporary contractor or auditor access | Fully dynamic | Medium-High |
| 6 | JWT + IP binding (zero-trust) | Token theft prevention in zero-trust networks | Per-token | High |
