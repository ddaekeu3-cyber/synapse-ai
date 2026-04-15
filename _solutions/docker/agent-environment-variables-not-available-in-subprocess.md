---
layout: solution
title: "Agent Environment Variables Not Available in Subprocess — Missing Config"
category: docker
description: "Agent sets an environment variable in Python. It spawns a subprocess. The subprocess doesn't see the variable. Or: Docker container has env vars but a subprocess launched with subprocess.run() doesn't inherit them. The subprocess fails with 'API_KEY not set'."
tags: [docker, environment-variables, subprocess, env, inheritance, process, configuration, shell]
---

# Agent Environment Variables Not Available in Subprocess — Missing Config

## Symptom
- Agent sets `os.environ["API_KEY"] = value` but a spawned subprocess exits with `API_KEY not set`
- Docker container env vars visible in Python but missing in `subprocess.run(["bash", "script.sh"])`
- Shell script run via subprocess can't find `DATABASE_URL` that was passed to the container
- `subprocess.Popen` with `shell=False` doesn't expand environment variables from parent
- `exec()` call inside subprocess uses a clean environment — missing all parent env vars
- Environment variable set in one thread not visible in subprocess spawned by another thread

## Root Cause
Subprocess environment inheritance depends on how the subprocess is launched. By default, `subprocess.run()` inherits the parent process environment — but only what was in `os.environ` at the time of the call, not variables set afterward in ways that bypass `os.environ`. `env={}` or `env={"KEY": "value"}` (without merging parent env) creates a completely clean environment. In Docker, variables must be declared in the Dockerfile or docker-compose — simply setting them in the entrypoint script may not propagate to all processes.

## Fix

### Option 1: Always merge parent environment when customizing subprocess env

```python
import subprocess
import os

# WRONG — completely replaces env, subprocess loses all parent vars
result = subprocess.run(
    ["python3", "worker.py"],
    env={"MY_CUSTOM_VAR": "value"},  # Subprocess has ONLY this var
    capture_output=True
)

# WRONG — subprocess.run() with no env= inherits parent,
# but only if API_KEY was set before the Python process started
# (not effective for keys set via os.environ later in the script)

# RIGHT — inherit parent env AND add custom vars
subprocess_env = {**os.environ, "MY_CUSTOM_VAR": "value"}
result = subprocess.run(
    ["python3", "worker.py"],
    env=subprocess_env,
    capture_output=True,
    text=True
)

# RIGHT — inherit and override specific vars:
def run_subprocess(cmd: list[str], extra_env: dict = None) -> subprocess.CompletedProcess:
    """
    Run subprocess with parent environment + optional overrides.
    Guarantees all parent env vars are available to the subprocess.
    """
    env = dict(os.environ)  # Copy current env
    if extra_env:
        env.update(extra_env)

    return subprocess.run(
        cmd,
        env=env,
        capture_output=True,
        text=True,
        timeout=60
    )

# Usage:
result = run_subprocess(
    ["bash", "deploy.sh"],
    extra_env={"DEPLOY_ENV": "staging", "DRY_RUN": "false"}
)
```

### Option 2: Set env vars before subprocess launch using os.environ

```python
import os
import subprocess

def configure_subprocess_env(required_vars: dict[str, str], optional_vars: dict[str, str] = None):
    """
    Ensure all required environment variables are set in os.environ
    before spawning subprocesses.
    Validates required vars — fails fast if missing.
    """
    missing = []
    for key, description in required_vars.items():
        if key not in os.environ:
            missing.append(f"  {key}: {description}")

    if missing:
        raise EnvironmentError(
            "Missing required environment variables for subprocess:\n" +
            "\n".join(missing) +
            "\n\nSet these before running the agent."
        )

    # Apply optional vars with defaults if not already set
    if optional_vars:
        for key, default_value in optional_vars.items():
            if key not in os.environ:
                os.environ[key] = default_value
                print(f"Set default: {key}={default_value}")

# Call at startup — before spawning any subprocess:
configure_subprocess_env(
    required_vars={
        "API_KEY": "Anthropic API key for model calls",
        "DATABASE_URL": "PostgreSQL connection string",
    },
    optional_vars={
        "LOG_LEVEL": "INFO",
        "WORKER_TIMEOUT": "60",
    }
)

# Now subprocesses inherit all vars from os.environ:
result = subprocess.run(["python3", "worker.py"], capture_output=True, text=True)
```

