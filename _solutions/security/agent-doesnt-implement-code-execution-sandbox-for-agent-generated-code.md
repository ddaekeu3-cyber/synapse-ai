---
title: "Agent Doesn't Implement Code Execution Sandbox for Agent-Generated Code"
description: "How to safely execute agent-generated Python, shell scripts, and other code in isolated sandboxes — using subprocess isolation, resource limits, network egress control, filesystem restrictions, and seccomp policies — preventing malicious or buggy code from escaping to the host."
date: 2025-01-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-code-execution-sandbox-for-agent-generated-code
tags:
  - security
  - sandbox
  - code-execution
  - isolation
  - resource-limits
  - seccomp
  - container-security
symptoms:
  - "Agent generates and runs Python code without any isolation from the host process"
  - "LLM-generated shell commands can read or write arbitrary files on the system"
  - "No CPU or memory limits on agent-executed code — runaway loops crash the host"
  - "Agent-generated code can make arbitrary outbound network connections"
  - "No timeout enforcement on code execution — infinite loops block indefinitely"
  - "User-influenced code could escape to access other users' data or system resources"
---

## Why This Happens

When agents execute code — for data analysis, automation, or code generation workflows — they run it in the same process or with the same OS privileges as the agent itself. A hallucinated `rm -rf /`, an accidental infinite loop, or a prompt injection that generates malicious code can damage the host system, exhaust resources, or exfiltrate data. Even well-intentioned code can call out to arbitrary URLs or read sensitive environment variables.

A proper execution sandbox isolates generated code in a restricted environment with explicit limits on CPU time, memory, filesystem access, network access, and syscall surface — ensuring that even malicious or buggy code cannot escape to affect the host.

---

## Solution 1: Subprocess Sandbox with Resource Limits

Execute code in a subprocess with CPU time, memory, and file descriptor limits enforced at the OS level.

