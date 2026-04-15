---
layout: solution
title: "Docker Volume Fills Disk — Agent Crashes Mid-Task"
category: docker
description: "Agent writes logs, output files, or intermediate data to a Docker volume that has no size limit. Volume fills the host disk. Agent crashes with 'No space left on device'. All in-progress work is lost."
tags: [docker, volume, disk, storage, logs, cleanup, limits, crash]
---

# Docker Volume Fills Disk — Agent Crashes Mid-Task

## Symptom
- `OSError: [Errno 28] No space left on device` mid-task
- Agent crashes after running for hours — all progress lost
- `df -h` shows host disk at 100% from Docker volume data
- Log files grow unbounded: 50GB log file for a 2-hour agent run
- Docker overlay2 directory consuming all available disk space
- `docker system df` shows volumes consuming hundreds of gigabytes

## Root Cause
Docker volumes are unbounded by default. Agents that write logs, intermediate files, or accumulated output without rotation/cleanup will fill the disk over time. This is especially common with: verbose logging at DEBUG level, agents that save every intermediate result, containerized agents running long multi-day tasks.

## Fix

### Option 1: Configure Docker logging limits

```yaml
# docker-compose.yml — set log limits for all services
services:
  agent:
    image: my-agent:latest
    logging:
      driver: "json-file"
      options:
        max-size: "100m"    # Max 100MB per log file
        max-file: "5"       # Keep 5 rotated files = 500MB max
    volumes:
      - agent-output:/app/output
    deploy:
      resources:
        limits:
          memory: 4g

volumes:
  agent-output:
    driver: local
    driver_opts:
      type: none
      o: bind
      device: /data/agent-output  # Explicit path on a large partition
```

```bash
# Or set globally in Docker daemon config:
# /etc/docker/daemon.json
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "100m",
    "max-file": "3"
  }
}
# Then: systemctl restart docker
```

### Option 2: Rotating log handler in agent code

```python
import logging
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
import os

def setup_rotating_logger(
    log_path: str,
    max_bytes: int = 100 * 1024 * 1024,  # 100MB
    backup_count: int = 5
) -> logging.Logger:
    """
    Set up rotating log handler — never exceeds max_bytes × backup_count.
    """
    logger = logging.getLogger("agent")
    logger.setLevel(logging.DEBUG)

    # Size-based rotation: 100MB × 5 files = 500MB max
    handler = RotatingFileHandler(
        log_path,
        maxBytes=max_bytes,
        backupCount=backup_count
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    ))
    logger.addHandler(handler)

    # Also log to console (no disk impact)
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)  # Less verbose to console
    logger.addHandler(console)

    return logger

logger = setup_rotating_logger("/app/logs/agent.log")
# → agent.log (active), agent.log.1, .2, .3, .4 — max 500MB total

# Time-based alternative: rotate daily, keep 7 days
def setup_daily_rotating_logger(log_path: str) -> logging.Logger:
    logger = logging.getLogger("agent")
    handler = TimedRotatingFileHandler(
        log_path,
        when="midnight",
        backupCount=7  # Keep 7 days
    )
    logger.addHandler(handler)
    return logger
```

### Option 3: Monitor disk usage and pause before overflow

```python
import shutil
import os
import asyncio

class DiskGuard:
    """
    Monitor available disk space and pause agent when running low.
    Prevents crash by giving operator time to intervene.
    """

    def __init__(
        self,
        path: str = "/",
        warning_threshold_gb: float = 5.0,
        critical_threshold_gb: float = 1.0,
        check_interval_seconds: float = 60.0
    ):
        self.path = path
        self.warning_gb = warning_threshold_gb
        self.critical_gb = critical_threshold_gb
        self.check_interval = check_interval_seconds
        self._paused = False

    def available_gb(self) -> float:
        stat = shutil.disk_usage(self.path)
        return stat.free / (1024 ** 3)

    def check(self) -> str:
        available = self.available_gb()
        total = shutil.disk_usage(self.path).total / (1024 ** 3)

        if available < self.critical_gb:
            return "critical"
        elif available < self.warning_gb:
            return "warning"
        return "ok"

    async def monitor(self, on_critical=None):
        """Run as background task — monitors disk continuously"""
        while True:
            status = self.check()
            available = self.available_gb()

            if status == "critical":
                print(f"CRITICAL: Only {available:.1f}GB free. Pausing agent.")
                self._paused = True
                if on_critical:
                    await on_critical(available)
                # Wait for space to be freed before resuming
                while self.available_gb() < self.warning_gb:
                    await asyncio.sleep(30)
                self._paused = False
                print("Disk space recovered — resuming.")

            elif status == "warning":
                print(f"WARNING: Only {available:.1f}GB free. Clean up soon.")

            await asyncio.sleep(self.check_interval)

    async def checkpoint(self):
        """Call before each major write — blocks if disk is critical"""
        if self._paused:
            print("Disk guard: waiting for free space...")
            while self._paused:
                await asyncio.sleep(5)

guard = DiskGuard(path="/app/output", warning_threshold_gb=10.0, critical_threshold_gb=2.0)
asyncio.create_task(guard.monitor())  # Background monitoring
```

