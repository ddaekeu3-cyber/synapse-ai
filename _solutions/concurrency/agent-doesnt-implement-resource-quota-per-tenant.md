---
layout: solution
title: "Agent Doesn't Implement Resource Quota Per Tenant"
category: concurrency
description: "Enforce per-tenant limits on concurrent requests, token consumption, and model calls to prevent any single tenant from monopolizing shared agent infrastructure."
tags: [multi-tenant, quota, resource-limits, fairness, isolation]
---

# Agent Doesn't Implement Resource Quota Per Tenant

## Problem

In multi-tenant agent deployments, one high-volume tenant can exhaust shared concurrency limits, rate-limit budgets, and token quotas—causing service degradation or complete unavailability for all other tenants. Without per-tenant quotas, the system has no fairness guarantees.

## Solution Options

### Option 1: Per-Tenant Concurrent Request Semaphore

```python
import anthropic
import asyncio
from dataclasses import dataclass, field

async_client = anthropic.AsyncAnthropic()

@dataclass
class TenantQuota:
    tenant_id: str
    max_concurrent: int
    semaphore: asyncio.Semaphore = field(init=False)
    active_requests: int = 0
    total_requests: int = 0
    rejected_requests: int = 0

    def __post_init__(self):
        self.semaphore = asyncio.Semaphore(self.max_concurrent)

TENANT_CONFIGS = {
    "enterprise_a": TenantQuota("enterprise_a", max_concurrent=8),
    "startup_b":    TenantQuota("startup_b",    max_concurrent=3),
    "free_tier_c":  TenantQuota("free_tier_c",  max_concurrent=1),
}

async def tenant_request(tenant_id: str, prompt: str, timeout: float = 10.0) -> str | None:
    quota = TENANT_CONFIGS.get(tenant_id)
    if not quota:
        raise ValueError(f"Unknown tenant: {tenant_id}")

    try:
        await asyncio.wait_for(quota.semaphore.acquire(), timeout=timeout)
    except asyncio.TimeoutError:
        quota.rejected_requests += 1
        print(f"  [{tenant_id}] REJECTED: concurrency limit ({quota.max_concurrent}) reached")
        return None

    quota.active_requests += 1
    quota.total_requests += 1
    try:
        resp = await async_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{"role": "user", "content": prompt}]
        )
        return resp.content[0].text
    finally:
        quota.active_requests -= 1
        quota.semaphore.release()

async def simulate_load():
    # Simulate concurrent requests from multiple tenants
    tasks = []
    for i in range(5):
        tasks.append(tenant_request("enterprise_a", f"Enterprise query #{i}"))
    for i in range(4):
        tasks.append(tenant_request("startup_b", f"Startup query #{i}"))
    for i in range(3):
        tasks.append(tenant_request("free_tier_c", f"Free query #{i}"))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    ok = sum(1 for r in results if isinstance(r, str) and r)
    print(f"\nCompleted: {ok}/{len(tasks)} requests")

    for tenant_id, quota in TENANT_CONFIGS.items():
        print(f"  {tenant_id}: total={quota.total_requests} rejected={quota.rejected_requests}")

asyncio.run(simulate_load())

# Expected Token Savings: quota enforcement prevents runaway tenants from burning shared budget
# Environment: SaaS API platforms, shared agent infrastructure, multi-org deployments
```

### Option 2: Token Quota Tracker with Burst and Sustained Limits

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field

async_client = anthropic.AsyncAnthropic()

@dataclass
class TokenQuota:
    tenant_id: str
    tokens_per_minute: int       # sustained rate
    burst_tokens: int            # burst allowance
    _minute_usage: int = 0
    _burst_remaining: int = 0
    _window_start: float = field(default_factory=time.time)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def __post_init__(self):
        self._burst_remaining = self.burst_tokens

    async def check_and_consume(self, estimated_tokens: int) -> tuple[bool, str]:
        async with self._lock:
            now = time.time()
            # Reset minute window
            if now - self._window_start >= 60:
                self._minute_usage = 0
                self._window_start = now

            # Check sustained limit
            if self._minute_usage + estimated_tokens > self.tokens_per_minute:
                # Try burst allowance
                if self._burst_remaining >= estimated_tokens:
                    self._burst_remaining -= estimated_tokens
                    return True, f"burst (remaining={self._burst_remaining})"
                return False, f"quota exceeded (used={self._minute_usage}/{self.tokens_per_minute})"

            self._minute_usage += estimated_tokens
            return True, f"ok (used={self._minute_usage}/{self.tokens_per_minute})"

