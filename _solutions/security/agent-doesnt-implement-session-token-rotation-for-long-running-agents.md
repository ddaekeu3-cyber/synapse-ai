---
title: "Agent Doesn't Implement Session Token Rotation for Long-Running Agents"
description: "Rotate authentication tokens, API keys, and session credentials during extended agent runs to limit the blast radius of token compromise and meet security compliance requirements."
difficulty: intermediate
category: security
tags: [security, session-management, token-rotation, authentication, long-running]
---

## Problem

Long-running agents authenticate once at startup and use the same token for hours or days. If the token is compromised mid-session—through logs, memory dumps, or network interception—the attacker has unlimited time to act. Static tokens also violate security compliance policies (SOC 2, ISO 27001) that require periodic credential rotation. The fix is to build token rotation into the agent's execution loop.

## Solutions

### Option 1: Time-Based Token Rotation

Rotate tokens on a fixed schedule regardless of usage, using a thread-safe refresh mechanism.

```python
import asyncio
import time
import os
from anthropic import AsyncAnthropic
from dataclasses import dataclass

@dataclass
class RotatingToken:
    value: str
    issued_at: float
    ttl_seconds: float = 3600.0  # Rotate every hour

    def is_expired(self) -> bool:
        return (time.monotonic() - self.issued_at) >= self.ttl_seconds * 0.8  # Rotate at 80% TTL

async def fetch_new_api_key() -> str:
    """Simulate fetching a fresh API key from a secrets manager."""
    # In production: AWS Secrets Manager, HashiCorp Vault, GCP Secret Manager
    await asyncio.sleep(0.1)  # Simulate network call
    return os.environ.get("ANTHROPIC_API_KEY", "")

class RotatingTokenAgent:
    def __init__(self, ttl_seconds: float = 3600.0):
        self._token: RotatingToken | None = None
        self._client: AsyncAnthropic | None = None
        self._lock = asyncio.Lock()
        self._ttl = ttl_seconds
        self._rotations = 0

    async def _ensure_fresh_token(self):
        if self._token and not self._token.is_expired():
            return

        async with self._lock:
            # Double-check after acquiring lock
            if self._token and not self._token.is_expired():
                return

            new_key = await fetch_new_api_key()
            self._token = RotatingToken(
                value=new_key,
                issued_at=time.monotonic(),
                ttl_seconds=self._ttl
            )
            self._client = AsyncAnthropic(api_key=new_key)
            self._rotations += 1
            print(f"[Security] Token rotated (rotation #{self._rotations})")

    async def complete(self, prompt: str) -> str:
        await self._ensure_fresh_token()
        response = await self._client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text

async def demo_time_based_rotation():
    # Short TTL for demo purposes
    agent = RotatingTokenAgent(ttl_seconds=5.0)

    prompts = [
        "What is 1+1?",
        "Name a color.",
        "What is the capital of France?",
    ]

    for prompt in prompts:
        result = await agent.complete(prompt)
        print(f"Q: {prompt} → {result.strip()[:60]}")
        await asyncio.sleep(2)

    print(f"\nTotal rotations: {agent._rotations}")

asyncio.run(demo_time_based_rotation())
```

### Option 2: Usage-Count-Based Rotation

Rotate after N requests to limit how much data any single token sees.

