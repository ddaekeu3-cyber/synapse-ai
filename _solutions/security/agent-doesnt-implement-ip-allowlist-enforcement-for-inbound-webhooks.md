---
title: "Agent Doesn't Implement IP Allowlist Enforcement for Inbound Webhooks"
description: "AI agent webhook endpoints that accept events from any IP address are vulnerable to spoofed events—an attacker can POST a fake tool completion, payment confirmation, or callback to trigger unauthorized agent actions. IP allowlist enforcement validates the source IP of every inbound webhook request against a configurable set of known-good CIDR ranges before any payload processing occurs."
date: 2025-02-23
difficulty: intermediate
category: security
slug: agent-doesnt-implement-ip-allowlist-enforcement-for-inbound-webhooks
tags:
  - ip-allowlist
  - webhook-security
  - cidr
  - inbound-validation
  - security
  - network-controls
  - source-validation
symptoms:
  - "Webhook endpoint accepts POST requests from any IP on the internet"
  - "An attacker could POST a fake Stripe payment event to trigger premium feature unlock"
  - "Tool completion callbacks not validated for source IP — spoofable by anyone"
  - "Agent processes GitHub webhook events regardless of source IP"
  - "No logging of rejected webhook sources for security audit"
---

## Problem

Webhook endpoints receive events from external services—payment processors, CI/CD systems, messaging platforms, tool execution callbacks—and trigger agent actions based on payload contents. Without IP allowlist enforcement, any actor can POST a crafted payload to the endpoint. Even with HMAC signature verification, defense in depth requires rejecting requests from unexpected IP ranges before signature checking: this eliminates a class of attacks where the signing secret is stolen and used from an arbitrary IP. Major services publish their webhook IP ranges (Stripe, GitHub, Twilio, Slack), enabling strict source validation.

---

## Solution 1: IPAllowlist — CIDR-Based Source Validation

```python
import ipaddress
import logging
from dataclasses import dataclass, field
from typing import FrozenSet, Iterable, List, Optional, Set, Union

logger = logging.getLogger(__name__)

IPNetwork = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]


@dataclass
class AllowlistEntry:
    network: IPNetwork
    label: str = ""
    source: str = ""


class IPAllowlist:
    """
    Validates source IP addresses against a set of allowed CIDR ranges.
    Supports IPv4 and IPv6, loopback/private ranges, and named entry sets
    for multi-provider webhook endpoints.

    Usage:
        allowlist = IPAllowlist()
        allowlist.add_cidr("192.30.252.0/22", label="GitHub webhooks")
        allowlist.add_cidr("54.187.174.169/32", label="Stripe webhooks")
        allowlist.add_private()       # allow all RFC1918 + loopback

        if not allowlist.is_allowed("203.0.113.5"):
            return 403, "Source IP not in allowlist"
    """

    # Common provider webhook IP ranges (representative examples — check official docs)
    KNOWN_PROVIDERS = {
        "github": [
            "192.30.252.0/22", "185.199.108.0/22", "140.82.112.0/20",
            "143.55.64.0/20",
        ],
        "stripe": [
            "3.18.12.63/32", "3.130.192.231/32", "13.235.14.237/32",
            "13.235.122.149/32", "18.211.135.69/32", "35.154.171.200/32",
            "52.15.183.38/32", "54.187.174.169/32",
        ],
        "slack": [
            "34.195.142.251/32", "54.242.85.56/32",
        ],
        "twilio": [
            "54.172.60.0/23", "54.244.51.0/24",
        ],
    }

    PRIVATE_CIDRS = [
        "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
        "127.0.0.0/8", "::1/128", "fc00::/7",
    ]

    def __init__(self, default_deny: bool = True):
        self._entries: List[AllowlistEntry] = []
        self._default_deny = default_deny

    def add_cidr(self, cidr: str, label: str = "", source: str = "manual") -> "IPAllowlist":
        network = ipaddress.ip_network(cidr, strict=False)
        self._entries.append(AllowlistEntry(network=network, label=label, source=source))
        logger.debug("allowlist_entry_added cidr=%s label=%s", cidr, label)
        return self

    def add_cidrs(self, cidrs: Iterable[str], label: str = "", source: str = "") -> "IPAllowlist":
        for cidr in cidrs:
            self.add_cidr(cidr, label=label, source=source)
        return self

    def add_provider(self, provider: str) -> "IPAllowlist":
        cidrs = self.KNOWN_PROVIDERS.get(provider.lower())
        if not cidrs:
            raise ValueError(f"Unknown provider '{provider}'. Known: {list(self.KNOWN_PROVIDERS)}")
        return self.add_cidrs(cidrs, label=f"{provider} webhooks", source=provider)

    def add_private(self) -> "IPAllowlist":
        return self.add_cidrs(self.PRIVATE_CIDRS, label="private/loopback", source="rfc1918")

    def is_allowed(self, ip: str) -> bool:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            logger.warning("allowlist_invalid_ip ip=%s", ip)
            return False
        for entry in self._entries:
            if addr in entry.network:
                return True
        return not self._default_deny

    def find_entry(self, ip: str) -> Optional[AllowlistEntry]:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return None
        for entry in self._entries:
            if addr in entry.network:
                return entry
        return None

    def __len__(self) -> int:
        return len(self._entries)
```

