---
layout: solution
title: "Docker Compose depends_on Doesn't Wait for Service Ready — Race Condition at Startup"
category: docker
description: "docker-compose depends_on only waits for container to start, not for the service inside to be ready. Agent container starts before database is accepting connections. Connection refused on startup."
tags: [docker-compose, depends_on, healthcheck, race-condition, startup, postgres, redis]
---

# Docker Compose depends_on Doesn't Wait for Service Ready — Race Condition at Startup

## Symptom
- Agent container fails immediately on startup: `Connection refused` to database
- `docker-compose up` starts all containers but agent fails before DB is ready
- Works if you start database first, wait 10 seconds, then start agent
- `depends_on: postgres` in docker-compose.yml doesn't help
- Fails intermittently — sometimes works, sometimes doesn't (timing-dependent)

## Root Cause
`depends_on` only waits for the *container* to start, not for the *service* inside to be ready to accept connections. PostgreSQL takes 2–5 seconds to initialize after the container starts. Redis takes 1–2 seconds. The agent tries to connect immediately and fails.

## Fix

### Option 1: Add healthchecks to services + condition in depends_on

```yaml
# docker-compose.yml
services:
  agent:
    build: .
    depends_on:
      postgres:
        condition: service_healthy  # Wait for healthcheck to pass
      redis:
        condition: service_healthy
    environment:
      - DATABASE_URL=postgresql://user:pass@postgres:5432/agentdb
      - REDIS_URL=redis://redis:6379

  postgres:
    image: postgres:16
    environment:
      POSTGRES_PASSWORD: password
      POSTGRES_DB: agentdb
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres -d agentdb"]
      interval: 5s      # Check every 5 seconds
      timeout: 5s       # Fail if check takes more than 5s
      retries: 10       # Allow 10 failures before marking unhealthy
      start_period: 10s # Don't start checking for 10s after container starts

  redis:
    image: redis:7-alpine
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 5
      start_period: 5s

  mongodb:
    image: mongo:7
    healthcheck:
      test: ["CMD", "mongosh", "--eval", "db.adminCommand('ping')"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 15s
```

### Option 2: Wait-for-it script in entrypoint

```dockerfile
# Dockerfile
FROM python:3.12-slim

# Add wait-for-it utility
ADD https://raw.githubusercontent.com/vishnubob/wait-for-it/master/wait-for-it.sh /wait-for-it.sh
RUN chmod +x /wait-for-it.sh

COPY . /app
WORKDIR /app
RUN pip install -r requirements.txt

# Wait for postgres to be ready, then start agent
ENTRYPOINT ["/wait-for-it.sh", "postgres:5432", "--timeout=60", "--", "python", "agent.py"]
```

```yaml
# Or in docker-compose command
services:
  agent:
    image: my-agent
    command: >
      sh -c "
        until nc -z postgres 5432; do
          echo 'Waiting for postgres...'
          sleep 2
        done
        echo 'Postgres is ready!'
        python agent.py
      "
```

### Option 3: Retry logic in the agent itself (resilient startup)

```python
import asyncpg, asyncio, os, time

async def connect_with_retry(dsn: str, max_attempts: int = 30, delay: float = 2.0):
    """Connect to database, retrying until successful or max_attempts exceeded"""
    for attempt in range(1, max_attempts + 1):
        try:
            conn = await asyncpg.connect(dsn)
            print(f"Database connected on attempt {attempt}")
            return conn
        except (asyncpg.CannotConnectNowError, ConnectionRefusedError, OSError) as e:
            if attempt == max_attempts:
                raise RuntimeError(f"Could not connect after {max_attempts} attempts: {e}")
            print(f"Attempt {attempt}/{max_attempts}: {e}. Retrying in {delay}s...")
            await asyncio.sleep(delay)

async def main():
    db = await connect_with_retry(os.environ["DATABASE_URL"])
    # Now safe to start agent
    await run_agent(db)
```

### Option 4: Healthcheck for your own agent

```python
from fastapi import FastAPI
import asyncpg, redis.asyncio as redis

app = FastAPI()

@app.get("/health")
async def health():
    """Liveness + readiness probe"""
    checks = {}

    # Check database
    try:
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        await conn.fetchval("SELECT 1")
        await conn.close()
        checks["database"] = "ok"
    except Exception as e:
        checks["database"] = f"error: {e}"

    # Check Redis
    try:
        r = redis.from_url(os.environ["REDIS_URL"])
        await r.ping()
        await r.aclose()
        checks["redis"] = "ok"
    except Exception as e:
        checks["redis"] = f"error: {e}"

    all_ok = all(v == "ok" for v in checks.values())
    status_code = 200 if all_ok else 503
    return {"status": "healthy" if all_ok else "unhealthy", "checks": checks}
```

```yaml
# Use your agent's own healthcheck in docker-compose
services:
  agent:
    build: .
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8080/health"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 30s
```

### Option 5: Kubernetes equivalent — readinessProbe

```yaml
# k8s deployment.yaml
spec:
  containers:
  - name: agent
    image: my-agent
    readinessProbe:
      httpGet:
        path: /health
        port: 8080
      initialDelaySeconds: 10
      periodSeconds: 5
      failureThreshold: 10  # 50 seconds total
    livenessProbe:
      httpGet:
        path: /health
        port: 8080
      initialDelaySeconds: 30
      periodSeconds: 10
```

## Healthcheck Commands by Service

| Service | Healthcheck command |
|---|---|
| PostgreSQL | `pg_isready -U $POSTGRES_USER -d $POSTGRES_DB` |
| MySQL | `mysqladmin ping -h localhost` |
| Redis | `redis-cli ping` |
| MongoDB | `mongosh --eval "db.adminCommand('ping')"` |
| Elasticsearch | `curl -f http://localhost:9200/_health` |
| RabbitMQ | `rabbitmq-diagnostics -q ping` |
| Kafka | `kafka-topics.sh --bootstrap-server localhost:9092 --list` |

## Expected Token Savings
Debugging intermittent startup race condition: ~6,000 tokens
Healthcheck + condition: prevents the race entirely

## Environment
- Any Docker Compose setup with multiple services that depend on each other
- Source: direct experience; depends_on confusion is one of the most common Docker mistakes