```python
import asyncio
import threading
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field

@dataclass
class UsageBoundedToken:
    api_key: str
    max_requests: int = 1000
    request_count: int = 0
    rotation_count: int = 0

    def should_rotate(self) -> bool:
        return self.request_count >= self.max_requests

    def increment(self):
        self.request_count += 1

class UsageBoundedAgent:
    def __init__(self, max_requests_per_token: int = 1000):
        self._state = UsageBoundedToken(
            api_key=self._fetch_key(),
            max_requests=max_requests_per_token
        )
        self._client = AsyncAnthropic(api_key=self._state.api_key)
        self._lock = asyncio.Lock()

    def _fetch_key(self) -> str:
        import os
        return os.environ.get("ANTHROPIC_API_KEY", "")

    async def _rotate_if_needed(self):
        if not self._state.should_rotate():
            return

        async with self._lock:
            if not self._state.should_rotate():
                return  # Another coroutine already rotated

            old_count = self._state.rotation_count
            new_key = self._fetch_key()
            self._state = UsageBoundedToken(
                api_key=new_key,
                max_requests=self._state.max_requests,
                rotation_count=old_count + 1
            )
            self._client = AsyncAnthropic(api_key=new_key)
            print(f"[Security] Token rotated after {self._state.max_requests} requests "
                  f"(total rotations: {self._state.rotation_count})")

    async def call(self, prompt: str) -> str:
        await self._rotate_if_needed()
        self._state.increment()

        response = await self._client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text

    @property
    def stats(self) -> dict:
        return {
            "requests_on_current_token": self._state.request_count,
            "total_rotations": self._state.rotation_count,
            "max_per_token": self._state.max_requests,
        }

async def demo_usage_rotation():
    # Rotate every 3 requests for demo
    agent = UsageBoundedAgent(max_requests_per_token=3)

    for i in range(10):
        await agent.call(f"Request {i}")

    print(f"Stats: {agent.stats}")

asyncio.run(demo_usage_rotation())
```

### Option 3: Proactive Vault-Backed Rotation

Pull fresh credentials from HashiCorp Vault or AWS Secrets Manager before each major task boundary.

```python
import asyncio
import os
import time
from anthropic import AsyncAnthropic
from dataclasses import dataclass
from typing import Protocol

class SecretsBackend(Protocol):
    async def get_secret(self, secret_id: str) -> str: ...
    async def rotate_secret(self, secret_id: str) -> str: ...

class EnvSecretsBackend:
    """Fallback: secrets from environment (dev/test only)."""
    async def get_secret(self, secret_id: str) -> str:
        return os.environ.get(secret_id, "")

    async def rotate_secret(self, secret_id: str) -> str:
        # In production: trigger vault rotation, return new secret
        await asyncio.sleep(0.05)
        return os.environ.get(secret_id, "")

@dataclass
class SecretLease:
    value: str
    lease_id: str
    renewable_at: float  # Unix timestamp

    def needs_renewal(self) -> bool:
        return time.time() >= self.renewable_at

class VaultBackedAgent:
    SECRET_ID = "anthropic/api_key"
    LEASE_DURATION = 3600  # 1 hour
    RENEW_BEFORE = 300     # Renew 5 minutes before expiry

    def __init__(self, backend: SecretsBackend):
        self._backend = backend
        self._lease: SecretLease | None = None
        self._client: AsyncAnthropic | None = None
        self._lock = asyncio.Lock()

    async def _get_or_renew_lease(self) -> SecretLease:
        if self._lease and not self._lease.needs_renewal():
            return self._lease

        async with self._lock:
            if self._lease and not self._lease.needs_renewal():
                return self._lease

            if self._lease:
                # Renew existing lease
                new_value = await self._backend.rotate_secret(self.SECRET_ID)
                action = "renewed"
            else:
                # Initial fetch
                new_value = await self._backend.get_secret(self.SECRET_ID)
                action = "fetched"

            import uuid
            self._lease = SecretLease(
                value=new_value,
                lease_id=str(uuid.uuid4()),
                renewable_at=time.time() + self.LEASE_DURATION - self.RENEW_BEFORE
            )
            self._client = AsyncAnthropic(api_key=new_value)
            print(f"[Vault] Credential {action} (lease: {self._lease.lease_id[:8]})")
            return self._lease

    async def run_task(self, prompt: str) -> str:
        """Called at task boundaries — always ensures fresh credentials."""
        await self._get_or_renew_lease()
        response = await self._client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text

async def demo_vault_backed():
    backend = EnvSecretsBackend()
    agent = VaultBackedAgent(backend)

    tasks = [
        "Summarize: token rotation improves security.",
        "List 3 benefits of credential rotation.",
        "What is a lease in secrets management?",
    ]

    for task in tasks:
        result = await agent.run_task(task)
        print(f"Task result: {result.strip()[:80]}")

asyncio.run(demo_vault_backed())
```

