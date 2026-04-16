---
layout: solution
title: "Agent Doesn't Implement Test Environment Isolation with Docker"
description: "How to use Docker and container orchestration to give every test run a fully isolated, reproducible environment — eliminating shared-state pollution between tests and 'works on my machine' failures."
tags: [testing, docker, isolation, containers, reproducibility, ci]
difficulty: intermediate
solution_count: 6
---

## Problem

Agent tests share databases, vector stores, and API mocks with other tests. A test that writes to the vector store affects another test's retrieval results. A test that mutates agent config changes behavior for subsequent tests. Running tests in parallel causes non-deterministic failures. Different developers' machines have different service versions, producing "works on my machine" bugs.

```python
# Bad: tests share a global database — order-dependent, non-reproducible
def test_store_memory():
    db.insert({"key": "fact", "value": "Paris is the capital of France"})

def test_retrieve_memory():
    result = db.get("fact")
    assert result == "Paris is the capital of France"
    # Fails if test_store_memory ran in a different order or process
```

---

## Solution 1 — pytest-docker: Spin Up Services Per Test Session

Use `pytest-docker` to start real service containers at the start of the test session and tear them down after. All tests in the session share a clean, real service.

```python
# conftest.py
import pytest
import time
import httpx

def wait_for_service(url: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            httpx.get(url, timeout=2.0)
            return
        except Exception:
            time.sleep(0.5)
    raise TimeoutError(f"Service at {url} not ready after {timeout}s")

@pytest.fixture(scope="session")
def docker_compose_file(pytestconfig):
    return str(pytestconfig.rootpath / "tests" / "docker-compose.test.yml")

@pytest.fixture(scope="session")
def docker_compose_project_name():
    return "agent_tests"

@pytest.fixture(scope="session")
def test_services(docker_services):
    """Wait for all services to be healthy before running tests."""
    docker_services.wait_until_responsive(
        check=lambda: httpx.get("http://localhost:6333/readyz").status_code == 200,
        timeout=30.0,
        pause=0.5,
    )
    return {
        "qdrant_url": "http://localhost:6333",
        "redis_url": "redis://localhost:6379",
        "postgres_dsn": "postgresql://agent:secret@localhost:5432/agent_test",
    }

@pytest.fixture
def vector_store(test_services):
    """Per-test isolated collection in shared Qdrant instance."""
    import uuid
    from qdrant_client import QdrantClient
    client = QdrantClient(url=test_services["qdrant_url"])
    collection = f"test_{uuid.uuid4().hex[:8]}"
    client.create_collection(collection, vectors_config={"size": 768, "distance": "Cosine"})
    yield client, collection
    client.delete_collection(collection)  # cleanup after test
```

```yaml
# tests/docker-compose.test.yml
version: "3.9"
services:
  qdrant:
    image: qdrant/qdrant:v1.7.4
    ports: ["6333:6333"]
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:6333/readyz"]
      interval: 2s
      timeout: 5s
      retries: 10

  redis:
    image: redis:7-alpine
    ports: ["6379:6379"]

  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: agent
      POSTGRES_PASSWORD: secret
      POSTGRES_DB: agent_test
    ports: ["5432:5432"]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U agent"]
      interval: 2s
      retries: 10
```

---

## Solution 2 — Per-Test Docker Container with testcontainers-python

Use `testcontainers` to give each test its own ephemeral container. Complete isolation: no state leaks between tests at all.

