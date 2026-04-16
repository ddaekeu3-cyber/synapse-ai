---
title: "Agent Doesn't Implement Multi-Tenant Isolation for Concurrent Requests"
description: "AI agents serving multiple tenants from a shared process risk state leakage, resource contention, and cross-tenant data exposure. Learn six patterns for robust multi-tenant isolation that keeps each tenant's state, resources, and data strictly separate."
date: 2026-04-16
difficulty: advanced
category: concurrency
slug: agent-doesnt-implement-multi-tenant-isolation-for-concurrent-requests
tags: [multi-tenant, isolation, concurrency, security, resource-quotas, state-management]
symptoms:
  - "Tenant A's conversation context leaks into Tenant B's responses"
  - "One tenant's heavy workload starves other tenants of CPU and API quota"
  - "Shared global state is mutated by concurrent requests from different tenants"
  - "A single tenant triggering an error crashes the agent for all other tenants"
  - "Tool execution for one tenant can read files written by another tenant"
---

## The Problem

AI agents often run as a shared service handling requests from multiple tenants (users, organizations, or API clients) concurrently. Without explicit isolation, state bleeds across tenants — a bug in one tenant's context management can expose another's conversation history, a resource-hungry tenant can exhaust the shared API quota, and a crash in one tenant's tool execution can cascade to all others.

Multi-tenant isolation means each tenant gets its own logical execution environment: separate state, separate resource budgets, and separate error domains.

```python
# ❌ Shared global state — tenant state bleeds
conversation_history = []  # Global! All tenants share this

async def handle(tenant_id: str, message: str):
    conversation_history.append({"role": "user", "content": message})
    # LEAK: Tenant B sees Tenant A's messages

# ✓ Per-tenant isolated state
async def handle(tenant_id: str, message: str):
    async with TenantContext(tenant_id) as ctx:
        ctx.history.append({"role": "user", "content": message})
        # Fully isolated: each tenant's history is independent
```

---

## Solution 1: Per-Tenant Context Manager with Scoped State

Use a context manager that creates an isolated execution scope per tenant, ensuring all state mutations are scoped to the current tenant and cleaned up on exit.

```python
import asyncio
import contextvars
import time
from dataclasses import dataclass, field
from typing import Any
from contextlib import asynccontextmanager


# Context variable — value is per-coroutine, not shared across tenants
_current_tenant: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_current_tenant", default=None
)


@dataclass
class TenantState:
    tenant_id: str
    conversation_history: list[dict] = field(default_factory=list)
    tool_results: list[dict] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)

    def touch(self):
        self.last_active = time.time()


class TenantStateRegistry:
    """Thread-safe registry of per-tenant state."""

    def __init__(self, ttl_seconds: float = 3600.0):
        self._states: dict[str, TenantState] = {}
        self._lock = asyncio.Lock()
        self.ttl = ttl_seconds

    async def get_or_create(self, tenant_id: str) -> TenantState:
        async with self._lock:
            if tenant_id not in self._states:
                self._states[tenant_id] = TenantState(tenant_id=tenant_id)
            state = self._states[tenant_id]
            state.touch()
            return state

    async def evict_expired(self):
        async with self._lock:
            now = time.time()
            expired = [
                tid for tid, state in self._states.items()
                if now - state.last_active > self.ttl
            ]
            for tid in expired:
                del self._states[tid]
            return len(expired)

    def tenant_count(self) -> int:
        return len(self._states)


_registry = TenantStateRegistry()


@asynccontextmanager
async def TenantContext(tenant_id: str):
    """
    Async context manager that:
    1. Sets the current tenant context variable
    2. Provides isolated state for this tenant
    3. Resets the context variable on exit
    """
    token = _current_tenant.set(tenant_id)
    state = await _registry.get_or_create(tenant_id)
    try:
        yield state
    finally:
        _current_tenant.reset(token)


def get_current_tenant() -> str | None:
    """Get the tenant ID for the current coroutine. Returns None if not in a tenant context."""
    return _current_tenant.get()


def require_tenant() -> str:
    """Get current tenant ID or raise if not in a tenant context."""
    tid = _current_tenant.get()
    if tid is None:
        raise RuntimeError("Operation requires a tenant context")
    return tid
```

---

## Solution 2: Per-Tenant Resource Quotas with Semaphores

Enforce resource limits per tenant: maximum concurrent LLM calls, token budget per hour, and maximum tool execution slots. Prevents one tenant from starving others.