TENANT_QUOTAS = {
    "enterprise": TokenQuota("enterprise", tokens_per_minute=50_000, burst_tokens=10_000),
    "startup":    TokenQuota("startup",    tokens_per_minute=10_000, burst_tokens=2_000),
    "free":       TokenQuota("free",       tokens_per_minute=2_000,  burst_tokens=500),
}

async def quota_gated_call(tenant_id: str, prompt: str, max_tokens: int = 256) -> str | None:
    quota = TENANT_QUOTAS.get(tenant_id)
    if not quota:
        return None

    estimated = len(prompt.split()) + max_tokens
    allowed, reason = await quota.check_and_consume(estimated)

    if not allowed:
        print(f"  [{tenant_id}] QUOTA BLOCKED: {reason}")
        return None

    resp = await async_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}]
    )
    print(f"  [{tenant_id}] OK: {reason} | {resp.usage.output_tokens}t out")
    return resp.content[0].text

async def main():
    tasks = [
        quota_gated_call("enterprise", "What is distributed consensus?"),
        quota_gated_call("startup", "Explain Raft algorithm."),
        quota_gated_call("free", "What is a mutex?"),
        quota_gated_call("free", "What is a semaphore?"),  # may hit quota
        quota_gated_call("startup", "Explain Paxos."),
    ]
    await asyncio.gather(*tasks)

asyncio.run(main())

# Expected Token Savings: per-tenant quotas prevent cross-tenant token exhaustion
# Environment: token-budgeted SaaS, metered API services, fair-use enforcement
```

### Option 3: SQLite-Backed Persistent Quota Registry

```python
import anthropic
import sqlite3
import time
import asyncio
from contextlib import contextmanager

async_client = anthropic.AsyncAnthropic()