```python
import pytest
import asyncio
import asyncpg
from testcontainers.postgres import PostgresContainer
from testcontainers.redis import RedisContainer

@pytest.fixture(scope="function")  # new container per test
def isolated_postgres():
    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url()

@pytest.fixture(scope="function")
def isolated_redis():
    with RedisContainer("redis:7-alpine") as redis:
        yield f"redis://{redis.get_container_host_ip()}:{redis.get_exposed_port(6379)}"

@pytest.fixture(scope="session")  # one container for the whole session (faster)
def shared_postgres():
    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url()

# Test with fully isolated DB — cannot interfere with any other test
@pytest.mark.asyncio
async def test_agent_memory_isolation(isolated_postgres):
    conn = await asyncpg.connect(isolated_postgres)
    try:
        # Create schema
        await conn.execute("""
            CREATE TABLE memories (
                id SERIAL PRIMARY KEY,
                key TEXT UNIQUE,
                value TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        # Test — this DB is exclusive to this test
        await conn.execute("INSERT INTO memories (key, value) VALUES ($1, $2)",
                           "capital_of_france", "Paris")
        row = await conn.fetchrow("SELECT value FROM memories WHERE key = $1",
                                  "capital_of_france")
        assert row["value"] == "Paris"
    finally:
        await conn.close()

# Session-scoped: faster because container starts once, but tests must clean up after themselves
@pytest.mark.asyncio
async def test_user_preferences(shared_postgres):
    conn = await asyncpg.connect(shared_postgres)
    try:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS preferences (
                user_id TEXT PRIMARY KEY,
                prefs JSONB
            )
        """)
        # Use a unique user_id per test to avoid collisions
        import uuid
        user_id = str(uuid.uuid4())
        await conn.execute("INSERT INTO preferences VALUES ($1, $2)",
                           user_id, '{"theme": "dark"}')
        row = await conn.fetchrow("SELECT prefs FROM preferences WHERE user_id = $1", user_id)
        assert row["prefs"]["theme"] == "dark"
    finally:
        await conn.close()
```

---

## Solution 3 — Docker Network Isolation: Tests in Their Own Network

Run each test suite in its own Docker network. Services on network A can't see services on network B. Eliminates port conflicts and cross-suite pollution.

```python
import subprocess
import uuid
import pytest
import os

class DockerTestNetwork:
    """Manages an isolated Docker network for a test session."""

    def __init__(self, name: str = None):
        self.name = name or f"test_net_{uuid.uuid4().hex[:8]}"
        self.containers: list[str] = []

    def create(self) -> None:
        subprocess.run(["docker", "network", "create", self.name], check=True, capture_output=True)

    def start_container(self, image: str, name: str, env: dict = None,
                        ports: dict = None) -> str:
        """Start a container on this network. Returns container ID."""
        cmd = ["docker", "run", "-d",
               "--network", self.name,
               "--name", f"{self.name}_{name}"]
        for k, v in (env or {}).items():
            cmd += ["-e", f"{k}={v}"]
        for host_port, container_port in (ports or {}).items():
            cmd += ["-p", f"{host_port}:{container_port}"]
        cmd.append(image)

        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        container_id = result.stdout.strip()
        self.containers.append(container_id)
        return container_id

    def destroy(self) -> None:
        for cid in self.containers:
            subprocess.run(["docker", "rm", "-f", cid], capture_output=True)
        subprocess.run(["docker", "network", "rm", self.name], capture_output=True)

@pytest.fixture(scope="session")
def test_network():
    net = DockerTestNetwork()
    net.create()
    net.start_container(
        image="redis:7-alpine",
        name="redis",
        ports={6380: 6379},  # use non-default port to avoid conflicts with local redis
    )
    net.start_container(
        image="postgres:16-alpine",
        name="postgres",
        env={"POSTGRES_PASSWORD": "test", "POSTGRES_DB": "agent"},
        ports={5433: 5432},
    )
    import time
    time.sleep(3)  # wait for services to start
    yield net
    net.destroy()

@pytest.fixture
def redis_url(test_network):
    return "redis://localhost:6380"

@pytest.fixture
def postgres_url(test_network):
    return "postgresql://postgres:test@localhost:5433/agent"
```

---

## Solution 4 — Immutable Test Image with All Dependencies Baked In

Build a Docker image that contains the agent code, all dependencies, and service stubs. Run every test in a fresh container from this image — guaranteed reproducibility.