### Option 3: Explicit env file for subprocess isolation

```python
import os
import tempfile
import subprocess
from pathlib import Path

def create_env_file(variables: dict[str, str]) -> str:
    """
    Write environment variables to a temp .env file.
    Subprocess loads this file — works even across exec() boundaries.
    """
    lines = []
    for key, value in variables.items():
        # Escape special characters in value
        escaped = value.replace("'", "'\\''")
        lines.append(f"export {key}='{escaped}'")

    content = "\n".join(lines)
    tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.env', delete=False)
    tmp.write(content)
    tmp.close()
    return tmp.name

def run_script_with_env(script_path: str, env_vars: dict[str, str]) -> subprocess.CompletedProcess:
    """
    Run a shell script with explicit env vars — works even if script uses exec or su.
    """
    env_file = create_env_file(env_vars)

    try:
        # Source the env file before running the script
        cmd = ["bash", "-c", f"source {env_file} && bash {script_path}"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return result
    finally:
        os.unlink(env_file)  # Clean up temp file

# Pass secrets explicitly — don't rely on inheritance:
result = run_script_with_env(
    script_path="./scripts/process_data.sh",
    env_vars={
        "API_KEY": os.environ["API_KEY"],
        "DATABASE_URL": os.environ["DATABASE_URL"],
        "OUTPUT_DIR": "/data/output"
    }
)
```

### Option 4: Docker env var debugging and Dockerfile best practices

```dockerfile
# Dockerfile — make env vars available to all processes

# BAD: ENV only sets at image build time
# ENV API_KEY=hardcoded_value  # Never hardcode secrets

# GOOD: Declare expected env vars (no default = required at runtime)
ENV API_KEY=""
ENV DATABASE_URL=""
ENV LOG_LEVEL="INFO"

# GOOD: Pass build args separately from runtime env
ARG BUILD_VERSION=dev
ENV BUILD_VERSION=${BUILD_VERSION}

# GOOD: Entrypoint validates env before starting agent
COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python3", "-m", "agent"]
```

```bash
#!/bin/bash
# docker-entrypoint.sh — validate env vars and export them

set -e

# Validate required env vars
required_vars=("API_KEY" "DATABASE_URL")
missing=()

for var in "${required_vars[@]}"; do
    if [[ -z "${!var}" ]]; then
        missing+=("$var")
    fi
done

if [[ ${#missing[@]} -gt 0 ]]; then
    echo "ERROR: Missing required environment variables: ${missing[*]}"
    echo "Pass them via: docker run -e API_KEY=... -e DATABASE_URL=..."
    exit 1
fi

# Export all vars so subprocesses inherit them
export API_KEY DATABASE_URL LOG_LEVEL

echo "Environment validated. Starting agent..."
exec "$@"
```

```yaml
# docker-compose.yml — proper env var injection
services:
  agent:
    image: my-agent:latest
    environment:
      # Reference host env vars (must be set on host):
      API_KEY: ${API_KEY}
      DATABASE_URL: ${DATABASE_URL}
      # Set directly:
      LOG_LEVEL: INFO
      WORKER_TIMEOUT: "60"
    env_file:
      - .env.local   # Optional local overrides
```

### Option 5: asyncio subprocess with environment