def init_quota_db(path: str = "/tmp/tenant_quotas.db") -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS quota_config (
            tenant_id TEXT PRIMARY KEY,
            requests_per_hour INTEGER,
            tokens_per_hour INTEGER,
            max_concurrent INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS quota_usage (
            tenant_id TEXT NOT NULL,
            window_start REAL NOT NULL,
            request_count INTEGER DEFAULT 0,
            token_count INTEGER DEFAULT 0,
            PRIMARY KEY (tenant_id, window_start)
        )
    """)
    conn.commit()
    # Seed tenant configs
    tenants = [
        ("enterprise", 1000, 500_000, 10),
        ("startup", 200, 100_000, 4),
        ("free", 20, 10_000, 1),
    ]
    for row in tenants:
        conn.execute("INSERT OR IGNORE INTO quota_config VALUES (?,?,?,?)", row)
    conn.commit()
    return conn

def current_window(interval_seconds: int = 3600) -> float:
    return float(int(time.time() // interval_seconds) * interval_seconds)

def check_quota(conn: sqlite3.Connection, tenant_id: str, estimated_tokens: int) -> tuple[bool, str]:
    window = current_window()
    config = conn.execute(
        "SELECT requests_per_hour, tokens_per_hour FROM quota_config WHERE tenant_id=?",
        (tenant_id,)
    ).fetchone()
    if not config:
        return False, "Unknown tenant"

    max_req, max_tokens = config
    usage = conn.execute(
        "SELECT request_count, token_count FROM quota_usage WHERE tenant_id=? AND window_start=?",
        (tenant_id, window)
    ).fetchone() or (0, 0)

    req_count, token_count = usage
    if req_count >= max_req:
        return False, f"Request quota: {req_count}/{max_req} this hour"
    if token_count + estimated_tokens > max_tokens:
        return False, f"Token quota: {token_count}/{max_tokens} this hour"
    return True, f"ok ({req_count+1}/{max_req} req, {token_count+estimated_tokens}/{max_tokens} tok)"

def consume_quota(conn: sqlite3.Connection, tenant_id: str, tokens_used: int) -> None:
    window = current_window()
    conn.execute("""
        INSERT INTO quota_usage (tenant_id, window_start, request_count, token_count)
        VALUES (?, ?, 1, ?)
        ON CONFLICT(tenant_id, window_start) DO UPDATE SET
            request_count = request_count + 1,
            token_count = token_count + excluded.token_count
    """, (tenant_id, window, tokens_used))
    conn.commit()

conn = init_quota_db()

async def db_quota_call(tenant_id: str, prompt: str) -> str | None:
    estimated = len(prompt.split()) + 200
    allowed, reason = check_quota(conn, tenant_id, estimated)
    if not allowed:
        print(f"  [{tenant_id}] BLOCKED: {reason}")
        return None
    resp = await async_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}]
    )
    actual_tokens = resp.usage.input_tokens + resp.usage.output_tokens
    consume_quota(conn, tenant_id, actual_tokens)
    print(f"  [{tenant_id}] OK: {reason}")
    return resp.content[0].text

async def main():
    tasks = [
        db_quota_call("enterprise", "What is Kubernetes?"),
        db_quota_call("startup", "Explain Docker containers."),
        db_quota_call("free", "What is a container?"),
    ]
    await asyncio.gather(*tasks)

asyncio.run(main())
conn.close()

# Expected Token Savings: persistent quotas survive restarts; accurate cross-process enforcement
# Environment: distributed agent clusters, quota-based billing, multi-process deployments
```

### Option 4: Fair-Share Scheduler with Priority Weights

```python
import anthropic
import asyncio
from dataclasses import dataclass, field
from collections import defaultdict

async_client = anthropic.AsyncAnthropic()

@dataclass
class TenantPriority:
    tenant_id: str
    weight: float          # relative share (enterprise=4, startup=2, free=1)
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    processed: int = 0
    total_tokens: int = 0

TENANTS = {
    "enterprise": TenantPriority("enterprise", weight=4.0),
    "startup":    TenantPriority("startup",    weight=2.0),
    "free":       TenantPriority("free",       weight=1.0),
}

class FairShareScheduler:
    def __init__(self, max_concurrent: int = 4):
        self.max_concurrent = max_concurrent
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.counters: defaultdict[str, float] = defaultdict(float)

    def pick_next_tenant(self, tenants: dict[str, TenantPriority]) -> str | None:
        """Weighted fair queue: pick tenant with lowest counter relative to weight."""
        eligible = [(tid, t) for tid, t in tenants.items() if not t.queue.empty()]
        if not eligible:
            return None
        # Pick tenant with smallest counter/weight ratio (most underserved)
        return min(eligible, key=lambda x: self.counters[x[0]] / x[1].weight)[0]

    async def dispatch(self, tenants: dict[str, TenantPriority]) -> None:
        total_weight = sum(t.weight for t in tenants.values())
        while any(not t.queue.empty() for t in tenants.values()):
            tenant_id = self.pick_next_tenant(tenants)
            if not tenant_id:
                break
            tenant = tenants[tenant_id]
            prompt = await tenant.queue.get()

            async with self.semaphore:
                self.counters[tenant_id] += 1.0 / tenant.weight
                resp = await async_client.messages.create(
                    model="claude-haiku-4-5-20251011",
                    max_tokens=128,
                    messages=[{"role": "user", "content": prompt}]
                )
                tenant.processed += 1
                tenant.total_tokens += resp.usage.output_tokens
                print(f"  [{tenant_id}] processed request {tenant.processed} ({resp.usage.output_tokens}t)")

async def main():
    scheduler = FairShareScheduler(max_concurrent=3)

    # Queue requests from different tenants
    for i in range(4):
        await TENANTS["enterprise"].queue.put(f"Enterprise query #{i}")
    for i in range(4):
        await TENANTS["startup"].queue.put(f"Startup query #{i}")
    for i in range(4):
        await TENANTS["free"].queue.put(f"Free query #{i}")

    print("Dispatching with fair-share scheduling...")
    await scheduler.dispatch(TENANTS)

    print("\n=== Fair Share Results ===")
    total_processed = sum(t.processed for t in TENANTS.values())
    for tid, tenant in TENANTS.items():
        pct = tenant.processed / max(total_processed, 1) * 100
        print(f"  {tid}: {tenant.processed} requests ({pct:.0f}%) weight={tenant.weight}")

asyncio.run(main())

# Expected Token Savings: fair-share ensures all tenants get proportional service; no starvation
# Environment: shared inference infrastructure, multi-tenant SaaS, platform fairness guarantees
```

### Option 5: Tenant Quota with Automatic Tier Downgrade

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field

async_client = anthropic.AsyncAnthropic()

@dataclass
class TenantConfig:
    tenant_id: str
    plan: str
    primary_model: str
    fallback_model: str
    quota_tpm: int          # tokens per minute
    used_tpm: int = 0
    window_start: float = field(default_factory=time.time)
    downgrades: int = 0

    def reset_if_new_window(self) -> None:
        if time.time() - self.window_start >= 60:
            self.used_tpm = 0
            self.window_start = time.time()

    def should_downgrade(self) -> bool:
        self.reset_if_new_window()
        return self.used_tpm > self.quota_tpm * 0.8  # downgrade at 80% usage

    def record_usage(self, tokens: int) -> None:
        self.reset_if_new_window()
        self.used_tpm += tokens

TENANTS = {
    "ent_001": TenantConfig("ent_001", "enterprise", "claude-sonnet-4-6", "claude-haiku-4-5-20251001", quota_tpm=50_000),
    "pro_002": TenantConfig("pro_002", "pro",        "claude-haiku-4-5-20251001", "claude-haiku-4-5-20251001", quota_tpm=10_000),
    "free_003": TenantConfig("free_003","free",      "claude-haiku-4-5-20251001", "claude-haiku-4-5-20251001", quota_tpm=2_000),
}

async def tenant_call(tenant_id: str, prompt: str, max_tokens: int = 256) -> tuple[str, str]:
    config = TENANTS.get(tenant_id)
    if not config:
        raise ValueError(f"Unknown tenant: {tenant_id}")

    if config.should_downgrade():
        model = config.fallback_model
        config.downgrades += 1
        print(f"  [{tenant_id}] DOWNGRADED to {model} (quota pressure {config.used_tpm}/{config.quota_tpm})")
    else:
        model = config.primary_model

    resp = await async_client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}]
    )
    total_tokens = resp.usage.input_tokens + resp.usage.output_tokens
    config.record_usage(total_tokens)
    return resp.content[0].text, model

async def main():
    tasks = [
        tenant_call("ent_001", "Explain microservices in depth."),
        tenant_call("ent_001", "Describe event-driven architecture."),
        tenant_call("pro_002", "What is a load balancer?"),
        tenant_call("free_003", "What is caching?"),
    ]
    results = await asyncio.gather(*tasks)
    for (text, model) in results:
        print(f"  [{model}] {text[:60]}...")

    print("\n=== Downgrade Stats ===")
    for tid, config in TENANTS.items():
        print(f"  {tid}: used={config.used_tpm} quota={config.quota_tpm} downgrades={config.downgrades}")

asyncio.run(main())

# Expected Token Savings: automatic downgrade to haiku saves ~60% cost during quota pressure
# Environment: multi-tier SaaS, graceful degradation, cost-aware model routing
```

### Option 6: Cross-Service Tenant Quota with Distributed Counter

```python
import anthropic
import sqlite3
import asyncio
import time
from dataclasses import dataclass

async_client = anthropic.AsyncAnthropic()

# Simulates a distributed counter (Redis in production) using SQLite
def init_distributed_counter(path: str = "/tmp/quota_counter.db") -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS counters (
            tenant_id TEXT NOT NULL,
            service TEXT NOT NULL,
            window INTEGER NOT NULL,
            count INTEGER DEFAULT 0,
            tokens INTEGER DEFAULT 0,
            PRIMARY KEY (tenant_id, service, window)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tenant_limits (
            tenant_id TEXT PRIMARY KEY,
            global_rpm INTEGER,
            global_tpm INTEGER
        )
    """)
    conn.executemany(
        "INSERT OR IGNORE INTO tenant_limits VALUES (?,?,?)",
        [("ent_a", 100, 200_000), ("pro_b", 30, 50_000), ("free_c", 5, 5_000)]
    )
    conn.commit()
    return conn

