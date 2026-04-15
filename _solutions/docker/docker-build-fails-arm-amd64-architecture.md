---
layout: solution
title: "Docker Build Fails on ARM — Architecture Mismatch Between Build and Deploy"
category: docker
description: "Docker image built on Apple Silicon (arm64) fails on Linux server (amd64). exec format error at runtime. Or CI/CD builds fail because base image doesn't have an arm64 variant."
tags: [docker, arm64, amd64, architecture, multiarch, buildx, apple-silicon]
---

# Docker Build Fails on ARM — Architecture Mismatch Between Build and Deploy

## Symptom
- `exec /bin/sh: exec format error` when running container on server
- Image built on M1/M2 Mac, fails on AWS EC2 Linux (x86_64)
- `WARNING: The requested image's platform (linux/amd64) does not match the detected host platform (linux/arm64)`
- CI/CD fails: base image has no arm64 variant
- Works locally (Mac), fails in production (Linux)

## Root Cause
Modern Macs use Apple Silicon (ARM / `linux/arm64`). Most cloud servers run x86_64 (`linux/amd64`). Docker images are architecture-specific. Building on ARM produces ARM-only images that can't run on x86_64 servers — and vice versa.

## Fix

### Option 1: Build for target architecture explicitly

```bash
# Build for amd64 specifically (even on ARM Mac)
docker build --platform linux/amd64 -t my-agent:latest .

# Or specify in docker-compose.yml
# docker-compose.yml:
# services:
#   agent:
#     build:
#       context: .
#       platforms:
#         - linux/amd64
```

### Option 2: Build multi-platform image with buildx

```bash
# Set up buildx builder with multi-platform support
docker buildx create --name multiarch --driver docker-container --use
docker buildx inspect --bootstrap

# Build and push multi-arch image (supports both ARM and AMD64)
docker buildx build \
    --platform linux/amd64,linux/arm64 \
    --tag your-registry/my-agent:latest \
    --push \
    .

# Pull the right architecture automatically on any platform
docker pull your-registry/my-agent:latest  # Gets arm64 on Mac, amd64 on Linux
```

### Option 3: Fix Dockerfile to use multi-arch base images

```dockerfile
# BAD — single arch base image
FROM ubuntu:22.04-amd64  # Only works on x86_64

# GOOD — multi-arch image (Docker pulls correct arch automatically)
FROM ubuntu:22.04  # Has linux/amd64, linux/arm64, linux/arm/v7 variants

# GOOD — Python multi-arch
FROM python:3.12-slim  # Supports amd64, arm64, arm/v7

# For AI/ML workloads — check platform-specific builds
# PyTorch has separate wheels for CUDA (amd64) vs MPS (arm64)
FROM pytorch/pytorch:2.1.0-cuda12.1-cudnn8-runtime  # amd64 + CUDA only
# vs
FROM python:3.12-slim  # Then install appropriate torch variant at runtime
```

### Option 4: Conditional dependencies by platform in Dockerfile

```dockerfile
FROM python:3.12-slim

# Install platform-specific dependencies
ARG TARGETPLATFORM
ARG TARGETARCH

RUN echo "Building for: $TARGETPLATFORM ($TARGETARCH)"

# Example: different packages for ARM vs AMD64
RUN if [ "$TARGETARCH" = "amd64" ]; then \
        pip install torch==2.1.0 --index-url https://download.pytorch.org/whl/cpu; \
    elif [ "$TARGETARCH" = "arm64" ]; then \
        pip install torch==2.1.0; \
    fi

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . /app
WORKDIR /app
CMD ["python", "agent.py"]
```

### Option 5: GitHub Actions multi-arch CI/CD

```yaml
# .github/workflows/docker.yml
name: Build and push multi-arch Docker image

on:
  push:
    branches: [main]

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Set up QEMU (enables cross-platform builds)
        uses: docker/setup-qemu-action@v3

      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@v3

      - name: Login to Docker Hub
        uses: docker/login-action@v3
        with:
          username: ${{ secrets.DOCKERHUB_USERNAME }}
          password: ${{ secrets.DOCKERHUB_TOKEN }}

      - name: Build and push
        uses: docker/build-push-action@v5
        with:
          platforms: linux/amd64,linux/arm64
          push: true
          tags: yourname/my-agent:latest
          cache-from: type=gha
          cache-to: type=gha,mode=max
```

### Option 6: Check and diagnose architecture issues

```bash
# Check what architecture your image was built for
docker inspect my-agent:latest | jq '.[0].Architecture'
# "arm64" or "amd64"

# Check what your current machine is
uname -m
# "arm64" (Apple Silicon) or "x86_64" (Intel/AMD)

# Check available platforms for a Docker Hub image
docker manifest inspect python:3.12-slim | jq '.manifests[].platform'

# Run amd64 image on ARM Mac (using emulation — slower but works for testing)
docker run --platform linux/amd64 my-agent:latest
```

## Architecture Quick Reference

| Machine | Architecture | Docker platform |
|---|---|---|
| Apple M1/M2/M3/M4 | ARM64 | `linux/arm64` |
| Intel Mac | x86_64 | `linux/amd64` |
| AWS EC2 (most) | x86_64 | `linux/amd64` |
| AWS Graviton | ARM64 | `linux/arm64` |
| Raspberry Pi 4 | ARM64/ARM | `linux/arm64` or `linux/arm/v7` |
| Google Cloud (most) | x86_64 | `linux/amd64` |

## Expected Token Savings
Debugging "exec format error" without knowing about architecture: ~6,000 tokens
Using `--platform linux/amd64` from the start: 0 wasted

## Environment
- Teams developing on Apple Silicon and deploying to x86 cloud infrastructure
- Source: direct experience; extremely common since Apple Silicon adoption
