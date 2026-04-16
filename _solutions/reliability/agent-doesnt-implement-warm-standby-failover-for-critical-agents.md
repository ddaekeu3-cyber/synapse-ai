---
title: "Agent Doesn't Implement Warm Standby Failover for Critical Agents"
description: "Keep a warmed replica agent ready to take over immediately when the primary fails—eliminating cold-start delays and ensuring continuous availability for mission-critical workloads."
difficulty: advanced
category: reliability
tags: [failover, high-availability, warm-standby, resilience, reliability]
---

## Problem

When a critical agent process crashes or becomes unresponsive, the fallback is a cold start: initialize the client, reload context, re-establish connections. This takes seconds to minutes, during which requests fail. For mission-critical agents—payment processors, customer-facing assistants, real-time monitors—that downtime is unacceptable. Warm standby keeps a pre-initialized replica ready to accept traffic instantly.

## Solutions

### Option 1: Active-Passive Warm Standby

Keep a passive replica warmed up and switch to it immediately on primary failure.

```python
import asyncio
import time
from anthropic import AsyncAnthropic
from dataclasses import dataclass
from enum import Enum

class AgentRole(Enum):
    PRIMARY = "primary"
    STANDBY = "standby"
    ACTIVE = "active"  # Standby promoted to active

@dataclass
class AgentInstance:
    instance_id: str
    role: AgentRole
    client: AsyncAnthropic
    healthy: bool = True
    requests_served: int = 0
    last_heartbeat: float = 0.0

class WarmStandbyPair:
    HEARTBEAT_INTERVAL = 5.0
    FAILURE_THRESHOLD = 10.0  # Seconds without heartbeat = failed

    def __init__(self):
        self.primary = AgentInstance(
            instance_id="primary-001",
            role=AgentRole.PRIMARY,
            client=AsyncAnthropic(),
        )
        self.standby = AgentInstance(
            instance_id="standby-001",
            role=AgentRole.STANDBY,
            client=AsyncAnthropic(),  # Warmed: client initialized, ready
        )
        self._active = self.primary
        self._failover_count = 0
        self._lock = asyncio.Lock()

    @property
    def active(self) -> AgentInstance:
        return self._active

    async def _heartbeat_loop(self):
        """Monitor primary health and trigger failover if needed."""
        while True:
            await asyncio.sleep(self.HEARTBEAT_INTERVAL)
            now = time.monotonic()

            if self._active == self.primary:
                elapsed = now - self.primary.last_heartbeat
                if self.primary.last_heartbeat > 0 and elapsed > self.FAILURE_THRESHOLD:
                    await self._failover()

    async def _failover(self):
        async with self._lock:
            if self._active != self.primary:
                return  # Already failed over

            print(f"[Failover] Primary unhealthy. Promoting standby to active.")
            self.primary.healthy = False
            self.standby.role = AgentRole.ACTIVE
            self._active = self.standby
            self._failover_count += 1

            # Spawn a new standby in the background
            asyncio.create_task(self._spawn_new_standby())

    async def _spawn_new_standby(self):
        await asyncio.sleep(2.0)  # Simulate initialization time
        new_standby = AgentInstance(
            instance_id=f"standby-{self._failover_count + 1:03d}",
            role=AgentRole.STANDBY,
            client=AsyncAnthropic(),
        )
        self.standby = new_standby
        print(f"[Failover] New standby {new_standby.instance_id} is warmed and ready.")

    async def complete(self, prompt: str) -> str:
        async with self._lock:
            instance = self._active

        instance.last_heartbeat = time.monotonic()
        response = await instance.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}]
        )
        instance.requests_served += 1
        return response.content[0].text

    def status(self) -> dict:
        return {
            "active_instance": self._active.instance_id,
            "active_role": self._active.role.value,
            "failovers": self._failover_count,
            "primary_healthy": self.primary.healthy,
            "standby_ready": self.standby.healthy,
        }

async def demo_warm_standby():
    pair = WarmStandbyPair()
    heartbeat_task = asyncio.create_task(pair._heartbeat_loop())

    # Normal operation
    for i in range(3):
        result = await pair.complete(f"Request {i}: what is {i+1}+{i+1}?")
        print(f"Request {i}: {result.strip()[:60]} [{pair.active.instance_id}]")

    print(f"\nStatus: {pair.status()}")

    heartbeat_task.cancel()

asyncio.run(demo_warm_standby())
```

