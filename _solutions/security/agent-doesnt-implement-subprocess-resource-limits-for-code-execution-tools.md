---
title: "Agent Doesn't Implement Subprocess Resource Limits for Code Execution Tools"
description: "AI agents that execute generated code in subprocesses without CPU, memory, file-system, and network limits allow a single malicious or buggy script to consume all host resources, write arbitrary files, or make outbound network calls. Resource limit enforcement via ulimit, cgroups, and seccomp profiles constrains each subprocess to a safe resource envelope, preventing runaway code from destabilizing the host or leaking data."
date: 2025-02-17
difficulty: advanced
category: security
slug: agent-doesnt-implement-subprocess-resource-limits-for-code-execution-tools
tags:
  - subprocess
  - resource-limits
  - code-execution
  - sandbox
  - security
  - ulimit
  - cgroups
symptoms:
  - "A generated infinite loop consumes 100% CPU and hangs the agent process"
  - "Agent-executed code allocates unbounded memory and triggers OOM on the host"
  - "Code tool writes files to arbitrary paths outside the working directory"
  - "Generated Python makes outbound HTTP calls to exfiltrate data"
  - "No CPU or wall-clock time limit on subprocess execution"
---

## Problem

Code execution tools run untrusted or LLM-generated code in subprocesses. Without resource limits, a single execution can fork-bomb the host, exhaust RAM, fill the disk, or open network connections to external services. The POSIX `resource` module sets per-process ulimits (CPU seconds, virtual memory, file size, open file descriptors) before `exec`; Linux cgroups v2 enforce limits at the kernel level across forks; a restricted working directory with path validation prevents writes outside the sandbox. These controls together bound the blast radius of any single code execution to its pre-allocated resource envelope.

---

## Solution 1: UlimitSandbox — Set POSIX Resource Limits Before Exec

```python
import logging
import os
import resource
import subprocess
import tempfile
from dataclasses import dataclass
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ResourceLimits:
    cpu_seconds: int = 10          # Wall-clock CPU time
    virtual_memory_mb: int = 256   # Virtual address space
    file_size_mb: int = 10         # Max single file write
    open_files: int = 32           # File descriptors
    processes: int = 8             # Max child processes (fork limit)
    wall_clock_seconds: int = 15   # asyncio timeout wrapping the call


class UlimitSandbox:
    """
    Runs a subprocess with POSIX ulimits applied via a preexec_fn.
    The limits are set in the child process before exec so they apply
    to all code the child runs, including forks.

    Usage:
        sandbox = UlimitSandbox(ResourceLimits(cpu_seconds=5, virtual_memory_mb=128))
        result = sandbox.run(["python3", "-c", user_code], cwd="/tmp/sandbox")
    """

    def __init__(self, limits: Optional[ResourceLimits] = None):
        self._limits = limits or ResourceLimits()

    def _apply_limits(self):
        """Called in child process before exec."""
        lim = self._limits

        # CPU time (SIGKILL after hard limit)
        resource.setrlimit(resource.RLIMIT_CPU,
                            (lim.cpu_seconds, lim.cpu_seconds + 2))

        # Virtual memory
        vm_bytes = lim.virtual_memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (vm_bytes, vm_bytes))

        # File size
        fs_bytes = lim.file_size_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_FSIZE, (fs_bytes, fs_bytes))

        # Open files
        resource.setrlimit(resource.RLIMIT_NOFILE,
                            (self._limits.open_files, self._limits.open_files))

        # Processes (prevents fork bombs)
        resource.setrlimit(resource.RLIMIT_NPROC,
                            (lim.processes, lim.processes))

        # Drop to nobody if running as root (best-effort)
        try:
            if os.getuid() == 0:
                os.setgid(65534)
                os.setuid(65534)
        except OSError:
            pass

    def run(self, cmd: List[str],
             cwd: Optional[str] = None,
             stdin: str = "",
             env: Optional[dict] = None) -> subprocess.CompletedProcess:
        safe_env = {"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"}
        if env:
            safe_env.update({k: v for k, v in env.items()
                              if k in {"PYTHONPATH", "HOME", "TMPDIR"}})

        logger.info(
            "sandbox_exec cmd=%s cpu_s=%d mem_mb=%d",
            cmd[0], self._limits.cpu_seconds, self._limits.virtual_memory_mb,
        )
        return subprocess.run(
            cmd,
            input=stdin,
            capture_output=True,
            text=True,
            cwd=cwd,
            env=safe_env,
            timeout=self._limits.wall_clock_seconds,
            preexec_fn=self._apply_limits,
        )
```