```dockerfile
# Dockerfile.test
FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl git && rm -rf /var/lib/apt/lists/*

# Install Python dependencies (cached layer)
COPY pyproject.toml requirements-test.txt ./
RUN pip install --no-cache-dir -r requirements-test.txt

# Copy agent code
COPY agent/ ./agent/
COPY tests/ ./tests/
COPY conftest.py ./

# Embedded SQLite for agent memory (no external DB needed)
ENV AGENT_DB_URL=sqlite:///test.db
ENV AGENT_ENV=test
ENV ANTHROPIC_API_KEY=sk-ant-test-key  # replaced by test mocks

# Run tests
CMD ["pytest", "tests/", "-v", "--tb=short", "-n", "auto"]
```

```python
# tests/conftest.py — configure mocks when AGENT_ENV=test
import os
import pytest
from unittest.mock import AsyncMock, patch

@pytest.fixture(autouse=True)
def mock_anthropic_api():
    """Always mock the Anthropic API in containerized tests."""
    if os.environ.get("AGENT_ENV") == "test":
        with patch("anthropic.AsyncAnthropic") as mock_cls:
            mock_client = AsyncMock()
            mock_cls.return_value = mock_client
            mock_client.messages.create.return_value = AsyncMock(
                content=[AsyncMock(text="mocked response", type="text")],
                stop_reason="end_turn",
                usage=AsyncMock(input_tokens=10, output_tokens=5),
            )
            yield mock_client
    else:
        yield None  # real API in non-container environments
```

```bash
#!/bin/bash
# run_tests_in_container.sh
set -e

IMAGE="agent-test:$(git rev-parse --short HEAD)"

# Build test image (cached if code hasn't changed)
docker build -f Dockerfile.test -t "$IMAGE" .

# Run tests in fresh container — completely isolated from host
docker run --rm \
  -e ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY}" \
  "$IMAGE" \
  pytest tests/ -v --tb=short "$@"
```

---

## Solution 5 — Compose Override for Test vs Production Services

Use Docker Compose overrides to substitute production services with lighter test equivalents — fake SMTP, in-memory Redis, SQLite instead of Postgres — without changing production config.

```yaml
# docker-compose.yml (production)
services:
  agent:
    image: agent:latest
    environment:
      - DB_URL=postgresql://agent:secret@postgres:5432/agent
      - REDIS_URL=redis://redis:6379
      - SMTP_HOST=smtp.sendgrid.net
    depends_on: [postgres, redis]

  postgres:
    image: postgres:16
    volumes: [pgdata:/var/lib/postgresql/data]

  redis:
    image: redis:7
```

```yaml
# docker-compose.test.yml (override — merged with production compose)
services:
  agent:
    environment:
      - DB_URL=sqlite:///test.db        # lighter than postgres
      - REDIS_URL=redis://redis-test:6379
      - SMTP_HOST=mailhog               # fake SMTP
      - AGENT_ENV=test
    depends_on: [redis-test, mailhog]

  postgres:
    # Disable production postgres entirely in tests
    profiles: [disabled]

  redis-test:
    image: redis:7-alpine
    # No persistence — pure in-memory

  mailhog:
    image: mailhog/mailhog:latest
    ports:
      - "8025:8025"  # web UI to inspect sent emails
```

```python
# conftest.py — validate emails via MailHog API
import httpx
import pytest

@pytest.fixture
async def email_inbox():
    """Clear MailHog inbox before test, return checker after."""
    async with httpx.AsyncClient() as client:
        await client.delete("http://localhost:8025/api/v1/messages")
    yield

    async with httpx.AsyncClient() as client:
        response = await client.get("http://localhost:8025/api/v2/messages")
        messages = response.json()["items"]

    return messages

@pytest.mark.asyncio
async def test_agent_sends_summary_email(email_inbox):
    await agent.process_and_email(user="test@example.com", content="Summary request")
    messages = email_inbox
    assert len(messages) == 1
    assert messages[0]["To"][0]["Mailbox"] == "test"
```

