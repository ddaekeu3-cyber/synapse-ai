---
title: "Agent Doesn't Implement Subresource Integrity for Tool Outputs"
description: "Agents that fetch external content via tools — documents, scripts, data files — do not verify content integrity; a compromised CDN or MITM attack can serve malicious content that the agent processes and acts on."
category: security
difficulty: advanced
tags: [integrity, sri, hash-verification, supply-chain, tool-security, content-verification, mitm]
---

# Agent Doesn't Implement Subresource Integrity for Tool Outputs

## Problem

When an agent's tool fetches external content — a document from S3, a script from npm CDN, a data file from a partner API — it implicitly trusts that the content is authentic. A compromised CDN, DNS hijack, or MITM attack can replace the expected content with malicious payloads. The web solved this for scripts and stylesheets with Subresource Integrity (SRI): a hash of the expected content is embedded in the reference, and the browser rejects content that doesn't match. The same principle applies to agent tool outputs: compute or look up the expected hash, verify before processing.

## Solution 1: Hash-Verified Tool Fetch

Before processing a fetched resource, verify its SHA-256 hash against a known-good value.

```python
import asyncio
import hashlib
from typing import Optional
import httpx
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

class IntegrityError(Exception):
    """Raised when fetched content does not match expected hash."""
    pass

def compute_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()

def compute_sri(content: bytes) -> str:
    """Compute a W3C SRI hash string: sha256-<base64>"""
    import base64
    digest = hashlib.sha256(content).digest()
    return "sha256-" + base64.b64encode(digest).decode()

async def verified_fetch(
    url: str,
    expected_hash: str,              # "sha256:<hex>" or SRI "sha256-<base64>"
    timeout: float = 10.0,
) -> bytes:
    """
    Fetch URL and verify content against expected hash.
    Raises IntegrityError if content doesn't match.
    Never returns unverified content.
    """
    async with httpx.AsyncClient(follow_redirects=False) as http:
        # Don't follow redirects — they could redirect to a compromised host
        resp = await http.get(url, timeout=timeout)
        resp.raise_for_status()
        content = resp.content

    # Normalize expected_hash format
    if expected_hash.startswith("sha256:"):
        actual = compute_sha256(content)
        expected = expected_hash[7:]
        match = actual == expected
    elif expected_hash.startswith("sha256-"):
        import base64
        actual_sri = compute_sri(content)
        match = actual_sri == expected_hash
    else:
        raise ValueError(f"Unsupported hash format: {expected_hash!r}")

    if not match:
        raise IntegrityError(
            f"Content integrity check failed for {url!r}. "
            f"Expected {expected_hash!r}, got computed hash that does not match. "
            f"Possible supply chain compromise or MITM attack."
        )

    return content

# Known-good hash registry for external resources the agent fetches
KNOWN_RESOURCE_HASHES: dict[str, str] = {
    "https://example.com/data/product_schema.json":
        "sha256:a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2c3d4e5f6a7b8c9d0e1f2a3b4",
    "https://cdn.example.com/configs/agent_config_v2.json":
        "sha256-" + "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",  # placeholder
}

async def agent_with_verified_fetch(url: str, user_query: str) -> dict:
    expected = KNOWN_RESOURCE_HASHES.get(url)
    if expected is None:
        return {"error": f"No known hash for {url!r} — refusing to fetch unregistered resource"}

    try:
        content = await verified_fetch(url, expected)
    except IntegrityError as exc:
        import logging
        logging.getLogger("security").critical("integrity_check_failed", extra={"url": url, "error": str(exc)})
        return {"error": "Content integrity verification failed", "url": url}
    except httpx.HTTPStatusError as exc:
        return {"error": f"HTTP {exc.response.status_code}", "url": url}

    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"Verified content from {url}:\n{content.decode(errors='replace')[:2000]}\n\n{user_query}",
        }],
    )
    return {"response": resp.content[0].text, "integrity_verified": True}
```

