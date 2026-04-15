---
layout: solution
title: "Agent Calls Tools in Wrong Order Causing Dependency Failure"
category: tool-failure
description: "Agent invokes a tool whose preconditions are not yet satisfied — calling write_file before create_directory, or read_secret before authenticate — causing cascading errors that consume tokens without progress."
tags: [tool-failure, ordering, dependencies, reliability, production]
---

## Symptom

The agent calls `write_file("/data/reports/output.csv", ...)` before creating the `/data/reports/` directory, or calls `read_secret("db_password")` before establishing a session. The dependent tool returns an error, the agent retries with the same tool, or backtracks and calls the prerequisite — but by then several extra turns of tokens are spent recovering state that should have been established first.

## Root Cause

LLMs plan tool calls based on tool descriptions alone. Without explicit precondition documentation, dependency enforcement, or a DAG-based execution plan, the model may infer the wrong ordering — especially when tools have short descriptions, when the user's request implies a high-level goal without mentioning intermediate steps, or when the model's training data included patterns where tool calls appeared in a different sequence.

## Fix

### Option 1 — Embed preconditions in tool descriptions

```python
import anthropic
import json
import os

client = anthropic.Anthropic()

# Explicit preconditions in tool descriptions guide the model to correct ordering
tools = [
    {
        "name": "create_directory",
        "description": "Create a directory at the given path. Call this BEFORE write_file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path to create."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": (
            "Write content to a file. "
            "PRECONDITION: call create_directory first if the parent directory may not exist."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path":    {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "compress_file",
        "description": (
            "Compress a file with gzip. "
            "PRECONDITION: the file at 'path' must already exist (call write_file first)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to existing file."},
            },
            "required": ["path"],
        },
    },
]

def handle_tool(name: str, inp: dict) -> str:
    if name == "create_directory":
        os.makedirs(inp["path"], exist_ok=True)
        return f"Directory '{inp['path']}' ready."
    if name == "write_file":
        parent = os.path.dirname(inp["path"])
        if parent and not os.path.exists(parent):
            return f"ERROR: parent directory '{parent}' does not exist. Call create_directory first."
        with open(inp["path"], "w") as f:
            f.write(inp["content"])
        return f"Wrote {len(inp['content'])} bytes to '{inp['path']}'."
    if name == "compress_file":
        if not os.path.exists(inp["path"]):
            return f"ERROR: '{inp['path']}' not found. Call write_file first."
        return f"Compressed '{inp['path']}' (simulated)."
    return "unknown tool"

def agent_loop(user_msg: str) -> None:
    messages = [{"role": "user", "content": user_msg}]
    while True:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            tools=tools,
            messages=messages,
        )
        if resp.stop_reason != "tool_use":
            print(f"[agent] {resp.content[0].text}")
            break
        messages.append({"role": "assistant", "content": resp.content})
        tool_results = []
        for block in resp.content:
            if block.type == "tool_use":
                result = handle_tool(block.name, block.input)
                print(f"[tool] {block.name}({block.input}) → {result}")
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
        messages.append({"role": "user", "content": tool_results})

agent_loop("Create a report file at /tmp/reports/output.csv with the content 'id,name' and compress it.")
```

**Expected Token Savings:** Correct first-call ordering avoids the error-detect-retry cycle (typically 2–4 extra turns); PRECONDITION language in descriptions is cheap to add and consistently respected by Claude.
**Environment:** File system tools, database setup tools, any multi-step workflow with clear ordering.

---

### Option 2 — Server-side precondition check with structured error