```python
import asyncio
import os
import resource
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Optional

@dataclass
class SandboxLimits:
    cpu_time_seconds: float = 10.0        # Wall-clock timeout
    max_memory_mb: int = 256              # RSS memory limit
    max_output_bytes: int = 1_048_576    # 1 MB stdout/stderr cap
    max_file_size_bytes: int = 10_485_760 # 10 MB max file write
    max_open_files: int = 20
    max_processes: int = 1               # No forking

@dataclass
class ExecutionResult:
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool
    memory_exceeded: bool
    duration_seconds: float
    error: Optional[str] = None

    @property
    def success(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.memory_exceeded


class SubprocessSandbox:
    """
    Executes Python code in an isolated subprocess with OS-level resource limits.
    Uses setrlimit to enforce CPU, memory, file size, and process count limits.
    """

    def __init__(self, limits: SandboxLimits | None = None):
        self.limits = limits or SandboxLimits()

    def _set_limits(self) -> None:
        """Called in the child process before exec — sets resource limits."""
        limits = self.limits

        # CPU time (SIGKILL on hard limit exceeded)
        cpu = int(limits.cpu_time_seconds)
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 2))

        # Virtual memory
        mem = limits.max_memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))

        # Max file write size
        resource.setrlimit(
            resource.RLIMIT_FSIZE,
            (limits.max_file_size_bytes, limits.max_file_size_bytes)
        )

        # Number of open file descriptors
        resource.setrlimit(
            resource.RLIMIT_NOFILE,
            (limits.max_open_files, limits.max_open_files)
        )

        # Max processes (prevent fork bombs)
        resource.setrlimit(resource.RLIMIT_NPROC, (limits.max_processes, limits.max_processes))

    async def execute_python(
        self,
        code: str,
        allowed_imports: list[str] | None = None,
        env_vars: dict[str, str] | None = None,
    ) -> ExecutionResult:
        """Execute Python code string in a sandboxed subprocess."""
        # Pre-check: block dangerous imports
        blocked = self._check_blocked_imports(code)
        if blocked:
            return ExecutionResult(
                stdout="", stderr=f"Blocked import: {blocked[0]}",
                exit_code=1, timed_out=False, memory_exceeded=False, duration_seconds=0,
                error=f"Security: blocked import '{blocked[0]}'",
            )

        with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
            f.write(self._wrap_code(code, allowed_imports))
            script_path = f.name

        start = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                "python3", script_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._safe_env(env_vars),
                preexec_fn=self._set_limits,
            )

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=self.limits.cpu_time_seconds + 2,  # OS limit + grace
                )
                timed_out = False
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                stdout_bytes, stderr_bytes = b"", b"Execution timed out"
                timed_out = True

            return ExecutionResult(
                stdout=stdout_bytes[:self.limits.max_output_bytes].decode("utf-8", errors="replace"),
                stderr=stderr_bytes[:self.limits.max_output_bytes].decode("utf-8", errors="replace"),
                exit_code=proc.returncode or 0,
                timed_out=timed_out,
                memory_exceeded=False,
                duration_seconds=time.monotonic() - start,
            )
        finally:
            try:
                os.unlink(script_path)
            except OSError:
                pass

    def _wrap_code(self, code: str, allowed_imports: list[str] | None) -> str:
        """Wrap user code with import restrictions."""
        header = ""
        if allowed_imports is not None:
            allowed_set = set(allowed_imports)
            header = f"""
import builtins
_original_import = builtins.__import__
_allowed = {repr(allowed_set)}
def _restricted_import(name, *args, **kwargs):
    base = name.split('.')[0]
    if base not in _allowed:
        raise ImportError(f"Import '{{name}}' not allowed in sandbox")
    return _original_import(name, *args, **kwargs)
builtins.__import__ = _restricted_import
"""
        return header + "\n" + code

    def _check_blocked_imports(self, code: str) -> list[str]:
        """Fast static check for obviously dangerous imports."""
        blocked = ["subprocess", "os.system", "ctypes", "pickle", "__import__"]
        import re
        found = []
        for pattern in blocked:
            if re.search(rf'\b{re.escape(pattern)}\b', code):
                found.append(pattern)
        return found

    def _safe_env(self, extra: dict | None) -> dict[str, str]:
        """Minimal safe environment — strip secrets from env."""
        safe = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": "/tmp",
            "TMPDIR": "/tmp",
        }
        if extra:
            safe.update(extra)
        return safe
```

---

## Solution 2: Restricted Python Interpreter (RestrictedPython)

Use AST-based code transformation to restrict Python builtins and attribute access.