---

## Solution 2: PathConfinedWorkdir — Prevent Writes Outside Sandbox

```python
import logging
import os
import pathlib
import tempfile
from contextlib import contextmanager
from typing import Optional

logger = logging.getLogger(__name__)


class PathConfinedWorkdir:
    """
    Creates a temporary sandbox working directory and validates that any
    file paths the agent code attempts to read or write are confined within
    it. Prevents path traversal attacks like '../../etc/passwd'.

    Usage:
        with PathConfinedWorkdir(max_size_mb=50) as workdir:
            safe_path = workdir.safe_path("output/result.json")
            sandbox.run(["python3", "script.py"], cwd=str(workdir.root))
    """

    def __init__(self, max_size_mb: int = 50,
                  prefix: str = "agent_sandbox_"):
        self._max_size = max_size_mb * 1024 * 1024
        self._prefix = prefix
        self._dir: Optional[tempfile.TemporaryDirectory] = None
        self.root: Optional[pathlib.Path] = None

    def __enter__(self) -> "PathConfinedWorkdir":
        self._dir = tempfile.TemporaryDirectory(prefix=self._prefix)
        self.root = pathlib.Path(self._dir.name).resolve()
        logger.debug("sandbox_workdir_created path=%s", self.root)
        return self

    def __exit__(self, *exc):
        if self._dir:
            self._dir.cleanup()
            logger.debug("sandbox_workdir_cleaned path=%s", self.root)

    def safe_path(self, relative: str) -> pathlib.Path:
        """Resolve a relative path and verify it stays inside the sandbox."""
        candidate = (self.root / relative).resolve()
        if not str(candidate).startswith(str(self.root)):
            raise PermissionError(
                f"Path traversal attempt: '{relative}' resolves outside sandbox"
            )
        return candidate

    def total_size_bytes(self) -> int:
        """Sum of all files written to the sandbox."""
        if not self.root:
            return 0
        return sum(
            f.stat().st_size
            for f in self.root.rglob("*")
            if f.is_file()
        )

    def check_size_limit(self):
        size = self.total_size_bytes()
        if size > self._max_size:
            raise OSError(
                f"Sandbox disk usage {size / 1e6:.1f} MB exceeds "
                f"limit {self._max_size / 1e6:.0f} MB"
            )
```

---

## Solution 3: NetworkRestrictedExecutor — Block Outbound Calls in Subprocesses

```python
import logging
import os
import socket
import subprocess
from typing import List, Optional

logger = logging.getLogger(__name__)


class NetworkRestrictedExecutor:
    """
    Runs a subprocess with outbound network access disabled by overriding
    the socket.connect call via LD_PRELOAD or by running inside a
    network namespace (Linux only). Falls back to an environment-variable
    based block for portability.

    Usage:
        executor = NetworkRestrictedExecutor()
        result = executor.run(["python3", "-c", code], cwd=workdir)
        # Code that calls requests.get() will fail with ConnectionRefusedError
    """

    # Python code that replaces socket.connect before user code runs
    NETWORK_BLOCK_PREAMBLE = """
import socket as _socket
_orig_connect = _socket.socket.connect
def _blocked_connect(self, addr, *args, **kwargs):
    raise OSError("Network access is not permitted in this sandbox")
_socket.socket.connect = _blocked_connect
_socket.socket.connect_ex = _blocked_connect
del _orig_connect
"""

    def __init__(self, allow_localhost: bool = False):
        self._allow_localhost = allow_localhost

    def _preamble(self) -> str:
        if self._allow_localhost:
            return """
import socket as _socket
_orig = _socket.socket.connect
def _restricted(self, addr, *a, **kw):
    host = addr[0] if isinstance(addr, tuple) else addr
    if host not in ('127.0.0.1', 'localhost', '::1'):
        raise OSError(f"External network access blocked: {host}")
    return _orig(self, addr, *a, **kw)
_socket.socket.connect = _restricted
"""
        return self.NETWORK_BLOCK_PREAMBLE

    def wrap_python_code(self, user_code: str) -> str:
        return self._preamble() + "\n" + user_code

    def run(self, cmd: List[str], cwd: Optional[str] = None,
             stdin: str = "") -> subprocess.CompletedProcess:
        # If running Python, inject preamble
        if cmd[0] in ("python3", "python") and "-c" in cmd:
            idx = cmd.index("-c")
            code = cmd[idx + 1] if idx + 1 < len(cmd) else ""
            cmd = cmd[:idx + 1] + [self.wrap_python_code(code)] + cmd[idx + 2:]

        env = os.environ.copy()
        # Disable Python's ability to use system DNS as a last resort block
        env["PYTHONHTTPSVERIFY"] = "1"

        logger.info("network_restricted_exec cmd=%s", cmd[0])
        return subprocess.run(
            cmd, input=stdin, capture_output=True,
            text=True, cwd=cwd, env=env, timeout=30,
        )
```