```python
import asyncio
import time
from dataclasses import dataclass, field
from collections import defaultdict, deque


@dataclass
class TenantQuota:
    tenant_id: str
    max_concurrent_llm_calls: int = 3
    max_tokens_per_hour: int = 500_000
    max_concurrent_tool_calls: int = 5
    max_requests_per_minute: int = 20


@dataclass
class QuotaUsage:
    token_timestamps: deque = field(default_factory=deque)  # (timestamp, tokens)
    request_timestamps: deque = field(default_factory=deque)


class TenantResourceManager:
    """Enforces per-tenant resource quotas using semaphores and sliding windows."""

    def __init__(self):
        self._quotas: dict[str, TenantQuota] = {}
        self._llm_semaphores: dict[str, asyncio.Semaphore] = {}
        self._tool_semaphores: dict[str, asyncio.Semaphore] = {}
        self._usage: dict[str, QuotaUsage] = defaultdict(QuotaUsage)
        self._default_quota = TenantQuota(tenant_id="default")

    def register_tenant(self, quota: TenantQuota):
        self._quotas[quota.tenant_id] = quota
        self._llm_semaphores[quota.tenant_id] = asyncio.Semaphore(quota.max_concurrent_llm_calls)
        self._tool_semaphores[quota.tenant_id] = asyncio.Semaphore(quota.max_concurrent_tool_calls)

    def _get_quota(self, tenant_id: str) -> TenantQuota:
        return self._quotas.get(tenant_id, self._default_quota)

    def _check_token_budget(self, tenant_id: str, tokens: int) -> bool:
        quota = self._get_quota(tenant_id)
        usage = self._usage[tenant_id]
        now = time.time()
        # Evict entries older than 1 hour
        cutoff = now - 3600
        while usage.token_timestamps and usage.token_timestamps[0][0] < cutoff:
            usage.token_timestamps.popleft()
        tokens_used = sum(t for _, t in usage.token_timestamps)
        return tokens_used + tokens <= quota.max_tokens_per_hour

    def _record_tokens(self, tenant_id: str, tokens: int):
        self._usage[tenant_id].token_timestamps.append((time.time(), tokens))

    def _check_request_rate(self, tenant_id: str) -> bool:
        quota = self._get_quota(tenant_id)
        usage = self._usage[tenant_id]
        now = time.time()
        cutoff = now - 60
        while usage.request_timestamps and usage.request_timestamps[0] < cutoff:
            usage.request_timestamps.popleft()
        return len(usage.request_timestamps) < quota.max_requests_per_minute

    def _record_request(self, tenant_id: str):
        self._usage[tenant_id].request_timestamps.append(time.time())

    async def llm_call(self, tenant_id: str, estimated_tokens: int = 1000):
        """Context manager: acquire LLM call slot for this tenant."""
        if not self._check_request_rate(tenant_id):
            raise QuotaExceededError(f"Tenant {tenant_id}: request rate limit exceeded")
        if not self._check_token_budget(tenant_id, estimated_tokens):
            raise QuotaExceededError(f"Tenant {tenant_id}: hourly token budget exceeded")

        sem = self._llm_semaphores.get(tenant_id)
        if sem is None:
            # Auto-register with default quota
            self.register_tenant(TenantQuota(tenant_id=tenant_id))
            sem = self._llm_semaphores[tenant_id]

        self._record_request(tenant_id)
        return self._LLMSlot(sem, self, tenant_id, estimated_tokens)

    class _LLMSlot:
        def __init__(self, sem, manager, tenant_id, tokens):
            self._sem = sem
            self._mgr = manager
            self._tenant = tenant_id
            self._tokens = tokens

        async def __aenter__(self):
            await self._sem.acquire()
            return self

        async def __aexit__(self, *_):
            self._mgr._record_tokens(self._tenant, self._tokens)
            self._sem.release()

    def usage_report(self, tenant_id: str) -> dict:
        usage = self._usage[tenant_id]
        quota = self._get_quota(tenant_id)
        now = time.time()
        tokens_1h = sum(t for ts, t in usage.token_timestamps if now - ts < 3600)
        requests_1m = sum(1 for ts in usage.request_timestamps if now - ts < 60)
        return {
            "tenant_id": tenant_id,
            "tokens_used_last_hour": tokens_1h,
            "token_budget": quota.max_tokens_per_hour,
            "token_utilization": tokens_1h / quota.max_tokens_per_hour,
            "requests_last_minute": requests_1m,
            "request_limit_per_minute": quota.max_requests_per_minute,
        }


class QuotaExceededError(Exception):
    pass
```