### Option 4: Background Rotation with Zero-Downtime Handoff

Rotate tokens in the background while in-flight requests complete on the old token.

```python
import asyncio
import os
import time
from anthropic import AsyncAnthropic
from dataclasses import dataclass

@dataclass
class TokenSlot:
    client: AsyncAnthropic
    created_at: float
    in_flight: int = 0

class ZeroDowntimeRotator:
    """
    Maintains two token slots: active and draining.
    New requests use active. Old requests complete on draining.
    """
    def __init__(self, rotation_interval: float = 3600.0):
        self._active: TokenSlot | None = None
        self._draining: TokenSlot | None = None
        self._interval = rotation_interval
        self._lock = asyncio.Lock()
        self._rotation_task: asyncio.Task | None = None

    async def start(self):
        key = os.environ.get("ANTHROPIC_API_KEY", "")
        self._active = TokenSlot(
            client=AsyncAnthropic(api_key=key),
            created_at=time.monotonic()
        )
        self._rotation_task = asyncio.create_task(self._rotation_loop())

    async def stop(self):
        if self._rotation_task:
            self._rotation_task.cancel()
        # Wait for draining slot to complete
        if self._draining:
            while self._draining.in_flight > 0:
                await asyncio.sleep(0.1)

    async def _fetch_new_key(self) -> str:
        await asyncio.sleep(0.05)  # Simulate secrets manager call
        return os.environ.get("ANTHROPIC_API_KEY", "")

    async def _rotation_loop(self):
        while True:
            await asyncio.sleep(self._interval)
            await self._rotate()

    async def _rotate(self):
        async with self._lock:
            new_key = await self._fetch_new_key()
            new_slot = TokenSlot(
                client=AsyncAnthropic(api_key=new_key),
                created_at=time.monotonic()
            )
            # Move active to draining, new slot becomes active
            self._draining = self._active
            self._active = new_slot
            print(f"[Rotation] Zero-downtime handoff complete. "
                  f"Draining slot has {self._draining.in_flight} in-flight requests.")

    async def create_message(self, **kwargs) -> str:
        async with self._lock:
            slot = self._active

        slot.in_flight += 1
        try:
            response = await slot.client.messages.create(**kwargs)
            return response.content[0].text
        finally:
            slot.in_flight -= 1
            # Clean up draining slot when empty
            if self._draining and self._draining.in_flight == 0:
                self._draining = None

async def demo_zero_downtime():
    rotator = ZeroDowntimeRotator(rotation_interval=2.0)  # Short for demo
    await rotator.start()

    async def make_request(i: int):
        result = await rotator.create_message(
            model="claude-haiku-4-5-20251001",
            max_tokens=50,
            messages=[{"role": "user", "content": f"Request {i}: say OK"}]
        )
        print(f"  Request {i} completed: {result.strip()[:30]}")

    # Simulate concurrent requests spanning a rotation
    tasks = [make_request(i) for i in range(6)]
    await asyncio.gather(*tasks)

    await asyncio.sleep(2.5)  # Let rotation happen
    tasks = [make_request(i) for i in range(6, 9)]
    await asyncio.gather(*tasks)

    await rotator.stop()
    print("Zero-downtime rotation demo complete.")

asyncio.run(demo_zero_downtime())
```

### Option 5: Scope-Limited Short-Lived Tokens

Issue minimal-scope tokens for each agent subtask and revoke them on completion.

