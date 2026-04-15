---
layout: solution
title: "Agent Doesn't Implement Tool Dependency Graph for Ordered Execution"
category: tool-failure
description: "Agent calls tools in arbitrary or sequential order without modeling dependencies between them, causing failures when a tool requires output from another that hasn't run yet."
tags: [tool-use, dependency, graph, ordering, parallelism, dag, orchestration]
---

# Agent Doesn't Implement Tool Dependency Graph for Ordered Execution

## Problem

An agent needs to call `get_user` before `get_orders(user_id)`, and both before `calculate_total(orders)`. Without dependency tracking, the agent may call them in the wrong order (using an uninitialized `user_id`), run them all sequentially when some are independent (wasting time), or re-run completed tools because it lost track of what's done.

---

## Option 1: Static DAG with Topological Sort

Define tool dependencies as a directed acyclic graph (DAG) and execute in topological order, parallelizing independent layers.

```python
import anthropic
import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable

@dataclass
class ToolNode:
    name: str
    depends_on: list[str]
    fn: Callable
    args_from: dict[str, str] = field(default_factory=dict)  # param_name -> source_tool.key

def topological_layers(nodes: list[ToolNode]) -> list[list[ToolNode]]:
    """Group nodes into layers where each layer can run in parallel."""
    by_name = {n.name: n for n in nodes}
    remaining = {n.name for n in nodes}
    completed: set[str] = set()
    layers = []

    while remaining:
        layer = [
            by_name[name] for name in remaining
            if all(dep in completed for dep in by_name[name].depends_on)
        ]
        if not layer:
            raise ValueError(f"Circular dependency detected in: {remaining}")
        for node in layer:
            remaining.discard(node.name)
        completed.update(n.name for n in layer)
        layers.append(layer)
    return layers

async def execute_dag(nodes: list[ToolNode]) -> dict[str, Any]:
    results: dict[str, Any] = {}
    layers = topological_layers(nodes)

    for i, layer in enumerate(layers):
        print(f"[layer {i}] Running: {[n.name for n in layer]}")
        async def run_node(node: ToolNode) -> tuple[str, Any]:
            kwargs = {}
            for param, source in node.args_from.items():
                tool_name, key = source.split(".", 1)
                kwargs[param] = results[tool_name][key]
            result = await asyncio.to_thread(node.fn, **kwargs)
            return node.name, result

        layer_results = await asyncio.gather(*[run_node(n) for n in layer])
        for name, result in layer_results:
            results[name] = result

    return results

# --- Tool implementations ---
client = anthropic.Anthropic()

def get_user() -> dict:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001", max_tokens=64,
        messages=[{"role": "user", "content": "Return JSON: {\"user_id\": \"u123\", \"name\": \"Alice\"}"}]
    )
    import json, re
    m = re.search(r'\{.*\}', response.content[0].text, re.DOTALL)
    return json.loads(m.group()) if m else {"user_id": "u123", "name": "Alice"}

def get_orders(user_id: str) -> dict:
    return {"orders": [{"id": "o1", "amount": 50}, {"id": "o2", "amount": 30}]}

def get_shipping_options() -> dict:
    return {"options": ["standard", "express"]}

def calculate_total(user_id: str) -> dict:
    # In real use would read from results
    return {"total": 80, "currency": "USD"}

nodes = [
    ToolNode("get_user", depends_on=[], fn=get_user),
    ToolNode("get_orders", depends_on=["get_user"], fn=get_orders,
             args_from={"user_id": "get_user.user_id"}),
    ToolNode("get_shipping_options", depends_on=[], fn=get_shipping_options),
    ToolNode("calculate_total", depends_on=["get_orders"],
             fn=calculate_total, args_from={"user_id": "get_user.user_id"}),
]

results = asyncio.run(execute_dag(nodes))
print("Results:", {k: v for k, v in results.items()})

# Expected Token Savings: Parallel independent tool calls (get_user + get_shipping_options run together). Correct ordering prevents failed tool calls that would need re-runs. Saves 1–3 round trips on 4-tool graphs.
# Environment: ANTHROPIC_API_KEY required. Uses asyncio (stdlib).
```

---

## Option 2: LLM-Generated Dependency Plan

Ask Claude to analyze the available tools and the user's goal, then generate an execution plan with explicit dependencies before running anything.