### Option 4: Automatic cleanup of intermediate files

```python
import os
import time
from pathlib import Path

class IntermediateFileManager:
    """
    Track temporary files created during a task.
    Clean them up automatically on completion or after TTL.
    """

    def __init__(self, base_dir: str, ttl_hours: float = 24.0):
        self.base_dir = Path(base_dir)
        self.ttl_seconds = ttl_hours * 3600
        self._tracked: list[Path] = []

    def create_temp_file(self, name: str, content: bytes = None) -> Path:
        """Create a tracked temp file — will be cleaned up automatically"""
        path = self.base_dir / f"tmp_{int(time.time())}_{name}"
        if content:
            path.write_bytes(content)
        self._tracked.append(path)
        return path

    def cleanup(self, force: bool = False):
        """Remove tracked files and any expired files in base_dir"""
        cleaned = 0
        cleaned_bytes = 0

        # Clean tracked files
        for path in self._tracked:
            if path.exists():
                size = path.stat().st_size
                path.unlink()
                cleaned += 1
                cleaned_bytes += size
        self._tracked.clear()

        # Also clean expired files in base_dir
        if self.base_dir.exists():
            cutoff = time.time() - self.ttl_seconds
            for path in self.base_dir.glob("tmp_*"):
                if force or path.stat().st_mtime < cutoff:
                    size = path.stat().st_size
                    path.unlink()
                    cleaned += 1
                    cleaned_bytes += size

        if cleaned:
            print(f"Cleaned {cleaned} files ({cleaned_bytes / 1024**2:.1f} MB)")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.cleanup()

# Usage — cleanup guaranteed even on exception:
with IntermediateFileManager("/app/tmp") as fm:
    chunks = fm.create_temp_file("chunks.pkl")
    cache = fm.create_temp_file("cache.json")
    run_agent_task(chunks, cache)
# → Files deleted on exit
```

### Option 5: Limit output directory size with cleanup policy

```python
import shutil
from pathlib import Path
import time

def enforce_output_dir_size_limit(
    output_dir: Path,
    max_size_gb: float = 20.0,
    keep_newest_n: int = 100,
    dry_run: bool = False
) -> dict:
    """
    Enforce a size limit on the output directory by deleting oldest files.
    """
    if not output_dir.exists():
        return {"deleted": 0, "freed_gb": 0}

    max_bytes = int(max_size_gb * 1024 ** 3)

    # Get all files sorted by modification time (oldest first)
    files = sorted(
        output_dir.rglob("*"),
        key=lambda p: p.stat().st_mtime if p.is_file() else 0
    )
    files = [f for f in files if f.is_file()]

    # Calculate current size
    total_size = sum(f.stat().st_size for f in files)

    deleted = 0
    freed = 0

    # Delete oldest files until under limit (but keep newest N)
    protected = set(files[-keep_newest_n:])
    for f in files:
        if total_size <= max_bytes:
            break
        if f in protected:
            continue
        size = f.stat().st_size
        if not dry_run:
            f.unlink()
        total_size -= size
        freed += size
        deleted += 1

    freed_gb = freed / 1024 ** 3
    if deleted:
        action = "Would delete" if dry_run else "Deleted"
        print(f"{action} {deleted} files, freed {freed_gb:.2f} GB")

    return {"deleted": deleted, "freed_gb": freed_gb}

# Run at agent startup and after each major task:
enforce_output_dir_size_limit(Path("/app/output"), max_size_gb=50.0)
```

### Option 6: Docker tmpfs for truly temporary data

```yaml
# docker-compose.yml — use tmpfs (RAM) for intermediate data
services:
  agent:
    image: my-agent:latest
    tmpfs:
      - /app/tmp:size=2g,mode=1777   # 2GB RAM-backed temp dir — auto-wiped
      - /app/cache:size=1g            # 1GB for cache — no disk impact
    volumes:
      - agent-output:/app/output      # Persistent output only
    environment:
      AGENT_TMP_DIR: /app/tmp
      AGENT_CACHE_DIR: /app/cache
      AGENT_OUTPUT_DIR: /app/output

# tmpfs advantages:
# - Auto-cleared on container restart
# - No disk I/O — faster reads/writes
# - Hard size limit — can't overflow disk
# - No cleanup needed
```

## Disk Usage Sources in Agent Containers

| Source | Risk | Fix |
|---|---|---|
| Container logs (json-file) | High | Set max-size/max-file in logging config |
| Debug log files | High | RotatingFileHandler, 100MB limit |
| Intermediate computation files | Medium | tmpfs or cleanup on completion |
| Accumulated output files | Medium | Size-limit policy, delete oldest |
| Docker overlay2 (image layers) | Low | `docker system prune` periodically |
| Downloaded model weights | Low | Store outside container on named volume |

## Expected Token Savings
Crash mid-task + restart + re-run: ~50,000 tokens + hours of lost compute
Disk monitoring pauses agent before crash: 0 lost work

## Environment
- Long-running containerized agents writing significant output or verbose logs
- Source: direct experience; disk-full crashes are one of the most disruptive agent failure modes in production