**When to use**: Any agent tool that fetches external files (JSON configs, data files, documents) from URLs. Integrity verification is the direct equivalent of browser SRI for agent pipelines.

---

## Solution 2: Content-Type + Schema Validation After Fetch

Even if you can't pre-compute a hash, validate that fetched content has the expected structure before the agent processes it.

```python
import asyncio
import json
from typing import Any
import httpx
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

class ContentValidationError(Exception):
    pass

def validate_json_schema(content: bytes, required_fields: list[str], max_size_bytes: int = 1_048_576) -> dict:
    """
    Parse and validate fetched JSON content.
    Rejects: oversized payloads, wrong content type, missing required fields.
    """
    if len(content) > max_size_bytes:
        raise ContentValidationError(
            f"Payload size {len(content)} exceeds limit {max_size_bytes} — possible DoS attempt"
        )

    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ContentValidationError(f"Invalid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ContentValidationError("Expected JSON object at root level")

    missing = [f for f in required_fields if f not in data]
    if missing:
        raise ContentValidationError(f"Missing required fields: {missing}")

    return data

async def safe_fetch_and_validate(
    url: str,
    expected_content_type: str = "application/json",
    required_fields: list[str] | None = None,
    max_size_mb: float = 1.0,
) -> dict:
    """
    Fetch URL with safety checks:
    1. Allowlisted URL scheme (https only)
    2. Content-Type header validation
    3. Size limit enforcement
    4. JSON schema validation
    """
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ContentValidationError(f"Only HTTPS URLs are allowed; got {parsed.scheme!r}")

    # Block SSRF: reject internal/private addresses
    private_prefixes = ("localhost", "127.", "10.", "172.16.", "172.17.", "192.168.", "[::1]")
    host = parsed.hostname or ""
    if any(host.startswith(p) or host == p.rstrip(".") for p in private_prefixes):
        raise ContentValidationError(f"SSRF blocked: private address {host!r}")

    max_bytes = int(max_size_mb * 1_048_576)

    async with httpx.AsyncClient(follow_redirects=False, timeout=10.0) as http:
        resp = await http.get(url)
        resp.raise_for_status()

        content_type = resp.headers.get("content-type", "")
        if expected_content_type not in content_type:
            raise ContentValidationError(
                f"Unexpected Content-Type {content_type!r}; expected {expected_content_type!r}"
            )

        content = resp.content
        if len(content) > max_bytes:
            raise ContentValidationError(f"Response too large: {len(content)} bytes")

    return validate_json_schema(content, required_fields or [], max_bytes)

async def agent_with_validated_tool_result(url: str, query: str) -> dict:
    try:
        data = await safe_fetch_and_validate(
            url=url,
            expected_content_type="application/json",
            required_fields=["name", "version", "data"],
            max_size_mb=0.5,
        )
    except ContentValidationError as exc:
        return {"error": str(exc), "security_event": "content_validation_failed"}

    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": f"Data: {data}\n\n{query}"}],
    )
    return {"response": resp.content[0].text}
```

**When to use**: Agents that fetch from partner APIs or CDNs where you don't control the content but need to validate its structure. Schema validation catches both MITM attacks and accidental API changes.

---

## Solution 3: Tool Output Signing — Agent-to-Agent Content Authenticity

When one agent produces content that another agent consumes, sign the output so the consumer can verify authenticity before processing.

