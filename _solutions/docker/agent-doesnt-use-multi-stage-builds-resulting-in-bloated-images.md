---
layout: solution
title: "Agent Doesn't Use Multi-Stage Builds Resulting in Bloated Images"
category: docker
description: "Agent container includes build tools, compiler caches, and test dependencies in the final image, producing 2–5 GB images that slow CI/CD pipelines and increase the attack surface."
tags: [docker, performance, security, ci-cd, production]
---

## Symptom

The agent Docker image is 3.2 GB because it includes `pip`, `gcc`, development headers, test frameworks, and intermediate build artefacts. CI/CD pipelines take 8 minutes to pull the image. The container startup time in Kubernetes is 45 seconds. A security scan flags 140 CVEs — many in build tools that are never used at runtime. The production container has `curl`, `bash`, and `make` available to a potential attacker who achieves code execution.

## Root Cause

A single-stage `FROM python:3.12 → COPY → RUN pip install` Dockerfile includes everything: the full Python base image (1.1 GB), build-time dependencies (gcc, libpq-dev, etc.), and all installed packages with their build artefacts. None of the build tools are needed at runtime — only the compiled `.so` files and Python packages. Multi-stage builds separate the build environment from the runtime environment, producing a minimal final image.

## Fix

### Option 1 — Basic two-stage build: builder → slim runtime

```dockerfile
# ── Stage 1: builder ─────────────────────────────────────────────────
FROM python:3.12 AS builder
WORKDIR /build

# Install build dependencies (only needed at compile time)
COPY requirements.txt .
RUN pip install --no-cache-dir --target /build/packages -r requirements.txt

# ── Stage 2: runtime ─────────────────────────────────────────────────
# python:3.12-slim is ~55 MB vs python:3.12 at ~1.1 GB
FROM python:3.12-slim AS runtime
WORKDIR /app

# Copy ONLY the installed packages — no build tools, no pip, no headers
COPY --from=builder /build/packages /app/packages
COPY agent.py .

ENV PYTHONPATH=/app/packages

# Non-root user
RUN useradd -u 1001 -r agent && chown -R agent /app
USER agent

CMD ["python", "agent.py"]
```

```python
# agent.py — same code, ~10x smaller image
import os
import anthropic

client = anthropic.Anthropic()
response = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=128,
    messages=[{"role": "user", "content": "Hello from a lean container."}],
)
print(response.content[0].text)
print(f"[info] UID={os.getuid()}, PYTHONPATH={os.environ.get('PYTHONPATH','not set')}")

# Build and compare:
# docker build -t agent-bloated -f Dockerfile.single .
# docker build -t agent-lean    -f Dockerfile.multi  .
# docker images | grep agent
# agent-lean     latest   abc123   5 minutes ago   180MB
# agent-bloated  latest   def456   3 minutes ago   1.8GB
```

**Expected Token Savings:** Smaller image = faster cold starts = less time spent waiting before the first Claude API call can be made; Kubernetes pods become ready 3–10× faster.
**Environment:** Any Dockerised agent; minimum viable multi-stage build with one `COPY --from=builder` line.

---

### Option 2 — Three-stage build: deps → test → runtime

```dockerfile
# ── Stage 1: dependency builder ──────────────────────────────────────
FROM python:3.12-slim AS deps
WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir --target /build/prod  -r requirements.txt
RUN pip install --no-cache-dir --target /build/dev   -r requirements-dev.txt -r requirements.txt

# ── Stage 2: test runner (never reaches production) ──────────────────
FROM python:3.12-slim AS test
WORKDIR /app
COPY --from=deps /build/dev /app/packages
COPY . .
ENV PYTHONPATH=/app/packages
RUN python -m pytest tests/ -x -q && echo "ALL TESTS PASSED"

# ── Stage 3: production runtime ───────────────────────────────────────
FROM python:3.12-slim AS runtime
WORKDIR /app

# Only copy production packages — test libs excluded
COPY --from=deps /build/prod /app/packages
COPY agent.py .

ENV PYTHONPATH=/app/packages
RUN useradd -u 1001 -r agent && chown -R agent /app
USER agent

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import anthropic; print('ok')" || exit 1

CMD ["python", "agent.py"]

# Build only the runtime stage (test stage runs automatically in the dependency graph):
# docker build --target runtime -t agent:prod .
#
# Or build the test stage to run tests without producing a runtime image:
# docker build --target test -t agent:test .
```

```python
# agent.py
import anthropic
client = anthropic.Anthropic()
print(client.messages.create(
    model="claude-haiku-4-5-20251001", max_tokens=64,
    messages=[{"role": "user", "content": "Hello from 3-stage build."}],
).content[0].text)
```

**Expected Token Savings:** Test dependencies (pytest, coverage, faker) are excluded from the production image — typically 200–500 MB savings; the test stage gates the build, so broken tests never produce a deployable image.
**Environment:** CI/CD pipelines that run tests as part of the Docker build; teams using BuildKit's `--target` for per-stage caching.

