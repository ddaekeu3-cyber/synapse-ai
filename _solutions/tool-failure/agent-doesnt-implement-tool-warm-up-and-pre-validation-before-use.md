---
layout: solution
title: "Agent Doesn't Implement Tool Warm-Up and Pre-Validation Before Use"
category: tool-failure
description: "Agents that call tools without pre-validation discover failures mid-task — after context has been consumed and partial work done. These patterns show how to warm up tools and validate their availability before committing to a plan."
tags: [tool-failure, validation, warm-up, pre-check, reliability, anthropic]
---

## Problem

An agent plans a multi-step workflow using five tools, then fails on step three when it discovers that tool is misconfigured, rate-limited, or unavailable. All tokens spent on steps one and two are wasted, and the user sees a mid-task failure. Pre-validation and warm-up detect these failures before the agent begins, enabling fast fallback or clear error messages.

---

### Option 1: Pre-Flight Health Check Before Agent Start

Run a lightweight health check on every tool before passing them to the agent.

```python
import asyncio
import anthropic
from dataclasses import dataclass
from typing import Callable, Awaitable

client = anthropic.AsyncAnthropic()

@dataclass
class ToolHealth:
    name: str
    healthy: bool
    latency_ms: float
    error: str | None

async def check_tool_health(tool_name: str, probe_fn: Callable[[], Awaitable[bool]]) -> ToolHealth:
    import time
    start = time.monotonic()
    try:
        ok = await probe_fn()
        latency = (time.monotonic() - start) * 1000
        return ToolHealth(tool_name, ok, latency, None)
    except Exception as e:
        latency = (time.monotonic() - start) * 1000
        return ToolHealth(tool_name, False, latency, str(e))

# Simulate tool probe functions
async def probe_database() -> bool:
    await asyncio.sleep(0.01)   # simulate DB ping
    return True

async def probe_email_service() -> bool:
    await asyncio.sleep(0.02)
    return True

async def probe_file_storage() -> bool:
    await asyncio.sleep(0.005)
    return True

async def probe_broken_tool() -> bool:
    raise ConnectionError("Service unavailable")

TOOL_PROBES = {
    "query_database": probe_database,
    "send_email": probe_email_service,
    "read_file": probe_file_storage,
    "broken_api": probe_broken_tool,
}

ALL_TOOLS = [
    {"name": "query_database", "description": "Query the database",
     "input_schema": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]}},
    {"name": "send_email", "description": "Send email",
     "input_schema": {"type": "object", "properties": {"to": {"type": "string"}, "body": {"type": "string"}}}},
    {"name": "read_file", "description": "Read a file",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "broken_api", "description": "Broken external API",
     "input_schema": {"type": "object", "properties": {"arg": {"type": "string"}}}},
]

async def run_with_preflight(task: str) -> str:
    # Run all health checks in parallel
    health_results = await asyncio.gather(*[
        check_tool_health(name, probe)
        for name, probe in TOOL_PROBES.items()
    ])

    healthy_names = set()
    for h in health_results:
        status = "OK" if h.healthy else f"FAIL ({h.error})"
        print(f"  [{h.name}] {status} {h.latency_ms:.1f}ms")
        if h.healthy:
            healthy_names.add(h.name)

    available_tools = [t for t in ALL_TOOLS if t["name"] in healthy_names]
    unavailable = [t["name"] for t in ALL_TOOLS if t["name"] not in healthy_names]

    if unavailable:
        print(f"[pre-flight: skipping unavailable tools: {unavailable}]")

    system = ""
    if unavailable:
        system = f"Note: the following tools are currently unavailable: {unavailable}. Plan around them."

    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=system,
        tools=available_tools,
        messages=[{"role": "user", "content": task}],
    )
    return next((b.text for b in response.content if b.type == "text"), "(tool call initiated)")

if __name__ == "__main__":
    async def main():
        print("=== Pre-flight health checks ===")
        result = await run_with_preflight(
            "Query the database for recent orders, then email the summary and log the result."
        )
        print(f"\nAgent response: {result[:400]}")
    asyncio.run(main())

# Expected Token Savings: Prevents 100% token waste on tasks that would fail mid-execution
# Environment: ANTHROPIC_API_KEY
```

