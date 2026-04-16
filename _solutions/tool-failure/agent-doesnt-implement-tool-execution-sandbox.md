---
layout: solution
title: "Agent Doesn't Implement Tool Execution Sandbox"
category: tool-failure
description: "Wrap tool calls in an isolated execution environment — capturing stdout/stderr, enforcing time and memory limits, blocking dangerous operations, and returning structured results instead of letting tool errors crash the agent."
tags: [tool-failure, sandbox, security, isolation, subprocess, python]
---

# Agent Doesn't Implement Tool Execution Sandbox

Agents that call tools directly share the process namespace — a tool crash kills the agent, a rogue tool can read secrets or spawn child processes, and an infinite-loop tool hangs the session. A sandbox enforces time limits, captures all output, blocks side effects, and returns structured pass/fail results regardless of what the tool does.

## Option 1: Subprocess Sandbox with Timeout and Output Capture

```python
import anthropic
import subprocess
import sys
import json

client = anthropic.Anthropic()

def sandbox_exec(code: str, timeout: int = 5) -> dict:
    """
    Execute Python code in a subprocess sandbox.
    Returns structured result with stdout, stderr, exit_code, timed_out.
    """
    try:
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "stdout":    result.stdout[:2000],
            "stderr":    result.stderr[:500],
            "exit_code": result.returncode,
            "timed_out": False,
            "success":   result.returncode == 0,
        }
    except subprocess.TimeoutExpired:
        return {
            "stdout": "", "stderr": "Timeout exceeded",
            "exit_code": -1, "timed_out": True, "success": False,
        }
    except Exception as e:
        return {
            "stdout": "", "stderr": str(e),
            "exit_code": -1, "timed_out": False, "success": False,
        }

def agent_with_sandbox(task: str) -> str:
    """Ask the model to write code, then sandbox-execute it."""
    # Step 1: generate code
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": f"Write a Python one-liner (no imports) that: {task}\nOutput ONLY the code.",
        }],
    )
    code = resp.content[0].text.strip().strip("```python").strip("```").strip()

    # Step 2: sandbox execute
    result = sandbox_exec(code, timeout=3)
    print(f"Code:     {code[:60]!r}")
    print(f"Success:  {result['success']}")
    print(f"Stdout:   {result['stdout'][:80]!r}")
    if result["stderr"]:
        print(f"Stderr:   {result['stderr'][:60]!r}")
    return result["stdout"] if result["success"] else f"ERROR: {result['stderr']}"

tasks = [
    "prints the sum of 1 to 100",
    "prints the first 5 fibonacci numbers",
    "prints the current working directory",
]
for task in tasks:
    print(f"\nTask: {task}")
    output = agent_with_sandbox(task)
    print(f"Output: {output[:80]}")

# Expected Token Savings: Sandbox prevents retry loops from crashed tools; structured result is compact
# Environment: subprocess isolation prevents file system writes and network calls from affecting agent process
```

## Option 2: Resource-Limited Sandbox with Memory and CPU Guards

```python
import anthropic
import subprocess
import sys
import resource
import os
import textwrap

client = anthropic.Anthropic()

SANDBOX_WRAPPER = textwrap.dedent("""\
import resource, sys, signal

# Hard memory limit: 64 MB
resource.setrlimit(resource.RLIMIT_AS, (64 * 1024 * 1024, 64 * 1024 * 1024))
# Hard CPU time: 3 seconds
resource.setrlimit(resource.RLIMIT_CPU, (3, 3))
# No new file descriptors beyond stdin/stdout/stderr
resource.setrlimit(resource.RLIMIT_NOFILE, (10, 10))

{user_code}
""")

def sandbox_exec_with_limits(code: str, timeout: int = 5) -> dict:
    """Subprocess sandbox with OS-level resource limits."""
    wrapped = SANDBOX_WRAPPER.format(user_code=code)
    try:
        proc = subprocess.run(
            [sys.executable, "-c", wrapped],
            capture_output=True,
            text=True,
            timeout=timeout,
            # Additional env isolation: strip most env vars
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        )
        return {
            "stdout":    proc.stdout[:2000],
            "stderr":    proc.stderr[:500],
            "exit_code": proc.returncode,
            "timed_out": False,
            "success":   proc.returncode == 0,
            "oom":       "MemoryError" in proc.stderr or proc.returncode == -9,
        }
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": "Wall-clock timeout", "exit_code": -1,
                "timed_out": True, "success": False, "oom": False}

# Test: normal computation
r = sandbox_exec_with_limits("print(sum(range(1000)))")
print(f"Normal: {r['stdout'].strip()} | ok={r['success']}")

# Test: memory bomb attempt
r = sandbox_exec_with_limits("x = 'A' * (100 * 1024 * 1024); print('leaked')")
print(f"MemBomb: success={r['success']} oom={r.get('oom')} stderr={r['stderr'][:50]!r}")

# Test: infinite loop — wall clock timeout fires
r = sandbox_exec_with_limits("while True: pass", timeout=2)
print(f"InfLoop: timed_out={r['timed_out']}")

# Expected Token Savings: Resource limits prevent runaway tools from consuming host RAM/CPU
# Environment: resource module is Unix-only; use Docker/containers for Windows or cross-platform isolation
```

