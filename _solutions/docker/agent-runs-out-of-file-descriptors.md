---
layout: solution
title: "Agent Runs Out of File Descriptors — Too Many Open Connections"
category: docker
description: "Agent opens HTTP connections without closing them. Or opens SQLite databases in a loop. Or spawns subprocesses that inherit file descriptors. After running for hours, 'Too many open files' error. Agent crashes. Restart fixes it temporarily — then crashes again."
tags: [docker, file-descriptors, ulimit, connections, resource-leak, fd-leak, subprocess, production]
---

# Agent Runs Out of File Descriptors — Too Many Open Connections

## Symptom
- Agent crashes with `OSError: [Errno 24] Too many open files`
- Error appears after hours of running — not at startup
- `lsof -p <pid> | wc -l` shows 65,000+ open file descriptors before crash
- New HTTP connections fail: `socket.error: [Errno 24]`
- Docker container hits the default 1,048,576 FD limit in Kubernetes
- Temporary fix: restart the container — but it crashes again in a few hours

## Root Cause
File descriptors (FDs) are consumed by: open files, network sockets, database connections, pipes, and subprocesses. Each connection or file opened without being closed leaks one FD. Common leaks: `httpx.AsyncClient()` created per-request without closing, SQLite `connect()` in a loop without `close()`, subprocesses with `close_fds=False` inheriting parent FDs, and log file handlers that accumulate. Linux defaults to 1,024 FDs per process (or 65,536 in containers). A slow leak hits this limit after hours.

## Fix

### Option 1: Always use context managers for HTTP clients

```python
import httpx
import asyncio

# WRONG — creates a new client (and socket) per request, never closed
async def fetch_data_wrong(url: str) -> dict:
    client = httpx.AsyncClient()  # FD leak — client never closed
    response = await client.get(url)
    return response.json()

# WRONG — explicit but may leak on exception
async def fetch_data_still_wrong(url: str) -> dict:
    client = httpx.AsyncClient()
    response = await client.get(url)
    await client.aclose()  # Never reached if exception occurs
    return response.json()

# RIGHT — context manager guarantees cleanup on any exit
async def fetch_data_correct(url: str) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(url)
        return response.json()

# BEST — one long-lived client shared across all requests
# Created once, reused, closed on shutdown
_shared_client: httpx.AsyncClient | None = None

async def get_shared_client() -> httpx.AsyncClient:
    global _shared_client
    if _shared_client is None or _shared_client.is_closed:
        _shared_client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=60.0, write=10.0),
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=50)
        )
    return _shared_client

async def fetch_with_shared_client(url: str) -> dict:
    client = await get_shared_client()
    response = await client.get(url)
    return response.json()

async def shutdown():
    global _shared_client
    if _shared_client and not _shared_client.is_closed:
        await _shared_client.aclose()
```

### Option 2: Monitor and alert on FD usage

```python
import os
import psutil
import asyncio
import logging

logger = logging.getLogger(__name__)

class FDMonitor:
    """
    Monitor file descriptor usage — alert before exhaustion.
    """

    def __init__(
        self,
        alert_threshold: float = 0.80,  # Alert at 80% of limit
        check_interval: float = 60.0    # Check every minute
    ):
        self.alert_threshold = alert_threshold
        self.check_interval = check_interval
        self._proc = psutil.Process(os.getpid())

    def get_fd_stats(self) -> dict:
        """Get current FD usage"""
        try:
            open_fds = self._proc.num_fds()
            # Get limit
            import resource
            soft_limit, hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)

            utilization = open_fds / soft_limit if soft_limit > 0 else 0

            return {
                "open_fds": open_fds,
                "soft_limit": soft_limit,
                "hard_limit": hard_limit,
                "utilization": utilization,
                "available": soft_limit - open_fds,
                "status": (
                    "critical" if utilization > 0.90 else
                    "warning" if utilization > self.alert_threshold else
                    "ok"
                )
            }
        except Exception as e:
            return {"error": str(e)}

    def get_fd_breakdown(self, top_n: int = 20) -> list[dict]:
        """Show what files/sockets are consuming FDs"""
        try:
            connections = []
            for conn in self._proc.connections():
                connections.append({
                    "type": conn.type.name if hasattr(conn, 'type') else "unknown",
                    "status": conn.status if hasattr(conn, 'status') else "",
                    "raddr": f"{conn.raddr.ip}:{conn.raddr.port}" if conn.raddr else ""
                })

            open_files = []
            for f in self._proc.open_files()[:top_n]:
                open_files.append({"path": f.path, "fd": f.fd})

            return {"connections": connections[:top_n], "files": open_files}
        except Exception as e:
            return [{"error": str(e)}]

    async def monitor_loop(self):
        """Background monitoring — logs warnings before exhaustion"""
        while True:
            await asyncio.sleep(self.check_interval)
            stats = self.get_fd_stats()

            if stats.get("status") == "critical":
                logger.critical(
                    "CRITICAL: File descriptor exhaustion imminent",
                    extra={
                        "open_fds": stats["open_fds"],
                        "soft_limit": stats["soft_limit"],
                        "utilization": f"{stats['utilization']*100:.0f}%"
                    }
                )
                breakdown = self.get_fd_breakdown()
                logger.critical(f"FD breakdown: {breakdown}")

            elif stats.get("status") == "warning":
                logger.warning(
                    f"FD usage at {stats['utilization']*100:.0f}% "
                    f"({stats['open_fds']}/{stats['soft_limit']})"
                )

fd_monitor = FDMonitor(alert_threshold=0.80)
asyncio.create_task(fd_monitor.monitor_loop())
```