```python
import asyncio
import base64
import hashlib
import hmac
import json
import secrets
import time
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

SIGNING_KEY = secrets.token_bytes(32)  # shared between producer and consumer agents

def sign_tool_output(tool_name: str, output: dict) -> dict:
    """
    Sign a tool output dict. Returns the output with a signature envelope.
    The consumer verifies the signature before processing.
    """
    payload = json.dumps({
        "tool": tool_name,
        "output": output,
        "timestamp": int(time.time()),
        "nonce": secrets.token_hex(8),
    }, sort_keys=True)

    sig = hmac.new(SIGNING_KEY, payload.encode(), hashlib.sha256).hexdigest()
    return {
        "_signed_payload": payload,
        "_signature": sig,
        "_tool": tool_name,
    }

def verify_tool_output(envelope: dict, max_age_seconds: int = 300) -> dict:
    """
    Verify and unpack a signed tool output.
    Raises ValueError on tampered or expired envelopes.
    Returns the original output dict on success.
    """
    payload_str = envelope.get("_signed_payload")
    signature = envelope.get("_signature")

    if not payload_str or not signature:
        raise ValueError("Missing signature fields — envelope may be tampered")

    expected_sig = hmac.new(SIGNING_KEY, payload_str.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected_sig):
        raise ValueError("Signature mismatch — tool output may have been tampered")

    payload = json.loads(payload_str)
    age = time.time() - payload.get("timestamp", 0)
    if age > max_age_seconds:
        raise ValueError(f"Tool output expired ({age:.0f}s > {max_age_seconds}s limit)")

    return payload["output"]

async def producer_agent(query: str) -> dict:
    """Agent that produces signed tool results."""
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": query}],
    )
    raw_output = {"result": resp.content[0].text, "query": query}
    signed = sign_tool_output("ai_search", raw_output)
    return signed

async def consumer_agent(signed_result: dict, follow_up: str) -> dict:
    """Agent that verifies and processes signed results from producer."""
    try:
        verified_output = verify_tool_output(signed_result, max_age_seconds=60)
    except ValueError as exc:
        import logging
        logging.getLogger("security").error("tool_output_tampered", extra={"error": str(exc)})
        return {"error": "Tool output failed integrity check — discarding"}

    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"Verified tool result: {verified_output}\n\n{follow_up}",
        }],
    )
    return {"response": resp.content[0].text, "integrity_verified": True}

async def demo():
    signed = await producer_agent("What are the main Python web frameworks?")
    result = await consumer_agent(signed, "Which one is best for APIs?")
    return result
```

**When to use**: Multi-agent pipelines where one agent's output is the input to another. Signing creates an authenticity chain that detects injection or tampering between agents.

---

## Solution 4: Allowlist-Only URL Fetching — Block Unknown Domains

Tool calls that fetch URLs must only access pre-approved domains. Any URL not in the allowlist is blocked before the request is made.

```python
import asyncio
import re
from urllib.parse import urlparse
from typing import Optional
import httpx
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

# Only these domains can be fetched by tool calls
FETCH_ALLOWLIST = frozenset({
    "api.anthropic.com",
    "api.github.com",
    "raw.githubusercontent.com",
    "data.example.com",
    "cdn.example.com",
    "partner-api.acme.com",
})

# Block known data-exfiltration or abuse-prone domains
FETCH_BLOCKLIST = frozenset({
    "requestbin.com",
    "webhook.site",
    "ngrok.io",
    "burpcollaborator.net",
})

def validate_fetch_url(url: str) -> tuple[bool, str]:
    """
    Validate a URL before making a fetch request.
    Returns (allowed, reason).
    """
    try:
        parsed = urlparse(url)
    except Exception as exc:
        return False, f"Malformed URL: {exc}"

    if parsed.scheme not in ("https",):
        return False, f"Only HTTPS allowed; got {parsed.scheme!r}"

    host = (parsed.hostname or "").lower()

    if not host:
        return False, "Empty hostname"

    # Block private/loopback addresses (SSRF prevention)
    private_ranges = [
        r"^localhost$",
        r"^127\.",
        r"^10\.",
        r"^172\.(1[6-9]|2\d|3[01])\.",
        r"^192\.168\.",
        r"^\[?::1\]?$",
    ]
    for pattern in private_ranges:
        if re.match(pattern, host):
            return False, f"SSRF blocked: {host!r} is a private address"

    # Blocklist check
    for blocked in FETCH_BLOCKLIST:
        if host == blocked or host.endswith("." + blocked):
            return False, f"Domain {host!r} is blocklisted"

    # Allowlist check
    for allowed in FETCH_ALLOWLIST:
        if host == allowed or host.endswith("." + allowed):
            return True, "allowed"

    return False, f"Domain {host!r} is not in the fetch allowlist"

async def allowlisted_fetch(url: str) -> bytes:
    allowed, reason = validate_fetch_url(url)
    if not allowed:
        raise PermissionError(f"Fetch blocked: {reason}")

    async with httpx.AsyncClient(follow_redirects=False, timeout=10.0) as http:
        resp = await http.get(url)
        resp.raise_for_status()
        return resp.content

async def agent_with_allowlisted_tool(url: str, query: str) -> dict:
    try:
        content = await allowlisted_fetch(url)
    except PermissionError as exc:
        import logging
        logging.getLogger("security").warning("fetch_blocked", extra={"url": url, "reason": str(exc)})
        return {"error": str(exc)}
    except httpx.HTTPError as exc:
        return {"error": f"HTTP error: {exc}"}

    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"Fetched content:\n{content.decode(errors='replace')[:2000]}\n\n{query}",
        }],
    )
    return {"response": resp.content[0].text}
```