## Option 3: Allowlist-Based Tool Input Validator Before Execution

```python
import anthropic
import re
import subprocess
import sys

client = anthropic.Anthropic()

# Allowlist of safe operations; blocklist of dangerous patterns
BLOCKED_PATTERNS = [
    r"\bos\.system\b",
    r"\bsubprocess\b",
    r"\b__import__\b",
    r"\bopen\s*\(",
    r"\beval\s*\(",
    r"\bexec\s*\(",
    r"\bimport\s+os\b",
    r"\bimport\s+sys\b",
    r"\bimport\s+shutil\b",
    r"\brm\b",
    r"\bchmod\b",
    r"socket",
    r"urllib",
    r"requests",
]

ALLOWED_IMPORTS = {"math", "re", "json", "datetime", "collections", "itertools", "functools"}

def validate_code(code: str) -> tuple[bool, str]:
    """Static analysis before execution — reject dangerous patterns."""
    for pattern in BLOCKED_PATTERNS:
        if re.search(pattern, code):
            return False, f"Blocked pattern: {pattern}"

    # Check imports against allowlist
    imports = re.findall(r"\bimport\s+(\w+)", code)
    for imp in imports:
        if imp not in ALLOWED_IMPORTS:
            return False, f"Import not allowed: {imp}"

    return True, "ok"

def sandboxed_tool_call(tool_name: str, code: str, timeout: int = 4) -> dict:
    """Validate then execute in subprocess sandbox."""
    valid, reason = validate_code(code)
    if not valid:
        return {"tool": tool_name, "success": False,
                "error": f"Validation failed: {reason}", "stdout": ""}

    try:
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=timeout,
        )
        return {
            "tool": tool_name,
            "success": result.returncode == 0,
            "stdout": result.stdout[:1000],
            "stderr": result.stderr[:200],
            "exit_code": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"tool": tool_name, "success": False,
                "error": "Timeout", "stdout": ""}

# Safe tool
r = sandboxed_tool_call("calculator", "import math; print(math.sqrt(144))")
print(f"Safe: {r['stdout'].strip()} | ok={r['success']}")

# Blocked: os.system
r = sandboxed_tool_call("shell", "import os; os.system('ls')")
print(f"Blocked os.system: ok={r['success']} error={r['error']!r}")

# Blocked: disallowed import
r = sandboxed_tool_call("network", "import requests; print(requests.get('http://example.com'))")
print(f"Blocked requests: ok={r['success']} error={r['error']!r}")

# Expected Token Savings: Validation rejection is instant; no subprocess spawn for blocked code
# Environment: static analysis catches obvious attacks; combine with subprocess isolation for defense-in-depth
```

## Option 4: Tool Result Normalizer — Structured Output Regardless of Tool Behavior