```python
import asyncio
import os
import uuid
import time
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field

@dataclass
class ScopedToken:
    token_id: str
    api_key: str
    scope: str          # "read-only", "tool-user", "full"
    expires_at: float
    revoked: bool = False

    def is_valid(self) -> bool:
        return not self.revoked and time.time() < self.expires_at

class TokenIssuer:
    """Issues and tracks short-lived scoped tokens."""

    SCOPE_PERMISSIONS = {
        "read-only": ["messages.create"],
        "tool-user": ["messages.create", "tools.invoke"],
        "full": ["messages.create", "tools.invoke", "files.read"],
    }

    def __init__(self):
        self._tokens: dict[str, ScopedToken] = {}

    async def issue(self, scope: str, ttl_seconds: float = 300.0) -> ScopedToken:
        token = ScopedToken(
            token_id=str(uuid.uuid4()),
            api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
            scope=scope,
            expires_at=time.time() + ttl_seconds,
        )
        self._tokens[token.token_id] = token
        print(f"[TokenIssuer] Issued {scope} token {token.token_id[:8]} (TTL={ttl_seconds}s)")
        return token

    def revoke(self, token_id: str):
        if token_id in self._tokens:
            self._tokens[token_id].revoked = True
            print(f"[TokenIssuer] Revoked token {token_id[:8]}")

    def active_count(self) -> int:
        return sum(1 for t in self._tokens.values() if t.is_valid())

class ScopedAgent:
    def __init__(self, issuer: TokenIssuer):
        self._issuer = issuer

    async def run_subtask(self, task: str, scope: str = "read-only") -> str:
        token = await self._issuer.issue(scope, ttl_seconds=60.0)
        try:
            if not token.is_valid():
                raise ValueError(f"Token {token.token_id} is not valid")

            client = AsyncAnthropic(api_key=token.api_key)
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=150,
                messages=[{"role": "user", "content": task}]
            )
            return response.content[0].text
        finally:
            # Always revoke token when subtask completes
            self._issuer.revoke(token.token_id)

async def demo_scoped_tokens():
    issuer = TokenIssuer()
    agent = ScopedAgent(issuer)

    subtasks = [
        ("Summarize the concept of least-privilege access.", "read-only"),
        ("What tools would a security agent need?", "tool-user"),
        ("List 3 zero-trust principles.", "read-only"),
    ]

    results = await asyncio.gather(*[
        agent.run_subtask(task, scope) for task, scope in subtasks
    ])

    for (task, scope), result in zip(subtasks, results):
        print(f"\n[{scope}] {task[:50]}")
        print(f"  → {result.strip()[:100]}")

    print(f"\nActive tokens after completion: {issuer.active_count()} (should be 0)")

asyncio.run(demo_scoped_tokens())
```

### Option 6: Audit-Logged Rotation with Compliance Report

Track every rotation event with cryptographic proof for SOC 2 / compliance audits.

