---
title: "Agent Doesn't Implement Tool Execution Sandbox"
description: "Agents that execute tool code in the same process as the LLM loop expose the host to filesystem reads, network exfiltration, and privilege escalation — a subprocess sandbox with resource limits and filesystem isolation contains the blast radius."
difficulty: advanced
category: security
tags: [security, sandbox, subprocess, isolation, resource-limits, code-execution]
---

# Agent Doesn't Implement Tool Execution Sandbox

## Problem

When an agent executes tool code (file read, shell command, Python eval, web fetch) directly in its own process with full host privileges, a compromised or malicious tool can read the agent's secrets, write to arbitrary paths, make unbounded network calls, or fork-bomb the host. Even well-intentioned tools that hit unexpected inputs (recursive file glob, infinite loop) can crash the entire agent service. Sandboxing isolates tool execution in a separate subprocess with restricted permissions, CPU/memory limits, and a controlled filesystem view.

**Symptoms:**
- A buggy tool's infinite loop hangs the entire agent process
- A tool that reads `os.environ` exposes API keys to the LLM context
- Filesystem tools can traverse to `~/.ssh/` or `/etc/shadow`
- Network tools can exfiltrate data to arbitrary external hosts
- One user's runaway tool process consumes all available CPU for other users

---

## Solution 1: Subprocess Isolation with Timeout and Resource Limits

Run tool code in a child subprocess with a hard timeout; kill the process on timeout or error.

```python
import asyncio
import json
import os
import resource
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class SandboxResult:
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool
    cpu_seconds: float

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


async def run_sandboxed(
    code: str,
    timeout_seconds: float = 5.0,
    max_memory_mb: int = 128,
    allowed_imports: Optional[list[str]] = None,
) -> SandboxResult:
    """Execute Python code in a subprocess with timeout and memory limit."""

    # Write code to a temp file
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        temp_path = f.name
        # Prepend import guard
        if allowed_imports is not None:
            allowed_set = json.dumps(allowed_imports)
            guard = f"""
import builtins
_allowed = set({allowed_set})
_original_import = builtins.__import__
def _safe_import(name, *args, **kwargs):
    base = name.split('.')[0]
    if base not in _allowed:
        raise ImportError(f"Import '{{name}}' is not allowed in sandbox")
    return _original_import(name, *args, **kwargs)
builtins.__import__ = _safe_import
"""
            f.write(guard)
        f.write(code)

    def preexec():
        """Called in child process before exec — set resource limits."""
        # Memory limit
        max_bytes = max_memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (max_bytes, max_bytes))
        # CPU time limit (seconds)
        resource.setrlimit(resource.RLIMIT_CPU, (int(timeout_seconds) + 1, int(timeout_seconds) + 2))
        # No new file descriptors beyond stdin/out/err
        resource.setrlimit(resource.RLIMIT_NOFILE, (16, 16))
        # No forking
        resource.setrlimit(resource.RLIMIT_NPROC, (1, 1))
        # Create new session to isolate signals
        os.setsid()

    start = asyncio.get_event_loop().time()
    proc = await asyncio.create_subprocess_exec(
        sys.executable, temp_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        preexec_fn=preexec if sys.platform != "win32" else None,
        env={  # Stripped environment — no API keys, no HOME leakage
            "PATH": "/usr/bin:/bin",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": "",
        },
    )

    timed_out = False
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_seconds
        )
    except asyncio.TimeoutError:
        timed_out = True
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = await proc.communicate()

    try:
        os.unlink(temp_path)
    except OSError:
        pass

    elapsed = asyncio.get_event_loop().time() - start
    return SandboxResult(
        stdout=stdout.decode("utf-8", errors="replace")[:10_000],
        stderr=stderr.decode("utf-8", errors="replace")[:2_000],
        exit_code=proc.returncode or 0,
        timed_out=timed_out,
        cpu_seconds=elapsed,
    )


async def demo():
    # Safe code
    result = await run_sandboxed(
        "print(sum(range(100)))",
        allowed_imports=["math", "json", "re"],
    )
    print(f"OK: {result.stdout.strip()} (exit={result.exit_code})")

    # Timeout
    result = await run_sandboxed("import time; time.sleep(60)", timeout_seconds=2.0)
    print(f"Timeout: timed_out={result.timed_out}")

    # Blocked import
    result = await run_sandboxed("import os; print(os.environ)", allowed_imports=["math"])
    print(f"Blocked: {result.stderr[:80]}")

# asyncio.run(demo())
```