**When to use**: All agents with URL-fetching tools. Allowlist validation is the most effective prevention for SSRF (Server-Side Request Forgery) and supply-chain attacks via URL redirection.

---

## Solution 5: Fetch Result Caching with Integrity Seal

Cache fetched resources with their content hash. On a cache hit, verify the stored hash before serving from cache to prevent cache poisoning.

```python
import asyncio
import hashlib
import time
from dataclasses import dataclass
import httpx
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

@dataclass
class CachedResource:
    url: str
    content: bytes
    sha256: str
    fetched_at: float
    ttl_seconds: float = 300.0

    def is_fresh(self) -> bool:
        return (time.monotonic() - self.fetched_at) < self.ttl_seconds

    def verify(self) -> bool:
        """Re-verify stored content against stored hash (detects cache tampering)."""
        return hashlib.sha256(self.content).hexdigest() == self.sha256

class IntegrityCache:
    """
    Resource cache that verifies content integrity on every cache hit.
    Protects against cache poisoning attacks.
    """

    def __init__(self):
        self._store: dict[str, CachedResource] = {}

    async def get_or_fetch(self, url: str, expected_hash: str | None = None) -> bytes:
        # Check cache
        cached = self._store.get(url)
        if cached and cached.is_fresh():
            if not cached.verify():
                del self._store[url]
                raise ValueError(f"Cache integrity check failed for {url!r} — cache may be poisoned")
            if expected_hash and not hmac_safe_compare(cached.sha256, expected_hash):
                raise ValueError(f"Cached hash does not match expected hash for {url!r}")
            return cached.content

        # Fetch fresh
        async with httpx.AsyncClient(follow_redirects=False, timeout=10.0) as http:
            resp = await http.get(url)
            resp.raise_for_status()
            content = resp.content

        sha256 = hashlib.sha256(content).hexdigest()

        if expected_hash and sha256 != expected_hash.lstrip("sha256:"):
            raise ValueError(f"Integrity check failed: fetched content hash does not match expected for {url!r}")

        self._store[url] = CachedResource(
            url=url,
            content=content,
            sha256=sha256,
            fetched_at=time.monotonic(),
        )
        return content

def hmac_safe_compare(a: str, b: str) -> bool:
    import hmac
    return hmac.compare_digest(a.encode(), b.encode())

integrity_cache = IntegrityCache()

async def agent_with_integrity_cache(url: str, known_hash: str | None, query: str) -> dict:
    try:
        content = await integrity_cache.get_or_fetch(url, expected_hash=known_hash)
    except ValueError as exc:
        return {"error": str(exc), "security_event": "integrity_violation"}
    except httpx.HTTPError as exc:
        return {"error": f"Fetch failed: {exc}"}

    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"Verified content:\n{content.decode(errors='replace')[:2000]}\n\n{query}",
        }],
    )
    return {"response": resp.content[0].text, "cache_sha256": hashlib.sha256(content).hexdigest()[:12]}
```