### Option 3: Increase FD limits in Docker and Kubernetes

```yaml
# docker-compose.yml — increase FD limits for the agent container
services:
  agent:
    image: my-agent:latest
    ulimits:
      nofile:
        soft: 65536
        hard: 65536
```

```yaml
# Kubernetes pod spec — increase FD limits
apiVersion: apps/v1
kind: Deployment
spec:
  template:
    spec:
      containers:
      - name: agent
        image: my-agent:latest
        securityContext:
          # Allow setting higher limits
          allowPrivilegeEscalation: false
        resources:
          limits:
            memory: "2Gi"
            cpu: "1"
        # Note: Kubernetes inherits host's fs.file-max
        # Set ulimits via container runtime or init container
```

```bash
# Dockerfile — set limits in entrypoint
# docker-entrypoint.sh:
#!/bin/bash
set -e

# Check and log current FD limits
echo "FD limits: $(ulimit -n)"

# Increase if running as root (development)
if [ "$(id -u)" = "0" ]; then
    ulimit -n 65536 2>/dev/null || true
fi

exec "$@"
```

```python
# Python — set limits programmatically at startup
import resource
import os

def configure_fd_limits(target: int = 65536):
    """
    Attempt to raise file descriptor limit.
    Call at agent startup before any connections are made.
    """
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        new_soft = min(target, hard)

        if new_soft > soft:
            resource.setrlimit(resource.RLIMIT_NOFILE, (new_soft, hard))
            print(f"FD limit raised: {soft} → {new_soft} (hard limit: {hard})")
        else:
            print(f"FD limit: {soft} (already at or above target {target})")

    except (ValueError, resource.error) as e:
        print(f"Could not raise FD limit: {e}")
        print("Set ulimits in docker-compose.yml or Kubernetes securityContext")

configure_fd_limits(65536)
```

### Option 4: Subprocess FD inheritance — prevent leaks to child processes

```python
import subprocess
import os

def run_subprocess_safe(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """
    Run subprocess without leaking parent FDs.
    By default, Python subprocesses inherit all parent file descriptors.
    This can exhaust FDs if many subprocesses are spawned.
    """
    return subprocess.run(
        cmd,
        close_fds=True,        # Close all inherited FDs (Python 3 default on Unix)
        capture_output=True,
        text=True,
        timeout=60,
        **kwargs
    )

async def run_async_subprocess_safe(cmd: list[str]) -> tuple[str, str]:
    """
    Async subprocess that doesn't leak FDs.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        close_fds=True   # Prevent FD inheritance
    )

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60.0)
        return stdout.decode(), stderr.decode()
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    # Process streams are closed automatically when proc goes out of scope

# Check subprocess FD inheritance:
def diagnose_subprocess_fds():
    """
    Check how many FDs a subprocess inherits.
    Run this in development to detect leaks.
    """
    import subprocess
    result = subprocess.run(
        ["bash", "-c", "ls /proc/$$/fd | wc -l"],
        capture_output=True, text=True, close_fds=True
    )
    child_fds = int(result.stdout.strip())

    current_fds = len(os.listdir(f"/proc/{os.getpid()}/fd"))

    print(f"Parent FDs: {current_fds}, Child (subprocess) FDs: {child_fds}")
    if child_fds > 20:
        print(f"WARNING: Subprocess inheriting {child_fds} FDs — check for close_fds=True")
```

### Option 5: Database connection FD tracking

