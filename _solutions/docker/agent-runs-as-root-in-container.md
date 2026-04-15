---
layout: solution
title: "Agent Runs as Root in Container"
category: docker
description: "Agent container runs as UID 0 (root), so a prompt injection or code execution vulnerability grants an attacker full container control and potential host escape."
tags: [docker, security, container, production, least-privilege]
---

## Symptom

A security scan flags the agent container with `USER root` or no `USER` directive (Docker default is root). During a red-team exercise, an attacker exploits a prompt injection to write arbitrary files, install packages, or read `/etc/shadow` inside the container. With root privileges, known kernel exploits can escape the container namespace entirely, compromising the host.

## Root Cause

Docker containers run as root by default when no `USER` instruction is specified in the Dockerfile. Developers prioritise convenience (root can install packages, bind to low ports, write anywhere) over security. In a stateless agent that only needs to call the Anthropic API and read a config file, root privileges are never necessary and provide no benefit — only risk.

## Fix

### Option 1 — Add USER directive to Dockerfile

```dockerfile
# Dockerfile
FROM python:3.12-slim

# Install dependencies as root (before dropping privileges)
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Create a non-root user and group
RUN groupadd --gid 1001 agent && \
    useradd  --uid 1001 --gid agent --shell /bin/bash --create-home agent

# Give the agent user ownership of the app directory
RUN chown -R agent:agent /app

# Drop to non-root before the entrypoint
USER agent

# Verify we're not root at startup
ENTRYPOINT ["python", "-c", \
  "import os,sys; uid=os.getuid(); print(f'[security] running as UID {uid}'); sys.exit(0 if uid!=0 else 1)"]
```

```python
# agent.py — verifies non-root at startup
import os
import sys
import anthropic

def assert_not_root() -> None:
    uid = os.getuid()
    if uid == 0:
        print("[security] FATAL: agent is running as root. Fix the Dockerfile USER directive.",
              file=sys.stderr)
        sys.exit(1)
    print(f"[security] running as UID {uid} — OK")

assert_not_root()

client = anthropic.Anthropic()
response = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=64,
    messages=[{"role": "user", "content": "Hello from a non-root container."}],
)
print(response.content[0].text)
```

**Expected Token Savings:** No direct token savings, but prevents a compromised agent from using its API key to make unlimited calls as a privileged process; blast radius of a credential leak is bounded.
**Environment:** Any Dockerised agent; minimum viable fix — one `USER` line in the Dockerfile.

---

### Option 2 — Distroless base image with no shell

```dockerfile
# Dockerfile.distroless
# Build stage (needs tools)
FROM python:3.12-slim AS builder
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --target /app/packages -r requirements.txt
COPY agent.py .

# Runtime stage — distroless has no shell, no package manager, minimal attack surface
FROM gcr.io/distroless/python3-debian12:nonroot
# :nonroot tag runs as UID 65532 by default

WORKDIR /app
COPY --from=builder /app/packages /app/packages
COPY --from=builder /app/agent.py .

ENV PYTHONPATH=/app/packages

CMD ["agent.py"]
```

```python
# agent.py
import os
import anthropic

print(f"[security] UID={os.getuid()} GID={os.getgid()} — distroless nonroot")

client = anthropic.Anthropic()
response = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=128,
    messages=[{"role": "user", "content": "Running in a distroless container."}],
)
print(response.content[0].text)

# Verify: docker inspect <container> | grep -i user
# Should show "65532" (distroless nonroot UID)
```

**Expected Token Savings:** Distroless removes bash, curl, wget, pip — tools an attacker would use after gaining code execution; reduces image size by ~50%, cutting CI/CD transfer and startup time.
**Environment:** Production agents with strict security requirements; pairs with read-only root filesystem for maximum hardening.

---

### Option 3 — Docker run with --user flag and read-only filesystem