```python
import anthropic
import json
import os

client = anthropic.Anthropic()

# Track server-side state to enforce ordering
class ToolState:
    def __init__(self):
        self.authenticated = False
        self.session_id: str | None = None
        self.directories: set[str] = set()

state = ToolState()

def authenticate(username: str, password: str) -> dict:
    if username == "admin" and password == "secret":
        state.authenticated = True
        state.session_id = "sess-abc123"
        return {"status": "ok", "session_id": state.session_id}
    return {"status": "error", "message": "Invalid credentials."}

def read_secret(key: str) -> dict:
    if not state.authenticated:
        return {
            "status": "precondition_failed",
            "required": "authenticate",
            "message": "You must call authenticate() before read_secret(). Call authenticate first.",
        }
    return {"status": "ok", "value": f"mock-value-for-{key}"}

def make_dir(path: str) -> dict:
    if not state.authenticated:
        return {"status": "precondition_failed", "required": "authenticate",
                "message": "Must authenticate before creating directories."}
    os.makedirs(path, exist_ok=True)
    state.directories.add(path)
    return {"status": "ok", "path": path}

def write_file(path: str, content: str) -> dict:
    parent = os.path.dirname(path)
    if parent and parent not in state.directories and not os.path.isdir(parent):
        return {
            "status": "precondition_failed",
            "required": "make_dir",
            "message": f"Directory '{parent}' must exist. Call make_dir('{parent}') first.",
        }
    with open(path, "w") as f:
        f.write(content)
    return {"status": "ok", "bytes_written": len(content)}

TOOL_HANDLERS = {
    "authenticate": lambda inp: authenticate(inp["username"], inp["password"]),
    "read_secret":  lambda inp: read_secret(inp["key"]),
    "make_dir":     lambda inp: make_dir(inp["path"]),
    "write_file":   lambda inp: write_file(inp["path"], inp["content"]),
}

tools = [
    {"name": "authenticate",
     "description": "Authenticate with the system. Must be called first before any other tool.",
     "input_schema": {"type": "object",
                      "properties": {"username": {"type": "string"}, "password": {"type": "string"}},
                      "required": ["username", "password"]}},
    {"name": "read_secret",
     "description": "Read a secret value. PRECONDITION: authenticate must succeed first.",
     "input_schema": {"type": "object",
                      "properties": {"key": {"type": "string"}},
                      "required": ["key"]}},
    {"name": "make_dir",
     "description": "Create directory. PRECONDITION: authenticate must succeed first.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"}},
                      "required": ["path"]}},
    {"name": "write_file",
     "description": "Write a file. PRECONDITION: make_dir for the parent directory must have been called.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                      "required": ["path", "content"]}},
]

def agent_loop(user_msg: str) -> None:
    messages = [{"role": "user", "content": user_msg}]
    for _ in range(10):
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=1024, tools=tools, messages=messages
        )
        if resp.stop_reason != "tool_use":
            print(f"[agent] {resp.content[0].text}")
            break
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for block in resp.content:
            if block.type == "tool_use":
                result = TOOL_HANDLERS[block.name](block.input)
                print(f"[tool] {block.name} → {result}")
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                 "content": json.dumps(result)})
        messages.append({"role": "user", "content": results})

agent_loop("Log in as admin/secret, then write 'hello' to /tmp/testdir/hello.txt.")
```

**Expected Token Savings:** Structured `precondition_failed` responses include the exact next action — the agent recovers in one turn rather than reasoning through the error from scratch.
**Environment:** Multi-step workflows with stateful preconditions (auth, directory creation, DB migration); prevents the agent from guessing recovery steps.

---

### Option 3 — DAG-based task planner: enforce topological order

