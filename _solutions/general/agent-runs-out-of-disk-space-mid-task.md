---
layout: solution
title: "Agent Runs Out of Disk Space Mid-Task — ENOSPC or No Space Left on Device"
category: general
description: "Agent fails mid-task with ENOSPC or 'No space left on device'. Happens when agent downloads large files, generates output, or writes logs in a constrained environment."
tags: [disk-space, enospc, storage, docker, cleanup]
---

# Agent Runs Out of Disk Space Mid-Task — ENOSPC or No Space Left on Device

## Symptom
- Error: `ENOSPC: no space left on device`
- Error: `OSError: [Errno 28] No space left on device`
- File write fails mid-task, corrupting output
- `git clone` or `npm install` fails partway through
- Docker container builds fail with storage errors
- Agent was working fine then suddenly fails

## Root Cause
Disk space exhausted. Common causes in agent environments:
1. Large file downloads (models, datasets, npm dependencies)
2. Log files growing unbounded
3. Docker image layers accumulating
4. Temporary files not cleaned up after tool calls
5. Container /tmp is full

## Fix

### Option 1: Check disk space before large operations

```python
import shutil, os

def check_disk_space(path="/", required_gb=2.0):
    """Verify sufficient disk space before large operation"""
    usage = shutil.disk_usage(path)
    available_gb = usage.free / (1024 ** 3)

    if available_gb < required_gb:
        raise DiskSpaceError(
            f"Insufficient disk space: {available_gb:.1f}GB available, "
            f"{required_gb}GB required. Free up space before proceeding."
        )

    return available_gb

# Call before large downloads or operations
check_disk_space(required_gb=5.0)
download_large_file(url)
```

### Option 2: Clean up temporary files after tool use

```python
import tempfile, contextlib, os

@contextlib.contextmanager
def temp_workspace():
    """Create temporary directory that's cleaned up automatically"""
    tmpdir = tempfile.mkdtemp(prefix="agent_work_")
    try:
        yield tmpdir
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
        print(f"Cleaned up temp workspace {tmpdir}")

# Usage
with temp_workspace() as work_dir:
    download_file(url, dest=work_dir)
    process_file(os.path.join(work_dir, "data.csv"))
# tmpdir automatically deleted after block
```

### Option 3: Docker — set storage limits

```yaml
# docker-compose.yml
services:
  agent:
    storage_opt:
      size: '20G'     # Limit container storage
    tmpfs:
      - /tmp:size=1G  # Tmpfs for /tmp (RAM-backed, cleaned on restart)
    volumes:
      - agent_data:/workspace  # Persistent volume for important data
```

```dockerfile
# Dockerfile — clean apt cache after install
RUN apt-get update && apt-get install -y \
    python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*  # Saves ~200MB
```

### Option 4: Monitor disk usage in agent loop

```bash
# Add to agent startup or periodic health check
df -h | awk '$5 > 80 {print "WARNING: " $6 " is " $5 " full"}'

# Clean Docker space
docker system prune -f          # Remove stopped containers
docker image prune -f           # Remove dangling images
docker volume prune -f          # Remove unused volumes
```

```python
import shutil

def disk_usage_check_hook(path="/"):
    usage = shutil.disk_usage(path)
    percent_used = (usage.used / usage.total) * 100
    if percent_used > 85:
        print(f"WARNING: Disk {percent_used:.0f}% full at {path}")
        cleanup_temp_files()
    if percent_used > 95:
        raise DiskSpaceError(f"Disk critically full ({percent_used:.0f}%) — stopping")
```

### Option 5: Identify space hogs

```bash
# Find largest directories
du -sh /* 2>/dev/null | sort -rh | head -20

# Find large files
find / -size +100M -type f 2>/dev/null | sort -k1 -rh | head -10

# Docker space usage
docker system df

# Log files
find /var/log -name "*.log" -size +50M -exec ls -lh {} \;
```

## Common Space Hogs in Agent Environments

| Source | Size | Cleanup |
|--------|------|---------|
| npm node_modules | 200MB–2GB | `rm -rf node_modules && npm ci` |
| Docker images | GBs | `docker image prune` |
| Agent logs | Can grow unbounded | Log rotation, size limit |
| pip cache | 500MB–5GB | `pip cache purge` |
| /tmp accumulated | Depends | `rm -rf /tmp/agent_*` |

## Expected Token Savings
Mid-task disk failure + restart: ~15,000 tokens (redoing work)
Pre-flight disk check: ~100 tokens

## Environment
- Containerized agents, cloud VMs, local development with limited disk
- Source: direct experience with Docker agent deployments