```python
import ast
import builtins
import types
from typing import Any

class ASTCodeValidator:
    """
    Validates Python code using AST analysis before execution.
    Catches dangerous patterns that import restrictions might miss.
    """

    FORBIDDEN_NODES = {
        ast.Import: ["subprocess", "os", "sys", "socket", "ctypes", "pickle", "marshal"],
        ast.ImportFrom: ["subprocess", "os", "sys", "socket", "ctypes"],
    }

    FORBIDDEN_CALLS = {
        "eval", "exec", "compile", "__import__", "open",
        "breakpoint", "input",
    }

    def validate(self, code: str) -> list[str]:
        """Returns list of violation messages. Empty = safe."""
        try:
            tree = ast.parse(code)
        except SyntaxError as exc:
            return [f"SyntaxError: {exc}"]

        violations = []
        for node in ast.walk(tree):
            # Check imports
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                modules = (
                    [alias.name.split(".")[0] for alias in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module.split(".")[0] if node.module else ""]
                )
                for mod in modules:
                    if mod in self.FORBIDDEN_NODES.get(type(node), []):
                        violations.append(f"Forbidden import: {mod}")

            # Check dangerous function calls
            if isinstance(node, ast.Call):
                func_name = None
                if isinstance(node.func, ast.Name):
                    func_name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    func_name = node.func.attr
                if func_name in self.FORBIDDEN_CALLS:
                    violations.append(f"Forbidden call: {func_name}()")

            # Check for __dunder__ attribute access
            if isinstance(node, ast.Attribute):
                if node.attr.startswith("__") and node.attr.endswith("__"):
                    if node.attr in ("__class__", "__bases__", "__subclasses__", "__globals__"):
                        violations.append(f"Forbidden attribute access: {node.attr}")

        return violations


class RestrictedExecutor:
    """
    Executes Python code in a restricted namespace with limited builtins.
    Complement to subprocess isolation — adds language-level restrictions.
    """

    SAFE_BUILTINS = {
        "abs", "all", "any", "bin", "bool", "chr", "dict", "dir",
        "divmod", "enumerate", "filter", "float", "format", "frozenset",
        "getattr", "hasattr", "hash", "hex", "int", "isinstance",
        "issubclass", "iter", "len", "list", "map", "max", "min",
        "next", "oct", "ord", "pow", "print", "range", "repr",
        "reversed", "round", "set", "slice", "sorted", "str", "sum",
        "tuple", "type", "zip",
    }

    def __init__(self):
        self.validator = ASTCodeValidator()
        self._safe_builtins = {
            name: getattr(builtins, name)
            for name in self.SAFE_BUILTINS
            if hasattr(builtins, name)
        }

    def execute(self, code: str, extra_globals: dict | None = None) -> dict:
        """
        Execute code in a restricted namespace.
        Returns the final global namespace (for inspecting outputs).
        """
        violations = self.validator.validate(code)
        if violations:
            raise SecurityError(f"Code validation failed: {violations}")

        namespace = {
            "__builtins__": self._safe_builtins,
            "__name__": "__sandbox__",
        }
        if extra_globals:
            namespace.update(extra_globals)

        exec(compile(code, "<sandbox>", "exec"), namespace)  # noqa: S102
        return namespace


class SecurityError(Exception):
    pass
```

---

## Solution 3: Docker Container Sandbox

For the strongest isolation, execute code in a throwaway Docker container with network disabled and read-only filesystem.

```python
import asyncio
import base64
import json
import tempfile
import os

class DockerSandbox:
    """
    Executes code in a disposable Docker container.
    Provides OS-level isolation: separate process namespace, filesystem, network.
    """

    DEFAULT_IMAGE = "python:3.11-slim"

    def __init__(
        self,
        image: str = DEFAULT_IMAGE,
        limits: SandboxLimits | None = None,
        network: str = "none",    # "none" = no network access
    ):
        self.image = image
        self.limits = limits or SandboxLimits()
        self.network = network

    async def execute_python(self, code: str) -> ExecutionResult:
        """Run Python code in a fresh Docker container."""
        # Encode code to avoid shell escaping issues
        code_b64 = base64.b64encode(code.encode()).decode()
        run_script = f"import base64; exec(base64.b64decode('{code_b64}').decode())"

        cmd = [
            "docker", "run",
            "--rm",                                          # Remove container on exit
            "--network", self.network,                       # Network isolation
            "--memory", f"{self.limits.max_memory_mb}m",    # Memory limit
            "--cpus", "0.5",                                 # CPU quota
            "--read-only",                                   # Read-only filesystem
            "--tmpfs", "/tmp:size=50m",                      # Writable /tmp only
            "--no-new-privileges",                           # No privilege escalation
            "--security-opt", "no-new-privileges:true",
            "--cap-drop", "ALL",                             # Drop all Linux capabilities
            "--user", "65534:65534",                         # nobody:nogroup
            self.image,
            "python3", "-c", run_script,
        ]

        start = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=self.limits.cpu_time_seconds + 5,
                )
                timed_out = False
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                stdout_b, stderr_b = b"", b"Container execution timed out"
                timed_out = True

            return ExecutionResult(
                stdout=stdout_b[:self.limits.max_output_bytes].decode("utf-8", errors="replace"),
                stderr=stderr_b[:self.limits.max_output_bytes].decode("utf-8", errors="replace"),
                exit_code=proc.returncode or 0,
                timed_out=timed_out,
                memory_exceeded=False,
                duration_seconds=time.monotonic() - start,
            )
        except FileNotFoundError:
            return ExecutionResult(
                stdout="", stderr="Docker not available",
                exit_code=127, timed_out=False, memory_exceeded=False,
                duration_seconds=0, error="Docker not installed",
            )
```