### Option 2: Health-Check-Driven Promotion

Use periodic health checks to detect degradation early and promote standby before total failure.

```python
import asyncio
import time
import statistics
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field
from enum import Enum

class HealthStatus(Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILED = "failed"

@dataclass
class HealthMetrics:
    latencies: list[float] = field(default_factory=list)
    error_count: int = 0
    success_count: int = 0

    def record_success(self, latency_ms: float):
        self.latencies.append(latency_ms)
        if len(self.latencies) > 20:
            self.latencies.pop(0)
        self.success_count += 1

    def record_error(self):
        self.error_count += 1

    @property
    def p95_latency(self) -> float:
        if not self.latencies:
            return 0.0
        sorted_l = sorted(self.latencies)
        idx = int(len(sorted_l) * 0.95)
        return sorted_l[idx]

    @property
    def error_rate(self) -> float:
        total = self.success_count + self.error_count
        return self.error_count / total if total > 0 else 0.0

    def status(self, p95_threshold_ms: float = 2000, error_rate_threshold: float = 0.1) -> HealthStatus:
        if self.error_rate > 0.3 or (self.latencies and self.p95_latency > p95_threshold_ms * 2):
            return HealthStatus.FAILED
        elif self.error_rate > error_rate_threshold or (self.latencies and self.p95_latency > p95_threshold_ms):
            return HealthStatus.DEGRADED
        return HealthStatus.HEALTHY

class HealthCheckedStandby:
    PROBE_INTERVAL = 10.0
    PROBE_PROMPT = "Reply with exactly: HEALTHY"

    def __init__(self):
        self._primary_client = AsyncAnthropic()
        self._standby_client = AsyncAnthropic()
        self._primary_metrics = HealthMetrics()
        self._standby_metrics = HealthMetrics()
        self._using_standby = False
        self._promotions = 0

    async def _probe(self, client: AsyncAnthropic, metrics: HealthMetrics) -> HealthStatus:
        start = time.monotonic()
        try:
            response = await asyncio.wait_for(
                client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=20,
                    messages=[{"role": "user", "content": self.PROBE_PROMPT}]
                ),
                timeout=5.0
            )
            latency_ms = (time.monotonic() - start) * 1000
            if "HEALTHY" in response.content[0].text.upper():
                metrics.record_success(latency_ms)
            else:
                metrics.record_error()
        except Exception:
            metrics.record_error()

        return metrics.status()

    async def _health_check_loop(self):
        while True:
            await asyncio.sleep(self.PROBE_INTERVAL)

            primary_status = await self._probe(self._primary_client, self._primary_metrics)

            if not self._using_standby and primary_status in (HealthStatus.DEGRADED, HealthStatus.FAILED):
                # Verify standby is healthy before promoting
                standby_status = await self._probe(self._standby_client, self._standby_metrics)
                if standby_status == HealthStatus.HEALTHY:
                    self._using_standby = True
                    self._promotions += 1
                    severity = "degraded" if primary_status == HealthStatus.DEGRADED else "failed"
                    print(f"[HealthCheck] Primary {severity}. Promoting standby "
                          f"(promotion #{self._promotions})")
            elif self._using_standby and primary_status == HealthStatus.HEALTHY:
                # Primary recovered — switch back
                self._using_standby = False
                print(f"[HealthCheck] Primary recovered. Failing back.")

    async def complete(self, prompt: str) -> str:
        client = self._standby_client if self._using_standby else self._primary_client
        metrics = self._standby_metrics if self._using_standby else self._primary_metrics

        start = time.monotonic()
        try:
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}]
            )
            metrics.record_success((time.monotonic() - start) * 1000)
            return response.content[0].text
        except Exception as e:
            metrics.record_error()
            raise

    def report(self) -> dict:
        return {
            "routing": "standby" if self._using_standby else "primary",
            "promotions": self._promotions,
            "primary_p95_ms": f"{self._primary_metrics.p95_latency:.0f}",
            "primary_error_rate": f"{self._primary_metrics.error_rate:.1%}",
            "primary_status": self._primary_metrics.status().value,
        }

async def demo_health_check_standby():
    agent = HealthCheckedStandby()
    health_task = asyncio.create_task(agent._health_check_loop())

    for i in range(5):
        result = await agent.complete(f"What is {i * 2}?")
        print(f"Request {i}: {result.strip()[:60]}")

    print(f"\nReport: {agent.report()}")
    health_task.cancel()

asyncio.run(demo_health_check_standby())
```