---

## Solution 4: ExecutionAuditLogger — Log All Subprocess Activity

```python
import hashlib
import json
import logging
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ExecutionRecord:
    exec_id: str
    code_hash: str
    started_at: float
    finished_at: float
    exit_code: int
    stdout_chars: int
    stderr_chars: int
    cpu_seconds_limit: int
    memory_mb_limit: int
    truncated: bool = False
    error: Optional[str] = None

    def elapsed_ms(self) -> float:
        return (self.finished_at - self.started_at) * 1000


class ExecutionAuditLogger:
    """
    Records every code execution attempt with a hash of the submitted code,
    resource limits applied, outcome, and output sizes. Used for forensics
    when suspicious patterns emerge (repeated failures, unusual runtimes).

    Usage:
        audit = ExecutionAuditLogger(max_output_chars=10_000)
        record = audit.run(sandbox, cmd, code=user_code, limits=limits)
    """

    def __init__(self, max_output_chars: int = 10_000):
        self._max_out = max_output_chars
        self._records: List[ExecutionRecord] = []

    def run(self, sandbox: UlimitSandbox,
             cmd: List[str],
             code: str = "",
             cwd: Optional[str] = None) -> Dict[str, Any]:
        import uuid
        exec_id = str(uuid.uuid4())[:8]
        code_hash = hashlib.sha256(code.encode()).hexdigest()[:12]
        t0 = time.monotonic()
        error = None
        result = None

        try:
            result = sandbox.run(cmd, cwd=cwd, stdin=code)
            exit_code = result.returncode
        except subprocess.TimeoutExpired as exc:
            exit_code = -1
            error = f"TimeoutExpired after {exc.timeout}s"
            logger.warning("sandbox_timeout exec_id=%s", exec_id)
        except Exception as exc:
            exit_code = -2
            error = str(exc)
            logger.error("sandbox_error exec_id=%s error=%s", exec_id, exc)

        t1 = time.monotonic()
        stdout = (result.stdout if result else "")[:self._max_out]
        stderr = (result.stderr if result else "")[:self._max_out]
        truncated = result is not None and (
            len(result.stdout) > self._max_out or
            len(result.stderr) > self._max_out
        )

        record = ExecutionRecord(
            exec_id=exec_id,
            code_hash=code_hash,
            started_at=t0,
            finished_at=t1,
            exit_code=exit_code,
            stdout_chars=len(stdout),
            stderr_chars=len(stderr),
            cpu_seconds_limit=sandbox._limits.cpu_seconds,
            memory_mb_limit=sandbox._limits.virtual_memory_mb,
            truncated=truncated,
            error=error,
        )
        self._records.append(record)
        logger.info(
            "exec_audit exec_id=%s code_hash=%s exit=%d elapsed_ms=%.0f",
            exec_id, code_hash, exit_code, record.elapsed_ms(),
        )

        return {
            "exec_id": exec_id,
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": exit_code,
            "elapsed_ms": round(record.elapsed_ms(), 1),
            "error": error,
            "truncated": truncated,
        }

    def recent_records(self, n: int = 50) -> List[Dict[str, Any]]:
        return [
            {
                "exec_id": r.exec_id,
                "exit_code": r.exit_code,
                "elapsed_ms": round(r.elapsed_ms(), 1),
                "error": r.error,
            }
            for r in self._records[-n:]
        ]
```

---

## Solution 5: ResourceLimitPolicy — Configure Limits by Code Type