---

## Solution 4: Output Validator and Sanitizer

After sandboxed code runs, validate and sanitize its output before passing it back to the agent.

```python
import re
import json
from typing import Any

class SandboxOutputSanitizer:
    """
    Validates and sanitizes sandbox execution output before the agent processes it.
    Prevents output-based injection attacks or exfiltration via stdout.
    """

    MAX_OUTPUT_LENGTH = 50_000  # characters
    SUSPICIOUS_PATTERNS = [
        re.compile(r"(?:API_KEY|SECRET|PASSWORD|TOKEN)\s*=\s*\S+", re.IGNORECASE),
        re.compile(r"[A-Za-z0-9+/]{40,}={0,2}"),  # Base64 blobs (possible secrets)
        re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"),  # IP addresses
    ]

    def sanitize(self, result: ExecutionResult) -> ExecutionResult:
        """Sanitize stdout/stderr, flag suspicious content."""
        stdout = result.stdout[:self.MAX_OUTPUT_LENGTH]
        stderr = result.stderr[:self.MAX_OUTPUT_LENGTH]

        for pattern in self.SUSPICIOUS_PATTERNS:
            stdout = pattern.sub("[REDACTED]", stdout)

        return ExecutionResult(
            stdout=stdout,
            stderr=stderr,
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            memory_exceeded=result.memory_exceeded,
            duration_seconds=result.duration_seconds,
            error=result.error,
        )

    def extract_structured_output(self, stdout: str) -> Any:
        """Attempt to parse stdout as JSON for structured results."""
        stdout = stdout.strip()
        if stdout.startswith("{") or stdout.startswith("["):
            try:
                return json.loads(stdout)
            except json.JSONDecodeError:
                pass
        return stdout

    def is_suspicious(self, stdout: str) -> bool:
        return any(p.search(stdout) for p in self.SUSPICIOUS_PATTERNS)
```

---

## Solution 5: Language-Aware Sandbox Selector

Choose the appropriate sandbox strategy based on the code language and risk level.

```python
from enum import Enum

class CodeLanguage(Enum):
    PYTHON = "python"
    SHELL  = "shell"
    SQL    = "sql"
    JS     = "javascript"

class RiskLevel(Enum):
    LOW    = "low"    # Read-only data analysis
    MEDIUM = "medium" # File I/O allowed
    HIGH   = "high"   # Network or system access needed

class SandboxSelector:
    """Selects the appropriate sandbox strategy for the given code and risk level."""

    def __init__(
        self,
        subprocess_sandbox: SubprocessSandbox,
        docker_sandbox: DockerSandbox,
        restricted_executor: RestrictedExecutor,
    ):
        self._subprocess = subprocess_sandbox
        self._docker = docker_sandbox
        self._restricted = restricted_executor
        self._sanitizer = SandboxOutputSanitizer()

    async def execute(
        self,
        code: str,
        language: CodeLanguage,
        risk_level: RiskLevel = RiskLevel.LOW,
    ) -> ExecutionResult:
        """Route to the appropriate sandbox based on language and risk."""
        if language == CodeLanguage.PYTHON:
            if risk_level == RiskLevel.LOW:
                # Lightest: in-process restricted execution
                try:
                    ns = self._restricted.execute(code)
                    output = str(ns.get("__output__", ""))
                    return ExecutionResult(
                        stdout=output, stderr="", exit_code=0,
                        timed_out=False, memory_exceeded=False, duration_seconds=0,
                    )
                except SecurityError as exc:
                    return ExecutionResult(
                        stdout="", stderr=str(exc), exit_code=1,
                        timed_out=False, memory_exceeded=False, duration_seconds=0,
                        error=str(exc),
                    )
            elif risk_level == RiskLevel.MEDIUM:
                result = await self._subprocess.execute_python(code)
            else:
                result = await self._docker.execute_python(code)

        elif language == CodeLanguage.SHELL:
            # Shell commands always use Docker for maximum isolation
            result = await self._docker.execute_python(f"import subprocess; subprocess.run({repr(code)}, shell=True, check=True)")
        else:
            return ExecutionResult(
                stdout="", stderr=f"Language {language.value} not supported",
                exit_code=1, timed_out=False, memory_exceeded=False, duration_seconds=0,
            )

        return self._sanitizer.sanitize(result)
```