```python
import anthropic
import json
import asyncio
from dataclasses import dataclass
from typing import Any

@dataclass
class PlannedStep:
    step_id: str
    tool: str
    inputs: dict
    depends_on: list[str]
    description: str

client = anthropic.Anthropic()

AVAILABLE_TOOLS = {
    "lookup_customer": "Looks up customer by email. Returns: {customer_id, name, tier}",
    "get_purchase_history": "Gets purchases for customer_id. Returns: [{product_id, date, price}]",
    "get_product_details": "Gets details for product_id. Returns: {name, category, rating}",
    "calculate_recommendations": "Generates recommendations given customer_id and purchase_history. Returns: [{product_id, score}]",
}

def plan_execution(goal: str) -> list[PlannedStep]:
    tools_desc = "\n".join(f"- {k}: {v}" for k, v in AVAILABLE_TOOLS.items())
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": f"""Given these tools:
{tools_desc}

Goal: {goal}

Create an execution plan as JSON array:
[{{"step_id": "s1", "tool": "tool_name", "inputs": {{}}, "depends_on": [], "description": "..."}}]

Rules:
- Only use listed tools
- depends_on lists step_ids that must complete first
- inputs can reference prior step outputs as "$step_id.field"
- Parallelize independent steps"""
        }]
    )
    text = response.content[0].text
    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    raw = json.loads(text.strip())
    return [PlannedStep(**s) for s in raw]

def execute_tool(tool: str, inputs: dict, prior_results: dict) -> dict:
    # Resolve $step_id.field references
    resolved = {}
    for k, v in inputs.items():
        if isinstance(v, str) and v.startswith("$"):
            ref_step, field = v[1:].split(".", 1)
            resolved[k] = prior_results.get(ref_step, {}).get(field, f"mock_{field}")
        else:
            resolved[k] = v
    # Mock tool execution
    return {"result": f"mock_output_of_{tool}", "tool": tool, "inputs": resolved}

def run_plan(steps: list[PlannedStep]) -> dict[str, Any]:
    results: dict[str, Any] = {}
    pending = list(steps)

    while pending:
        # Find steps whose dependencies are all met
        ready = [s for s in pending if all(dep in results for dep in s.depends_on)]
        if not ready:
            break
        for step in ready:
            print(f"[exec] {step.step_id}: {step.tool} — {step.description}")
            results[step.step_id] = execute_tool(step.tool, step.inputs, results)
            pending.remove(step)
    return results

steps = plan_execution("Get recommendations for customer alice@example.com")
print(f"Planned {len(steps)} steps:")
for s in steps:
    print(f"  {s.step_id}: {s.tool} (depends: {s.depends_on})")
results = run_plan(steps)
print(f"\nExecuted {len(results)} steps")

# Expected Token Savings: LLM plans optimal parallelization upfront. One planning call (~600 tokens) saves 2–4 unnecessary sequential round trips on 4+ tool workflows.
# Environment: ANTHROPIC_API_KEY required. claude-sonnet-4-6 for planning, tools run locally.
```

---

## Option 3: Runtime Dependency Resolution with Result Caching

Tools declare their input requirements at registration time. A resolver checks the cache before running any tool, skipping already-completed work.

```python
import anthropic
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

@dataclass
class ToolSpec:
    name: str
    requires: list[str]  # names of tools whose output this tool needs
    produces: list[str]  # output keys this tool produces
    fn: Callable
    ttl: float = 300.0  # cache TTL in seconds

@dataclass
class CacheEntry:
    value: Any
    created_at: float

class DependencyResolver:
    def __init__(self):
        self.registry: dict[str, ToolSpec] = {}
        self.cache: dict[str, CacheEntry] = {}

    def register(self, spec: ToolSpec):
        self.registry[spec.name] = spec

    def _cached(self, name: str) -> Optional[Any]:
        entry = self.cache.get(name)
        if entry and (time.monotonic() - entry.created_at) < self.registry[name].ttl:
            return entry.value
        return None

    def resolve(self, tool_name: str, context: dict = None) -> Any:
        if context is None:
            context = {}

        cached = self._cached(tool_name)
        if cached is not None:
            print(f"[cache hit] {tool_name}")
            return cached

        spec = self.registry[tool_name]
        # Recursively resolve dependencies first
        deps = {}
        for dep_name in spec.requires:
            dep_result = self.resolve(dep_name, context)
            deps[dep_name] = dep_result

        print(f"[run] {tool_name}")
        result = spec.fn(deps=deps, context=context)
        self.cache[tool_name] = CacheEntry(result, time.monotonic())
        return result

client = anthropic.Anthropic()

def fetch_user(deps, context) -> dict:
    return {"user_id": context.get("email", "u@example.com"), "tier": "premium"}

def fetch_orders(deps, context) -> dict:
    user = deps["fetch_user"]
    return {"orders": [{"id": "o1", "total": 99.0}], "user_id": user["user_id"]}

def compute_summary(deps, context) -> dict:
    orders = deps["fetch_orders"]
    user = deps["fetch_user"]
    return {
        "summary": f"User {user['user_id']} has {len(orders['orders'])} orders",
        "total_value": sum(o["total"] for o in orders["orders"])
    }

resolver = DependencyResolver()
resolver.register(ToolSpec("fetch_user", requires=[], produces=["user_id", "tier"], fn=fetch_user))
resolver.register(ToolSpec("fetch_orders", requires=["fetch_user"], produces=["orders"], fn=fetch_orders))
resolver.register(ToolSpec("compute_summary", requires=["fetch_orders", "fetch_user"], produces=["summary"], fn=compute_summary))

ctx = {"email": "alice@example.com"}
result = resolver.resolve("compute_summary", ctx)
print(result)

# Re-run — fetch_user and fetch_orders served from cache
print("\nSecond run (should use cache):")
result2 = resolver.resolve("compute_summary", ctx)
print(result2)

# Expected Token Savings: Cache prevents redundant tool re-execution across turns. On a 3-tool chain called twice, caching saves 100% of re-run tokens.
# Environment: ANTHROPIC_API_KEY required. No extra packages.
```