---

## Solution 3: Tenant-Scoped Tool Execution Sandbox

Tool calls (file I/O, subprocess, database queries) must be scoped to the tenant making the request. This sandbox enforces path prefixes, database row-level security, and execution namespace isolation.

```python
import os
import asyncio
from pathlib import Path
from dataclasses import dataclass


@dataclass
class TenantSandbox:
    tenant_id: str
    workspace_root: Path         # e.g. /var/agent_workspaces/tenant-123/
    allowed_db_schemas: list[str]  # e.g. ["tenant_123"]
    max_file_size_bytes: int = 10 * 1024 * 1024  # 10 MB
    max_subprocess_timeout: float = 30.0


class SandboxedToolExecutor:
    """
    Wraps tool execution to enforce per-tenant isolation:
    - File operations confined to tenant workspace
    - Database queries restricted to tenant schema
    - Subprocess execution with tenant-specific environment
    """

    def __init__(self, base_workspace: str = "/var/agent_workspaces"):
        self.base_workspace = Path(base_workspace)

    def get_sandbox(self, tenant_id: str) -> TenantSandbox:
        workspace = self.base_workspace / f"tenant-{tenant_id}"
        workspace.mkdir(parents=True, exist_ok=True)
        return TenantSandbox(
            tenant_id=tenant_id,
            workspace_root=workspace,
            allowed_db_schemas=[f"tenant_{tenant_id}"],
        )

    def _safe_path(self, sandbox: TenantSandbox, relative_path: str) -> Path:
        """Resolve path and verify it stays within tenant workspace."""
        resolved = (sandbox.workspace_root / relative_path).resolve()
        try:
            resolved.relative_to(sandbox.workspace_root.resolve())
        except ValueError:
            raise SecurityError(
                f"Path traversal attempt by tenant {sandbox.tenant_id}: "
                f"'{relative_path}' resolves outside workspace"
            )
        return resolved

    async def read_file(self, tenant_id: str, path: str) -> str:
        sandbox = self.get_sandbox(tenant_id)
        safe = self._safe_path(sandbox, path)
        if not safe.exists():
            raise FileNotFoundError(f"File not found: {path}")
        if safe.stat().st_size > sandbox.max_file_size_bytes:
            raise ValueError(f"File too large: {safe.stat().st_size} bytes")
        return safe.read_text()

    async def write_file(self, tenant_id: str, path: str, content: str):
        sandbox = self.get_sandbox(tenant_id)
        safe = self._safe_path(sandbox, path)
        safe.parent.mkdir(parents=True, exist_ok=True)
        if len(content.encode()) > sandbox.max_file_size_bytes:
            raise ValueError("Content too large")
        safe.write_text(content)

    async def execute_query(self, tenant_id: str, query: str,
                            db_connection) -> list[dict]:
        """Execute DB query with row-level security for this tenant."""
        sandbox = self.get_sandbox(tenant_id)

        # Validate query only touches allowed schemas
        import re
        schema_refs = re.findall(r'\b(\w+)\.\w+', query)
        for schema in schema_refs:
            if schema not in sandbox.allowed_db_schemas and schema != "public":
                raise SecurityError(
                    f"Tenant {tenant_id} attempted to access schema '{schema}'"
                )

        # Prepend SET search_path to enforce schema isolation
        schema = sandbox.allowed_db_schemas[0]
        full_query = f"SET search_path = {schema}; {query}"
        return await db_connection.fetch(full_query)

    async def run_subprocess(self, tenant_id: str, command: list[str]) -> tuple[str, str]:
        """Run subprocess in tenant workspace with restricted environment."""
        sandbox = self.get_sandbox(tenant_id)
        env = {
            "HOME": str(sandbox.workspace_root),
            "TMPDIR": str(sandbox.workspace_root / "tmp"),
            "PATH": "/usr/bin:/bin",
            "TENANT_ID": tenant_id,
        }
        (sandbox.workspace_root / "tmp").mkdir(exist_ok=True)

        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(sandbox.workspace_root),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=sandbox.max_subprocess_timeout,
            )
            return stdout.decode(), stderr.decode()
        except asyncio.TimeoutError:
            proc.kill()
            raise TimeoutError(f"Subprocess timed out for tenant {tenant_id}")


class SecurityError(Exception):
    pass
```

---

## Solution 4: Tenant-Aware Error Isolation with Bulkheads

Prevent errors in one tenant's execution from propagating to other tenants. Each tenant gets its own error domain and circuit breaker.