---

### Option 2: Schema Validation Before Tool Registration

Validate each tool's JSON schema is well-formed and parameters are resolvable before registering with the agent.

```python
import json
import jsonschema
import anthropic
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class ValidationResult:
    tool_name: str
    valid: bool
    errors: list[str]

META_SCHEMA = {
    "type": "object",
    "required": ["name", "description", "input_schema"],
    "properties": {
        "name": {"type": "string", "minLength": 1, "pattern": "^[a-z_][a-z0-9_]*$"},
        "description": {"type": "string", "minLength": 5},
        "input_schema": {
            "type": "object",
            "required": ["type"],
            "properties": {
                "type": {"type": "string", "enum": ["object"]},
                "properties": {"type": "object"},
            },
        },
    },
}

def validate_tool_schema(tool: dict) -> ValidationResult:
    errors = []
    try:
        jsonschema.validate(tool, META_SCHEMA)
    except jsonschema.ValidationError as e:
        errors.append(f"schema: {e.message}")

    # Check for required fields in input_schema
    input_schema = tool.get("input_schema", {})
    required = input_schema.get("required", [])
    properties = input_schema.get("properties", {})
    for req_field in required:
        if req_field not in properties:
            errors.append(f"required field '{req_field}' not defined in properties")

    # Check description quality
    desc = tool.get("description", "")
    if len(desc) < 10:
        errors.append("description too short — may confuse model")

    return ValidationResult(
        tool_name=tool.get("name", "unknown"),
        valid=len(errors) == 0,
        errors=errors,
    )

CANDIDATE_TOOLS = [
    # Valid tool
    {
        "name": "search_documents",
        "description": "Search through indexed documents by keyword",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["query"],
        },
    },
    # Bad: name has uppercase
    {
        "name": "SendEmail",
        "description": "Send email",
        "input_schema": {"type": "object", "properties": {"to": {"type": "string"}}},
    },
    # Bad: required field not in properties
    {
        "name": "write_record",
        "description": "Write a record to the database table",
        "input_schema": {
            "type": "object",
            "properties": {"data": {"type": "object"}},
            "required": ["data", "table_name"],   # table_name missing from properties
        },
    },
    # Bad: description too short
    {
        "name": "ping",
        "description": "Ping",
        "input_schema": {"type": "object", "properties": {}},
    },
]

def register_validated_tools(candidates: list[dict]) -> list[dict]:
    valid_tools = []
    for tool in candidates:
        result = validate_tool_schema(tool)
        if result.valid:
            print(f"  ✓ {result.tool_name}")
            valid_tools.append(tool)
        else:
            print(f"  ✗ {result.tool_name}: {'; '.join(result.errors)}")
    return valid_tools

def run_agent_with_validated_tools(task: str) -> str:
    print("=== Tool Schema Validation ===")
    tools = register_validated_tools(CANDIDATE_TOOLS)
    print(f"[{len(tools)}/{len(CANDIDATE_TOOLS)} tools passed validation]")

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        tools=tools,
        messages=[{"role": "user", "content": task}],
    )
    return next((b.text for b in response.content if b.type == "text"), "(tool call)")

if __name__ == "__main__":
    result = run_agent_with_validated_tools("Search for documents about quarterly reports.")
    print(f"\nAgent: {result[:300]}")

# Expected Token Savings: Catches schema errors before API call; prevents 400 errors that waste tokens
# Environment: ANTHROPIC_API_KEY
```

---

### Option 3: Lazy Warm-Up with Result Caching

Warm up tools on first use and cache the warm-up result to avoid redundant checks on subsequent calls.