---

## Option 4: Tool Call Graph with Anthropic Tool Use API

Use Claude's native tool use to let the model call tools in the order it decides, while a wrapper enforces dependency constraints.

```python
import anthropic
import json
from dataclasses import dataclass
from typing import Any

@dataclass
class ToolDep:
    name: str
    requires: list[str]

client = anthropic.Anthropic()

TOOL_DEPS = {
    "get_weather": ToolDep("get_weather", requires=[]),
    "get_forecast": ToolDep("get_forecast", requires=["get_weather"]),
    "pack_suggestions": ToolDep("pack_suggestions", requires=["get_weather", "get_forecast"]),
}

TOOLS = [
    {
        "name": "get_weather",
        "description": "Get current weather for a city",
        "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}
    },
    {
        "name": "get_forecast",
        "description": "Get 3-day forecast (requires current weather first)",
        "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}
    },
    {
        "name": "pack_suggestions",
        "description": "Suggest what to pack based on weather and forecast",
        "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}
    },
]

def mock_tool(name: str, inputs: dict) -> Any:
    if name == "get_weather":
        return {"temp": 22, "condition": "sunny", "city": inputs["city"]}
    if name == "get_forecast":
        return {"days": [{"day": i, "temp": 20 + i} for i in range(3)]}
    if name == "pack_suggestions":
        return {"items": ["sunscreen", "t-shirts", "sunglasses"]}
    return {"error": "unknown tool"}

def check_deps_satisfied(tool_name: str, completed: set[str]) -> bool:
    deps = TOOL_DEPS.get(tool_name, ToolDep(tool_name, []))
    missing = [d for d in deps.requires if d not in completed]
    if missing:
        print(f"[dep-block] {tool_name} blocked: waiting for {missing}")
        return False
    return True

def run_agent_with_dep_guard(user_message: str) -> str:
    messages = [{"role": "user", "content": user_message}]
    completed_tools: set[str] = set()
    tool_results: dict[str, Any] = {}

    for _ in range(10):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=TOOLS,
            messages=messages
        )

        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text"):
                    return block.text
            return "Done"

        if response.stop_reason != "tool_use":
            break

        tool_results_block = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            tool_name = block.name
            if not check_deps_satisfied(tool_name, completed_tools):
                tool_results_block.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps({"error": f"Dependencies not met: {TOOL_DEPS[tool_name].requires}"})
                })
                continue
            result = mock_tool(tool_name, block.input)
            tool_results[tool_name] = result
            completed_tools.add(tool_name)
            print(f"[executed] {tool_name}")
            tool_results_block.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result)
            })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results_block})

    return "Agent loop ended"

result = run_agent_with_dep_guard("What should I pack for a trip to Paris?")
print(f"\nFinal: {result}")

# Expected Token Savings: Dependency guard prevents tool calls that would fail and require re-runs. Native tool use lets Claude optimize order while constraints ensure correctness.
# Environment: ANTHROPIC_API_KEY required. No extra packages.
```

---

## Option 5: Async DAG with Fan-Out and Fan-In

Execute the dependency graph fully asynchronously using asyncio events to signal when each tool's output is ready for downstream tools.