```python
import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


class TenantCircuitState(Enum):
    CLOSED = "closed"     # Normal operation
    OPEN = "open"         # Tenant's calls are blocked (too many errors)
    HALF_OPEN = "half_open"  # Testing recovery


@dataclass
class TenantCircuit:
    tenant_id: str
    state: TenantCircuitState = TenantCircuitState.CLOSED
    failure_count: int = 0
    last_failure: float = 0.0
    opened_at: float | None = None
    consecutive_successes: int = 0

    failure_threshold: int = 5
    reset_timeout: float = 60.0
    success_threshold: int = 2  # Successes needed to close from half-open


class TenantBulkheadManager:
    """
    Per-tenant circuit breakers and error isolation.
    A cascade failure in Tenant A's tools does NOT affect Tenant B.
    """

    def __init__(self):
        self._circuits: dict[str, TenantCircuit] = {}
        self._tenant_semaphores: dict[str, asyncio.Semaphore] = {}
        self._error_logs: dict[str, list[dict]] = {}

    def _get_circuit(self, tenant_id: str) -> TenantCircuit:
        if tenant_id not in self._circuits:
            self._circuits[tenant_id] = TenantCircuit(tenant_id=tenant_id)
        return self._circuits[tenant_id]

    def _get_semaphore(self, tenant_id: str, max_concurrent: int = 10) -> asyncio.Semaphore:
        if tenant_id not in self._tenant_semaphores:
            self._tenant_semaphores[tenant_id] = asyncio.Semaphore(max_concurrent)
        return self._tenant_semaphores[tenant_id]

    def _check_circuit(self, circuit: TenantCircuit) -> tuple[bool, str]:
        now = time.time()
        if circuit.state == TenantCircuitState.OPEN:
            if now - circuit.opened_at > circuit.reset_timeout:
                circuit.state = TenantCircuitState.HALF_OPEN
                return True, "half_open_probe"
            return False, f"circuit_open:tenant={circuit.tenant_id}"
        return True, "ok"

    def _record_success(self, circuit: TenantCircuit):
        circuit.failure_count = 0
        if circuit.state == TenantCircuitState.HALF_OPEN:
            circuit.consecutive_successes += 1
            if circuit.consecutive_successes >= circuit.success_threshold:
                circuit.state = TenantCircuitState.CLOSED
                circuit.consecutive_successes = 0
                print(f"[bulkhead] Circuit CLOSED for tenant {circuit.tenant_id}")
        elif circuit.state == TenantCircuitState.CLOSED:
            circuit.consecutive_successes += 1

    def _record_failure(self, circuit: TenantCircuit, error: Exception):
        circuit.failure_count += 1
        circuit.last_failure = time.time()
        circuit.consecutive_successes = 0
        if circuit.state == TenantCircuitState.HALF_OPEN:
            circuit.state = TenantCircuitState.OPEN
            circuit.opened_at = time.time()
        elif (circuit.state == TenantCircuitState.CLOSED and
              circuit.failure_count >= circuit.failure_threshold):
            circuit.state = TenantCircuitState.OPEN
            circuit.opened_at = time.time()
            print(f"[bulkhead] Circuit OPENED for tenant {circuit.tenant_id} "
                  f"after {circuit.failure_count} failures")

    async def execute(self, tenant_id: str, operation: Callable,
                      *args, max_concurrent: int = 10, **kwargs) -> Any:
        """Execute operation within tenant's isolated error domain."""
        circuit = self._get_circuit(tenant_id)
        allowed, reason = self._check_circuit(circuit)
        if not allowed:
            raise TenantCircuitOpenError(f"Tenant {tenant_id} circuit open: {reason}")

        sem = self._get_semaphore(tenant_id, max_concurrent)
        async with sem:
            try:
                result = await operation(*args, **kwargs)
                self._record_success(circuit)
                return result
            except Exception as e:
                self._record_failure(circuit, e)
                # Log to tenant-specific error log (not shared)
                if tenant_id not in self._error_logs:
                    self._error_logs[tenant_id] = []
                self._error_logs[tenant_id].append({
                    "timestamp": time.time(),
                    "error": str(e),
                    "circuit_state": circuit.state.value,
                })
                raise  # Re-raise — doesn't affect other tenants

    def tenant_health(self, tenant_id: str) -> dict:
        circuit = self._get_circuit(tenant_id)
        return {
            "tenant_id": tenant_id,
            "circuit_state": circuit.state.value,
            "failure_count": circuit.failure_count,
            "last_failure_ago": (
                time.time() - circuit.last_failure if circuit.last_failure else None
            ),
            "recent_errors": len(self._error_logs.get(tenant_id, [])),
        }


class TenantCircuitOpenError(Exception):
    pass
```