**When to use**: Agents that repeatedly fetch the same external resources. The integrity-sealed cache prevents both bandwidth waste (repeated fetches) and cache poisoning (tampered cached values).

---

## Solution 6: Tool Output Audit Log — Record All External Content Fetched

Log every external resource fetched by tool calls with its hash, so you can audit what content the agent processed and detect anomalies.

```python
import asyncio
import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass, field
import httpx
from anthropic import AsyncAnthropic

client = AsyncAnthropic()
audit_logger = logging.getLogger("tool_fetch_audit")

@dataclass
class FetchAuditRecord:
    request_id: str
    url: str
    content_hash: str
    content_size_bytes: int
    content_type: str
    status_code: int
    fetched_at: float = field(default_factory=time.time)
    integrity_verified: bool = False
    expected_hash: str | None = None
    integrity_match: bool | None = None

async def audited_fetch(
    request_id: str,
    url: str,
    expected_hash: str | None = None,
    timeout: float = 10.0,
) -> tuple[bytes, FetchAuditRecord]:
    """
    Fetch URL with full audit logging.
    Returns (content, audit_record).
    """
    async with httpx.AsyncClient(follow_redirects=False, timeout=timeout) as http:
        resp = await http.get(url)
        resp.raise_for_status()
        content = resp.content

    sha256 = hashlib.sha256(content).hexdigest()
    integrity_match = None
    if expected_hash:
        expected_bare = expected_hash.lstrip("sha256:")
        integrity_match = sha256 == expected_bare

    record = FetchAuditRecord(
        request_id=request_id,
        url=url,
        content_hash=sha256,
        content_size_bytes=len(content),
        content_type=resp.headers.get("content-type", "unknown"),
        status_code=resp.status_code,
        integrity_verified=expected_hash is not None,
        expected_hash=expected_hash,
        integrity_match=integrity_match,
    )

    # Structured audit log — queryable in Elasticsearch, CloudWatch Logs, etc.
    audit_logger.info("tool_fetch", extra={
        "audit_record": asdict(record),
        "integrity_ok": integrity_match if integrity_match is not None else "not_checked",
    })

    if integrity_match is False:
        audit_logger.critical("tool_fetch_integrity_failure", extra={
            "url": url,
            "expected_hash": expected_hash,
            "actual_hash": sha256,
            "request_id": request_id,
        })

    return content, record

async def agent_with_audit_trail(request_id: str, url: str, query: str) -> dict:
    try:
        content, audit = await audited_fetch(request_id, url, expected_hash=None)
    except httpx.HTTPError as exc:
        return {"error": str(exc)}

    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"Content ({audit.content_hash[:12]}...):\n{content.decode(errors='replace')[:2000]}\n\n{query}",
        }],
    )

    return {
        "response": resp.content[0].text,
        "audit": {
            "url": audit.url,
            "sha256": audit.content_hash,
            "size_bytes": audit.content_size_bytes,
        },
    }
```

**When to use**: Production agents in regulated environments (SOC 2, HIPAA, financial compliance). An audit log of every external fetch with content hashes enables forensic analysis of what content the agent processed during any given request.

---

## Comparison

| Solution | Prevents MITM | Prevents SSRF | Detects Cache Poisoning | Audit Trail | Setup Cost | Best For |
|---|---|---|---|---|---|---|
| Hash-verified fetch | Yes | No | No | No | Low | Known-static resources |
| Content-type + schema validation | Partial | Yes | No | No | Low | Unknown-hash resources |
| Tool output signing | Yes | No | No | No | Medium | Agent-to-agent pipelines |
| Allowlist-only fetching | Partial | Yes | No | No | Low | All URL-fetching tools |
| Integrity-sealed cache | Yes | No | Yes | No | Medium | Frequently-fetched resources |
| Audit log | No | No | No | Yes | Low | Compliance/forensics |

**Rule of thumb**: Always implement URL allowlisting (Solution 4) — it's free and blocks SSRF completely. Add hash verification (Solution 1) for any resource whose content you control. Add audit logging (Solution 6) in production environments with compliance requirements.