```python
import time
import asyncio
from dataclasses import dataclass, field
from typing import Callable, Awaitable, Any
import anthropic

client = anthropic.AsyncAnthropic()

@dataclass
class WarmUpCache:
    ttl_seconds: float = 300.0
    _cache: dict[str, tuple[bool, float, str]] = field(default_factory=dict)
    _warming: dict[str, asyncio.Event] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def is_warm(self, tool_name: str) -> tuple[bool | None, str]:
        if tool_name not in self._cache:
            return None, "not checked"
        ok, ts, error = self._cache[tool_name]
        if time.monotonic() - ts > self.ttl_seconds:
            del self._cache[tool_name]
            return None, "cache expired"
        return ok, error or "ok"

    def store(self, tool_name: str, ok: bool, error: str = "") -> None:
        self._cache[tool_name] = (ok, time.monotonic(), error)

warm_cache = WarmUpCache()

async def ensure_warm(tool_name: str, probe: Callable[[], Awaitable[bool]]) -> tuple[bool, str]:
    """Check cache; if miss, warm up (with deduplication for concurrent callers)."""
    ok, msg = warm_cache.is_warm(tool_name)
    if ok is not None:
        print(f"  [{tool_name}] cache hit: {msg}")
        return ok, msg

    async with warm_cache._lock:
        # Double-check after acquiring lock
        ok, msg = warm_cache.is_warm(tool_name)
        if ok is not None:
            return ok, msg

        print(f"  [{tool_name}] warming up...")
        try:
            result = await probe()
            warm_cache.store(tool_name, result)
            return result, "warm"
        except Exception as e:
            warm_cache.store(tool_name, False, str(e))
            return False, str(e)

# Tool implementations and probes
async def db_probe() -> bool:
    await asyncio.sleep(0.02)
    return True

async def cache_probe() -> bool:
    await asyncio.sleep(0.005)
    return True

async def search_probe() -> bool:
    await asyncio.sleep(0.01)
    return True

PROBES = {
    "query_db": db_probe,
    "cache_get": cache_probe,
    "full_text_search": search_probe,
}

TOOLS = [
    {"name": k, "description": f"Execute {k}",
     "input_schema": {"type": "object", "properties": {"arg": {"type": "string"}}}}
    for k in PROBES
]

async def call_with_lazy_warmup(task: str, required_tools: list[str]) -> str:
    # Warm only the tools this task needs (lazy)
    print("=== Lazy warm-up for required tools ===")
    warmup_results = await asyncio.gather(*[
        ensure_warm(t, PROBES[t]) for t in required_tools if t in PROBES
    ])

    available = [t for t, (ok, _) in zip(required_tools, warmup_results) if ok]
    failed = [t for t, (ok, _) in zip(required_tools, warmup_results) if not ok]

    if failed:
        print(f"[warm-up failed for: {failed}]")

    tools = [t for t in TOOLS if t["name"] in available]

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        tools=tools,
        messages=[{"role": "user", "content": task}],
    )
    return next((b.text for b in response.content if b.type == "text"), "(tool call)")

async def run_multiple_tasks():
    tasks = [
        ("Find recent sales records", ["query_db", "full_text_search"]),
        ("Get cached user profile", ["cache_get"]),
        ("Search and query analytics", ["full_text_search", "query_db"]),  # warm-ups reuse cache
    ]
    for task, tools in tasks:
        print(f"\nTask: {task}")
        result = await call_with_lazy_warmup(task, tools)
        print(f"Result: {result[:200]}")

if __name__ == "__main__":
    asyncio.run(run_multiple_tasks())

# Expected Token Savings: Warm-up cache avoids repeated probe overhead; lazy init skips unused tools
# Environment: ANTHROPIC_API_KEY
```

---

### Option 4: Parameter Contract Validation with Dry-Run Mode

Simulate tool invocations with synthetic arguments to validate contracts before the agent uses them for real.