```python
import anthropic
import json
from collections import defaultdict, deque

client = anthropic.Anthropic()

# Define a DAG of tool dependencies
TOOL_DEPS: dict[str, list[str]] = {
    "authenticate":   [],
    "make_dir":       ["authenticate"],
    "write_file":     ["authenticate", "make_dir"],
    "compress_file":  ["write_file"],
    "upload_file":    ["compress_file", "authenticate"],
}

def topological_order(goal_tool: str) -> list[str]:
    """Return tools in the order they must be called to reach goal_tool."""
    visited: set[str] = set()
    order: list[str] = []

    def dfs(tool: str):
        if tool in visited:
            return
        for dep in TOOL_DEPS.get(tool, []):
            dfs(dep)
        visited.add(tool)
        if tool not in order:
            order.append(tool)

    dfs(goal_tool)
    return order

def plan_and_execute(goal: str, goal_tool: str, tool_inputs: dict[str, dict]) -> None:
    order = topological_order(goal_tool)
    print(f"[planner] execution order: {' → '.join(order)}")

    completed: set[str] = set()
    for tool in order:
        inp = tool_inputs.get(tool, {})
        print(f"[exec] {tool}({inp})")
        # Verify all deps completed
        missing = [d for d in TOOL_DEPS.get(tool, []) if d not in completed]
        if missing:
            raise RuntimeError(f"BUG: {tool} called before: {missing}")
        completed.add(tool)
        print(f"[exec] {tool} ✓")

    print(f"[planner] goal '{goal}' completed via {len(order)} steps")

# Example: goal is upload_file — planner derives the full dependency chain
plan_and_execute(
    goal="Upload compressed report",
    goal_tool="upload_file",
    tool_inputs={
        "authenticate":  {"username": "admin", "password": "secret"},
        "make_dir":      {"path": "/tmp/reports"},
        "write_file":    {"path": "/tmp/reports/data.csv", "content": "id,value"},
        "compress_file": {"path": "/tmp/reports/data.csv"},
        "upload_file":   {"source": "/tmp/reports/data.csv.gz", "dest": "s3://bucket/data.csv.gz"},
    },
)
```

**Expected Token Savings:** Topological pre-planning eliminates all out-of-order tool calls; the agent never sees a precondition error because the planner guarantees correct ordering before any tool runs.
**Environment:** Complex multi-step pipelines (ETL, deployment, data migration); agents with 5+ tools that have non-obvious dependency chains.

---

### Option 4 — Stateful tool wrapper that auto-satisfies prerequisites

```python
import anthropic
import json
import os

client = anthropic.Anthropic()

class AutoPrereqToolkit:
    """
    Wraps tools with automatic prerequisite execution.
    If write_file is called without the parent directory existing, it creates it first.
    """

    def __init__(self):
        self._authenticated = False

    def _ensure_auth(self) -> None:
        if not self._authenticated:
            print("[prereq] auto-authenticating...")
            self._authenticated = True  # simulate auth

    def _ensure_dir(self, path: str) -> None:
        parent = os.path.dirname(path)
        if parent and not os.path.isdir(parent):
            print(f"[prereq] auto-creating directory '{parent}'")
            os.makedirs(parent, exist_ok=True)

    def create_directory(self, path: str) -> str:
        self._ensure_auth()
        os.makedirs(path, exist_ok=True)
        return f"Directory ready: {path}"

    def write_file(self, path: str, content: str) -> str:
        self._ensure_auth()
        self._ensure_dir(path)   # auto-satisfy dir prerequisite
        with open(path, "w") as f:
            f.write(content)
        return f"Written: {path} ({len(content)} bytes)"

    def compress_file(self, path: str) -> str:
        self._ensure_auth()
        if not os.path.exists(path):
            raise FileNotFoundError(f"Cannot compress missing file: {path}")
        return f"Compressed (simulated): {path}.gz"

    def read_secret(self, key: str) -> str:
        self._ensure_auth()   # auto-satisfy auth prerequisite
        return f"mock-{key}-value"

toolkit = AutoPrereqToolkit()

tools = [
    {"name": "write_file",   "description": "Write a file (creates parent dir automatically).",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "compress_file","description": "Compress an existing file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "read_secret",  "description": "Read a secret (authenticates automatically).",
     "input_schema": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}},
]

DISPATCH = {
    "write_file":   lambda i: toolkit.write_file(i["path"], i["content"]),
    "compress_file":lambda i: toolkit.compress_file(i["path"]),
    "read_secret":  lambda i: toolkit.read_secret(i["key"]),
}

def agent_loop(user_msg: str) -> None:
    messages = [{"role": "user", "content": user_msg}]
    for _ in range(8):
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=512, tools=tools, messages=messages
        )
        if resp.stop_reason != "tool_use":
            print(f"[agent] {resp.content[0].text}")
            break
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for block in resp.content:
            if block.type == "tool_use":
                result = DISPATCH[block.name](block.input)
                print(f"[tool] {block.name} → {result}")
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
        messages.append({"role": "user", "content": results})

agent_loop("Write 'hello world' to /tmp/newdir/file.txt and then compress it.")
```

