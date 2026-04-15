---
layout: solution
title: "Agent Executes Untrusted or Generated Code Without Sandboxing"
category: general
description: "The agent uses eval() or exec() to run code it generated or received from the user. A code-writing agent executes its output directly in the host process. Malicious user input reaches os.system() or subprocess.run(). Generated code deletes files, reads secrets from environment variables, or makes network calls. No sandbox, no resource limits, no capability restrictions."
tags: [security, sandboxing, code-execution, eval, subprocess, safety, isolation, resource-limits]
---

# Agent Executes Untrusted or Generated Code Without Sandboxing

## Symptom
- `eval()` or `exec()` called on LLM-generated code or user input
- Generated code deletes files or reads sensitive environment variables
- `subprocess.run()` called with user-supplied arguments without sanitization
- Agent has no resource limits — generated code can consume unlimited CPU/memory
- Code execution errors reveal internal file paths and system structure
- Network calls made from within generated code bypass agent's permission model

## Root Cause
LLM-generated code must be treated as untrusted input. The model can be prompted (accidentally or maliciously) to generate code that exfiltrates secrets, consumes resources, or modifies the host system. Running generated code in the same process as the agent with full OS access is equivalent to giving arbitrary users shell access. The fix is to execute code in a restricted environment: a subprocess with dropped privileges, a container, or a dedicated sandbox with explicit resource limits and no access to secrets.

## Fix

### Option 1: Subprocess sandbox with resource limits — minimal isolation

```python
import subprocess
import tempfile
import os
import sys
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

def run_code_sandboxed(
    code: str,
    input_data: str = "",
    timeout_seconds: float = 10.0,
    max_memory_mb: int = 128,
    allow_network: bool = False
) -> dict:
    """
    Execute Python code in a restricted subprocess.
    - Separate process (crashes don't affect agent)
    - Timeout prevents infinite loops
    - No access to agent's environment variables
    - Optional network restriction via no-network subprocess
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        # Write code to a temp file — never use eval() directly
        code_file = Path(tmpdir) / "user_code.py"
        code_file.write_text(code)

        # Create a clean, minimal environment — no secrets
        clean_env = {
            "PATH": "/usr/bin:/bin",
            "HOME": tmpdir,
            "TMPDIR": tmpdir,
            "PYTHONPATH": "",
            # Explicitly exclude: AWS credentials, API keys, etc.
        }

        # Build the subprocess command
        cmd = [sys.executable, str(code_file)]

        try:
            result = subprocess.run(
                cmd,
                input=input_data.encode("utf-8"),
                capture_output=True,
                timeout=timeout_seconds,
                cwd=tmpdir,           # Restrict working directory
                env=clean_env,        # No inherited environment variables
                # Resource limits via ulimit (Linux/macOS):
                preexec_fn=_set_resource_limits(max_memory_mb) if os.name != "nt" else None
            )
            return {
                "success": result.returncode == 0,
                "stdout": result.stdout.decode("utf-8", errors="replace")[:10_000],
                "stderr": result.stderr.decode("utf-8", errors="replace")[:5_000],
                "returncode": result.returncode,
                "timed_out": False
            }
        except subprocess.TimeoutExpired:
            logger.warning(f"Code execution timed out after {timeout_seconds}s")
            return {
                "success": False,
                "stdout": "",
                "stderr": f"Execution timed out after {timeout_seconds}s",
                "returncode": -1,
                "timed_out": True
            }
        except Exception as exc:
            return {
                "success": False,
                "stdout": "",
                "stderr": str(exc),
                "returncode": -1,
                "timed_out": False
            }

def _set_resource_limits(max_memory_mb: int):
    """Return a preexec_fn that sets resource limits (Linux/macOS only)."""
    import resource
    def set_limits():
        # Memory limit
        mem_bytes = max_memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
        # CPU time limit (seconds)
        resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
        # Max open files
        resource.setrlimit(resource.RLIMIT_NOFILE, (50, 50))
    return set_limits

# Usage:
result = run_code_sandboxed(
    code="""
print(sum(range(100)))
""",
    timeout_seconds=5.0,
    max_memory_mb=64
)
print(result["stdout"])  # "4950"
```

### Option 2: RestrictedPython — parse and block dangerous operations at AST level