---

## Solution 6: Audit Logger for Code Execution

Log all sandbox executions with input code, output, duration, and security events for audit and forensics.

```python
import json
import hashlib
import time
import logging

audit_log = logging.getLogger("sandbox.audit")

class SandboxAuditLogger:
    """Logs all sandbox execution events for security auditing."""

    def __init__(self, session_id: str, agent_id: str):
        self.session_id = session_id
        self.agent_id = agent_id
        self._executions: list[dict] = []

    def log_execution(
        self,
        code: str,
        result: ExecutionResult,
        language: str = "python",
        risk_level: str = "low",
    ) -> str:
        execution_id = hashlib.sha256(
            f"{self.session_id}{time.time()}{code[:100]}".encode()
        ).hexdigest()[:12]

        record = {
            "execution_id": execution_id,
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "timestamp": time.time(),
            "language": language,
            "risk_level": risk_level,
            "code_hash": hashlib.sha256(code.encode()).hexdigest()[:16],
            "code_length": len(code),
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "memory_exceeded": result.memory_exceeded,
            "duration_ms": round(result.duration_seconds * 1000, 1),
            "output_length": len(result.stdout),
            "had_error": bool(result.error),
            "success": result.success,
        }
        self._executions.append(record)
        audit_log.info("SANDBOX_EXEC %s", json.dumps(record))

        if not result.success:
            audit_log.warning(
                "SANDBOX_FAILURE exec=%s exit=%d timed_out=%s",
                execution_id, result.exit_code, result.timed_out,
            )
        return execution_id

    def get_session_summary(self) -> dict:
        total = len(self._executions)
        return {
            "session_id": self.session_id,
            "total_executions": total,
            "failures": sum(1 for e in self._executions if not e["success"]),
            "timeouts": sum(1 for e in self._executions if e["timed_out"]),
            "avg_duration_ms": (
                sum(e["duration_ms"] for e in self._executions) / total if total else 0
            ),
        }
```

---

## Comparison

| Solution | Isolation Level | Setup Complexity | Overhead | Best For |
|---|---|---|---|---|
| Subprocess + rlimit | Process | Low | Low (~5ms) | Quick CPU/memory isolation |
| AST Validator + Restricted Exec | Language | Very Low | Negligible | Trusted code with import restriction |
| Docker Container | OS | Medium (needs Docker) | High (~500ms) | Untrusted / user-supplied code |
| Output Sanitizer | N/A (post-exec) | Very Low | Negligible | Preventing output-based data leaks |
| Sandbox Selector | All levels | Medium | Variable | Routing by risk level |
| Audit Logger | N/A (logging) | Very Low | Negligible | Compliance and forensics |

**Use subprocess isolation with rlimit** for trusted agent-generated code where Docker overhead is unacceptable. **Use Docker** for any user-influenced code, prompt injection risk, or when the agent can be instructed by end users. **Always apply AST validation** as a fast pre-execution filter before reaching the subprocess or Docker boundary. **Always apply output sanitization** to prevent the executed code from leaking secrets back to the agent via stdout. **Log all executions** for security auditing — agent-generated code execution is a high-risk event that must be attributable.