---

## Solution 2: WebhookIPMiddleware — WSGI/ASGI Middleware Layer

```python
import logging
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class WebhookIPMiddleware:
    """
    ASGI middleware that enforces IP allowlist validation before any
    request processing. Supports X-Forwarded-For and X-Real-IP header
    extraction for deployments behind a reverse proxy or load balancer.

    Usage (FastAPI / Starlette):
        app = FastAPI()
        allowlist = IPAllowlist().add_provider("github").add_private()
        app.add_middleware(WebhookIPMiddleware,
                            allowlist=allowlist,
                            webhook_paths=["/webhooks/"],
                            trust_proxy=True)
    """

    def __init__(
        self,
        app: Any,
        allowlist: IPAllowlist,
        webhook_paths: Optional[List[str]] = None,
        trust_proxy: bool = True,
        proxy_header: str = "X-Forwarded-For",
        block_status: int = 403,
    ):
        self._app = app
        self._allowlist = allowlist
        self._paths = [p.rstrip("/") for p in (webhook_paths or ["/webhook"])]
        self._trust_proxy = trust_proxy
        self._proxy_header = proxy_header.lower().replace("-", "_")
        self._block_status = block_status
        self._rejected = 0
        self._allowed = 0

    def _extract_ip(self, scope: Dict) -> str:
        headers = dict(scope.get("headers", []))
        if self._trust_proxy:
            forwarded = headers.get(b"x-forwarded-for", b"").decode()
            if forwarded:
                return forwarded.split(",")[0].strip()
            real_ip = headers.get(b"x-real-ip", b"").decode()
            if real_ip:
                return real_ip.strip()
        client = scope.get("client")
        return client[0] if client else "unknown"

    def _is_webhook_path(self, path: str) -> bool:
        return any(path.rstrip("/").startswith(p) for p in self._paths)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self._is_webhook_path(scope.get("path", "")):
            await self._app(scope, receive, send)
            return

        source_ip = self._extract_ip(scope)
        if not self._allowlist.is_allowed(source_ip):
            self._rejected += 1
            entry = self._allowlist.find_entry(source_ip)
            logger.warning(
                "webhook_ip_rejected ip=%s path=%s label=%s",
                source_ip, scope.get("path"), entry.label if entry else "none",
            )
            await self._send_response(send, self._block_status,
                                       b"Forbidden: source IP not allowed")
            return

        self._allowed += 1
        entry = self._allowlist.find_entry(source_ip)
        logger.info("webhook_ip_allowed ip=%s label=%s path=%s",
                     source_ip, entry.label if entry else "unknown", scope.get("path"))
        await self._app(scope, receive, send)

    async def _send_response(self, send, status: int, body: bytes):
        await send({"type": "http.response.start", "status": status,
                     "headers": [[b"content-type", b"text/plain"]]})
        await send({"type": "http.response.body", "body": body})

    @property
    def stats(self) -> Dict:
        return {"allowed": self._allowed, "rejected": self._rejected}
```