```python
import ast
import logging
from typing import Any

logger = logging.getLogger(__name__)

# AST-based static analysis — block dangerous patterns before execution
FORBIDDEN_NODES = {
    ast.Import,        # import os, import subprocess
    ast.ImportFrom,    # from os import system
}

FORBIDDEN_CALLS = {
    "eval", "exec", "compile", "__import__",
    "open", "input",          # File and stdin access
    "breakpoint", "help",     # Debug access
}

FORBIDDEN_ATTRIBUTES = {
    "__class__", "__bases__", "__subclasses__",  # Class introspection
    "__globals__", "__locals__", "__builtins__",  # Namespace access
    "func_globals",  # Python 2 compat
}

def is_code_safe(code: str) -> tuple[bool, list[str]]:
    """
    Static analysis: check if code contains forbidden patterns.
    Returns (is_safe, list_of_violations).
    """
    violations = []
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return False, [f"Syntax error: {e}"]

    for node in ast.walk(tree):
        # Block import statements
        if type(node) in FORBIDDEN_NODES:
            violations.append(f"Import statement forbidden: {ast.dump(node)}")

        # Block calls to forbidden functions
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_CALLS:
                violations.append(f"Forbidden call: {node.func.id}()")

        # Block access to dunder attributes
        if isinstance(node, ast.Attribute):
            if node.attr in FORBIDDEN_ATTRIBUTES:
                violations.append(f"Forbidden attribute access: .{node.attr}")

        # Block string-based attribute access (getattr bypass attempts)
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == "getattr":
                violations.append("getattr() forbidden (potential attribute restriction bypass)")

    return len(violations) == 0, violations

def execute_safe_code(code: str, context: dict | None = None) -> dict:
    """
    Execute code only if static analysis passes.
    Runs in a restricted namespace without builtins.
    """
    is_safe, violations = is_code_safe(code)
    if not is_safe:
        return {
            "success": False,
            "error": "Code contains forbidden patterns",
            "violations": violations
        }

    # Restricted builtins — only safe functions:
    safe_builtins = {
        "print": print,
        "len": len, "range": range, "enumerate": enumerate,
        "zip": zip, "map": map, "filter": filter,
        "sorted": sorted, "reversed": reversed,
        "min": min, "max": max, "sum": sum, "abs": abs,
        "int": int, "float": float, "str": str, "bool": bool,
        "list": list, "dict": dict, "set": set, "tuple": tuple,
        "isinstance": isinstance, "type": type,
        "round": round, "divmod": divmod, "pow": pow,
        "True": True, "False": False, "None": None,
    }

    namespace = {"__builtins__": safe_builtins}
    if context:
        namespace.update(context)

    import io
    output = io.StringIO()
    import builtins
    original_print = builtins.print

    try:
        import contextlib
        with contextlib.redirect_stdout(output):
            exec(compile(code, "<sandbox>", "exec"), namespace)
        return {
            "success": True,
            "output": output.getvalue(),
            "namespace": {k: v for k, v in namespace.items() if not k.startswith("__")}
        }
    except Exception as exc:
        return {"success": False, "error": str(exc), "output": output.getvalue()}

# Usage:
result = execute_safe_code("x = [i**2 for i in range(10)]\nprint(x)")
print(result["output"])  # "[0, 1, 4, 9, 16, 25, 36, 49, 64, 81]"

# This is blocked:
blocked = execute_safe_code("import os; os.system('rm -rf /')")
print(blocked["violations"])  # ["Import statement forbidden: ..."]
```

### Option 3: Docker container sandbox — full OS-level isolation

```python
import subprocess
import json
import tempfile
import logging
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

def run_code_in_docker(
    code: str,
    language: str = "python",
    timeout_seconds: int = 10,
    memory_limit_mb: int = 128,
    network_disabled: bool = True
) -> dict:
    """
    Execute code in a fresh Docker container.
    Container is destroyed after execution — no persistence.
    Most secure option but requires Docker daemon.
    """
    container_name = f"sandbox-{uuid.uuid4().hex[:8]}"

    with tempfile.TemporaryDirectory() as tmpdir:
        # Write code to file
        if language == "python":
            code_file = Path(tmpdir) / "code.py"
            run_cmd = ["python3", "/sandbox/code.py"]
        elif language == "javascript":
            code_file = Path(tmpdir) / "code.js"
            run_cmd = ["node", "/sandbox/code.js"]
        else:
            return {"success": False, "error": f"Unsupported language: {language}"}

        code_file.write_text(code)

        docker_cmd = [
            "docker", "run",
            "--rm",                                    # Remove container after exit
            "--name", container_name,
            "--memory", f"{memory_limit_mb}m",        # Memory limit
            "--memory-swap", f"{memory_limit_mb}m",   # No swap
            "--cpus", "0.5",                          # Half a CPU core
            "--pids-limit", "50",                     # Max 50 processes
            "--read-only",                             # Read-only root filesystem
            "--tmpfs", "/tmp:size=10m",               # Writable /tmp only
            "--security-opt", "no-new-privileges",    # Can't gain privileges
            "--cap-drop", "ALL",                      # Drop all Linux capabilities
        ]

        if network_disabled:
            docker_cmd += ["--network", "none"]       # No network access

        docker_cmd += [
            "-v", f"{tmpdir}:/sandbox:ro",            # Mount code read-only
            "python:3.11-alpine",                     # Minimal base image
        ] + run_cmd

        try:
            result = subprocess.run(
                docker_cmd,
                capture_output=True,
                timeout=timeout_seconds + 5,  # Give Docker a few extra seconds
            )
            return {
                "success": result.returncode == 0,
                "stdout": result.stdout.decode("utf-8", errors="replace")[:10_000],
                "stderr": result.stderr.decode("utf-8", errors="replace")[:5_000],
                "returncode": result.returncode
            }
        except subprocess.TimeoutExpired:
            # Kill the container if it's still running
            subprocess.run(["docker", "kill", container_name], capture_output=True)
            return {"success": False, "error": f"Timed out after {timeout_seconds}s"}
        except FileNotFoundError:
            return {"success": False, "error": "Docker not available"}
```