```python
import anthropic
import subprocess
import sys
import json
import time

client = anthropic.Anthropic()

def run_tool(tool_fn, *args, timeout: int = 5, **kwargs) -> dict:
    """
    Wrap any callable tool, capturing exceptions and normalizing output.
    Returns a consistent ToolResult dict regardless of what the tool does.
    """
    t0 = time.monotonic()
    try:
        import signal

        def handler(signum, frame):
            raise TimeoutError("Tool timeout")

        signal.signal(signal.SIGALRM, handler)
        signal.alarm(timeout)
        try:
            result = tool_fn(*args, **kwargs)
        finally:
            signal.alarm(0)

        elapsed = (time.monotonic() - t0) * 1000
        return {
            "success": True,
            "result": result,
            "error": None,
            "timed_out": False,
            "duration_ms": round(elapsed, 1),
        }
    except TimeoutError:
        return {"success": False, "result": None, "error": "Timeout",
                "timed_out": True, "duration_ms": timeout * 1000}
    except Exception as e:
        elapsed = (time.monotonic() - t0) * 1000
        return {"success": False, "result": None, "error": f"{type(e).__name__}: {e}",
                "timed_out": False, "duration_ms": round(elapsed, 1)}

# Example tools — some well-behaved, some broken
def good_tool(n: int) -> str:
    return f"Result: {n * n}"

def crashing_tool(n: int) -> str:
    raise ValueError(f"Tool crashed with input {n}")

def slow_tool(n: int) -> str:
    import time; time.sleep(10)
    return "never reached"

def build_tool_message(tool_result: dict, tool_name: str) -> str:
    if tool_result["success"]:
        return str(tool_result["result"])
    return (f"Tool '{tool_name}' failed: {tool_result['error']}. "
            f"Duration: {tool_result['duration_ms']:.0f}ms. "
            "Please handle this error or try an alternative approach.")

# Agent loop with sandboxed tool calls
tools_to_call = [
    ("calculator", good_tool, 7),
    ("risky_tool", crashing_tool, 3),
    ("slow_tool",  slow_tool, 1),
]

messages = []
for tool_name, fn, arg in tools_to_call:
    result = run_tool(fn, arg, timeout=2)
    tool_msg = build_tool_message(result, tool_name)
    messages.append({"role": "user", "content": f"Tool {tool_name} returned: {tool_msg}"})
    print(f"[{tool_name}] success={result['success']} {result['duration_ms']:.0f}ms "
          f"| {tool_msg[:60]!r}")

resp = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=150,
    messages=messages + [{"role": "user", "content": "Summarize what happened with each tool."}],
)
print(f"\nAgent summary: {resp.content[0].text[:120]}")

# Expected Token Savings: Normalized error messages are short; no stack traces injected into context
# Environment: signal.SIGALRM is Unix-only; use threading.Timer for cross-platform timeout
```

## Option 5: Sandboxed Tool Registry with Per-Tool Permissions

```python
import anthropic
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Callable

client = anthropic.Anthropic()

@dataclass
class ToolPolicy:
    name: str
    timeout_s: int = 5
    allow_network: bool = False
    allow_filesystem: bool = False
    allow_subprocess: bool = False
    max_output_chars: int = 2000

@dataclass
class ToolRegistry:
    _tools: dict[str, tuple[Callable, ToolPolicy]] = field(default_factory=dict)

    def register(self, fn: Callable, policy: ToolPolicy):
        self._tools[policy.name] = (fn, policy)

    def call(self, name: str, *args, **kwargs) -> dict:
        if name not in self._tools:
            return {"success": False, "error": f"Unknown tool: {name}"}
        fn, policy = self._tools[name]
        return self._sandboxed_call(fn, policy, *args, **kwargs)

    def _sandboxed_call(self, fn: Callable, policy: ToolPolicy, *args, **kwargs) -> dict:
        import threading
        result_box = {}

        def run():
            try:
                result_box["result"] = fn(*args, **kwargs)
                result_box["success"] = True
            except Exception as e:
                result_box["result"] = None
                result_box["success"] = False
                result_box["error"] = f"{type(e).__name__}: {e}"

        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(timeout=policy.timeout_s)
        if t.is_alive():
            return {"success": False, "error": "Timeout", "timed_out": True}

        if result_box.get("success"):
            raw = str(result_box["result"])
            return {"success": True, "result": raw[:policy.max_output_chars]}
        return {"success": False, "error": result_box.get("error", "unknown")}

# Register tools with different permission levels
registry = ToolRegistry()

registry.register(
    lambda n: sum(range(n)),
    ToolPolicy("sum_tool", timeout_s=2, max_output_chars=100),
)
registry.register(
    lambda s: s.upper(),
    ToolPolicy("upper_tool", timeout_s=1),
)
registry.register(
    lambda: (_ for _ in ()).throw(RuntimeError("always fails")),
    ToolPolicy("broken_tool", timeout_s=2),
)

# Use registry in agent
for call in [("sum_tool", 100), ("upper_tool", "hello world"), ("broken_tool",), ("unknown_tool",)]:
    name, *args = call
    r = registry.call(name, *args)
    print(f"[{name}] success={r['success']} | "
          f"{r.get('result', r.get('error', ''))!r:.50s}")

# Expected Token Savings: Registry centralizes policy; per-tool timeouts prevent one slow tool from blocking all
# Environment: threading.Thread timeout is cross-platform; policy.allow_network/filesystem for future enforcement
```

