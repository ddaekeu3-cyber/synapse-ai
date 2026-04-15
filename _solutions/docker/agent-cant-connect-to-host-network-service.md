---
layout: solution
title: "Dockerized Agent Can't Connect to Service Running on Host — Connection Refused"
category: docker
description: "Agent running in Docker container tries to connect to localhost:5432 (or any port). Gets 'Connection refused' because localhost inside Docker is the container itself, not the host machine."
tags: [docker, networking, localhost, host-network, connection-refused]
---

# Dockerized Agent Can't Connect to Service Running on Host — Connection Refused

## Symptom
- `psycopg2.OperationalError: could not connect to server: Connection refused` (port 5432)
- `redis.exceptions.ConnectionError: Error 111 connecting to localhost:6379`
- Agent works fine locally but fails when run in Docker
- `curl localhost:8080` inside container returns connection refused
- Service is definitely running and accessible from the host machine

## Root Cause
Inside a Docker container, `localhost` refers to the container's own network namespace — not the host machine. A service running on the host machine's `localhost:5432` is not accessible at `localhost:5432` from within a container.

## Fix

### Option 1: Use host.docker.internal (Mac/Windows)

```python
# WRONG — resolves to container itself
DATABASE_URL = "postgresql://user:pass@localhost:5432/db"

# RIGHT — resolves to host machine on Mac and Windows Docker Desktop
DATABASE_URL = "postgresql://user:pass@host.docker.internal:5432/db"
```

```yaml
# docker-compose.yml — add extra_hosts for Linux compatibility
services:
  agent:
    image: my-agent
    extra_hosts:
      - "host.docker.internal:host-gateway"  # Linux: map to actual host IP
    environment:
      - DATABASE_URL=postgresql://user:pass@host.docker.internal:5432/db
```

### Option 2: Use host network mode (Linux)

```bash
# Run container with host networking — no network isolation
docker run --network=host my-agent

# Now localhost inside container = host machine localhost
# Note: --network=host doesn't work on Mac/Windows Docker Desktop
```

```yaml
# docker-compose.yml
services:
  agent:
    image: my-agent
    network_mode: host  # Linux only
```

### Option 3: Move the service into Docker Compose (recommended)

```yaml
# docker-compose.yml — run everything together
services:
  agent:
    build: .
    environment:
      - DATABASE_URL=postgresql://postgres:password@postgres:5432/agentdb
      - REDIS_URL=redis://redis:6379
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy

  postgres:
    image: postgres:16
    environment:
      POSTGRES_PASSWORD: password
      POSTGRES_DB: agentdb
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
      interval: 5s
      timeout: 5s
      retries: 5

  redis:
    image: redis:7-alpine
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
```

Inside the `agent` container, `postgres` and `redis` are the hostnames — Docker Compose creates a shared network automatically.

### Option 4: Use the host machine's actual IP

```bash
# Find host IP from inside container
docker run --rm alpine ip route | grep default | awk '{print $3}'
# e.g., 172.17.0.1

# Or get host IP on the host machine
hostname -I | awk '{print $1}'
# e.g., 192.168.1.100
```

```python
import os, socket

def get_host_ip() -> str:
    """Get host machine IP from inside Docker container"""
    # Try host.docker.internal first (Mac/Windows)
    try:
        return socket.gethostbyname("host.docker.internal")
    except socket.gaierror:
        pass

    # Fall back to default gateway IP (Linux)
    import subprocess
    result = subprocess.run(
        ["ip", "route", "show", "default"],
        capture_output=True, text=True
    )
    # "default via 172.17.0.1 dev eth0"
    parts = result.stdout.split()
    if "via" in parts:
        return parts[parts.index("via") + 1]

    return os.environ.get("HOST_IP", "localhost")

DATABASE_HOST = get_host_ip()
```

### Option 5: Environment variable for flexibility

```python
import os

# Let the environment dictate the host
DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_PORT = int(os.environ.get("DB_PORT", "5432"))

# When running locally: DB_HOST=localhost (default)
# When running in Docker: DB_HOST=host.docker.internal
# When in Docker Compose: DB_HOST=postgres (service name)
```

```bash
# Local development
python agent.py

# Docker run
docker run -e DB_HOST=host.docker.internal my-agent

# Docker Compose
# Set DB_HOST=postgres in docker-compose.yml environment
```

## Connectivity Quick Reference

| Where agent runs | Where service runs | Use this host |
|---|---|---|
| Local (no Docker) | Local | `localhost` |
| Docker container | Same Docker Compose | Service name (e.g., `postgres`) |
| Docker container | Host machine (Mac/Win) | `host.docker.internal` |
| Docker container | Host machine (Linux) | `172.17.0.1` or `--network=host` |
| Docker container | Another container | Container name or shared network |
| Docker container | Remote server | IP address or hostname |

## Diagnosis

```bash
# Test connectivity from inside a container
docker run --rm alpine sh -c "
  apk add --no-cache curl netcat-openbsd &&
  nc -zv host.docker.internal 5432 && echo 'OK' || echo 'FAILED'
"

# Check what localhost resolves to inside container
docker exec -it my-container-name getent hosts localhost
# Should show 127.0.0.1 (container loopback, not host)
```

## Expected Token Savings
Debugging Docker networking confusion: ~6,000 tokens
Knowing host.docker.internal upfront: prevents the issue entirely

## Environment
- Dockerized agents connecting to host-side services (databases, message queues, APIs)
- Source: direct experience, Docker networking documentation