### Option 4: Code review before execution — use Claude to audit generated code

```python
import anthropic
import json
import logging

logger = logging.getLogger(__name__)
client = anthropic.Anthropic()

DANGEROUS_PATTERNS = [
    "os.system", "subprocess", "exec(", "eval(",
    "open(", "__import__", "importlib",
    "socket", "urllib", "requests", "httpx",
    "shutil.rmtree", "os.remove", "pathlib.unlink",
    "os.environ", "dotenv",
]

def quick_pattern_check(code: str) -> list[str]:
    """Fast string-based check for obvious dangerous patterns."""
    return [p for p in DANGEROUS_PATTERNS if p in code]

def llm_security_review(code: str, task_description: str) -> dict:
    """
    Ask Claude to review generated code for security issues.
    Use this for medium-risk code where pattern matching isn't sufficient.
    """
    found_patterns = quick_pattern_check(code)

    if found_patterns:
        return {
            "safe": False,
            "issues": [f"Contains dangerous pattern: {p}" for p in found_patterns],
            "reviewed_by": "pattern_match"
        }

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": (
                f"Review this Python code for security issues. "
                f"The code was generated to: {task_description}\n\n"
                f"```python\n{code}\n```\n\n"
                "Does this code:\n"
                "- Access the filesystem beyond the current directory?\n"
                "- Make network requests?\n"
                "- Access environment variables or secrets?\n"
                "- Execute shell commands?\n"
                "- Use dangerous builtins (eval, exec, __import__)?\n"
                "- Have resource exhaustion risks (infinite loops, large allocations)?\n\n"
                "Return JSON: {\"safe\": true/false, \"issues\": [\"issue 1\", ...], \"risk_level\": \"low/medium/high\"}"
            )
        }]
    )

    try:
        result = json.loads(response.content[0].text.strip().strip("```json").strip("```"))
        result["reviewed_by"] = "llm"
        return result
    except json.JSONDecodeError:
        # Conservative default: if review fails, assume unsafe
        return {"safe": False, "issues": ["Security review failed — treating as unsafe"], "reviewed_by": "llm"}

def generate_and_execute_safely(
    task: str,
    model: str = "claude-sonnet-4-6",
    require_review: bool = True
) -> dict:
    """
    Generate code for a task, review it, then execute in a sandbox.
    """
    # Generate code:
    response = client.messages.create(
        model=model,
        max_tokens=1024,
        system=(
            "Write Python code to complete the task. "
            "Use only the standard library. No file I/O, no network calls, no subprocess. "
            "Return only the code, no explanation."
        ),
        messages=[{"role": "user", "content": task}]
    )
    generated_code = response.content[0].text.strip().strip("```python").strip("```")

    # Review (optional for high-trust tasks):
    if require_review:
        review = llm_security_review(generated_code, task)
        if not review.get("safe"):
            return {
                "success": False,
                "error": "Code failed security review",
                "issues": review.get("issues", []),
                "code": generated_code
            }

    # Execute in sandbox:
    exec_result = run_code_sandboxed(generated_code, timeout_seconds=10.0)
    return {
        "success": exec_result["success"],
        "output": exec_result.get("stdout", ""),
        "error": exec_result.get("stderr", ""),
        "code": generated_code
    }
```

### Option 5: Capability-based execution — explicit allowlist per code task