---

## Solution 2: Docker Container Sandbox via API

Spin up a lightweight Docker container per tool execution; destroy it after completion.

```python
import asyncio
import json
import tempfile
import os
from dataclasses import dataclass
from typing import Optional

# pip install aiodocker
try:
    import aiodocker
    HAS_AIODOCKER = True
except ImportError:
    HAS_AIODOCKER = False


@dataclass
class ContainerResult:
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool


async def run_in_container(
    code: str,
    image: str = "python:3.12-slim",
    timeout_seconds: float = 10.0,
    memory_limit_mb: int = 128,
    network_disabled: bool = True,
) -> ContainerResult:
    """Execute code in an ephemeral Docker container."""
    if not HAS_AIODOCKER:
        raise ImportError("pip install aiodocker")

    async with aiodocker.Docker() as docker:
        container = await docker.containers.create({
            "Image": image,
            "Cmd": ["python", "-c", code],
            "HostConfig": {
                "Memory": memory_limit_mb * 1024 * 1024,
                "MemorySwap": memory_limit_mb * 1024 * 1024,  # No swap
                "NanoCpus": 500_000_000,  # 0.5 CPU
                "NetworkMode": "none" if network_disabled else "bridge",
                "ReadonlyRootfs": True,  # Read-only filesystem
                "SecurityOpt": ["no-new-privileges:true"],
                "CapDrop": ["ALL"],  # Drop all Linux capabilities
                "Privileged": False,
            },
            "NetworkDisabled": network_disabled,
        })

        try:
            await container.start()
            timed_out = False
            try:
                await asyncio.wait_for(container.wait(), timeout=timeout_seconds)
            except asyncio.TimeoutError:
                timed_out = True
                await container.kill()

            logs = await container.log(stdout=True, stderr=True)
            return ContainerResult(
                stdout="\n".join(logs),
                stderr="",
                exit_code=0 if not timed_out else -1,
                timed_out=timed_out,
            )
        finally:
            try:
                await container.delete(force=True)
            except Exception:
                pass


async def demo():
    result = await run_in_container("print(2 ** 32)")
    print(f"Result: {result.stdout.strip()}")

# asyncio.run(demo())
```

---

## Solution 3: Restricted Python Evaluator with AST Validation

Parse the code's AST before executing and reject dangerous node types (import, exec, open, subprocess).

```python
import ast
import asyncio
import math
import re
from dataclasses import dataclass
from typing import Any, Optional


FORBIDDEN_NODES = {
    ast.Import,
    ast.ImportFrom,
    ast.Global,
    ast.Nonlocal,
    ast.Delete,
}

FORBIDDEN_NAMES = {
    "__import__", "eval", "exec", "compile", "open",
    "globals", "locals", "vars", "dir", "getattr", "setattr", "delattr",
    "hasattr", "__class__", "__subclasses__", "__bases__",
    "subprocess", "os", "sys", "builtins",
}

SAFE_BUILTINS = {
    "abs", "all", "any", "bin", "bool", "chr", "dict", "divmod",
    "enumerate", "filter", "float", "format", "frozenset", "hex",
    "int", "isinstance", "issubclass", "iter", "len", "list",
    "map", "max", "min", "next", "oct", "ord", "pow", "print",
    "range", "repr", "reversed", "round", "set", "slice", "sorted",
    "str", "sum", "tuple", "type", "zip",
    "True", "False", "None",
}


class ASTValidator(ast.NodeVisitor):
    def __init__(self):
        self.errors: list[str] = []

    def generic_visit(self, node):
        if type(node) in FORBIDDEN_NODES:
            self.errors.append(f"Forbidden node: {type(node).__name__}")
        super().generic_visit(node)

    def visit_Name(self, node):
        if node.id in FORBIDDEN_NAMES:
            self.errors.append(f"Forbidden name: {node.id}")
        self.generic_visit(node)

    def visit_Attribute(self, node):
        if node.attr.startswith("__") and node.attr.endswith("__"):
            self.errors.append(f"Dunder attribute access forbidden: {node.attr}")
        self.generic_visit(node)

    def visit_Call(self, node):
        if isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_NAMES:
            self.errors.append(f"Forbidden call: {node.func.id}")
        self.generic_visit(node)


@dataclass
class EvalResult:
    value: Any = None
    stdout: str = ""
    error: Optional[str] = None
    blocked: bool = False
    block_reasons: list[str] = None  # type: ignore

    def __post_init__(self):
        if self.block_reasons is None:
            self.block_reasons = []


def safe_eval(
    code: str,
    extra_globals: Optional[dict] = None,
    max_code_length: int = 2000,
) -> EvalResult:
    if len(code) > max_code_length:
        return EvalResult(blocked=True, block_reasons=["code_too_long"])

    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as exc:
        return EvalResult(error=f"SyntaxError: {exc}")

    validator = ASTValidator()
    validator.visit(tree)
    if validator.errors:
        return EvalResult(blocked=True, block_reasons=validator.errors)

    safe_globals = {
        "__builtins__": {name: __builtins__[name] for name in SAFE_BUILTINS
                        if isinstance(__builtins__, dict) and name in __builtins__},  # type: ignore
        "math": math,
        **(extra_globals or {}),
    }

    import io, contextlib
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            exec(compile(tree, "<sandbox>", "exec"), safe_globals)
        return EvalResult(value=safe_globals.get("result"), stdout=buf.getvalue())
    except Exception as exc:
        return EvalResult(error=str(exc), stdout=buf.getvalue())


def demo():
    # Safe
    r1 = safe_eval("result = sum(range(100))\nprint(result)")
    print(f"OK: value={r1.value} stdout={r1.stdout.strip()}")

    # Blocked: import
    r2 = safe_eval("import os; print(os.environ)")
    print(f"Blocked: {r2.block_reasons}")

    # Blocked: eval
    r3 = safe_eval("eval('2+2')")
    print(f"Blocked: {r3.block_reasons}")

    # Allowed: math
    r4 = safe_eval("result = math.sqrt(144)")
    print(f"Math: {r4.value}")

demo()
```

