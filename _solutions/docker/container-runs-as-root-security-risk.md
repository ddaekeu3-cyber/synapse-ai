---
layout: solution
title: "Agent Container Runs as Root — Security Risk and Permission Problems"
category: docker
description: "Agent runs as root inside Docker container. If compromised, attacker has full container access. File outputs are owned by root, causing permission errors on the host. Volume mounts behave differently than expected."
tags: [docker, root, security, permissions, user, non-root, container]
---

# Agent Container Runs as Root — Security Risk and Permission Problems

## Symptom
- Output files created by agent are owned by `root:root` on the host
- Host user cannot delete or edit agent output files without `sudo`
- Security audit flags container running as UID 0
- If agent runs untrusted code, compromise gives root-level container access
- `docker exec` into container shows `whoami` → `root`
- Volume-mounted files have wrong ownership after agent writes to them

## Root Cause
Docker containers run as root by default unless explicitly configured otherwise. Most base images (python, node, ubuntu) have no non-root user set up, so `RUN`, `CMD`, and `ENTRYPOINT` all run as UID 0. This is a security risk and causes host filesystem permission issues when using bind mounts.

## Fix

### Option 1: Add non-root user in Dockerfile

```dockerfile
FROM python:3.12-slim

# Install dependencies as root (needed for apt, pip installs to system paths)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Create non-root user and group
RUN groupadd --gid 1000 agent && \
    useradd --uid 1000 --gid agent --shell /bin/bash --create-home agent

# Set working directory and give ownership to agent user
WORKDIR /app
COPY --chown=agent:agent . .

# Create output directory with correct ownership
RUN mkdir -p /app/output && chown agent:agent /app/output

# Switch to non-root user for runtime
USER agent

CMD ["python", "agent.py"]
```

### Option 2: Match container UID to host user (prevents permission issues on mounts)

```dockerfile
FROM python:3.12-slim

RUN pip install --no-cache-dir -r requirements.txt

# Use build args to match host user UID/GID
ARG HOST_UID=1000
ARG HOST_GID=1000

RUN groupadd --gid ${HOST_GID} agent && \
    useradd --uid ${HOST_UID} --gid ${HOST_GID} --create-home agent

WORKDIR /app
COPY --chown=${HOST_UID}:${HOST_GID} . .

USER agent
CMD ["python", "agent.py"]
```

```bash
# Build with your actual host UID/GID — files written in container
# will be owned by your user on the host
docker build \
  --build-arg HOST_UID=$(id -u) \
  --build-arg HOST_GID=$(id -g) \
  -t my-agent .

# Files written to /app/output will be owned by your host user
docker run -v $(pwd)/output:/app/output my-agent
```

### Option 3: docker-compose with user specification

```yaml
# docker-compose.yml
services:
  agent:
    build: .
    # Override user at runtime — matches host user to avoid permission issues
    user: "${UID}:${GID}"
    volumes:
      - ./output:/app/output
      - ./config:/app/config:ro  # Read-only config mount
    environment:
      - AGENT_OUTPUT_DIR=/app/output
    # Ensure the container has no extra capabilities
    cap_drop:
      - ALL
    cap_add:
      - NET_BIND_SERVICE  # Only if agent needs to bind to ports < 1024
    security_opt:
      - no-new-privileges:true  # Prevent privilege escalation
    read_only: true  # Make root filesystem read-only
    tmpfs:
      - /tmp  # Still allow writes to /tmp
```

```bash
# Set UID/GID from host before running
export UID=$(id -u)
export GID=$(id -g)
docker compose up
```

### Option 4: Runtime user override without Dockerfile changes

```bash
# Run existing image as non-root without modifying Dockerfile
docker run \
  --user 1000:1000 \
  --volume $(pwd)/output:/app/output \
  my-agent

# Verify non-root execution
docker run --rm --user 1000:1000 my-agent whoami
# → Should print username or "I have no name!" (harmless — UID 1000 is used)
```

### Option 5: Security hardening for agent containers

```dockerfile
FROM python:3.12-slim AS base

# Minimal attack surface: no shell, no package manager in final image
FROM base AS builder
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.12-slim AS final

# Copy only installed packages, not build tools
COPY --from=builder /install /usr/local

# Create non-root user
RUN useradd --uid 1000 --create-home agent

# Copy app with correct ownership
WORKDIR /app
COPY --chown=agent:agent src/ ./src/

# Drop all Linux capabilities
USER agent

# Verify we're non-root at build time (fails build if still root)
RUN test "$(id -u)" != "0" || (echo "ERROR: Running as root!" && exit 1)

ENTRYPOINT ["python", "src/agent.py"]
```

### Option 6: Fix output file ownership when you can't change the image

```bash
#!/bin/bash
# Wrapper script: run agent, then fix ownership of outputs

CONTAINER_NAME="agent-run-$(date +%s)"
OUTPUT_DIR="./output"

mkdir -p "$OUTPUT_DIR"

# Run agent container
docker run \
  --name "$CONTAINER_NAME" \
  --volume "$(pwd)/$OUTPUT_DIR:/app/output" \
  my-agent

EXIT_CODE=$?

# Fix ownership of any files created as root
if [ -d "$OUTPUT_DIR" ]; then
  docker run --rm \
    --volume "$(pwd)/$OUTPUT_DIR:/fix" \
    --user root \
    alpine sh -c "chown -R $(id -u):$(id -g) /fix"
  echo "Fixed ownership of $OUTPUT_DIR"
fi

exit $EXIT_CODE
```

```python
# Python equivalent: fix file ownership after agent run
import os
import subprocess
from pathlib import Path

def fix_output_ownership(output_dir: Path):
    """Fix root-owned files in output directory after container run"""
    uid = os.getuid()
    gid = os.getgid()

    for path in output_dir.rglob("*"):
        if path.stat().st_uid == 0:  # Root-owned
            try:
                os.chown(path, uid, gid)
                print(f"Fixed ownership: {path}")
            except PermissionError:
                subprocess.run(["sudo", "chown", f"{uid}:{gid}", str(path)])
```

## Root vs Non-Root Comparison

| Concern | Running as root | Running as non-root |
|---|---|---|
| Security risk | High — full container UID 0 | Low — limited blast radius |
| Output file ownership | Files owned by root on host | Files owned by host user |
| `sudo` needed to delete | Yes | No |
| Privilege escalation | Possible | Blocked with `no-new-privileges` |
| Compliance (SOC2, etc.) | Fails most audits | Passes |
| Mount volume writes | Works but messy | Works cleanly with UID match |

## Expected Token Savings
Debugging permission errors caused by root-owned output files: ~5,000 tokens
Non-root user + UID match prevents all of these at build time: 0 wasted

## Environment
- Any agent containerized with Docker; critical when using bind-mounted output directories
- Source: direct experience with agent containers in production; security audits consistently flag root containers