```python
import asyncio
import hashlib
import json
import os
import time
import uuid
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field
from pathlib import Path

AUDIT_LOG = Path("token_rotation_audit.jsonl")

@dataclass
class RotationEvent:
    event_id: str
    timestamp: float
    reason: str                    # "scheduled", "usage_limit", "manual", "compromise"
    old_token_hash: str            # SHA-256 of old token (never store raw)
    new_token_hash: str
    rotated_by: str               # agent ID or service name
    success: bool
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "timestamp": self.timestamp,
            "iso_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.timestamp)),
            "reason": self.reason,
            "old_token_hash": self.old_token_hash,
            "new_token_hash": self.new_token_hash,
            "rotated_by": self.rotated_by,
            "success": self.success,
            "metadata": self.metadata,
        }

def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:16]  # Prefix only

def log_rotation(event: RotationEvent):
    with open(AUDIT_LOG, "a") as f:
        f.write(json.dumps(event.to_dict()) + "\n")

class AuditedRotatingAgent:
    def __init__(self, agent_id: str, rotation_interval: float = 3600.0):
        self._agent_id = agent_id
        self._interval = rotation_interval
        self._current_key = os.environ.get("ANTHROPIC_API_KEY", "")
        self._client = AsyncAnthropic(api_key=self._current_key)
        self._request_count = 0
        self._rotation_count = 0

    async def _rotate(self, reason: str):
        old_key = self._current_key
        try:
            # Fetch new key from secrets manager
            await asyncio.sleep(0.05)
            new_key = os.environ.get("ANTHROPIC_API_KEY", "")

            self._current_key = new_key
            self._client = AsyncAnthropic(api_key=new_key)
            self._rotation_count += 1

            event = RotationEvent(
                event_id=str(uuid.uuid4()),
                timestamp=time.time(),
                reason=reason,
                old_token_hash=hash_token(old_key),
                new_token_hash=hash_token(new_key),
                rotated_by=self._agent_id,
                success=True,
                metadata={"rotation_number": self._rotation_count,
                          "requests_before_rotation": self._request_count}
            )
            log_rotation(event)
            print(f"[Audit] Token rotated: reason={reason}, event={event.event_id[:8]}")
        except Exception as e:
            event = RotationEvent(
                event_id=str(uuid.uuid4()),
                timestamp=time.time(),
                reason=reason,
                old_token_hash=hash_token(old_key),
                new_token_hash="",
                rotated_by=self._agent_id,
                success=False,
                metadata={"error": str(e)}
            )
            log_rotation(event)
            raise

    async def complete(self, prompt: str) -> str:
        self._request_count += 1
        response = await self._client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text

    def compliance_report(self) -> dict:
        events = []
        if AUDIT_LOG.exists():
            events = [json.loads(l) for l in AUDIT_LOG.read_text().splitlines() if l.strip()]

        agent_events = [e for e in events if e["rotated_by"] == self._agent_id]
        return {
            "agent_id": self._agent_id,
            "total_rotations": len(agent_events),
            "successful_rotations": sum(1 for e in agent_events if e["success"]),
            "rotation_reasons": {r: sum(1 for e in agent_events if e["reason"] == r)
                               for r in set(e["reason"] for e in agent_events)},
            "last_rotation": agent_events[-1]["iso_time"] if agent_events else None,
        }

async def demo_audited_rotation():
    agent = AuditedRotatingAgent("agent-prod-001", rotation_interval=3600.0)

    # Simulate scheduled rotation
    await agent._rotate("scheduled")
    await agent.complete("What is security compliance?")
    await agent.complete("Explain SOC 2 Type II.")

    # Simulate emergency rotation after suspected compromise
    await agent._rotate("compromise")
    await agent.complete("What is token rotation?")

    report = agent.compliance_report()
    print(f"\nCompliance report:\n{json.dumps(report, indent=2)}")

asyncio.run(demo_audited_rotation())
```

## Comparison

| Approach | Rotation Trigger | Zero Downtime | Audit Trail | Complexity |
|---|---|---|---|---|
| Time-Based Rotation | Schedule (TTL) | Near-zero | Basic log | Low |
| Usage-Count Rotation | Request count | Near-zero | Basic log | Low |
| Vault-Backed Rotation | Task boundary | Near-zero | Vault audit | Medium |
| Zero-Downtime Handoff | Schedule + draining | True zero | None | High |
| Scope-Limited Tokens | Per-subtask | Yes (new token) | Per-token | Medium |
| Audit-Logged Rotation | Any trigger | Near-zero | Full compliance | Medium |

**Choose Time-Based Rotation** as the minimum viable security practice—even a 1-hour TTL dramatically limits blast radius. **Choose Vault-Backed Rotation** when deploying in regulated environments that require centralized secrets management. **Choose Audit-Logged Rotation** when you need to produce evidence of credential hygiene for SOC 2, ISO 27001, or similar compliance frameworks.