---

## Solution 4: Filesystem Jail with chroot-Like Path Restrictions

Restrict tool filesystem access to a dedicated work directory; reject path traversal attempts.

```python
import asyncio
import os
import pathlib
import tempfile
from dataclasses import dataclass
from typing import Optional


class FilesystemJail:
    """
    Restricts all file operations to a temporary directory.
    Raises PermissionError on path traversal or access outside jail.
    """

    def __init__(self, jail_root: Optional[str] = None):
        if jail_root:
            self._root = pathlib.Path(jail_root).resolve()
        else:
            self._tmpdir = tempfile.TemporaryDirectory()
            self._root = pathlib.Path(self._tmpdir.name).resolve()

    def _safe_path(self, relative: str) -> pathlib.Path:
        resolved = (self._root / relative).resolve()
        if not str(resolved).startswith(str(self._root)):
            raise PermissionError(
                f"Path traversal detected: '{relative}' → '{resolved}' "
                f"is outside jail '{self._root}'"
            )
        return resolved

    def read(self, path: str, max_bytes: int = 1_048_576) -> bytes:
        safe = self._safe_path(path)
        with open(safe, "rb") as f:
            return f.read(max_bytes)

    def write(self, path: str, data: bytes, max_bytes: int = 10_485_760) -> int:
        if len(data) > max_bytes:
            raise ValueError(f"Write exceeds limit: {len(data)} > {max_bytes}")
        safe = self._safe_path(path)
        safe.parent.mkdir(parents=True, exist_ok=True)
        with open(safe, "wb") as f:
            return f.write(data)

    def list_dir(self, path: str = ".") -> list[str]:
        safe = self._safe_path(path)
        return [p.name for p in safe.iterdir()]

    def exists(self, path: str) -> bool:
        try:
            return self._safe_path(path).exists()
        except PermissionError:
            return False

    def cleanup(self) -> None:
        if hasattr(self, "_tmpdir"):
            self._tmpdir.cleanup()


class JailedToolAgent:
    def __init__(self, api_key: str):
        import anthropic
        self.client = anthropic.AsyncAnthropic(api_key=api_key)

    async def run_file_tool(self, tool_name: str, args: dict) -> dict:
        jail = FilesystemJail()
        try:
            if tool_name == "read_file":
                path = args.get("path", "")
                data = jail.read(path)
                return {"content": data.decode("utf-8", errors="replace")}

            elif tool_name == "write_file":
                path = args.get("path", "output.txt")
                content = args.get("content", "")
                n = jail.write(path, content.encode())
                return {"bytes_written": n}

            elif tool_name == "list_files":
                path = args.get("path", ".")
                return {"files": jail.list_dir(path)}

            else:
                return {"error": f"Unknown tool: {tool_name}"}

        except PermissionError as e:
            print(f"[jail] BLOCKED path traversal: {e}")
            return {"error": "access_denied", "detail": str(e)}
        finally:
            jail.cleanup()


def demo():
    jail = FilesystemJail()
    jail.write("output/result.txt", b"Hello from jail!")
    content = jail.read("output/result.txt")
    print(f"Read: {content.decode()}")

    # Path traversal attempt
    try:
        jail.read("../../etc/passwd")
    except PermissionError as e:
        print(f"Blocked: {e}")

    jail.cleanup()

demo()
```