---

## Solution 5: Tenant-Scoped Async Task Registry

Track all async tasks per tenant. When a tenant disconnects or their session expires, cleanly cancel all their in-flight tasks without touching other tenants' work.

```python
import asyncio
import time
import weakref
from dataclasses import dataclass, field


@dataclass
class TenantTask:
    task: asyncio.Task
    tenant_id: str
    task_name: str
    created_at: float = field(default_factory=time.time)
    timeout_at: float | None = None


class TenantTaskRegistry:
    """
    Tracks all async tasks per tenant.
    Supports per-tenant cancellation, timeout enforcement, and task listing.
    """

    def __init__(self, default_timeout_seconds: float = 300.0):
        self._tasks: dict[str, list[TenantTask]] = {}
        self._default_timeout = default_timeout_seconds
        self._lock = asyncio.Lock()

    async def create_task(
        self,
        tenant_id: str,
        coro,
        name: str = "",
        timeout_seconds: float | None = None,
    ) -> asyncio.Task:
        timeout = timeout_seconds or self._default_timeout
        deadline = time.time() + timeout

        task = asyncio.create_task(coro, name=f"{tenant_id}:{name}")

        tenant_task = TenantTask(
            task=task,
            tenant_id=tenant_id,
            task_name=name,
            timeout_at=deadline,
        )

        async with self._lock:
            if tenant_id not in self._tasks:
                self._tasks[tenant_id] = []
            self._tasks[tenant_id].append(tenant_task)

        # Auto-cleanup when done
        task.add_done_callback(
            lambda t: asyncio.create_task(self._cleanup_task(tenant_id, t))
        )

        # Schedule timeout
        asyncio.create_task(self._enforce_timeout(tenant_task))

        return task

    async def _enforce_timeout(self, tenant_task: TenantTask):
        remaining = tenant_task.timeout_at - time.time()
        if remaining > 0:
            await asyncio.sleep(remaining)
        if not tenant_task.task.done():
            tenant_task.task.cancel()
            print(f"[task_registry] Timeout: {tenant_task.tenant_id}:{tenant_task.task_name}")

    async def _cleanup_task(self, tenant_id: str, task: asyncio.Task):
        async with self._lock:
            if tenant_id in self._tasks:
                self._tasks[tenant_id] = [
                    tt for tt in self._tasks[tenant_id] if tt.task is not task
                ]

    async def cancel_tenant(self, tenant_id: str, reason: str = "tenant_cancelled"):
        """Cancel all in-flight tasks for a specific tenant."""
        async with self._lock:
            tenant_tasks = self._tasks.pop(tenant_id, [])

        cancelled = 0
        for tt in tenant_tasks:
            if not tt.task.done():
                tt.task.cancel()
                cancelled += 1

        if cancelled:
            # Wait for all cancellations to propagate
            await asyncio.gather(
                *(tt.task for tt in tenant_tasks if not tt.task.done()),
                return_exceptions=True,
            )
            print(f"[task_registry] Cancelled {cancelled} tasks for tenant {tenant_id}: {reason}")
        return cancelled

    async def cancel_all_tenants(self):
        """Emergency: cancel all tasks for all tenants."""
        async with self._lock:
            all_tenant_ids = list(self._tasks.keys())
        for tid in all_tenant_ids:
            await self.cancel_tenant(tid, "system_shutdown")

    def tenant_task_count(self, tenant_id: str) -> int:
        tasks = self._tasks.get(tenant_id, [])
        return sum(1 for tt in tasks if not tt.task.done())

    def global_summary(self) -> dict:
        return {
            tid: {"active_tasks": sum(1 for tt in tasks if not tt.task.done())}
            for tid, tasks in self._tasks.items()
        }
```

---

## Solution 6: Full Multi-Tenant Agent Orchestrator

Combines all isolation patterns into a single `MultiTenantAgent` class that manages state, resources, sandbox, bulkhead, and task lifecycle per tenant.