---

## Solution 3: DynamicIPFetcher — Pull Provider IP Ranges from Official APIs

```python
import json
import logging
import time
from typing import Dict, List, Optional
from urllib.request import urlopen

logger = logging.getLogger(__name__)


class DynamicIPFetcher:
    """
    Fetches webhook IP ranges from provider APIs at startup and on a
    refresh schedule, keeping the allowlist current as providers add
    or change IP ranges. Falls back to bundled static ranges on fetch failure.

    Usage:
        fetcher = DynamicIPFetcher(allowlist=ip_allowlist)
        await fetcher.refresh()         # pull latest ranges
        fetcher.schedule_refresh(interval=3600)  # refresh hourly
    """

    GITHUB_META_URL = "https://api.github.com/meta"
    CLOUDFLARE_IPS_URL = "https://www.cloudflare.com/ips-v4"

    def __init__(self, allowlist: IPAllowlist, timeout: int = 10):
        self._allowlist = allowlist
        self._timeout = timeout
        self._last_refresh: float = 0.0
        self._refresh_errors: int = 0

    def _fetch_json(self, url: str) -> Optional[dict]:
        try:
            with urlopen(url, timeout=self._timeout) as resp:
                return json.loads(resp.read())
        except Exception as exc:
            logger.error("ip_fetch_failed url=%s error=%s", url, exc)
            self._refresh_errors += 1
            return None

    def refresh_github(self) -> int:
        data = self._fetch_json(self.GITHUB_META_URL)
        if not data:
            return 0
        hooks = data.get("hooks", [])
        added = 0
        for cidr in hooks:
            try:
                self._allowlist.add_cidr(cidr, label="GitHub webhooks (live)", source="github-api")
                added += 1
            except ValueError:
                pass
        logger.info("github_ips_refreshed count=%d", added)
        self._last_refresh = time.time()
        return added

    def refresh_cloudflare(self) -> int:
        try:
            with urlopen(self.CLOUDFLARE_IPS_URL, timeout=self._timeout) as resp:
                lines = resp.read().decode().strip().splitlines()
        except Exception as exc:
            logger.error("cloudflare_ip_fetch_failed error=%s", exc)
            return 0
        added = 0
        for cidr in lines:
            cidr = cidr.strip()
            if cidr:
                try:
                    self._allowlist.add_cidr(cidr, label="Cloudflare", source="cloudflare-api")
                    added += 1
                except ValueError:
                    pass
        logger.info("cloudflare_ips_refreshed count=%d", added)
        return added

    def refresh_all(self) -> Dict[str, int]:
        return {
            "github": self.refresh_github(),
            "cloudflare": self.refresh_cloudflare(),
        }

    @property
    def age_seconds(self) -> float:
        return time.time() - self._last_refresh if self._last_refresh else float("inf")
```

---

## Solution 4: ProxySafeIPExtractor — Secure Client IP Resolution