---

## Solution 5: Network Egress Filtering for Tool Calls

Allow tools to make HTTP requests only to an explicit allowlist of domains; block everything else.

```python
import asyncio
import re
from typing import Optional
from urllib.parse import urlparse

# pip install aiohttp
try:
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False


ALLOWED_DOMAINS = {
    "api.anthropic.com",
    "api.openai.com",
    "serpapi.com",
    "en.wikipedia.org",
    "api.github.com",
}

BLOCKED_CIDRS = [
    "127.0.0.0/8",    # Loopback
    "10.0.0.0/8",     # Private
    "172.16.0.0/12",  # Private
    "192.168.0.0/16", # Private
    "169.254.0.0/16", # Link-local (AWS metadata)
    "0.0.0.0/8",
]


def is_allowed_url(url: str) -> tuple[bool, str]:
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False, f"Scheme '{parsed.scheme}' not allowed (only http/https)"

        host = parsed.netloc.split(":")[0].lower()

        # Check allowlist
        if host not in ALLOWED_DOMAINS:
            # Check wildcard subdomains
            for allowed in ALLOWED_DOMAINS:
                if host.endswith(f".{allowed}") or host == allowed:
                    return True, "ok"
            return False, f"Domain '{host}' not in allowlist"

        return True, "ok"
    except Exception as exc:
        return False, f"URL parse error: {exc}"


class EgressFilteredSession:
    """aiohttp ClientSession wrapper that validates URLs before requests."""

    def __init__(self):
        if HAS_AIOHTTP:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10.0),
                headers={"User-Agent": "agent-sandbox/1.0"},
            )

    async def get(self, url: str, **kwargs) -> dict:
        allowed, reason = is_allowed_url(url)
        if not allowed:
            print(f"[egress] BLOCKED: {url} — {reason}")
            return {"error": "network_access_denied", "url": url, "reason": reason}

        async with self._session.get(url, **kwargs) as resp:
            text = await resp.text()
            return {"status": resp.status, "body": text[:5000]}

    async def post(self, url: str, **kwargs) -> dict:
        allowed, reason = is_allowed_url(url)
        if not allowed:
            print(f"[egress] BLOCKED POST: {url} — {reason}")
            return {"error": "network_access_denied"}

        async with self._session.post(url, **kwargs) as resp:
            text = await resp.text()
            return {"status": resp.status, "body": text[:5000]}

    async def close(self) -> None:
        if HAS_AIOHTTP:
            await self._session.close()


async def demo():
    session = EgressFilteredSession()

    # Allowed
    result = await session.get("https://en.wikipedia.org/wiki/Sandbox")
    print(f"Wikipedia: status={result.get('status')}")

    # Blocked — SSRF attempt
    result = await session.get("http://169.254.169.254/latest/meta-data/")
    print(f"AWS metadata: {result.get('error')}")

    # Blocked — unknown domain
    result = await session.get("https://attacker.example.com/exfil")
    print(f"Attacker: {result.get('reason')}")

    await session.close()

# asyncio.run(demo())
```

---

## Solution 6: Tool Execution Audit Log with Anomaly Detection

Log every tool call with arguments and results; flag anomalous patterns (large output, unexpected domains, high frequency).