## Option 6: Sandbox with SQLite Execution Audit Trail

```python
import anthropic
import subprocess
import sys
import sqlite3
import time
import hashlib

client = anthropic.Anthropic()
DB = "tool_sandbox_audit.db"

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS tool_executions (
            exec_id TEXT, tool_name TEXT, code_hash TEXT,
            success INTEGER, timed_out INTEGER,
            exit_code INTEGER, output_len INTEGER,
            duration_ms REAL, ts REAL
        )
    """)
    con.commit(); con.close()

def audited_sandbox(tool_name: str, code: str, timeout: int = 5) -> dict:
    """Execute in subprocess sandbox and audit result to SQLite."""
    init_db()
    code_hash = hashlib.sha256(code.encode()).hexdigest()[:12]
    exec_id = f"{tool_name}-{int(time.time()*1000)}"
    t0 = time.monotonic()

    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=timeout,
        )
        duration_ms = (time.monotonic() - t0) * 1000
        result = {
            "exec_id": exec_id,
            "success": proc.returncode == 0,
            "timed_out": False,
            "exit_code": proc.returncode,
            "stdout": proc.stdout[:2000],
            "stderr": proc.stderr[:300],
            "duration_ms": round(duration_ms, 1),
        }
    except subprocess.TimeoutExpired:
        duration_ms = (time.monotonic() - t0) * 1000
        result = {
            "exec_id": exec_id,
            "success": False, "timed_out": True,
            "exit_code": -1, "stdout": "", "stderr": "Timeout",
            "duration_ms": round(duration_ms, 1),
        }

    con = sqlite3.connect(DB)
    con.execute(
        "INSERT INTO tool_executions VALUES (?,?,?,?,?,?,?,?,?)",
        (exec_id, tool_name, code_hash,
         int(result["success"]), int(result["timed_out"]),
         result["exit_code"], len(result["stdout"]),
         result["duration_ms"], time.time()),
    )
    con.commit(); con.close()
    return result

def audit_report():
    con = sqlite3.connect(DB)
    rows = con.execute("""
        SELECT tool_name,
               COUNT(*) calls,
               SUM(success) successes,
               SUM(timed_out) timeouts,
               ROUND(AVG(duration_ms),1) avg_ms
        FROM tool_executions GROUP BY tool_name
    """).fetchall()
    con.close()
    print("\nTool Execution Audit:")
    for r in rows:
        print(f"  {r[0]:15s} calls={r[1]} ok={r[2]} timeout={r[3]} avg={r[4]}ms")

# Run several tool calls
calls = [
    ("math_tool",    "print(2**10)"),
    ("sort_tool",    "print(sorted([3,1,2]))"),
    ("crash_tool",   "raise ValueError('simulated crash')"),
    ("timeout_tool", "import time; time.sleep(10)"),
    ("math_tool",    "print(sum(range(50)))"),
]
for name, code in calls:
    r = audited_sandbox(name, code, timeout=2)
    status = "ok" if r["success"] else ("timeout" if r["timed_out"] else "fail")
    print(f"[{name:14s}] {status:7s} {r['duration_ms']:6.1f}ms | {(r['stdout'] or r['stderr'])[:40]!r}")

audit_report()

# Expected Token Savings: Audit log reveals flaky tools before they become context-corrupting failures
# Environment: SQLite audit is append-only; query tool_executions for SLA monitoring and retry budgeting
```

## Comparison

| Option | Isolation | Resource Limits | Input Validation | Audit Log |
|--------|----------|----------------|-----------------|-----------|
| 1 — Subprocess + Timeout | Subprocess | Wall-clock only | No | No |
| 2 — OS Resource Limits | Subprocess + OS | Memory + CPU | No | No |
| 3 — Allowlist Validator | Subprocess | Wall-clock | Static analysis | No |
| 4 — Result Normalizer | In-process (signal) | Wall-clock | No | No |
| 5 — Registry + Policy | Thread isolation | Per-tool timeout | Policy flags | No |
| 6 — Audited Sandbox | Subprocess | Wall-clock | No | SQLite |