```python
import json
import asyncio
from dataclasses import dataclass
from typing import Any
import anthropic

client = anthropic.AsyncAnthropic()

@dataclass
class DryRunResult:
    tool_name: str
    passed: bool
    errors: list[str]
    warnings: list[str]
    latency_ms: float

def generate_synthetic_args(input_schema: dict) -> dict:
    """Generate valid dummy args from a JSON schema."""
    properties = input_schema.get("properties", {})
    required = input_schema.get("required", [])
    args = {}
    for field, spec in properties.items():
        ftype = spec.get("type", "string")
        if ftype == "string":
            args[field] = f"test_{field}"
        elif ftype == "integer":
            args[field] = 1
        elif ftype == "boolean":
            args[field] = True
        elif ftype == "array":
            args[field] = []
        elif ftype == "object":
            args[field] = {}
    return args

async def dry_run_tool(tool: dict, executor: callable) -> DryRunResult:
    import time
    errors = []
    warnings = []
    start = time.monotonic()

    synthetic = generate_synthetic_args(tool.get("input_schema", {}))
    required = tool.get("input_schema", {}).get("required", [])

    # Check all required args are generatable
    for r in required:
        if r not in synthetic:
            errors.append(f"cannot generate required arg '{r}'")

    # Try executing with synthetic args
    if not errors:
        try:
            result = await executor(tool["name"], synthetic, dry_run=True)
            if result is None:
                warnings.append("dry-run returned None — check handler")
        except NotImplementedError:
            warnings.append("dry-run not implemented for this tool")
        except Exception as e:
            errors.append(f"dry-run failed: {e}")

    latency = (time.monotonic() - start) * 1000
    return DryRunResult(tool["name"], len(errors) == 0, errors, warnings, latency)

# Simulated tool executor
async def tool_executor(tool_name: str, args: dict, dry_run: bool = False) -> Any:
    if dry_run:
        # In dry-run mode, just validate args structure
        if tool_name == "query_database":
            if "sql" not in args:
                raise ValueError("sql arg required")
            return {"rows": [], "dry_run": True}
        elif tool_name == "send_email":
            if "to" not in args:
                raise ValueError("to arg required")
            return {"sent": False, "dry_run": True}
        elif tool_name == "broken_tool":
            raise ConnectionError("Service unreachable")
        return {"dry_run": True}
    # Real execution would go here
    return {"result": f"executed {tool_name}"}

TOOLS = [
    {"name": "query_database", "description": "Run a SQL query against the database",
     "input_schema": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]}},
    {"name": "send_email", "description": "Send an email notification",
     "input_schema": {"type": "object", "properties": {"to": {"type": "string"}, "subject": {"type": "string"}}, "required": ["to"]}},
    {"name": "broken_tool", "description": "Tool with unreliable backend",
     "input_schema": {"type": "object", "properties": {"arg": {"type": "string"}}}},
]

async def run_with_dry_run_validation(task: str) -> str:
    print("=== Dry-run validation ===")
    results = await asyncio.gather(*[dry_run_tool(t, tool_executor) for t in TOOLS])

    valid_tools = []
    for r in results:
        if r.passed:
            print(f"  ✓ {r.tool_name} ({r.latency_ms:.1f}ms)")
            if r.warnings:
                print(f"    ⚠ {'; '.join(r.warnings)}")
            valid_tools.append(next(t for t in TOOLS if t["name"] == r.tool_name))
        else:
            print(f"  ✗ {r.tool_name}: {'; '.join(r.errors)}")

    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        tools=valid_tools,
        messages=[{"role": "user", "content": task}],
    )
    return next((b.text for b in response.content if b.type == "text"), "(tool call)")

if __name__ == "__main__":
    async def main():
        result = await run_with_dry_run_validation(
            "Query for overdue invoices and send email alerts."
        )
        print(f"\nAgent: {result[:300]}")
    asyncio.run(main())

# Expected Token Savings: Dry-run catches broken tools without consuming main task tokens
# Environment: ANTHROPIC_API_KEY
```

---

### Option 5: Dependency Graph Validation Before Execution

Model tool dependencies as a DAG and verify all prerequisites are satisfiable before starting.