---

## Solution 6 — CI Matrix with Docker Buildx for Multi-Architecture Testing

Build and test the agent image for multiple architectures (amd64, arm64) in parallel CI to catch platform-specific failures before deployment.

```yaml
# .github/workflows/test-isolated.yml
name: Isolated Container Tests
on: [push, pull_request]

jobs:
  build-test-image:
    runs-on: ubuntu-latest
    outputs:
      image-tag: ${{ steps.meta.outputs.tags }}
    steps:
      - uses: actions/checkout@v4
      - uses: docker/setup-buildx-action@v3
      - uses: docker/build-push-action@v5
        with:
          context: .
          file: Dockerfile.test
          push: false
          load: true
          tags: agent-test:${{ github.sha }}
          cache-from: type=gha
          cache-to: type=gha,mode=max

  test-isolated:
    needs: build-test-image
    runs-on: ubuntu-latest
    strategy:
      matrix:
        test-group: [unit, integration, e2e]
        shard: [0, 1, 2, 3]
    steps:
      - uses: actions/checkout@v4
      - name: Run isolated tests
        run: |
          docker run --rm \
            -e ANTHROPIC_API_KEY=${{ secrets.ANTHROPIC_API_KEY }} \
            -e PYTEST_TOTAL_SHARDS=4 \
            -e PYTEST_SHARD_INDEX=${{ matrix.shard }} \
            agent-test:${{ github.sha }} \
            pytest tests/${{ matrix.test-group }}/ -v --tb=short

  # Run integration tests against real services in Docker Compose
  integration-with-services:
    needs: build-test-image
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Start test services
        run: docker compose -f docker-compose.test.yml up -d --wait
      - name: Run integration tests
        run: |
          docker compose -f docker-compose.test.yml run --rm \
            -e ANTHROPIC_API_KEY=${{ secrets.ANTHROPIC_API_KEY }} \
            agent pytest tests/integration/ -v
      - name: Collect logs on failure
        if: failure()
        run: docker compose -f docker-compose.test.yml logs
      - name: Teardown
        if: always()
        run: docker compose -f docker-compose.test.yml down -v
```

```python
# pytest plugin for Docker health checking in CI
import subprocess
import pytest
import time

def pytest_configure(config):
    """Check Docker is available before running container tests."""
    try:
        subprocess.run(["docker", "info"], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.exit("Docker not available — skipping container tests", returncode=0)

def wait_for_compose_health(timeout: float = 60.0) -> None:
    """Wait until all Compose services are healthy."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["docker", "compose", "-f", "docker-compose.test.yml",
             "ps", "--format", "json"],
            capture_output=True, text=True,
        )
        import json
        services = [json.loads(line) for line in result.stdout.strip().split("\n") if line]
        if all(s.get("Health") in ("healthy", "") for s in services):
            return
        time.sleep(1)
    raise TimeoutError("Compose services not healthy within timeout")
```

---

## Comparison

| Approach | Isolation Level | Startup Speed | Reproducible | CI Friendly | Best For |
|---|---|---|---|---|---|
| pytest-docker (session) | Service-level | Fast (shared) | **Yes** | **Yes** | Standard test suites |
| testcontainers (per-test) | **Container-per-test** | Slow | **Yes** | Yes | Maximum isolation |
| Docker network isolation | Network-level | Medium | **Yes** | **Yes** | Multi-suite parallel CI |
| Immutable test image | **Full environment** | Build-cached | **Yes** | **Yes** | Reproducibility guarantee |
| Compose overrides | Service substitution | Fast | Partial | **Yes** | Lightweight service swaps |
| Matrix + Buildx | **Multi-arch** | Parallel | **Yes** | **Yes** | Cross-platform validation |