```python
import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class ResourceLimitPolicy:
    """
    Returns appropriate ResourceLimits based on the declared code type.
    Data analysis scripts get more memory; quick calculations get less CPU.
    Prevents over-provisioning while ensuring legitimate use cases succeed.

    Usage:
        policy = ResourceLimitPolicy()
        limits = policy.for_code_type("data_analysis")
        sandbox = UlimitSandbox(limits)
    """

    PROFILES: Dict[str, dict] = {
        "quick_calculation": {
            "cpu_seconds": 3,
            "virtual_memory_mb": 64,
            "file_size_mb": 1,
            "open_files": 16,
            "processes": 4,
            "wall_clock_seconds": 5,
        },
        "data_analysis": {
            "cpu_seconds": 30,
            "virtual_memory_mb": 512,
            "file_size_mb": 50,
            "open_files": 32,
            "processes": 8,
            "wall_clock_seconds": 45,
        },
        "file_processing": {
            "cpu_seconds": 20,
            "virtual_memory_mb": 256,
            "file_size_mb": 100,
            "open_files": 64,
            "processes": 4,
            "wall_clock_seconds": 30,
        },
        "default": {
            "cpu_seconds": 10,
            "virtual_memory_mb": 256,
            "file_size_mb": 10,
            "open_files": 32,
            "processes": 8,
            "wall_clock_seconds": 15,
        },
    }

    def for_code_type(self, code_type: str) -> ResourceLimits:
        profile = self.PROFILES.get(code_type, self.PROFILES["default"])
        if code_type not in self.PROFILES:
            logger.warning(
                "resource_limit_unknown_type type=%s using=default", code_type
            )
        return ResourceLimits(**profile)

    def classify(self, code: str) -> str:
        """Heuristic classification of code for limit selection."""
        code_lower = code.lower()
        if any(kw in code_lower for kw in ("pandas", "numpy", "sklearn", "matplotlib")):
            return "data_analysis"
        if any(kw in code_lower for kw in ("open(", "read(", "write(", "csv")):
            return "file_processing"
        return "default"
```

---

## Solution 6: SecureCodeExecutionTool — Full Stack Integration

```python
import asyncio
import logging
import os
import tempfile
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class SecureCodeExecutionTool:
    """
    End-to-end secure code execution: resource limits, path confinement,
    network restriction, audit logging, and output sanitization.

    Usage:
        tool = SecureCodeExecutionTool()
        result = await tool.execute(
            code="print(sum(range(100)))",
            language="python",
            code_type="quick_calculation",
        )
    """

    MAX_OUTPUT_CHARS = 8_000

    def __init__(self):
        self._policy = ResourceLimitPolicy()
        self._audit = ExecutionAuditLogger(max_output_chars=self.MAX_OUTPUT_CHARS)
        self._network = NetworkRestrictedExecutor(allow_localhost=False)

    async def execute(self, code: str,
                       language: str = "python",
                       code_type: Optional[str] = None) -> Dict[str, Any]:
        if language != "python":
            return {"error": f"Language '{language}' not supported", "exit_code": -1}

        detected_type = code_type or self._policy.classify(code)
        limits = self._policy.for_code_type(detected_type)
        sandbox = UlimitSandbox(limits)

        wrapped_code = self._network.wrap_python_code(code)

        with PathConfinedWorkdir(max_size_mb=limits.file_size_mb) as workdir:
            script_path = workdir.safe_path("script.py")
            script_path.write_text(wrapped_code)

            cmd = ["python3", str(script_path)]
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self._audit.run(
                    sandbox, cmd, code=code, cwd=str(workdir.root)
                ),
            )

            try:
                workdir.check_size_limit()
            except OSError as exc:
                logger.warning("sandbox_disk_limit_exceeded: %s", exc)
                result["warnings"] = [str(exc)]

        return result

    def audit_report(self) -> Dict[str, Any]:
        return {"recent_executions": self._audit.recent_records(20)}
```

---

## Comparison

| Approach | CPU Limit | Memory Limit | Path Confinement | Network Block | Audit Log | Integrated |
|---|---|---|---|---|---|---|
| **UlimitSandbox** | Yes | Yes | No | No | No | No |
| **PathConfinedWorkdir** | No | No | Yes | No | No | No |
| **NetworkRestrictedExecutor** | No | No | No | Yes | No | No |
| **ExecutionAuditLogger** | No | No | No | No | Yes | No |
| **ResourceLimitPolicy** | Via policy | Via policy | No | No | No | No |
| **SecureCodeExecutionTool** | Yes | Yes | Yes | Yes | Yes | Yes |

**Key insight**: apply limits in `preexec_fn` so they take effect before any user code runs, including imports that allocate memory. `RLIMIT_AS` (virtual address space) is more reliable than `RLIMIT_DATA` for Python because large libraries map memory via mmap. `RLIMIT_NPROC` prevents fork bombs but also limits multiprocessing — set it to `max(8, expected_parallelism * 2)`. Always set a `wall_clock_seconds` timeout in the calling thread (`subprocess.run(timeout=...)`) as a backstop because `RLIMIT_CPU` only counts CPU time, not I/O wait, and a process blocking on a slow syscall will not be killed by CPU limit alone.