### Option 3: Context-Synced Standby

Keep the standby replica synchronized with the primary's conversation context so failover is seamless.

```python
import asyncio
import copy
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field

client_primary = AsyncAnthropic()
client_standby = AsyncAnthropic()

@dataclass
class SyncedConversation:
    messages: list[dict] = field(default_factory=list)
    system: str = ""
    _standby_synced: bool = True

    def add_user(self, content: str):
        self._standby_synced = False
        self.messages.append({"role": "user", "content": content})

    def add_assistant(self, content: str):
        self.messages.append({"role": "assistant", "content": content})

    def snapshot(self) -> list[dict]:
        """Deep copy for standby sync."""
        return copy.deepcopy(self.messages)

class ContextSyncedPair:
    SYNC_INTERVAL = 3  # Sync every N turns

    def __init__(self, system_prompt: str = ""):
        self._convo = SyncedConversation(system=system_prompt)
        self._standby_snapshot: list[dict] = []
        self._using_standby = False
        self._turn_count = 0
        self._sync_count = 0

    def _sync_standby(self):
        """Push current context snapshot to standby."""
        self._standby_snapshot = self._convo.snapshot()
        self._sync_count += 1

    async def chat(self, user_message: str) -> str:
        self._convo.add_user(user_message)
        self._turn_count += 1

        # Sync standby context periodically
        if self._turn_count % self.SYNC_INTERVAL == 0:
            self._sync_standby()

        active_client = client_standby if self._using_standby else client_primary
        messages = (
            self._standby_snapshot + [{"role": "user", "content": user_message}]
            if self._using_standby and self._standby_snapshot
            else self._convo.messages
        )

        try:
            kwargs = {"model": "claude-haiku-4-5-20251001", "max_tokens": 200, "messages": messages}
            if self._convo.system:
                kwargs["system"] = self._convo.system

            response = await active_client.messages.create(**kwargs)
            text = response.content[0].text
            self._convo.add_assistant(text)
            return text
        except Exception:
            if not self._using_standby:
                print(f"[ContextSync] Primary failed at turn {self._turn_count}. "
                      f"Failing over to standby (context synced at turn "
                      f"{self._turn_count - (self._turn_count % self.SYNC_INTERVAL)}).")
                self._using_standby = True
                # Retry on standby
                return await self.chat_on_standby(user_message)
            raise

    async def chat_on_standby(self, user_message: str) -> str:
        messages = self._standby_snapshot + [{"role": "user", "content": user_message}]
        response = await client_standby.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=messages
        )
        text = response.content[0].text
        self._convo.add_assistant(text)
        return text

    def stats(self) -> dict:
        return {
            "turns": self._turn_count,
            "syncs": self._sync_count,
            "using_standby": self._using_standby,
            "context_size": len(self._convo.messages),
            "standby_snapshot_size": len(self._standby_snapshot),
        }

async def demo_context_synced():
    agent = ContextSyncedPair(system_prompt="You are a helpful assistant. Be concise.")

    conversation = [
        "My name is Alice.",
        "I'm working on a Python project.",
        "What are best practices for error handling?",
        "How should I structure my exceptions?",
        "Can you give me an example?",
    ]

    for msg in conversation:
        response = await agent.chat(msg)
        print(f"User: {msg}")
        print(f"Agent: {response.strip()[:80]}\n")

    print(f"Stats: {agent.stats()}")

asyncio.run(demo_context_synced())
```