```python
import asyncio
from dataclasses import dataclass, field
from collections import defaultdict, deque
import anthropic

client = anthropic.AsyncAnthropic()

@dataclass
class ToolNode:
    name: str
    requires: list[str] = field(default_factory=list)  # tools that must succeed first
    produces: list[str] = field(default_factory=list)  # artifacts/outputs this tool creates
    available: bool = True

TOOL_GRAPH = {
    "authenticate":   ToolNode("authenticate", requires=[], produces=["auth_token"]),
    "fetch_user":     ToolNode("fetch_user", requires=["authenticate"], produces=["user_data"]),
    "query_orders":   ToolNode("query_orders", requires=["authenticate"], produces=["order_list"]),
    "generate_report":ToolNode("generate_report", requires=["fetch_user", "query_orders"], produces=["report"]),
    "send_email":     ToolNode("send_email", requires=["authenticate", "generate_report"], produces=[]),
    "broken_dep":     ToolNode("broken_dep", requires=["nonexistent_tool"], produces=["data"]),
}

def validate_dependency_graph(graph: dict[str, ToolNode], requested: list[str]) -> tuple[bool, list[str], list[str]]:
    errors = []
    warnings = []
    all_available = set(graph.keys())

    # Check all dependencies exist
    for name, node in graph.items():
        for dep in node.requires:
            if dep not in all_available:
                errors.append(f"{name} depends on '{dep}' which does not exist")

    # Topological sort to detect cycles
    in_degree = defaultdict(int)
    adjacency = defaultdict(list)
    for name, node in graph.items():
        for dep in node.requires:
            adjacency[dep].append(name)
            in_degree[name] += 1

    queue = deque([n for n in graph if in_degree[n] == 0])
    processed = []
    while queue:
        node = queue.popleft()
        processed.append(node)
        for dependent in adjacency[node]:
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                queue.append(dependent)

    if len(processed) != len(graph):
        cycle_nodes = set(graph.keys()) - set(processed)
        errors.append(f"Circular dependency detected among: {cycle_nodes}")

    # Check requested tools are reachable
    for req in requested:
        if req not in graph:
            errors.append(f"Requested tool '{req}' not in graph")

    return len(errors) == 0, errors, warnings

def topological_order(graph: dict[str, ToolNode], targets: list[str]) -> list[str]:
    """Return execution order for targets and their transitive dependencies."""
    visited = set()
    order = []

    def visit(name: str):
        if name in visited or name not in graph:
            return
        visited.add(name)
        for dep in graph[name].requires:
            visit(dep)
        order.append(name)

    for t in targets:
        visit(t)
    return order

async def run_with_dag_validation(task: str, target_tools: list[str]) -> str:
    print("=== Dependency Graph Validation ===")
    valid, errors, warnings = validate_dependency_graph(TOOL_GRAPH, target_tools)

    for e in errors:
        print(f"  ✗ ERROR: {e}")
    for w in warnings:
        print(f"  ⚠ WARN: {w}")

    if not valid:
        return f"Cannot start: dependency graph has {len(errors)} error(s). Fix before proceeding."

    exec_order = topological_order(TOOL_GRAPH, target_tools)
    print(f"  ✓ Valid graph. Execution order: {exec_order}")

    # Build tool list in correct order
    tools = [
        {"name": name, "description": f"Execute {name} (deps: {TOOL_GRAPH[name].requires})",
         "input_schema": {"type": "object", "properties": {"arg": {"type": "string"}}}}
        for name in exec_order
    ]

    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=f"Execute tools in this order: {exec_order}",
        tools=tools,
        messages=[{"role": "user", "content": task}],
    )
    return next((b.text for b in response.content if b.type == "text"), "(tool calls initiated)")

if __name__ == "__main__":
    async def main():
        print("--- Valid dependency chain ---")
        r = await run_with_dag_validation("Generate and email the monthly report.",
                                          ["send_email"])
        print(r[:300])

        print("\n--- Invalid: broken dependency ---")
        r2 = await run_with_dag_validation("Use the broken tool.", ["broken_dep"])
        print(r2)

    asyncio.run(main())

# Expected Token Savings: Catches broken dependency chains without any wasted tool execution tokens
# Environment: ANTHROPIC_API_KEY
```

---

### Option 6: Periodic Background Re-Validation with Circuit Breaker

Continuously probe tools in the background and open a circuit breaker on failures — agents always see current health state.