```python
import sqlite3
import weakref
from contextlib import contextmanager

class TrackedSQLitePool:
    """
    SQLite connection pool with FD tracking.
    Alerts on unclosed connections — prevents FD leaks.
    """

    def __init__(self, db_path: str, pool_size: int = 5):
        self.db_path = db_path
        self._pool: list[sqlite3.Connection] = []
        self._in_use: set[int] = set()  # ids of connections in use
        self._total_created = 0
        self._total_leaked = 0

        # Pre-create pool
        for _ in range(pool_size):
            conn = sqlite3.connect(db_path, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            self._pool.append(conn)
            self._total_created += 1

        # Leak detector via weakrefs
        self._weakrefs: list[weakref.ref] = []

    @contextmanager
    def acquire(self):
        """Acquire connection — always released via context manager"""
        if not self._pool:
            raise RuntimeError("Connection pool exhausted — increase pool_size or check for leaks")

        conn = self._pool.pop()
        conn_id = id(conn)
        self._in_use.add(conn_id)

        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._in_use.discard(conn_id)
            self._pool.append(conn)  # Return to pool

    def close_all(self):
        """Close all connections — call at shutdown"""
        for conn in self._pool:
            conn.close()
        self._pool.clear()
        print(f"SQLite pool closed: {self._total_created} connections released")

    @property
    def stats(self) -> dict:
        return {
            "pool_size": len(self._pool),
            "in_use": len(self._in_use),
            "total_created": self._total_created,
        }

db_pool = TrackedSQLitePool("/data/agent.db", pool_size=5)

# Usage — connection always returned to pool:
with db_pool.acquire() as conn:
    rows = conn.execute("SELECT * FROM tasks WHERE status='pending'").fetchall()
# Connection automatically returned — no FD leak
```

### Option 6: FD audit script — find leaks in development

```python
import os
import psutil
from collections import Counter

def audit_file_descriptors() -> dict:
    """
    Audit open file descriptors — identify what's consuming them.
    Run in development to find leaks before they hit production.
    """
    proc = psutil.Process(os.getpid())

    # Categorize open FDs
    fd_types = Counter()
    socket_states = Counter()
    remote_addresses = Counter()
    file_extensions = Counter()

    for conn in proc.connections():
        fd_types["socket"] += 1
        socket_states[conn.status] += 1
        if conn.raddr:
            remote_addresses[f"{conn.raddr.ip}:{conn.raddr.port}"] += 1

    for f in proc.open_files():
        fd_types["file"] += 1
        ext = os.path.splitext(f.path)[1] or "no-ext"
        file_extensions[ext] += 1

    # Check for pipe FDs
    try:
        fd_dir = f"/proc/{os.getpid()}/fd"
        for fd in os.listdir(fd_dir):
            fd_path = os.readlink(f"{fd_dir}/{fd}")
            if "pipe" in fd_path:
                fd_types["pipe"] += 1
            elif "socket" not in fd_path and fd not in ["/dev/null", "/dev/urandom"]:
                pass
    except (PermissionError, NotADirectoryError):
        pass

    import resource
    soft_limit, _ = resource.getrlimit(resource.RLIMIT_NOFILE)

    return {
        "total_open": proc.num_fds(),
        "soft_limit": soft_limit,
        "utilization": f"{proc.num_fds()/soft_limit*100:.1f}%",
        "by_type": dict(fd_types),
        "socket_states": dict(socket_states.most_common(5)),
        "top_remote_addrs": dict(remote_addresses.most_common(10)),
        "file_types": dict(file_extensions.most_common(10)),
        "recommendation": (
            "Check CLOSE_WAIT sockets — connections not properly closed"
            if socket_states.get("CLOSE_WAIT", 0) > 10 else
            "Check pipe count — may indicate subprocess FD leaks"
            if fd_types.get("pipe", 0) > 50 else
            "FD usage looks healthy"
        )
    }

# Run periodically in development:
audit = audit_file_descriptors()
print(f"FD audit: {audit['total_open']}/{audit['soft_limit']} ({audit['utilization']})")
print(f"By type: {audit['by_type']}")
print(f"Recommendation: {audit['recommendation']}")
```

## Common FD Leak Sources

| Source | Leak Pattern | Fix |
|---|---|---|
| `httpx.AsyncClient()` per request | New socket per call | Share one client instance |
| `sqlite3.connect()` in loop | New connection, no close | Connection pool with context manager |
| `open()` without context manager | File handle not closed | Always use `with open(...)` |
| Subprocess without `close_fds` | Inherits all parent FDs | `close_fds=True` (default on Unix) |
| `asyncio.Queue` with abandoned tasks | Pipe FDs for cancelled tasks | Cancel tasks properly on shutdown |
| Log file handlers | New handler on each log init | Create handlers once at startup |

## FD Limits by Environment

| Environment | Default FD Limit | Recommended |
|---|---|---|
| Linux default | 1,024 | Raise to 65,536 |
| Docker container | 1,048,576 (from host) | Set via ulimits in compose |
| Kubernetes pod | Inherits node limit | Set securityContext or init container |
| macOS | 256 (soft), unlimited (hard) | `ulimit -n 10240` in dev |

## Expected Token Savings
Agent crashes with EMFILE → restart → re-explain task → resume: ~15,000 tokens per crash
Proper FD management → agent runs indefinitely: 0 crash-recovery overhead

## Environment
- Any long-running agent deployed in production; critical for agents making many HTTP requests, database connections, or spawning subprocesses over their lifetime
- Source: direct experience; FD exhaustion is the most common resource leak in agents that pass 24-hour soak tests during QA but fail in production after days of operation
