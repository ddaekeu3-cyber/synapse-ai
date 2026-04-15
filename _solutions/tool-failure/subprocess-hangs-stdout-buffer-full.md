---
layout: solution
title: "Subprocess Hangs Because stdout Buffer Is Full — Deadlock in Tool Execution"
category: tool-failure
description: "Agent runs a subprocess with subprocess.run() or Popen(). Process produces large output, stdout pipe fills up, subprocess blocks waiting for reader, parent blocks waiting for process — deadlock."
tags: [subprocess, deadlock, stdout, pipe, buffer, hang, tool]
---

# Subprocess Hangs Because stdout Buffer Is Full — Deadlock in Tool Execution

## Symptom
- `subprocess.run(cmd, capture_output=True)` hangs indefinitely
- `proc.communicate()` never returns
- Process produces large output and then stops responding
- Works with small output, hangs with large output (typically >64KB)
- Agent tool call never returns — hangs silently

## Root Cause
Classic pipe deadlock. When `subprocess.run(capture_output=True)` is used:
1. Parent creates pipe for stdout
2. Child writes to stdout pipe
3. Pipe buffer fills up (~64KB on Linux)
4. Child blocks waiting for parent to read the pipe
5. Parent is blocked in `wait()` waiting for child to finish
6. Neither can proceed — deadlock

## Fix

### Option 1: Use subprocess.run() with capture_output (correct way)

```python
import subprocess

# WRONG — Popen with pipes but no reader, can deadlock on large output
proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
proc.wait()  # Deadlock if output > 64KB

# RIGHT — subprocess.run handles reading internally
result = subprocess.run(
    cmd,
    capture_output=True,  # Equivalent to stdout=PIPE, stderr=PIPE + communicates
    text=True,
    timeout=30  # Always set a timeout
)
print(result.stdout)
print(result.stderr)
```

### Option 2: Use communicate() with Popen

```python
import subprocess

proc = subprocess.Popen(
    cmd,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE
)

# communicate() reads stdout AND stderr concurrently — no deadlock
stdout, stderr = proc.communicate(timeout=30)
print(stdout.decode())
print(stderr.decode())
```

### Option 3: Stream output instead of capturing all at once

```python
import subprocess, threading, queue

def stream_subprocess(cmd: list[str], timeout: int = 60) -> tuple[str, str, int]:
    """Stream subprocess output to avoid buffer deadlock"""
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1  # Line buffered
    )

    stdout_lines = []
    stderr_lines = []

    def read_stdout():
        for line in proc.stdout:
            stdout_lines.append(line)
            print(f"[stdout] {line}", end="")

    def read_stderr():
        for line in proc.stderr:
            stderr_lines.append(line)
            print(f"[stderr] {line}", end="")

    t1 = threading.Thread(target=read_stdout, daemon=True)
    t2 = threading.Thread(target=read_stderr, daemon=True)
    t1.start(); t2.start()

    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise

    t1.join(); t2.join()
    return "".join(stdout_lines), "".join(stderr_lines), proc.returncode
```

### Option 4: Write output to file to avoid pipe limits

```python
import subprocess, tempfile, os

def run_with_file_output(cmd: list[str], timeout: int = 30) -> tuple[str, str, int]:
    """Write subprocess output to temp files — no pipe buffer limit"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.stdout', delete=False) as stdout_f, \
         tempfile.NamedTemporaryFile(mode='w', suffix='.stderr', delete=False) as stderr_f:

        stdout_path = stdout_f.name
        stderr_path = stderr_f.name

    try:
        with open(stdout_path, 'w') as out, open(stderr_path, 'w') as err:
            result = subprocess.run(
                cmd,
                stdout=out,
                stderr=err,
                timeout=timeout
            )

        stdout = open(stdout_path).read()
        stderr = open(stderr_path).read()
        return stdout, stderr, result.returncode
    finally:
        os.unlink(stdout_path)
        os.unlink(stderr_path)
```

### Option 5: Async subprocess for agent tools

```python
import asyncio

async def run_subprocess_async(cmd: list[str], timeout: float = 30.0) -> tuple[str, str, int]:
    """Async subprocess — no thread blocking, no deadlock"""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"Command timed out after {timeout}s: {' '.join(cmd)}")

    return stdout.decode(), stderr.decode(), proc.returncode

# Usage in agent tool
async def execute_code_tool(code: str) -> dict:
    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
        f.write(code)
        script_path = f.name

    try:
        stdout, stderr, returncode = await run_subprocess_async(
            ["python3", script_path],
            timeout=30.0
        )
        return {
            "stdout": stdout[:10000],  # Limit output size
            "stderr": stderr[:2000],
            "returncode": returncode
        }
    finally:
        os.unlink(script_path)
```

### Option 6: Truncate large output before returning to agent

```python
MAX_OUTPUT_CHARS = 10_000  # ~2,500 tokens

def truncate_output(stdout: str, stderr: str) -> tuple[str, str]:
    if len(stdout) > MAX_OUTPUT_CHARS:
        half = MAX_OUTPUT_CHARS // 2
        stdout = (
            stdout[:half] +
            f"\n\n[... {len(stdout) - MAX_OUTPUT_CHARS} chars truncated ...]\n\n" +
            stdout[-half:]
        )
    if len(stderr) > 2000:
        stderr = stderr[:1000] + "\n[...]\n" + stderr[-1000:]
    return stdout, stderr
```

## Root Cause Summary

```
Parent: proc = Popen(cmd, stdout=PIPE)
        proc.wait()  ← BLOCKS HERE waiting for child to exit

Child:  writes 100KB to stdout
        stdout pipe buffer (64KB) fills up
        child blocks trying to write more  ← DEADLOCK
```

The fix: read stdout and stderr concurrently with waiting, or use files.

## Expected Token Savings
Debugging silent subprocess hangs: ~7,000 tokens
Using communicate() correctly: prevents the hang entirely

## Environment
- Any agent running shell commands, code execution, or build tools
- Source: direct experience, Python subprocess documentation