def atomic_increment(conn: sqlite3.Connection, tenant_id: str, service: str,
                      token_count: int, window_seconds: int = 60) -> tuple[int, int]:
    """Returns (new_request_count, new_token_count) for this window."""
    window = int(time.time() // window_seconds)
    conn.execute("""
        INSERT INTO counters (tenant_id, service, window, count, tokens) VALUES (?,?,?,1,?)
        ON CONFLICT(tenant_id, service, window) DO UPDATE SET
            count = count + 1, tokens = tokens + excluded.tokens
    """, (tenant_id, service, window, token_count))
    conn.commit()
    row = conn.execute(
        "SELECT count, tokens FROM counters WHERE tenant_id=? AND service=? AND window=?",
        (tenant_id, service, window)
    ).fetchone()
    return row if row else (1, token_count)

def check_global_quota(conn: sqlite3.Connection, tenant_id: str, estimated_tokens: int,
                         window_seconds: int = 60) -> tuple[bool, str]:
    limits = conn.execute(
        "SELECT global_rpm, global_tpm FROM tenant_limits WHERE tenant_id=?",
        (tenant_id,)
    ).fetchone()
    if not limits:
        return False, "Unknown tenant"

    max_rpm, max_tpm = limits
    window = int(time.time() // window_seconds)
    # Aggregate across all services
    totals = conn.execute(
        "SELECT SUM(count), SUM(tokens) FROM counters WHERE tenant_id=? AND window=?",
        (tenant_id, window)
    ).fetchone()
    req_count = (totals[0] or 0)
    token_count = (totals[1] or 0)

    if req_count >= max_rpm:
        return False, f"Global RPM quota: {req_count}/{max_rpm}"
    if token_count + estimated_tokens > max_tpm:
        return False, f"Global TPM quota: {token_count}/{max_tpm}"
    return True, f"ok (req={req_count+1}/{max_rpm}, tok={token_count+estimated_tokens}/{max_tpm})"

conn = init_distributed_counter()

async def distributed_quota_call(tenant_id: str, service: str, prompt: str) -> str | None:
    estimated = len(prompt.split()) + 150
    allowed, reason = check_global_quota(conn, tenant_id, estimated)
    if not allowed:
        print(f"  [{tenant_id}/{service}] BLOCKED: {reason}")
        return None

    resp = await async_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        messages=[{"role": "user", "content": prompt}]
    )
    actual = resp.usage.input_tokens + resp.usage.output_tokens
    req_c, tok_c = atomic_increment(conn, tenant_id, service, actual)
    print(f"  [{tenant_id}/{service}] OK: {reason} -> actual_tok={actual}")
    return resp.content[0].text

async def main():
    tasks = [
        distributed_quota_call("ent_a", "chat", "What is Kubernetes?"),
        distributed_quota_call("ent_a", "search", "Explain Docker."),
        distributed_quota_call("pro_b", "chat", "What is Helm?"),
        distributed_quota_call("free_c", "chat", "What is a pod?"),
        distributed_quota_call("free_c", "chat", "What is a node?"),
    ]
    await asyncio.gather(*tasks)

asyncio.run(main())
conn.close()

# Expected Token Savings: global quota prevents sum-of-services exceeding per-tenant limits
# Environment: microservice architectures, distributed agent fleets, multi-service platforms
```

## Comparison

| Option | Quota Type | Persistence | Auto-Downgrade | Best For |
|--------|-----------|-------------|----------------|----------|
| 1 | Concurrent requests | No | No | Burst isolation |
| 2 | Token rate (burst+sustained) | No | No | Token-metered billing |
| 3 | Request + token per hour | SQLite | No | Persistent multi-process |
| 4 | Fair-share scheduling | No | No | Proportional fairness |
| 5 | Token quota + model downgrade | No | Yes | Cost-adaptive degradation |
| 6 | Distributed global counter | SQLite | No | Multi-service coordination |
