---
layout: default
title: "Docker & Sandbox Errors in AI Agents — Permission, Network, and Mount Fixes"
description: "Fix EACCES permission denied, Docker networking, sandbox escape, and container configuration errors in OpenClaw AI agents. Copy-paste fixes included."
permalink: /guide/docker-errors
---

# Docker and Sandbox Error Guide for AI Agents

Docker and sandbox errors in AI agent setups are usually permission or networking issues — straightforward once you know where to look. The hard part is that agents running inside containers often can't diagnose their own environment, so the error messages are misleading.

---

## Permission Errors

### EACCES: Permission Denied

**Symptom:** `EACCES: permission denied, open '/path/to/file'` inside a container.

**Root cause:** Container running as root, but the target file is owned by a different UID. Or running as non-root with insufficient permissions on mounted volume.

**Fix:** Match the container user to the file owner:

```bash
# Option 1: Run container as host user
docker run --user $(id -u):$(id -g) your-image

# Option 2: Fix volume permissions before mounting
sudo chown -R $(id -u):$(id -g) ./your-volume-dir
docker run -v ./your-volume-dir:/app/data your-image

# Option 3: Add user to the file's group
docker run --group-add $(stat -c '%g' /path/to/file) your-image
```

---

### Permission Denied on Socket or Device

**Symptom:** Agent can't access `/var/run/docker.sock` or `/dev/...` devices.

**Fix:**
```bash
# Docker socket access
docker run -v /var/run/docker.sock:/var/run/docker.sock \
  --group-add $(stat -c '%g' /var/run/docker.sock) \
  your-image

# Device access
docker run --device /dev/device-name your-image
```

---

### Sandbox Permission Denied (OpenClaw-specific)

**Symptom:** OpenClaw sandbox returns permission denied when agent tries to write to `/tmp` or execute shell commands.

**Root cause:** OpenClaw's default sandbox profile blocks writes outside of designated directories.

**Fix:**
```yaml
# openclaw.config.yaml
sandbox:
  writable_paths:
    - /tmp
    - /home/agent/workspace
  allow_shell: true
  allow_network: true  # Only if the agent needs outbound calls
```

---

## Networking Errors

### Container Can't Reach Host Services

**Symptom:** Agent inside container can't reach `localhost:8080` or other host services.

**Root cause:** `localhost` inside a container refers to the container itself, not the host.

**Fix:**
```bash
# Linux: use host.docker.internal (Docker 20.10+) or host network
docker run --add-host host.docker.internal:host-gateway your-image

# Or use host network (Linux only)
docker run --network host your-image
```

Then in your agent config, replace `localhost` with `host.docker.internal`.

---

### Container Can't Reach Other Containers

**Symptom:** Container A can't connect to Container B on a shared network.

**Fix:**
```yaml
# docker-compose.yml
services:
  agent:
    networks:
      - agent-net
  gateway:
    networks:
      - agent-net

networks:
  agent-net:
    driver: bridge
```

Use service names (`gateway:9991`), not IP addresses — IPs change between restarts.

---

### DNS Resolution Fails Inside Container

**Symptom:** Container can ping by IP but not by hostname. `getaddrinfo ENOTFOUND api.example.com`.

**Fix:**
```bash
# Use Google DNS explicitly
docker run --dns 8.8.8.8 --dns 8.8.4.4 your-image

# Or add to daemon config
# /etc/docker/daemon.json
{ "dns": ["8.8.8.8", "8.8.4.4"] }
```

---

## Volume and Mount Errors

### Volume Mount Shows Empty Directory

**Symptom:** Files exist on the host but the container sees an empty directory at the mount point.

**Root cause:** Mount path inside the container conflicts with a directory created during image build (`COPY` or `RUN` in Dockerfile).

**Fix:**
```dockerfile
# Bad: COPY creates /app/data, then mount overwrites it
COPY ./data /app/data

# Good: Use a separate build dir, mount to clean path
COPY ./data /app/data-baked
# Mount to /app/data-runtime, reference both in your code
```

---

### Bind Mount File Changes Not Reflected

**Symptom:** Editing a file on the host doesn't update inside the container.

**Root cause:** macOS Docker Desktop uses osxfs or VirtioFS — file events may not propagate correctly.

**Fix:**
```yaml
# docker-compose.yml — use delegated for write-heavy, cached for read-heavy
volumes:
  - ./src:/app/src:delegated
```

Or switch to named volumes for critical data (faster, consistent):
```yaml
volumes:
  agent-data:
    driver: local
```

---

## Resource and Limit Errors

### Container OOM Killed (exit code 137)

**Symptom:** Container exits with code 137. No useful error message.

**Root cause:** Container exceeded memory limit and was killed by the kernel OOM killer.

**Fix:**
```bash
# Check if OOM killed
docker inspect $CONTAINER_ID | grep -A2 '"OOMKilled"'

# Increase memory limit
docker run --memory 4g --memory-swap 4g your-image

# Or reduce agent context window to use less memory
```

---

### Disk Space Exhausted Inside Container

**Symptom:** `ENOSPC: no space left on device` inside container. Host has disk space.

**Root cause:** Container's overlay filesystem is separate from host filesystem. Default container storage is limited.

**Fix:**
```bash
# Clean up old containers and images
docker system prune -f

# Increase container storage limit (Docker Desktop)
# Settings → Resources → Disk image size

# Or write large outputs to a mounted volume instead of container filesystem
```

---

## OpenClaw Sandbox-Specific

### Sandbox Blocks URL Requests from Agent

**Symptom:** Agent's HTTP requests to external APIs fail with `connection refused` or timeout, but direct curl from host works.

**Root cause:** OpenClaw sandbox has network egress disabled by default for security.

**Fix:**
```yaml
sandbox:
  allow_network: true
  allowed_hosts:          # Allowlist specific hosts (preferred)
    - "api.anthropic.com"
    - "api.openai.com"
    - "your-internal-api.example.com"
```

---

## Quick Reference

| Error | Likely Cause | Fix |
|-------|-------------|-----|
| `EACCES permission denied` | UID mismatch | `--user $(id -u):$(id -g)` |
| Can't reach host services | `localhost` is container | Use `host.docker.internal` |
| DNS fails | Default DNS blocked | `--dns 8.8.8.8` |
| Empty volume mount | Build-time dir conflicts | Use clean mount path |
| OOM killed (exit 137) | Memory limit | Increase `--memory` |
| ENOSPC in container | Container disk limit | `docker system prune` |
| Network blocked in sandbox | OpenClaw egress policy | `allow_network: true` |

---

[← View all Docker/sandbox solutions](/synapse-ai/solutions/docker/)

<div class="cta-box">
  <h3>Auto-diagnose container errors</h3>
  <p>SynapseAI identifies Docker and sandbox error patterns and suggests targeted fixes without trial and error.</p>
  <code>clawhub install synapse-ai</code>
</div>