```python
import asyncio
import os

async def run_async_subprocess(
    cmd: list[str],
    extra_env: dict[str, str] = None,
    timeout: float = 60.0
) -> tuple[str, str, int]:
    """
    Run subprocess asynchronously with inherited + extra environment.
    Returns (stdout, stderr, returncode).
    """
    env = {**os.environ}
    if extra_env:
        env.update(extra_env)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise TimeoutError(f"Subprocess timed out after {timeout}s: {cmd}")

    return stdout.decode(), stderr.decode(), proc.returncode

async def run_worker(worker_id: int) -> dict:
    """Run worker subprocess with proper env inheritance"""
    stdout, stderr, code = await run_async_subprocess(
        cmd=["python3", "-m", "worker", f"--id={worker_id}"],
        extra_env={
            "WORKER_ID": str(worker_id),
            "PYTHONPATH": os.environ.get("PYTHONPATH", "")
        },
        timeout=120.0
    )

    if code != 0:
        raise RuntimeError(
            f"Worker {worker_id} failed (exit code {code}):\n"
            f"STDOUT: {stdout[:500]}\n"
            f"STDERR: {stderr[:500]}"
        )

    return {"worker_id": worker_id, "output": stdout}
```

### Option 6: Debug missing env vars in subprocess

```python
import subprocess
import os

def debug_subprocess_environment(cmd: list[str]) -> dict:
    """
    Run a subprocess that prints its own environment.
    Useful for diagnosing why a subprocess can't see expected vars.
    """
    env_dump = subprocess.run(
        ["python3", "-c", "import os, json; print(json.dumps(dict(os.environ)))"],
        capture_output=True,
        text=True,
        env={**os.environ}  # Explicit inheritance
    )

    if env_dump.returncode != 0:
        return {"error": env_dump.stderr}

    import json
    subprocess_env = json.loads(env_dump.stdout)

    # Compare with parent
    parent_only = {k for k in os.environ if k not in subprocess_env}
    subprocess_only = {k for k in subprocess_env if k not in os.environ}
    different_values = {
        k for k in os.environ
        if k in subprocess_env and os.environ[k] != subprocess_env[k]
    }

    report = {
        "parent_env_count": len(os.environ),
        "subprocess_env_count": len(subprocess_env),
        "missing_in_subprocess": list(parent_only),
        "extra_in_subprocess": list(subprocess_only),
        "different_values": list(different_values),
    }

    if parent_only:
        print(f"WARNING: These vars are NOT inherited by subprocess: {parent_only}")
    if different_values:
        print(f"WARNING: These vars have different values in subprocess: {different_values}")

    return report

# Run this to diagnose env inheritance issues:
report = debug_subprocess_environment(["python3", "worker.py"])
print(f"Missing in subprocess: {report['missing_in_subprocess']}")
```

## Subprocess Environment Inheritance Rules

| Launch Method | Inherits Parent Env? | Notes |
|---|---|---|
| `subprocess.run(cmd)` | Yes | Default — inherits `os.environ` at launch time |
| `subprocess.run(cmd, env={})` | No | Empty env — explicit empty dict replaces all |
| `subprocess.run(cmd, env={**os.environ, "K": "V"})` | Yes + extras | Correct pattern |
| `os.system(cmd)` | Yes | Shell inherits env, but avoid — no output capture |
| `os.execve(cmd, args, env)` | Explicit only | Must pass env explicitly — replaces process |
| Docker `CMD` | Yes | Inherits from `docker run -e` / Dockerfile `ENV` |
| Docker `exec` into container | Yes | Same env as container |
| `su - user` in container | No | Login shell resets env — use `su -m` or `sudo -E` |

## Expected Token Savings
Subprocess fails → agent retries with debug → long investigation loop: ~15,000 tokens
Proper env inheritance → subprocess runs first time: 0 debugging overhead

## Environment
- Any agent that spawns subprocesses, shell scripts, or child processes; critical for agents running code execution tools, build systems, or multi-process pipelines
- Source: direct experience; subprocess environment issues are the most common cause of "works on my machine" failures when agents are containerized