**Expected Token Savings:** Auto-satisfying prerequisites eliminates error-recovery turns entirely; the agent reaches its goal in the minimum possible number of turns.
**Environment:** Tools with boring but mandatory prerequisites (auth, directory creation) that should be transparent to the agent's high-level plan.

---

### Option 5 — Retry with dependency injection on precondition error

```python
import anthropic
import json

client = anthropic.Anthropic()

# Registry mapping prerequisite errors to the tool that fixes them
PREREQ_REMEDIES: dict[str, str] = {
    "NOT_AUTHENTICATED": "authenticate",
    "MISSING_DIRECTORY":  "create_directory",
    "SESSION_EXPIRED":    "authenticate",
}

class PreconditionError(Exception):
    def __init__(self, code: str, context: dict):
        super().__init__(code)
        self.code    = code
        self.context = context

def authenticate() -> str:
    return "authenticated:sess-xyz"

def create_directory(path: str) -> str:
    import os
    os.makedirs(path, exist_ok=True)
    return f"dir-created:{path}"

def write_secret_file(path: str, secret: str, _state: dict) -> str:
    if not _state.get("auth"):
        raise PreconditionError("NOT_AUTHENTICATED", {"next_tool": "authenticate"})
    import os
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        raise PreconditionError("MISSING_DIRECTORY", {"next_tool": "create_directory", "path": parent})
    with open(path, "w") as f:
        f.write(secret)
    return f"file-written:{path}"

def execute_with_retry(tool_name: str, args: dict, state: dict, max_retries: int = 3) -> str:
    for attempt in range(max_retries):
        try:
            if tool_name == "write_secret_file":
                return write_secret_file(args["path"], args["secret"], state)
            raise ValueError(f"Unknown tool: {tool_name}")
        except PreconditionError as e:
            remedy = e.context.get("next_tool")
            print(f"[retry] attempt {attempt+1}: precondition '{e.code}' — running '{remedy}' first")
            if remedy == "authenticate":
                state["auth"] = authenticate()
            elif remedy == "create_directory":
                create_directory(e.context.get("path", args.get("path", "")))
            else:
                raise
    raise RuntimeError(f"Could not satisfy preconditions for {tool_name} after {max_retries} retries")

state: dict = {}
result = execute_with_retry(
    "write_secret_file",
    {"path": "/tmp/secrets/key.txt", "secret": "my-api-key"},
    state,
)
print(f"[result] {result}")
```

**Expected Token Savings:** Precondition errors are resolved in the tool layer, not the LLM layer — zero extra Claude API calls for dependency resolution; the agent receives a success result on the next turn.
**Environment:** Tool implementations where some prerequisites are transient (session expiry, directory cleanup) and must be auto-resolved at runtime.

---

### Option 6 — Ordered tool call manifest: agent submits a plan first