```bash
# docker-compose.yml
# version: "3.9"
# services:
#   agent:
#     image: synapse-agent:latest
#     user: "1001:1001"               # override even if Dockerfile uses root
#     read_only: true                  # filesystem is read-only
#     tmpfs:                           # writable temp dir in memory only
#       - /tmp:size=64m,mode=1777
#     security_opt:
#       - no-new-privileges:true       # prevent setuid escalation
#     cap_drop:
#       - ALL                          # drop all Linux capabilities
#     cap_add:
#       - NET_BIND_SERVICE             # add back only what's needed
#     environment:
#       - ANTHROPIC_API_KEY
```

```python
# agent.py — runtime security assertions
import os
import sys
import stat
import anthropic

def security_audit() -> None:
    uid = os.getuid()
    assert uid != 0, f"Running as root (UID {uid}) — forbidden in production"

    # Verify /tmp is writable (tmpfs mount)
    try:
        test_file = "/tmp/.security_check"
        with open(test_file, "w") as f:
            f.write("ok")
        os.remove(test_file)
    except PermissionError:
        pass  # read-only fs — /tmp tmpfs not mounted; acceptable in some configs

    print(f"[security] UID={uid}, assertions passed")

security_audit()

client = anthropic.Anthropic()
response = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=128,
    messages=[{"role": "user", "content": "Security hardened agent checking in."}],
)
print(response.content[0].text)
```

**Expected Token Savings:** `no-new-privileges` prevents a containerised agent from escalating privileges even if a SUID binary is found; `cap_drop: ALL` means even root-equivalent operations fail, bounding the damage from any prompt injection that achieves code execution.
**Environment:** Docker Compose deployments; Kubernetes (use `securityContext` instead of docker flags).

---

### Option 4 — Kubernetes securityContext with non-root enforcement

```yaml
# kubernetes/deployment.yaml
# apiVersion: apps/v1
# kind: Deployment
# metadata:
#   name: synapse-agent
# spec:
#   template:
#     spec:
#       securityContext:
#         runAsNonRoot: true          # Kubernetes rejects root containers
#         runAsUser:  1001
#         runAsGroup: 1001
#         fsGroup:    1001
#         seccompProfile:
#           type: RuntimeDefault      # restrict syscalls
#       containers:
#         - name: agent
#           image: synapse-agent:latest
#           securityContext:
#             allowPrivilegeEscalation: false
#             readOnlyRootFilesystem:  true
#             capabilities:
#               drop: ["ALL"]
#           volumeMounts:
#             - name: tmp
#               mountPath: /tmp
#       volumes:
#         - name: tmp
#           emptyDir:
#             sizeLimit: 64Mi
```

```python
# agent.py — validates K8s security context at runtime
import os
import sys
import anthropic

def validate_k8s_security():
    uid = os.getuid()
    if uid == 0:
        print("[security] FATAL: running as root in Kubernetes pod", file=sys.stderr)
        sys.exit(1)

    # Check readOnlyRootFilesystem by attempting a write
    try:
        with open("/test-write", "w") as f:
            f.write("x")
        os.remove("/test-write")
        print("[security] WARNING: root filesystem is writable — check readOnlyRootFilesystem")
    except PermissionError:
        print(f"[security] UID={uid}, root FS read-only — K8s securityContext OK")

validate_k8s_security()

client = anthropic.Anthropic()
response = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=128,
    messages=[{"role": "user", "content": "Hello from a hardened K8s pod."}],
)
print(response.content[0].text)

# Verify: kubectl exec <pod> -- id
# Should output: uid=1001(agent) gid=1001(agent)
```

**Expected Token Savings:** K8s `runAsNonRoot: true` admission control rejects misconfigured deployments before the pod starts — catching config drift in CI before it reaches production.
**Environment:** Kubernetes deployments; works with OPA/Gatekeeper policies for org-wide enforcement.

---

### Option 5 — Multi-stage build with explicit permission hardening