### Option 4: Multi-Region Warm Standby

Maintain standby instances in multiple geographic regions for disaster recovery.

```python
import asyncio
import time
import random
from anthropic import AsyncAnthropic
from dataclasses import dataclass

@dataclass
class RegionalInstance:
    region: str
    client: AsyncAnthropic
    priority: int          # Lower = preferred
    latency_ms: float = 0.0
    healthy: bool = True
    requests: int = 0

class MultiRegionFailover:
    REGIONS = ["us-east-1", "eu-west-1", "ap-southeast-1"]

    def __init__(self):
        self._instances = [
            RegionalInstance(
                region=region,
                client=AsyncAnthropic(),
                priority=i,
            )
            for i, region in enumerate(self.REGIONS)
        ]
        self._current_region_idx = 0
        self._failovers: list[str] = []

    def _healthy_instances(self) -> list[RegionalInstance]:
        return sorted(
            [inst for inst in self._instances if inst.healthy],
            key=lambda x: (x.priority, x.latency_ms)
        )

    async def _measure_latency(self, instance: RegionalInstance):
        start = time.monotonic()
        try:
            await asyncio.wait_for(
                instance.client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=5,
                    messages=[{"role": "user", "content": "ping"}]
                ),
                timeout=3.0
            )
            instance.latency_ms = (time.monotonic() - start) * 1000
            instance.healthy = True
        except Exception:
            instance.healthy = False
            instance.latency_ms = float("inf")

    async def _latency_probe_loop(self):
        while True:
            tasks = [self._measure_latency(inst) for inst in self._instances]
            await asyncio.gather(*tasks)
            await asyncio.sleep(30.0)

    async def complete(self, prompt: str) -> tuple[str, str]:
        """Returns (response_text, serving_region)."""
        healthy = self._healthy_instances()
        if not healthy:
            raise RuntimeError("All regions unavailable")

        # Try regions in priority order
        for instance in healthy:
            try:
                response = await asyncio.wait_for(
                    instance.client.messages.create(
                        model="claude-haiku-4-5-20251001",
                        max_tokens=200,
                        messages=[{"role": "user", "content": prompt}]
                    ),
                    timeout=5.0
                )
                instance.requests += 1
                return response.content[0].text, instance.region
            except Exception:
                instance.healthy = False
                self._failovers.append(instance.region)
                print(f"[MultiRegion] {instance.region} failed. Trying next region.")

        raise RuntimeError("All regions exhausted")

    def region_report(self) -> dict:
        return {
            inst.region: {
                "healthy": inst.healthy,
                "priority": inst.priority,
                "latency_ms": f"{inst.latency_ms:.0f}" if inst.latency_ms != float("inf") else "∞",
                "requests_served": inst.requests,
            }
            for inst in self._instances
        }

async def demo_multi_region():
    failover = MultiRegionFailover()

    for i in range(5):
        try:
            result, region = await failover.complete(f"Brief response to request {i}")
            print(f"Request {i} served by {region}: {result.strip()[:60]}")
        except RuntimeError as e:
            print(f"Request {i} failed: {e}")

    import json
    print(f"\nRegion report:\n{json.dumps(failover.region_report(), indent=2)}")

asyncio.run(demo_multi_region())
```

### Option 5: State-Machine-Driven Failover

Model agent health as an explicit state machine with well-defined transitions and cooldowns.