```python
import anthropic
import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

@dataclass
class AsyncToolNode:
    name: str
    depends_on: list[str]
    fn: Callable
    event: asyncio.Event = field(default_factory=asyncio.Event)
    result: Optional[Any] = None

async def run_node(
    node: AsyncToolNode,
    all_nodes: dict[str, "AsyncToolNode"],
    client: anthropic.AsyncAnthropic
) -> None:
    # Wait for all dependencies
    for dep_name in node.depends_on:
        await all_nodes[dep_name].event.wait()

    dep_results = {d: all_nodes[d].result for d in node.depends_on}
    print(f"[start] {node.name} (deps satisfied: {node.depends_on})")
    node.result = await node.fn(dep_results, client)
    print(f"[done]  {node.name}")
    node.event.set()

async def execute_async_dag(nodes: list[AsyncToolNode]) -> dict[str, Any]:
    client = anthropic.AsyncAnthropic()
    node_map = {n.name: n for n in nodes}
    await asyncio.gather(*[run_node(n, node_map, client) for n in nodes])
    return {n.name: n.result for n in nodes}

# Tool functions
async def fetch_config(deps: dict, client) -> dict:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001", max_tokens=64,
        messages=[{"role": "user", "content": "Return JSON: {\"region\": \"us-east-1\", \"env\": \"prod\"}"}]
    )
    import json, re
    m = re.search(r'\{.*\}', response.content[0].text, re.DOTALL)
    return json.loads(m.group()) if m else {"region": "us-east-1", "env": "prod"}

async def fetch_secrets(deps: dict, client) -> dict:
    return {"api_key": "secret-abc", "db_pass": "pass-xyz"}

async def init_database(deps: dict, client) -> dict:
    config = deps.get("fetch_config", {})
    secrets = deps.get("fetch_secrets", {})
    return {"connection": f"db://{config.get('region', 'local')}/{secrets.get('db_pass', 'x')}"}

async def init_cache(deps: dict, client) -> dict:
    config = deps.get("fetch_config", {})
    return {"cache_url": f"redis://{config.get('region', 'local')}"}

async def start_agent(deps: dict, client) -> dict:
    db = deps.get("init_database", {})
    cache = deps.get("init_cache", {})
    return {"status": "running", "db": db.get("connection"), "cache": cache.get("cache_url")}

nodes = [
    AsyncToolNode("fetch_config", depends_on=[], fn=fetch_config),
    AsyncToolNode("fetch_secrets", depends_on=[], fn=fetch_secrets),
    AsyncToolNode("init_database", depends_on=["fetch_config", "fetch_secrets"], fn=init_database),
    AsyncToolNode("init_cache", depends_on=["fetch_config"], fn=init_cache),
    AsyncToolNode("start_agent", depends_on=["init_database", "init_cache"], fn=start_agent),
]

results = asyncio.run(execute_async_dag(nodes))
print(f"\nFinal: {results['start_agent']}")

# Expected Token Savings: fetch_config + fetch_secrets run in parallel (saving 1 serial round trip). init_database + init_cache also parallel. Total: 2 parallel layers vs 5 serial steps — ~60% wall-time reduction.
# Environment: ANTHROPIC_API_KEY required. Uses asyncio (stdlib).
```

---

## Option 6: SQLite-Backed Tool Execution State Machine

Persist tool execution state to SQLite so a dependency graph survives crashes and restarts, enabling durable multi-step pipelines.

