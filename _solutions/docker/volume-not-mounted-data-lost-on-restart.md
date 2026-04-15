---
layout: solution
title: "Agent Data Lost on Container Restart — Docker Volume Not Mounted"
category: docker
description: "Agent writes files, logs, or session data inside the container. On restart, all data is gone. The container's filesystem is ephemeral — data must be in a mounted volume to persist."
tags: [docker, volume, persistence, data-loss, restart]
---

# Agent Data Lost on Container Restart — Docker Volume Not Mounted

## Symptom
- Agent writes files during session (logs, outputs, session state, .env files)
- Container restarts (crash, update, OOM kill, manual restart)
- All written files are gone — container starts fresh
- Agent session memory is lost despite the agent "saving" to disk
- Problem persists even with `restart: always` policy

## Root Cause
Docker containers have an ephemeral filesystem layer. Files written inside the container exist only in that layer and are lost when the container is removed or recreated. Only data in mounted volumes (bound to host filesystem or Docker named volumes) persists across restarts.

## Fix

### Option 1: Mount workspace as named volume

```yaml
# docker-compose.yml
services:
  agent:
    image: your-agent:latest
    volumes:
      - agent_workspace:/workspace    # Named volume — persists across restarts
      - agent_logs:/var/log/agent

volumes:
  agent_workspace:    # Docker manages this volume
  agent_logs:
```

Data in `/workspace` now survives container restarts and recreation.

### Option 2: Bind mount to host directory

```yaml
# docker-compose.yml
services:
  agent:
    volumes:
      - ./agent-data:/workspace       # Host directory ./agent-data → /workspace in container
      - ./logs:/var/log/agent

# Files in ./agent-data on host are directly accessible
# Survives ANY container operation including docker-compose down
```

### Option 3: Check what needs to persist

```dockerfile
# In Dockerfile — label directories that MUST be volumes
VOLUME /workspace          # Agent working directory
VOLUME /var/log/agent      # Logs
# Do NOT volume your app code — that should be immutable in the image
```

```bash
# Verify volumes are mounted before running
docker inspect <container_id> --format '{{.Mounts}}' | python3 -m json.tool
```

### Option 4: Agent startup check for persistent storage

```python
import os

WORKSPACE_MARKER = "/workspace/.persistent"
CONTAINER_MARKER = "/tmp/.container_only"

def verify_persistence():
    """Check if workspace is actually persisted"""
    if not os.path.exists(WORKSPACE_MARKER):
        # First run or no volume — write the marker
        os.makedirs("/workspace", exist_ok=True)
        open(WORKSPACE_MARKER, 'w').write("persistent")
        print("Warning: Workspace may not be mounted as a volume. Check docker-compose.yml")
    else:
        print("Workspace persistence verified")

    # Warn if agent is writing to paths that won't persist
    non_persistent_paths = ["/tmp", "/var/tmp", "/root"]
    for path in non_persistent_paths:
        if any(p.startswith(path) for p in agent.output_paths):
            print(f"Warning: Agent writing to non-persistent path: {path}")
```

### Option 5: Session state as external storage

For stateful agents, use external storage instead of container filesystem:

```python
import json, os

# BAD — ephemeral
def save_session(session_data):
    with open("/tmp/session.json", 'w') as f:
        json.dump(session_data, f)

# GOOD — external storage
import redis
r = redis.Redis(host=os.environ['REDIS_HOST'])

def save_session(session_id, session_data):
    r.setex(f"session:{session_id}", 86400, json.dumps(session_data))

def load_session(session_id):
    data = r.get(f"session:{session_id}")
    return json.loads(data) if data else None
```

## Persistent Path Quick Reference

| Path pattern | Persists? | Fix |
|-------------|-----------|-----|
| `/workspace/*` | Only if volume mounted | Add volume mount |
| `/tmp/*` | Never | Use /workspace instead |
| Named volume | Yes | Default if configured |
| Bind mount | Yes | Links to host directory |
| `/app/*` (from image) | Read-only | Don't write here |

## Expected Token Savings
Debugging missing files after restart: ~8,000 tokens
Lost session state + recovery: 20,000–100,000 tokens (task-dependent)

## Environment
- Any stateful agent in Docker
- Source: direct experience, Docker storage documentation