```python
import asyncio
import anthropic
from dataclasses import dataclass


class MultiTenantAgent:
    """
    Production multi-tenant agent with full isolation:
    - Per-tenant state (TenantContext)
    - Per-tenant resource quotas (TenantResourceManager)
    - Per-tenant tool sandboxing (SandboxedToolExecutor)
    - Per-tenant error isolation (TenantBulkheadManager)
    - Per-tenant task lifecycle (TenantTaskRegistry)
    """

    def __init__(self, workspace_root: str = "/var/agent_workspaces"):
        self._registry = TenantStateRegistry()
        self._resources = TenantResourceManager()
        self._sandbox = SandboxedToolExecutor(workspace_root)
        self._bulkhead = TenantBulkheadManager()
        self._tasks = TenantTaskRegistry()
        self._client = anthropic.AsyncAnthropic()

    def configure_tenant(self, tenant_id: str, quota: TenantQuota):
        """Register a tenant with custom resource limits."""
        self._resources.register_tenant(quota)

    async def handle_message(
        self, tenant_id: str, message: str, max_tokens: int = 1024
    ) -> str:
        """
        Handle a message from a tenant with full isolation.
        Each tenant's execution is independent and resource-capped.
        """
        # Check and acquire LLM slot for this tenant
        try:
            llm_slot = await self._resources.llm_call(tenant_id, estimated_tokens=max_tokens)
        except QuotaExceededError as e:
            return f"[quota_exceeded] {e}"

        # Execute within tenant's error domain
        async def _process():
            async with llm_slot:
                async with TenantContext(tenant_id) as state:
                    state.conversation_history.append({
                        "role": "user", "content": message
                    })

                    resp = await self._client.messages.create(
                        model="claude-haiku-4-5-20251001",
                        max_tokens=max_tokens,
                        messages=state.conversation_history,
                    )

                    reply = resp.content[0].text
                    state.conversation_history.append({
                        "role": "assistant", "content": reply
                    })
                    return reply

        try:
            return await self._bulkhead.execute(tenant_id, _process)
        except TenantCircuitOpenError:
            return "[service_unavailable] Tenant is experiencing issues. Try again later."
        except Exception as e:
            return f"[error] {str(e)}"

    async def read_tenant_file(self, tenant_id: str, path: str) -> str:
        """Read a file from the tenant's isolated workspace."""
        return await self._sandbox.read_file(tenant_id, path)

    async def write_tenant_file(self, tenant_id: str, path: str, content: str):
        """Write a file to the tenant's isolated workspace."""
        await self._sandbox.write_file(tenant_id, path, content)

    async def evict_tenant(self, tenant_id: str):
        """Clean up all resources for a tenant (on logout/expiry)."""
        cancelled = await self._tasks.cancel_tenant(tenant_id, "eviction")
        print(f"[mt_agent] Evicted tenant {tenant_id}: {cancelled} tasks cancelled")

    def tenant_status(self, tenant_id: str) -> dict:
        return {
            "health": self._bulkhead.tenant_health(tenant_id),
            "resources": self._resources.usage_report(tenant_id),
            "active_tasks": self._tasks.tenant_task_count(tenant_id),
        }

    async def evict_expired_tenants(self):
        """Background task: clean up expired tenant state."""
        evicted = await self._registry.evict_expired()
        if evicted:
            print(f"[mt_agent] Evicted {evicted} expired tenant states")
```

---

## Comparison

| Pattern | State Isolation | Resource Isolation | Error Isolation | Operational Cost |
|---|---|---|---|---|
| Per-tenant context manager | Full (context vars) | No | No | Very low |
| Per-tenant resource quotas | No | Full (semaphores + budgets) | No | Low |
| Sandboxed tool execution | Full (workspace prefix) | Partial (size limits) | No | Medium |
| Bulkhead + circuit breaker | No | Partial (concurrency) | Full | Low |
| Per-tenant task registry | No | Partial (timeout) | Partial | Low |
| MultiTenantAgent (full) | Full | Full | Full | Medium |

**Recommendations:**
- Use **TenantContext** (Solution 1) as the baseline — context variables guarantee state isolation across coroutines with zero overhead.
- Add **resource quotas** (Solution 2) before going to production to prevent "noisy neighbor" problems.
- Use **sandboxed tool execution** (Solution 3) for any agent with file system, database, or subprocess tools.
- Deploy **bulkhead + circuit breaker** (Solution 4) when tenants run distinct workloads that can fail independently.
- Use the **MultiTenantAgent** (Solution 6) as the production-grade orchestrator when serving more than a handful of tenants — the isolation guarantees are worth the setup cost.
- Always monitor per-tenant resource utilization; a single misconfigured tenant can consume 90% of shared capacity without per-tenant quotas.