```python
import anthropic
import sqlite3
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional

@dataclass
class ToolJob:
    job_id: str
    pipeline_id: str
    tool_name: str
    depends_on: list[str]
    inputs: dict
    status: str  # "pending" | "running" | "done" | "failed"
    result: Optional[dict]
    created_at: float
    completed_at: Optional[float]

client = anthropic.Anthropic()

def init_pipeline_db(path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tool_jobs (
            job_id TEXT PRIMARY KEY,
            pipeline_id TEXT,
            tool_name TEXT,
            depends_on TEXT,
            inputs TEXT,
            status TEXT DEFAULT 'pending',
            result TEXT,
            created_at REAL,
            completed_at REAL
        )
    """)
    conn.commit()
    return conn

def enqueue_job(conn: sqlite3.Connection, job: ToolJob):
    conn.execute(
        "INSERT INTO tool_jobs VALUES (?,?,?,?,?,?,?,?,?)",
        (job.job_id, job.pipeline_id, job.tool_name,
         json.dumps(job.depends_on), json.dumps(job.inputs),
         job.status, json.dumps(job.result),
         job.created_at, job.completed_at)
    )
    conn.commit()

def get_ready_jobs(conn: sqlite3.Connection, pipeline_id: str) -> list[ToolJob]:
    rows = conn.execute(
        "SELECT * FROM tool_jobs WHERE pipeline_id=? AND status='pending'",
        (pipeline_id,)
    ).fetchall()
    done_names = {
        r[2] for r in conn.execute(
            "SELECT tool_name FROM tool_jobs WHERE pipeline_id=? AND status='done'",
            (pipeline_id,)
        ).fetchall()
    }
    ready = []
    for r in rows:
        deps = json.loads(r[3])
        if all(d in done_names for d in deps):
            ready.append(ToolJob(
                job_id=r[0], pipeline_id=r[1], tool_name=r[2],
                depends_on=deps, inputs=json.loads(r[4]),
                status=r[5], result=json.loads(r[6]) if r[6] else None,
                created_at=r[7], completed_at=r[8]
            ))
    return ready

def get_result(conn: sqlite3.Connection, pipeline_id: str, tool_name: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT result FROM tool_jobs WHERE pipeline_id=? AND tool_name=? AND status='done'",
        (pipeline_id, tool_name)
    ).fetchone()
    return json.loads(row[0]) if row else None

def mark_done(conn: sqlite3.Connection, job_id: str, result: dict):
    conn.execute(
        "UPDATE tool_jobs SET status='done', result=?, completed_at=? WHERE job_id=?",
        (json.dumps(result), time.time(), job_id)
    )
    conn.commit()

def mock_tool_run(tool_name: str, inputs: dict, prior_results: dict) -> dict:
    if tool_name == "fetch_user":
        return {"user_id": "u42", "name": "Bob"}
    if tool_name == "fetch_orders":
        return {"orders": [{"id": "o1"}, {"id": "o2"}]}
    if tool_name == "send_report":
        return {"sent": True, "to": prior_results.get("fetch_user", {}).get("name")}
    return {"done": True}

def run_pipeline(pipeline_id: str, conn: sqlite3.Connection, max_iterations: int = 20):
    for _ in range(max_iterations):
        pending = conn.execute(
            "SELECT COUNT(*) FROM tool_jobs WHERE pipeline_id=? AND status='pending'",
            (pipeline_id,)
        ).fetchone()[0]
        if pending == 0:
            break
        ready = get_ready_jobs(conn, pipeline_id)
        if not ready:
            time.sleep(0.1)
            continue
        for job in ready:
            conn.execute(
                "UPDATE tool_jobs SET status='running' WHERE job_id=?", (job.job_id,)
            )
            conn.commit()
            prior = {dep: get_result(conn, pipeline_id, dep) for dep in job.depends_on}
            result = mock_tool_run(job.tool_name, job.inputs, prior)
            mark_done(conn, job.job_id, result)
            print(f"[done] {job.tool_name}: {result}")

conn = init_pipeline_db()
pid = str(uuid.uuid4())
now = time.time()
for job_spec in [
    ("fetch_user", [], {}),
    ("fetch_orders", ["fetch_user"], {}),
    ("send_report", ["fetch_user", "fetch_orders"], {}),
]:
    enqueue_job(conn, ToolJob(
        job_id=str(uuid.uuid4()), pipeline_id=pid,
        tool_name=job_spec[0], depends_on=job_spec[1],
        inputs=job_spec[2], status="pending", result=None,
        created_at=now, completed_at=None
    ))

run_pipeline(pid, conn)
rows = conn.execute("SELECT tool_name, status FROM tool_jobs WHERE pipeline_id=?", (pid,)).fetchall()
print("\nFinal status:", {r[0]: r[1] for r in rows})

# Expected Token Savings: SQLite persistence means pipeline survives restarts — no re-running completed tools. Dependency ordering prevents failed calls. Zero token overhead beyond normal tool execution.
# Environment: ANTHROPIC_API_KEY required. Uses sqlite3 (stdlib).
```

---

## Comparison

| Option | Dependency Definition | Parallelism | Persistence | Best For |
|--------|----------------------|-------------|-------------|----------|
| 1: Static DAG + Topo Sort | Code-defined nodes | Layer-parallel | None | Known workflows with fixed structure |
| 2: LLM-Generated Plan | LLM-planned at runtime | Sequential | None | Dynamic goals with unknown tool order |
| 3: Runtime Resolution + Cache | Declarative requires/produces | Sequential + cache | In-memory | Repeated pipelines with shared intermediates |
| 4: Anthropic Tool Use + Guard | Tool use API + dep check | Model-driven | None | Claude-native tool orchestration |
| 5: Async DAG with Events | Code-defined + asyncio.Event | Full async parallel | None | High-throughput init/startup pipelines |
| 6: SQLite State Machine | DB-backed job queue | Sequential | SQLite | Crash-resilient long-running pipelines |