```python
import ipaddress
import logging
from typing import List, Optional

logger = logging.getLogger(__name__)


class ProxySafeIPExtractor:
    """
    Extracts the real client IP from requests that pass through a
    reverse proxy or CDN. Validates that the claimed proxy chain
    is trusted before trusting forwarded headers—preventing IP
    spoofing via crafted X-Forwarded-For headers.

    Usage:
        extractor = ProxySafeIPExtractor(
            trusted_proxies=["10.0.0.0/8", "172.16.0.0/12"],
            num_trusted_hops=1,
        )
        real_ip = extractor.extract(
            remote_addr="10.0.1.5",  # load balancer IP
            x_forwarded_for="203.0.113.7, 10.0.1.5",
        )
        # real_ip = "203.0.113.7"
    """

    def __init__(
        self,
        trusted_proxies: Optional[List[str]] = None,
        num_trusted_hops: int = 1,
    ):
        self._trusted_nets = []
        for cidr in (trusted_proxies or []):
            self._trusted_nets.append(ipaddress.ip_network(cidr, strict=False))
        self._hops = num_trusted_hops

    def _is_trusted_proxy(self, ip: str) -> bool:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(addr in net for net in self._trusted_nets)

    def extract(self, remote_addr: str, x_forwarded_for: str = "",
                 x_real_ip: str = "") -> str:
        """
        Returns the real client IP, stripping trusted proxy hops from
        the X-Forwarded-For chain. Falls back to remote_addr if the
        proxy chain is invalid or untrusted.
        """
        if not self._is_trusted_proxy(remote_addr):
            # Direct connection — remote_addr IS the client
            return remote_addr

        if x_real_ip and self._is_trusted_proxy(remote_addr):
            return x_real_ip.strip()

        if x_forwarded_for:
            # XFF: client, proxy1, proxy2, ... (rightmost is most recent)
            hops = [h.strip() for h in x_forwarded_for.split(",")]
            # Strip trusted proxy hops from the right
            trusted_count = 0
            for hop in reversed(hops):
                if self._is_trusted_proxy(hop):
                    trusted_count += 1
                else:
                    break
            idx = max(0, len(hops) - trusted_count - self._hops)
            if 0 <= idx < len(hops):
                candidate = hops[idx].strip()
                try:
                    ipaddress.ip_address(candidate)
                    return candidate
                except ValueError:
                    logger.warning("xff_invalid_ip candidate=%s xff=%s",
                                    candidate, x_forwarded_for)

        logger.warning("ip_extraction_fallback remote_addr=%s", remote_addr)
        return remote_addr
```

---

## Solution 5: WebhookIPAuditLogger — Track Allowed and Rejected Sources

```python
import json
import logging
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class WebhookIPAuditLogger:
    """
    Records every webhook source IP (allowed and rejected) as a structured
    audit event. Provides aggregated statistics for security dashboards:
    top rejected IPs, allowed sources by provider, rejection rate over time.

    Usage:
        audit = WebhookIPAuditLogger(max_records=10_000)
        audit.log_allowed(ip="192.30.252.1", path="/webhooks/github",
                           provider="GitHub")
        audit.log_rejected(ip="203.0.113.7", path="/webhooks/github")
        print(audit.summary())
    """

    def __init__(self, max_records: int = 10_000):
        self._max = max_records
        self._records: List[Dict[str, Any]] = []
        self._allowed_by_provider: Dict[str, int] = defaultdict(int)
        self._rejected_by_ip: Dict[str, int] = defaultdict(int)

    def _record(self, event: str, **fields):
        if len(self._records) >= self._max:
            self._records.pop(0)
        entry = {"event": event, "ts": time.time(), **fields}
        self._records.append(entry)
        logger.info(json.dumps(entry))

    def log_allowed(self, ip: str, path: str = "", provider: str = ""):
        self._allowed_by_provider[provider or "unknown"] += 1
        self._record("webhook_allowed", ip=ip, path=path, provider=provider)

    def log_rejected(self, ip: str, path: str = "", reason: str = "not_in_allowlist"):
        self._rejected_by_ip[ip] += 1
        self._record("webhook_rejected", ip=ip, path=path, reason=reason)

    def summary(self) -> Dict[str, Any]:
        total = len(self._records)
        rejected = [r for r in self._records if r["event"] == "webhook_rejected"]
        allowed = [r for r in self._records if r["event"] == "webhook_allowed"]
        top_rejected = sorted(self._rejected_by_ip.items(),
                               key=lambda x: x[1], reverse=True)[:10]
        return {
            "total_events": total,
            "allowed_count": len(allowed),
            "rejected_count": len(rejected),
            "rejection_rate_pct": round(len(rejected) / max(total, 1) * 100, 1),
            "allowed_by_provider": dict(self._allowed_by_provider),
            "top_rejected_ips": [{"ip": ip, "count": c} for ip, c in top_rejected],
        }

    def recent_rejections(self, n: int = 20) -> List[Dict]:
        return [r for r in reversed(self._records)
                if r["event"] == "webhook_rejected"][:n]
```

---

## Solution 6: WebhookSecurityGate — Combined IP + Signature Validation