```python
import asyncio
import time
from anthropic import AsyncAnthropic
from enum import Enum
from dataclasses import dataclass, field

class FailoverState(Enum):
    NORMAL = "normal"                   # Primary serving
    PROBING = "probing"                 # Detecting potential failure
    FAILING_OVER = "failing_over"       # In transition
    ON_STANDBY = "on_standby"          # Standby serving
    RECOVERING = "recovering"           # Primary coming back
    FAILED_PERMANENTLY = "failed"       # Both failed

@dataclass
class StateMachineFailover:
    state: FailoverState = FailoverState.NORMAL
    consecutive_errors: int = 0
    consecutive_successes: int = 0
    last_transition: float = field(default_factory=time.monotonic)
    transitions: list[tuple[FailoverState, FailoverState, str]] = field(default_factory=list)

    ERROR_THRESHOLD = 3       # Errors before failover
    RECOVERY_THRESHOLD = 5    # Successes before fail-back
    COOLDOWN_SECONDS = 30.0   # Min time before attempting fail-back

    def on_success(self) -> FailoverState:
        self.consecutive_errors = 0
        self.consecutive_successes += 1

        if self.state == FailoverState.RECOVERING:
            if (self.consecutive_successes >= self.RECOVERY_THRESHOLD
                    and time.monotonic() - self.last_transition >= self.COOLDOWN_SECONDS):
                return self._transition(FailoverState.NORMAL, "Primary fully recovered")

        return self.state

    def on_error(self) -> FailoverState:
        self.consecutive_successes = 0
        self.consecutive_errors += 1

        if self.state == FailoverState.NORMAL:
            if self.consecutive_errors >= 1:
                return self._transition(FailoverState.PROBING, "First error detected")

        elif self.state == FailoverState.PROBING:
            if self.consecutive_errors >= self.ERROR_THRESHOLD:
                return self._transition(FailoverState.FAILING_OVER, "Error threshold reached")

        elif self.state == FailoverState.ON_STANDBY:
            if self.consecutive_errors >= self.ERROR_THRESHOLD * 2:
                return self._transition(FailoverState.FAILED_PERMANENTLY, "Both instances failed")

        return self.state

    def _transition(self, new_state: FailoverState, reason: str) -> FailoverState:
        old_state = self.state
        self.state = new_state
        self.last_transition = time.monotonic()
        self.consecutive_errors = 0
        self.consecutive_successes = 0
        self.transitions.append((old_state, new_state, reason))
        print(f"[FSM] {old_state.value} → {new_state.value}: {reason}")
        return new_state

class FSMDrivenAgent:
    def __init__(self):
        self._fsm = StateMachineFailover()
        self._primary = AsyncAnthropic()
        self._standby = AsyncAnthropic()

    async def complete(self, prompt: str) -> str:
        if self._fsm.state == FailoverState.FAILED_PERMANENTLY:
            raise RuntimeError("Agent permanently failed")

        use_standby = self._fsm.state in (
            FailoverState.ON_STANDBY,
            FailoverState.FAILING_OVER,
        )
        client = self._standby if use_standby else self._primary

        try:
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=150,
                messages=[{"role": "user", "content": prompt}]
            )
            self._fsm.on_success()
            if self._fsm.state == FailoverState.FAILING_OVER:
                self._fsm._transition(FailoverState.ON_STANDBY, "Standby confirmed healthy")
            return response.content[0].text
        except Exception as e:
            new_state = self._fsm.on_error()
            if new_state == FailoverState.FAILING_OVER:
                # Immediate retry on standby
                response = await self._standby.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=150,
                    messages=[{"role": "user", "content": prompt}]
                )
                self._fsm._transition(FailoverState.ON_STANDBY, "Standby taking over")
                return response.content[0].text
            raise

    def fsm_state(self) -> dict:
        return {
            "state": self._fsm.state.value,
            "consecutive_errors": self._fsm.consecutive_errors,
            "consecutive_successes": self._fsm.consecutive_successes,
            "transitions": [(a.value, b.value, r) for a, b, r in self._fsm.transitions],
        }

async def demo_fsm_failover():
    agent = FSMDrivenAgent()
    prompts = [f"What is {i}+{i}?" for i in range(6)]

    for prompt in prompts:
        try:
            result = await agent.complete(prompt)
            print(f"[{agent._fsm.state.value}] {result.strip()[:60]}")
        except Exception as e:
            print(f"[{agent._fsm.state.value}] ERROR: {e}")

asyncio.run(demo_fsm_failover())
```

### Option 6: Warm Standby with Preloaded Context Window