```python
import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
import anthropic

client = anthropic.AsyncAnthropic()

class CircuitState(Enum):
    CLOSED = "closed"       # normal operation
    OPEN = "open"           # tool blocked
    HALF_OPEN = "half_open" # testing recovery

@dataclass
class ToolCircuit:
    name: str
    failure_threshold: int = 3
    recovery_timeout: float = 30.0
    probe_interval: float = 10.0

    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    last_failure: float = 0.0
    last_success: float = field(default_factory=time.monotonic)
    probe_fn: callable = None

    def record_success(self):
        self.failure_count = 0
        self.last_success = time.monotonic()
        if self.state != CircuitState.CLOSED:
            print(f"  [{self.name}] circuit CLOSED (recovered)")
        self.state = CircuitState.CLOSED

    def record_failure(self):
        self.failure_count += 1
        self.last_failure = time.monotonic()
        if self.failure_count >= self.failure_threshold:
            if self.state != CircuitState.OPEN:
                print(f"  [{self.name}] circuit OPEN after {self.failure_count} failures")
            self.state = CircuitState.OPEN

    def is_available(self) -> bool:
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.OPEN:
            if time.monotonic() - self.last_failure > self.recovery_timeout:
                self.state = CircuitState.HALF_OPEN
                print(f"  [{self.name}] circuit HALF_OPEN (testing)")
                return True
            return False
        return True  # HALF_OPEN: allow one attempt

class ToolHealthRegistry:
    def __init__(self):
        self._circuits: dict[str, ToolCircuit] = {}
        self._running = False

    def register(self, circuit: ToolCircuit):
        self._circuits[circuit.name] = circuit

    def available_tools(self) -> list[str]:
        return [name for name, c in self._circuits.items() if c.is_available()]

    async def _probe_all(self):
        for name, circuit in self._circuits.items():
            if circuit.probe_fn is None:
                continue
            try:
                ok = await circuit.probe_fn()
                if ok:
                    circuit.record_success()
                else:
                    circuit.record_failure()
            except Exception:
                circuit.record_failure()

    async def start_background_probing(self):
        self._running = True
        while self._running:
            await self._probe_all()
            await asyncio.sleep(min(c.probe_interval for c in self._circuits.values()))

    def stop(self):
        self._running = False

registry = ToolHealthRegistry()

# Register tool circuits with probe functions
async def db_probe(): await asyncio.sleep(0.01); return True
async def api_probe(): await asyncio.sleep(0.02); return True
async def flaky_probe():
    import random
    await asyncio.sleep(0.01)
    if random.random() < 0.6:   # 60% failure rate
        raise ConnectionError("Flaky service")
    return True

registry.register(ToolCircuit("database", probe_fn=db_probe))
registry.register(ToolCircuit("external_api", probe_fn=api_probe))
registry.register(ToolCircuit("flaky_service", failure_threshold=2, probe_fn=flaky_probe))

TOOL_DEFINITIONS = {
    name: {"name": name, "description": f"Execute {name}",
           "input_schema": {"type": "object", "properties": {"arg": {"type": "string"}}}}
    for name in ["database", "external_api", "flaky_service"]
}

async def agent_call_with_registry(task: str) -> str:
    available = registry.available_tools()
    print(f"[available tools: {available}]")
    tools = [TOOL_DEFINITIONS[n] for n in available]

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        tools=tools,
        messages=[{"role": "user", "content": task}],
    )
    return next((b.text for b in response.content if b.type == "text"), "(tool call)")

async def run_demo():
    # Run one probe cycle to seed the registry
    await registry._probe_all()

    for i in range(3):
        print(f"\n=== Call {i+1} ===")
        result = await agent_call_with_registry("Use all available tools to process data.")
        print(f"Agent: {result[:200]}")
        await registry._probe_all()   # simulate background probing

if __name__ == "__main__":
    asyncio.run(run_demo())

# Expected Token Savings: Circuit breaker prevents repeated failed tool calls; probing is background, zero agent tokens
# Environment: ANTHROPIC_API_KEY
```

---

## Comparison

| Option | Approach | When Checked | Overhead | Best For |
|--------|----------|-------------|----------|----------|
| 1 | Parallel health probes before start | Pre-flight | Low (parallel) | Catch unavailable services early |
| 2 | JSON schema validation | Pre-registration | None (local) | Catch malformed tool definitions |
| 3 | Lazy warm-up with TTL cache | First use per tool | Low (cached) | Long-running agents with many tools |
| 4 | Dry-run with synthetic args | Pre-flight | Low | Verify parameter contracts |
| 5 | Dependency graph DAG validation | Pre-flight | None (local) | Multi-tool workflows with ordering |
| 6 | Background probing + circuit breaker | Continuous | Background only | Production agents needing live health |