```python
import hashlib
import hmac
import logging
import time
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


class WebhookSecurityGate:
    """
    Combines IP allowlist validation with HMAC signature verification.
    IP check runs first (fast, no payload read) to reject obviously
    spoofed requests. Signature check runs second to authenticate
    requests from allowed IP ranges.

    Usage:
        gate = WebhookSecurityGate(
            allowlist=ip_allowlist,
            signing_secret=os.environ["WEBHOOK_SECRET"],
            sig_header="X-Hub-Signature-256",
            hash_alg="sha256",
        )

        @app.post("/webhooks/github")
        async def handle(request: Request):
            source_ip = request.client.host
            body = await request.body()
            sig = request.headers.get("X-Hub-Signature-256", "")
            is_valid, reason = gate.validate(source_ip, body, sig)
            if not is_valid:
                raise HTTPException(403, reason)
    """

    def __init__(
        self,
        allowlist: IPAllowlist,
        signing_secret: str,
        sig_header: str = "X-Hub-Signature-256",
        hash_alg: str = "sha256",
        audit: Optional[WebhookIPAuditLogger] = None,
    ):
        self._allowlist = allowlist
        self._secret = signing_secret.encode() if isinstance(signing_secret, str) else signing_secret
        self._sig_header = sig_header
        self._hash_alg = hash_alg
        self._audit = audit

    def _verify_signature(self, body: bytes, signature: str) -> bool:
        expected = hmac.new(self._secret, body, self._hash_alg).hexdigest()
        prefix = f"{self._hash_alg}="
        if signature.startswith(prefix):
            signature = signature[len(prefix):]
        return hmac.compare_digest(expected, signature)

    def validate(self, source_ip: str, body: bytes, signature: str) -> tuple:
        """Returns (is_valid: bool, reason: str)."""
        # Step 1: IP check
        if not self._allowlist.is_allowed(source_ip):
            if self._audit:
                self._audit.log_rejected(source_ip, reason="ip_not_allowed")
            logger.warning("webhook_rejected_ip ip=%s", source_ip)
            return False, "Source IP not in allowlist"

        # Step 2: Signature check
        if not signature:
            if self._audit:
                self._audit.log_rejected(source_ip, reason="missing_signature")
            return False, "Missing signature header"

        if not self._verify_signature(body, signature):
            if self._audit:
                self._audit.log_rejected(source_ip, reason="invalid_signature")
            logger.warning("webhook_rejected_sig ip=%s", source_ip)
            return False, "Invalid HMAC signature"

        entry = self._allowlist.find_entry(source_ip)
        provider = entry.label if entry else ""
        if self._audit:
            self._audit.log_allowed(source_ip, provider=provider)
        return True, "ok"
```

---

## Comparison

| Approach | CIDR Matching | Proxy-Safe | Dynamic Refresh | Audit Logging | HMAC Combo | ASGI Middleware |
|---|---|---|---|---|---|---|
| **IPAllowlist** | Yes | No | No | No | No | No |
| **WebhookIPMiddleware** | Via allowlist | Yes | No | No | No | Yes |
| **DynamicIPFetcher** | Via allowlist | No | Yes | No | No | No |
| **ProxySafeIPExtractor** | No | Yes | No | No | No | No |
| **WebhookIPAuditLogger** | No | No | No | Yes | No | No |
| **WebhookSecurityGate** | Via allowlist | No | No | Via audit | Yes | No |

**Key insight**: the minimum viable deployment is `IPAllowlist().add_provider("github").add_private()` checked at the top of each webhook handler before any payload processing. For load-balanced deployments, combine `ProxySafeIPExtractor` (to get the real client IP from X-Forwarded-For) with `WebhookIPMiddleware` (to enforce the allowlist). Always combine IP allowlisting with HMAC signature verification via `WebhookSecurityGate`—IP ranges can be spoofed on the open internet (though harder), but a stolen HMAC secret from an unexpected IP would still be caught. Use `DynamicIPFetcher.refresh_github()` at startup to ensure IP ranges are current without hardcoding values that change over time.