Pre-load the standby with the same system prompt and tool definitions so it can serve immediately.

```python
import asyncio
from anthropic import AsyncAnthropic
from dataclasses import dataclass

@dataclass
class AgentConfig:
    system_prompt: str
    tools: list[dict]
    model: str = "claude-haiku-4-5-20251001"
    max_tokens: int = 500

class PreloadedStandby:
    """Standby is pre-initialized with full config—no cold-start penalty."""

    def __init__(self, config: AgentConfig):
        self._config = config
        self._primary = AsyncAnthropic()
        self._standby = AsyncAnthropic()
        self._on_standby = False
        self._failover_count = 0

        # Warm both: same config pre-applied, no initialization on failover
        self._primary_kwargs = self._build_kwargs()
        self._standby_kwargs = self._build_kwargs()

    def _build_kwargs(self) -> dict:
        return {
            "model": self._config.model,
            "max_tokens": self._config.max_tokens,
            "system": self._config.system_prompt,
            "tools": self._config.tools,
        }

    async def _try_primary(self, messages: list[dict]) -> str:
        response = await asyncio.wait_for(
            self._primary.messages.create(
                **self._primary_kwargs,
                messages=messages,
            ),
            timeout=8.0,
        )
        return response.content[0].text if response.content[0].type == "text" else "[tool_use]"

    async def _try_standby(self, messages: list[dict]) -> str:
        response = await self._standby.messages.create(
            **self._standby_kwargs,
            messages=messages,
        )
        return response.content[0].text if response.content[0].type == "text" else "[tool_use]"

    async def complete(self, messages: list[dict]) -> tuple[str, str]:
        if self._on_standby:
            result = await self._try_standby(messages)
            return result, "standby"

        try:
            result = await self._try_primary(messages)
            return result, "primary"
        except Exception as e:
            self._on_standby = True
            self._failover_count += 1
            print(f"[PreloadedStandby] Primary failed ({e}). "
                  f"Instant failover to pre-warmed standby (#{self._failover_count}).")
            result = await self._try_standby(messages)
            return result, "standby"

    def status(self) -> dict:
        return {
            "serving": "standby" if self._on_standby else "primary",
            "failovers": self._failover_count,
            "config_preloaded": True,
        }

async def demo_preloaded_standby():
    config = AgentConfig(
        system_prompt="You are a concise assistant. Answer in one sentence.",
        tools=[{
            "name": "get_time",
            "description": "Get the current time",
            "input_schema": {"type": "object", "properties": {}}
        }],
    )

    agent = PreloadedStandby(config)

    prompts = [
        "What is recursion?",
        "Name a programming language.",
        "What is a REST API?",
    ]

    for prompt in prompts:
        messages = [{"role": "user", "content": prompt}]
        result, source = await agent.complete(messages)
        print(f"[{source}] {prompt}: {result.strip()[:80]}")

    print(f"\nStatus: {agent.status()}")

asyncio.run(demo_preloaded_standby())
```

## Comparison

| Approach | Failover Speed | Context Continuity | Complexity | Best For |
|---|---|---|---|---|
| Active-Passive Warm Standby | ~0ms | Lost on failover | Medium | Stateless agents |
| Health-Check-Driven Promotion | Proactive (before full failure) | Lost on failover | Medium | Degradation detection |
| Context-Synced Standby | ~0ms | Periodic checkpoint | High | Conversational agents |
| Multi-Region Warm Standby | ~0ms | Lost on failover | High | Disaster recovery |
| State-Machine-Driven Failover | ~0ms (auto-retry) | Preserved in request | High | Complex failure patterns |
| Preloaded Config Standby | True 0ms | Config preserved | Low | Tool-heavy agents |

**Choose Active-Passive Warm Standby** for stateless request-response agents where context loss on failover is acceptable. **Choose Context-Synced Standby** for conversational agents where losing context mid-session damages user experience. **Choose Multi-Region Warm Standby** when geographic resilience is required. **Choose State-Machine-Driven Failover** when you need precise control over transition timing and cooldown periods.