```python
import asyncio
import hashlib
import json
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Optional
import anthropic


@dataclass
class ToolCallRecord:
    call_id: str
    session_id: str
    tool_name: str
    input_hash: str      # Hash of args (not raw args — may contain secrets)
    output_tokens: int
    duration_ms: float
    timestamp: float = field(default_factory=time.time)
    flagged: bool = False
    flag_reasons: list[str] = field(default_factory=list)


class ToolAuditMonitor:
    def __init__(
        self,
        max_calls_per_minute: int = 30,
        max_output_tokens: int = 2000,
        window_seconds: float = 60.0,
    ):
        self._calls: list[ToolCallRecord] = []
        self._rate_windows: dict[str, deque] = defaultdict(lambda: deque())
        self._max_rate = max_calls_per_minute
        self._max_output = max_output_tokens
        self._window = window_seconds

    def record(
        self,
        session_id: str,
        tool_name: str,
        args: dict,
        result: Any,
        duration_ms: float,
    ) -> ToolCallRecord:
        import secrets
        args_hash = hashlib.sha256(json.dumps(args, sort_keys=True).encode()).hexdigest()[:8]
        output_str = json.dumps(result)
        output_tokens = len(output_str) // 4  # Rough estimate

        flags: list[str] = []

        # Rate check per session
        now = time.time()
        window = self._rate_windows[session_id]
        cutoff = now - self._window
        while window and window[0] < cutoff:
            window.popleft()
        window.append(now)
        if len(window) > self._max_rate:
            flags.append(f"rate_limit_exceeded:{len(window)}_calls_per_min")

        # Output size check
        if output_tokens > self._max_output:
            flags.append(f"output_too_large:{output_tokens}_tokens")

        record = ToolCallRecord(
            call_id=secrets.token_hex(6),
            session_id=session_id,
            tool_name=tool_name,
            input_hash=args_hash,
            output_tokens=output_tokens,
            duration_ms=duration_ms,
            flagged=bool(flags),
            flag_reasons=flags,
        )
        self._calls.append(record)

        if flags:
            print(
                f"[audit] FLAGGED tool={tool_name} session={session_id} "
                f"flags={flags}"
            )
        return record

    def session_summary(self, session_id: str) -> dict:
        session_calls = [c for c in self._calls if c.session_id == session_id]
        return {
            "session_id": session_id,
            "total_calls": len(session_calls),
            "flagged_calls": sum(1 for c in session_calls if c.flagged),
            "total_output_tokens": sum(c.output_tokens for c in session_calls),
            "tools_used": list({c.tool_name for c in session_calls}),
        }


monitor = ToolAuditMonitor()


class AuditedToolAgent:
    def __init__(self, api_key: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)

    async def _execute_tool(
        self, tool_name: str, args: dict, session_id: str
    ) -> Any:
        start = time.perf_counter()

        # Actual tool execution (replace with real implementation)
        if tool_name == "web_search":
            result = [{"title": f"Result for {args.get('query')}", "url": "https://example.com"}]
        elif tool_name == "read_file":
            result = {"content": "file contents here"}
        else:
            result = {"error": "unknown tool"}

        duration_ms = (time.perf_counter() - start) * 1000
        monitor.record(session_id, tool_name, args, result, duration_ms)
        return result

    async def run(self, session_id: str, user_message: str) -> str:
        response = await self.client.messages.create(
            model="claude-opus-4-6",
            max_tokens=256,
            messages=[{"role": "user", "content": user_message}],
        )
        return response.content[0].text


async def demo():
    agent = AuditedToolAgent(api_key="sk-...")

    for i in range(35):  # Trigger rate limit flag
        await agent._execute_tool("web_search", {"query": f"term {i}"}, "sess_audit")

    summary = monitor.session_summary("sess_audit")
    print(f"Summary: {summary}")

# asyncio.run(demo())
```

---

## Comparison

| Solution | Isolation Level | Network Control | Filesystem | CPU/Mem Limits | Complexity |
|---|---|---|---|---|---|
| Subprocess + resource limits | Process | Inherited | Host (env stripped) | Yes | Medium |
| Docker container | Container | None (disabled) | Read-only | Yes | High |
| AST validator + restricted eval | In-process | No | No | No | Medium |
| Filesystem jail (path restrict) | In-process | No | Jail root | No | Low |
| Egress URL allowlist | In-process | Yes | No | No | Low |
| Audit log + anomaly detection | No isolation | No | No | No | Low |

**Recommendation:** Layer Solution 1 (subprocess + resource limits) as the baseline for Python tool execution — it provides process isolation with CPU/memory caps in ~40 lines. Add Solution 4 (filesystem jail) for any tool that reads or writes files. Add Solution 5 (egress filtering) for any tool that makes HTTP requests. Use Solution 2 (Docker) only if your infrastructure supports it and the isolation requirement is high (e.g., executing user-submitted code).