```dockerfile
# Dockerfile.hardened
FROM python:3.12-slim AS builder
WORKDIR /build

# Install deps
COPY requirements.txt .
RUN pip install --no-cache-dir --target /build/dist -r requirements.txt

# ── Runtime stage ──────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# Create non-root user
RUN groupadd -g 1001 agent && \
    useradd  -u 1001 -g 1001 -d /app -s /sbin/nologin -M agent

WORKDIR /app

# Copy deps from builder
COPY --from=builder --chown=agent:agent /build/dist ./dist
COPY --chown=agent:agent agent.py .

# Make everything read-only for the agent user
RUN chmod -R 550 /app && \
    chmod 440 /app/agent.py

ENV PYTHONPATH=/app/dist

# Switch to non-root
USER agent

# Verify at build time
RUN python -c "import os; assert os.getuid() != 0, 'Build-time root check failed'"

ENTRYPOINT ["python", "agent.py"]
```

```python
# agent.py
import os, sys, stat, anthropic

uid = os.getuid()
print(f"[security] UID={uid} ({os.environ.get('USER', 'agent')})")
assert uid != 0, "FATAL: running as root"

# Verify agent.py itself is not world-writable
mode = stat.filemode(os.stat(__file__).st_mode)
print(f"[security] agent.py permissions: {mode}")

client = anthropic.Anthropic()
r = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=64,
    messages=[{"role": "user", "content": "Hello from hardened container."}],
)
print(r.content[0].text)
```

**Expected Token Savings:** Read-only file permissions prevent an attacker with code execution from modifying `agent.py` to exfiltrate API keys on the next invocation.
**Environment:** High-security production environments; financial services, healthcare, or government workloads with compliance requirements.

---

### Option 6 — Rootless Docker (daemon runs without root)

```bash
# Setup rootless Docker (run once per machine):
# dockerd-rootless-setuptool.sh install
# export DOCKER_HOST=unix://$XDG_RUNTIME_DIR/docker.sock

# Build and run with rootless Docker — the daemon itself runs as your user
# docker build -t synapse-agent .
# docker run --rm synapse-agent
```

```dockerfile
# Dockerfile for rootless Docker — simpler, no USER directive needed
# Rootless Docker maps container UID 0 to your host user UID
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt agent.py ./
RUN pip install --no-cache-dir -r requirements.txt
# No USER directive needed — rootless Docker maps root→host-user automatically
CMD ["python", "agent.py"]
```

```python
# agent.py
import os
import subprocess
import anthropic

def check_rootless() -> None:
    uid = os.getuid()
    # In rootless Docker, container UID 0 maps to the host user UID
    # The process sees UID 0 inside the container but has no host root privileges
    print(f"[security] container UID={uid}")
    try:
        result = subprocess.run(["cat", "/proc/self/status"],
                                capture_output=True, text=True)
        for line in result.stdout.splitlines():
            if line.startswith(("Uid:", "Gid:")):
                print(f"[security] {line}")
    except Exception:
        pass

check_rootless()

client = anthropic.Anthropic()
response = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=128,
    messages=[{"role": "user", "content": "Hello from rootless Docker."}],
)
print(response.content[0].text)

# Verify rootless: docker info | grep -i rootless
# Should show: rootless: true
```

**Expected Token Savings:** Rootless Docker eliminates the entire container escape risk class — even if the agent achieves container root, it maps to the host unprivileged user; no additional Dockerfile changes needed for security.
**Environment:** Developer workstations and CI runners where installing rootless Docker is feasible; reduces operational complexity vs per-image USER directives.

---

## Comparison

| Option | Root Prevented By | Shell Access | Read-only FS | Kubernetes Ready | Best For |
|---|---|---|---|---|---|
| 1. USER directive | Dockerfile | Yes | No | Yes | Minimum viable fix; all environments |
| 2. Distroless | Base image | No (no shell) | Optional | Yes | Maximum attack surface reduction |
| 3. --user + read-only | Docker run flags | Yes | Yes | No | Docker Compose; dev enforcement |
| 4. K8s securityContext | Admission control | Yes | Yes | Yes | Kubernetes; org-wide policy enforcement |
| 5. Multi-stage + chmod | Dockerfile + perms | Yes | Partial (file perms) | Yes | Compliance-heavy environments |
| 6. Rootless Docker | Daemon architecture | Yes (maps to host user) | No | Partial | Dev workstations; CI runners |
