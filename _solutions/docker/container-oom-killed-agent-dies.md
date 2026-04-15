---
layout: solution
title: "Docker Container OOM Killed — Agent Process Silently Terminated"
category: docker
description: "Docker container runs out of memory and the kernel OOM killer terminates the agent process. Container restarts but agent loses all in-progress work. No application-level error logged."
tags: [docker, oom, memory-limit, oom-killer, container]
---

# Docker Container OOM Killed — Agent Process Silently Terminated

## Symptom
- Agent suddenly stops responding with no application error
- Container restarts automatically (if restart policy set)
- `docker logs` shows no application error — just stops mid-line
- `docker inspect <container>` shows `OOMKilled: true`
- Happens more frequently under load or with large context windows
- All in-progress work is lost

## Root Cause
The Docker container's memory limit was exceeded. The Linux OOM (Out of Memory) killer terminates processes to free memory — it picks the largest memory consumer, which is usually the agent process. No application-level handler can catch this; it's a kernel-level SIGKILL.

## Diagnosis

```bash
# Check if container was OOM killed
docker inspect <container_name> --format '{{.State.OOMKilled}}'
# → true

# Check container exit code (137 = killed by signal)
docker inspect <container_name> --format '{{.State.ExitCode}}'
# → 137 (128 + 9 for SIGKILL)

# Check system OOM log
dmesg | grep -i "oom\|killed process"
# → [timestamp] Out of memory: Kill process 12345 (node) score 850 ...

# Monitor memory usage live
docker stats <container_name>
```

## Fix

### Option 1: Increase container memory limit

```yaml
# docker-compose.yml
services:
  agent:
    mem_limit: 4g          # Was 512m — increase to what the agent actually needs
    memswap_limit: 4g      # Disable swap (set equal to mem_limit)
    oom_kill_disable: false # Keep enabled — disabling OOM kill can freeze the host
```

### Option 2: Profile actual memory usage first

```bash
# Run without limit, measure peak usage
docker run --memory-swap -1 your-agent:latest &
docker stats --no-stream  # Note peak memory

# Set limit to peak × 1.5 (safety margin)
```

### Option 3: Add memory monitoring + graceful degradation

```python
import resource, os

def get_memory_usage_mb():
    """Get current process memory usage in MB"""
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_maxrss / 1024  # Linux returns KB

MEMORY_WARNING_MB = 3000  # Warn at 3GB
MEMORY_LIMIT_MB = 3500    # Start shedding load at 3.5GB

async def check_memory_before_large_op():
    mem = get_memory_usage_mb()
    if mem > MEMORY_LIMIT_MB:
        raise MemoryError(f"Memory limit reached ({mem:.0f}MB) — refusing large operation")
    if mem > MEMORY_WARNING_MB:
        print(f"Warning: High memory usage {mem:.0f}MB")
```

### Option 4: Checkpoint work before OOM can occur

```python
import signal

class OOMProtectedAgent:
    def __init__(self, checkpoint_path):
        self.checkpoint_path = checkpoint_path
        # Save state before potential OOM on SIGUSR1 or periodic
        signal.signal(signal.SIGUSR1, self._emergency_checkpoint)

    def _emergency_checkpoint(self, signum, frame):
        """Called manually or via monitoring when memory is high"""
        self.save_checkpoint()

    async def run_task(self, task):
        try:
            for step in self.plan_steps(task):
                result = await self.execute_step(step)
                self.save_checkpoint()  # Checkpoint after each step
                yield result
        except MemoryError:
            # Re-raise after checkpoint is saved
            self.save_checkpoint()
            raise
```

### Option 5: Set memory limits per operation type

```yaml
# docker-compose.yml
services:
  agent:
    mem_limit: 2g
  agent-large-context:
    image: your-agent:latest
    mem_limit: 8g          # Separate service for memory-intensive work
    profiles: ["large"]    # Only start when needed
```

## Prevention Checklist
- [ ] `docker inspect` checked for OOMKilled after any unexplained restarts
- [ ] Memory limit set based on profiled peak usage × 1.5
- [ ] Agent checkpoints work periodically (not only on success)
- [ ] Memory monitoring alerts configured before OOM limit

## Expected Token Savings
Restarting lost work after OOM kill: 10,000–100,000 tokens (task-dependent)
Proper memory limits + checkpointing: prevents loss entirely

## Environment
- Docker deployments of AI agents
- Most common: agents with large context windows or file processing
- Source: direct experience