---

### Option 3 — Distroless final stage: no OS tools at all

```dockerfile
# ── Stage 1: builder (needs pip, gcc) ────────────────────────────────
FROM python:3.12-slim AS builder
WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends gcc && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --target /build/packages -r requirements.txt

# ── Stage 2: distroless runtime ───────────────────────────────────────
# gcr.io/distroless/python3-debian12:nonroot
# - No shell (sh, bash, dash) — attacker cannot run commands interactively
# - No package manager (apt, pip) — cannot install new tools
# - No curl/wget — cannot exfiltrate data easily
# - Runs as UID 65532 (nonroot) by default
FROM gcr.io/distroless/python3-debian12:nonroot
WORKDIR /app

COPY --from=builder /build/packages /app/packages
COPY agent.py .

ENV PYTHONPATH=/app/packages

# distroless requires specifying the python binary path explicitly
CMD ["/usr/bin/python3", "agent.py"]

# Image size comparison (typical):
# python:3.12             1.15 GB
# python:3.12-slim         155 MB
# distroless python3        55 MB
# distroless python3:debug  70 MB  (adds busybox shell for debugging)
```

```python
# agent.py
import os
import anthropic

client = anthropic.Anthropic()
print(f"[security] UID={os.getuid()} (distroless nonroot=65532)")
resp = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=64,
    messages=[{"role": "user", "content": "Hello from distroless."}],
)
print(resp.content[0].text)
```

**Expected Token Savings:** Distroless images are 50–75 MB vs 150–1100 MB for standard Python images; faster image pulls in Kubernetes reduce pod startup time; no shell means attackers can't use code execution to run further commands.
**Environment:** Security-sensitive production agents (fintech, healthcare); high-frequency pod scaling where image pull speed matters.

---

### Option 4 — Layer caching optimisation: deps before code

```dockerfile
# ── Stage 1: dependency cache layer ──────────────────────────────────
FROM python:3.12-slim AS deps
WORKDIR /deps

# Copy ONLY requirements — not the application code
# This layer is cached and reused as long as requirements.txt doesn't change
COPY requirements.txt .
RUN pip install --no-cache-dir --target /deps/packages -r requirements.txt

# ── Stage 2: runtime ─────────────────────────────────────────────────
FROM python:3.12-slim AS runtime
WORKDIR /app

# Copy the cached dep layer (rarely invalidated)
COPY --from=deps /deps/packages /app/packages

# Copy application code LAST (frequently changes — invalidates only this layer)
COPY agent.py .
COPY config/ ./config/

ENV PYTHONPATH=/app/packages
RUN useradd -u 1001 -r agent && chown -R agent /app
USER agent

CMD ["python", "agent.py"]

# Layer analysis:
# Layer 1 (python:3.12-slim base):  155 MB  — reused from registry cache
# Layer 2 (pip install packages):   120 MB  — cached as long as requirements unchanged
# Layer 3 (COPY agent.py):            0.5 MB — rebuilt on every code change
#
# Without this ordering:
# Layer 1: 155 MB
# Layer 2: COPY all code + requirements.txt
# Layer 3: pip install — ALWAYS rebuilt when any code changes → 2-5 min penalty per build
```

```python
# agent.py
import anthropic
client = anthropic.Anthropic()
resp = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=128,
    messages=[{"role": "user", "content": "Explain layer caching in 2 sentences."}],
)
print(resp.content[0].text)
```

**Expected Token Savings:** Proper layer ordering reduces typical CI build time from 3–5 minutes (full pip install) to 15–30 seconds (only application code layer rebuilt); faster iteration means more agent testing per hour.
**Environment:** Any CI/CD pipeline; the most impactful single change for most agent Docker builds.

---

### Option 5 — BuildKit cache mounts for pip

```dockerfile
# syntax=docker/dockerfile:1.4
# ── Stage 1: builder with persistent pip cache ────────────────────────
FROM python:3.12-slim AS builder
WORKDIR /build

# BuildKit cache mount: pip's HTTP cache persists across builds on the same host
# Packages that haven't changed are served from cache — no network download
RUN --mount=type=cache,target=/root/.cache/pip \
    --mount=type=bind,source=requirements.txt,target=requirements.txt \
    pip install --target /build/packages -r requirements.txt

# ── Stage 2: runtime ─────────────────────────────────────────────────
FROM python:3.12-slim AS runtime
WORKDIR /app

COPY --from=builder /build/packages /app/packages
COPY agent.py .

ENV PYTHONPATH=/app/packages
RUN useradd -u 1001 -r agent && chown -R agent /app
USER agent

CMD ["python", "agent.py"]

# Build with BuildKit:
# DOCKER_BUILDKIT=1 docker build -t agent:prod .
# Or: docker buildx build -t agent:prod .
#
# First build: downloads packages (e.g., 45 seconds)
# Subsequent builds with same deps: serves from cache (e.g., 3 seconds)
# The cache is NOT included in the final image — pure build-time optimisation
```

