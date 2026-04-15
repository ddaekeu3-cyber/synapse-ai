---
layout: solution
title: "Background process termination crashes Claude Code in Docker containers"
category: docker
source: https://github.com/anthropics/claude-code/issues/16135
description: "When running Claude Code inside a Docker container, killing background processes (either manually with or when Claude autonomously decides to kill them)"
---

# Background process termination crashes Claude Code in Docker containers

## 증상
When running Claude Code inside a Docker container, killing background processes (either manually with `k` or when Claude autonomously decides to kill them) causes Claude Code itself to crash with exit code 137 (SIGKILL).

## 원인
Container permission, networking, or environment variable misconfiguration inside the sandbox.

## 해결법
Manually using `setsid` to isolate background processes works:
```bash
setsid uvicorn api:app --port 8000 > /tmp/server.log 2>&1 &
```

However, this loses Claude's background process monitoring and notification features.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/16135