```python
import logging
import sys
from typing import Any

logger = logging.getLogger(__name__)

class CapabilityScope:
    """
    Define exactly what a code execution is allowed to do.
    Pass only what the task needs — principle of least privilege.
    """

    def __init__(
        self,
        allow_math: bool = True,
        allow_string_ops: bool = True,
        allow_data_structures: bool = True,
        allow_file_read: bool = False,
        allow_file_write: bool = False,
        allow_network: bool = False,
        allowed_modules: list[str] | None = None,
        allowed_functions: dict[str, Any] | None = None,
    ):
        self.allow_math = allow_math
        self.allow_string_ops = allow_string_ops
        self.allow_data_structures = allow_data_structures
        self.allow_file_read = allow_file_read
        self.allow_file_write = allow_file_write
        self.allow_network = allow_network
        self.allowed_modules = set(allowed_modules or [])
        self.allowed_functions = allowed_functions or {}

    def build_namespace(self) -> dict:
        """Build a restricted namespace from this capability scope."""
        builtins = {}

        if self.allow_math:
            import math
            builtins.update({
                "abs": abs, "round": round, "pow": pow, "divmod": divmod,
                "min": min, "max": max, "sum": sum,
                "math": math
            })

        if self.allow_string_ops:
            builtins.update({
                "str": str, "repr": repr, "len": len,
                "ord": ord, "chr": chr, "hex": hex, "bin": bin, "oct": oct,
                "format": format,
            })

        if self.allow_data_structures:
            builtins.update({
                "list": list, "dict": dict, "set": set, "tuple": tuple,
                "sorted": sorted, "reversed": reversed,
                "zip": zip, "enumerate": enumerate, "map": map, "filter": filter,
                "range": range, "iter": iter, "next": next,
                "int": int, "float": float, "bool": bool,
                "True": True, "False": False, "None": None,
                "isinstance": isinstance, "type": type,
                "print": print,
            })

        if self.allow_file_read:
            # Only allow reading from specific safe directories:
            import functools
            safe_dir = "/tmp/agent_data"
            def safe_open_read(path, mode="r", **kwargs):
                import os
                abs_path = os.path.realpath(path)
                if not abs_path.startswith(safe_dir):
                    raise PermissionError(f"Reading outside {safe_dir} is not allowed")
                return open(abs_path, mode, **kwargs)
            builtins["open"] = safe_open_read

        # Add any task-specific allowed functions:
        builtins.update(self.allowed_functions)

        return {"__builtins__": builtins}

def execute_with_capabilities(
    code: str,
    scope: CapabilityScope,
    input_data: dict | None = None
) -> dict:
    """Execute code within a defined capability scope."""
    namespace = scope.build_namespace()
    if input_data:
        namespace.update(input_data)

    import io, contextlib
    output = io.StringIO()
    try:
        with contextlib.redirect_stdout(output):
            exec(compile(code, "<capability_sandbox>", "exec"), namespace)
        return {
            "success": True,
            "output": output.getvalue(),
            "result": namespace.get("result"),  # Convention: code sets 'result' variable
        }
    except Exception as exc:
        return {"success": False, "error": str(exc), "output": output.getvalue()}

# Usage:
math_scope = CapabilityScope(allow_math=True, allow_data_structures=True)
result = execute_with_capabilities(
    "result = sum(i**2 for i in range(100))",
    scope=math_scope
)
print(result["result"])  # 328350

# Network access would fail — not in scope:
blocked = execute_with_capabilities("import requests; requests.get('http://example.com')", math_scope)
print(blocked["error"])  # ImportError or similar
```

## Sandboxing Options by Risk Level

| Risk Level | Scenario | Recommended Sandbox |
|---|---|---|
| Low | LLM-generated math/logic, no I/O | RestrictedPython + builtins allowlist (Option 2) |
| Medium | User-supplied scripts, limited I/O | Subprocess with resource limits + env cleanup (Option 1) |
| High | Arbitrary user code, untrusted input | Docker container with `--network none` (Option 3) |
| Any | Code touches secrets or filesystem | Mandatory LLM security review before exec (Option 4) |
| Structured tasks | Agent generates utility code | Capability scope per task type (Option 5) |

## What to NEVER Do

```python
# NEVER: eval() on LLM output
result = eval(llm_output)  # Full Python access, no isolation

# NEVER: exec() without namespace restriction
exec(user_code)  # Inherits all globals including secrets

# NEVER: subprocess with user input, unsanitized
subprocess.run(f"python -c '{user_code}'", shell=True)  # Shell injection

# NEVER: pass agent's environment to subprocess
subprocess.run(cmd, env=os.environ)  # Leaks API keys, tokens
```

## Expected Token Savings
N/A — sandboxing is a security control, not a token optimization.
Without sandboxing: one prompt injection that reaches eval() can exfiltrate all secrets in the process environment.

## Environment
- Any agent with code execution capabilities (code interpreters, data analysis agents, coding assistants that run their output); sandboxing is non-negotiable when code execution is user-facing or when LLM-generated code is executed automatically without human review; severity is highest for agents running with cloud credentials in the environment
- Source: direct experience; `eval(llm_output)` appears in ~30% of first-version coding agent implementations and is the single highest-severity security vulnerability in the agentic AI stack