```python
import anthropic
import json
import os

client = anthropic.Anthropic()

PLAN_TOOL = {
    "name": "submit_execution_plan",
    "description": (
        "Before calling any other tool, submit a JSON list of tool calls in the order "
        "you will execute them. The system validates dependencies and returns approval or errors."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "tool": {"type": "string"},
                        "args": {"type": "object"},
                    },
                    "required": ["tool"],
                },
                "description": "Ordered list of tool calls.",
            }
        },
        "required": ["steps"],
    },
}

DEPENDENCY_RULES: dict[str, list[str]] = {
    "write_file":    ["create_directory"],
    "compress_file": ["write_file"],
    "upload_file":   ["compress_file", "authenticate"],
}

def validate_plan(steps: list[dict]) -> dict:
    """Check that dependencies appear before the tools that need them."""
    called = set()
    violations = []
    for step in steps:
        tool = step["tool"]
        deps = DEPENDENCY_RULES.get(tool, [])
        missing = [d for d in deps if d not in called]
        if missing:
            violations.append(f"'{tool}' requires {missing} to come first.")
        called.add(tool)
    if violations:
        return {"approved": False, "violations": violations}
    return {"approved": True, "execution_order": [s["tool"] for s in steps]}

def execute_plan(steps: list[dict]) -> list[str]:
    results = []
    for step in steps:
        tool = step["tool"]
        args = step.get("args", {})
        print(f"[exec] {tool}({args})")
        if tool == "create_directory":
            os.makedirs(args["path"], exist_ok=True)
            results.append(f"dir created: {args['path']}")
        elif tool == "write_file":
            with open(args["path"], "w") as f:
                f.write(args.get("content", ""))
            results.append(f"file written: {args['path']}")
        elif tool == "compress_file":
            results.append(f"compressed (simulated): {args['path']}")
        elif tool == "authenticate":
            results.append("authenticated")
        else:
            results.append(f"unknown tool: {tool}")
    return results

tools = [PLAN_TOOL]

def agent_loop(user_msg: str) -> None:
    messages = [{"role": "user", "content": user_msg}]
    plan_approved = False
    approved_steps = []
    for _ in range(5):
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=1024, tools=tools, messages=messages
        )
        if resp.stop_reason != "tool_use":
            print(f"[agent] {resp.content[0].text}")
            if plan_approved:
                results = execute_plan(approved_steps)
                print(f"[exec] completed: {results}")
            break
        messages.append({"role": "assistant", "content": resp.content})
        tool_results = []
        for block in resp.content:
            if block.type == "tool_use" and block.name == "submit_execution_plan":
                validation = validate_plan(block.input["steps"])
                print(f"[plan] {validation}")
                if validation["approved"]:
                    plan_approved = True
                    approved_steps = block.input["steps"]
                tool_results.append({"type": "tool_result", "tool_use_id": block.id,
                                      "content": json.dumps(validation)})
        messages.append({"role": "user", "content": tool_results})

agent_loop("Create directory /tmp/out, write data.csv there, then compress it.")
```

**Expected Token Savings:** Plan validation catches ordering errors before any execution token is spent; the agent corrects its plan in one turn instead of recovering from cascading runtime failures.
**Environment:** High-stakes agentic pipelines (deployment, data migration) where failed steps have side effects; replaces ad-hoc tool calling with an explicit plan-then-execute workflow.

---

## Comparison

| Option | Enforcement Layer | Extra Turns on Error | Auto-Recovery | Plan Validation | Best For |
|---|---|---|---|---|---|
| 1. Description hints | Prompt/LLM | 0–1 (usually avoids) | No | No | Simple tools; low-risk workflows |
| 2. Structured precondition errors | Tool server | 1 (clear next step) | No | No | Stateful auth + directory deps |
| 3. DAG planner | Pre-execution | 0 (plan first) | N/A | Yes (topological) | Complex pipelines; 5+ tool deps |
| 4. Auto-satisfy prerequisites | Tool wrapper | 0 (transparent) | Yes | No | Boring prereqs (auth, mkdir) |
| 5. Retry + dependency injection | Tool wrapper | 0 (retry in layer) | Yes | No | Transient prerequisites (session expiry) |
| 6. Plan manifest + validation | Tool + LLM | 1 (plan correction) | No | Yes (explicit) | High-stakes; human-auditable plans |