```python
# agent.py
import anthropic
client = anthropic.Anthropic()
resp = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=64,
    messages=[{"role": "user", "content": "Hello from a BuildKit-cached build."}],
)
print(resp.content[0].text)
```

**Expected Token Savings:** BuildKit cache mounts reduce repeated build times from minutes to seconds; faster CI loops mean more agent versions tested per hour — cheaper iteration without reducing image quality.
**Environment:** CI/CD runners with persistent volumes (GitHub Actions with `actions/cache`, self-hosted runners); BuildKit must be enabled (default in Docker 20.10+).

---

### Option 6 — Image size audit script: find what's bloating the image

```python
#!/usr/bin/env python3
"""
Script to audit Docker image layer sizes and identify bloat.
Run this as part of CI to fail builds that exceed size thresholds.
"""
import subprocess
import json
import sys

MAX_IMAGE_SIZE_MB  = 300   # fail if image exceeds this
WARN_LAYER_SIZE_MB = 50    # warn on large individual layers

def get_image_size_mb(image: str) -> float:
    result = subprocess.run(
        ["docker", "image", "inspect", image, "--format", "{{.Size}}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"[error] cannot inspect {image}: {result.stderr}")
        return 0
    return int(result.stdout.strip()) / (1024 * 1024)

def get_layer_history(image: str) -> list[dict]:
    result = subprocess.run(
        ["docker", "history", "--no-trunc", "--format",
         '{"cmd":"{{.CreatedBy}}","size":"{{.Size}}"}', image],
        capture_output=True, text=True,
    )
    layers = []
    for line in result.stdout.strip().splitlines():
        try:
            entry = json.loads(line)
            size_str = entry["size"].replace("MB", "").replace("GB", "00").replace("kB", "").strip()
            size_mb = float(size_str) if size_str.replace(".", "").isdigit() else 0
            layers.append({"cmd": entry["cmd"][:80], "size_mb": size_mb})
        except (json.JSONDecodeError, ValueError):
            pass
    return layers

def audit_image(image: str) -> bool:
    size_mb = get_image_size_mb(image)
    print(f"[audit] {image}: {size_mb:.1f} MB (limit: {MAX_IMAGE_SIZE_MB} MB)")

    layers = get_layer_history(image)
    large_layers = [l for l in layers if l["size_mb"] >= WARN_LAYER_SIZE_MB]
    if large_layers:
        print("[audit] large layers:")
        for l in large_layers:
            print(f"  {l['size_mb']:.0f} MB — {l['cmd']}")

    if size_mb > MAX_IMAGE_SIZE_MB:
        print(f"[FAIL] image exceeds {MAX_IMAGE_SIZE_MB} MB limit. Use multi-stage build to reduce size.")
        return False
    print(f"[PASS] image size within limit")
    return True

# Also demonstrate the agent code itself is size-agnostic
import anthropic
client = anthropic.Anthropic()
resp = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=64,
    messages=[{"role": "user", "content": "Suggest one way to reduce Docker image size."}],
)
print(f"\n[claude] {resp.content[0].text[:100]}")

# CI integration:
if __name__ == "__main__":
    image = sys.argv[1] if len(sys.argv) > 1 else "agent:latest"
    ok = audit_image(image)
    sys.exit(0 if ok else 1)
```

**Expected Token Savings:** Size audit in CI catches regressions (e.g., accidentally adding a dev dependency to requirements.txt) before they reach production; maintaining a lean image keeps cold-start latency low, which directly impacts time-to-first-token.
**Environment:** CI/CD pipelines; pairs with any of the above Dockerfile patterns as an automated quality gate.

---

## Comparison

| Option | Final Stage Base | Has Shell | Image Size (typical) | Build Cache | Test Isolation | Best For |
|---|---|---|---|---|---|---|
| 1. Basic 2-stage | python:3.12-slim | Yes | ~180 MB | Layer cache | No | Quickest win; most agents |
| 2. 3-stage (test) | python:3.12-slim | Yes | ~180 MB | Layer cache | Yes | CI/CD with test gating |
| 3. Distroless | distroless:nonroot | No | ~55 MB | Layer cache | No | Security-first; no shell needed |
| 4. Layer ordering | python:3.12-slim | Yes | ~180 MB | Optimal layers | No | Fastest incremental rebuilds |
| 5. BuildKit cache | python:3.12-slim | Yes | ~180 MB | Pip HTTP cache | No | Self-hosted CI; repeated builds |
| 6. Size audit | N/A (CI tool) | N/A | N/A | N/A | N/A | Automated size regression detection |
