---
layout: solution
title: "Claude Code hangs indefinitely in epoll_pwait loop on gVisor ARM64 (macOS Docker Desktop - OrbStack)"
category: docker
source: https://github.com/anthropics/claude-code/issues/35454
description: "- [x] I have searched existing issues and this hasn't been reported"
---

# Claude Code hangs indefinitely in epoll_pwait loop on gVisor ARM64 (macOS Docker Desktop - OrbStack)

## 증상
- [x] I have searched [existing issues](https://github.com/anthropics/claude-code/issues?q=is%3Aissue%20state%3Aopen%20label%3Abug) and this hasn't been reported yet

## 원인
Container permission, networking, or environment variable misconfiguration inside the sandbox.

## 해결법
Running with standard Docker (no gVisor) works perfectly:
```bash
docker run -it --rm \
  -e CLAUDE_CODE_OAUTH_TOKEN="..." \
  claude-gvisor-test \
  claude --dangerously-skip-permissions
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/35454
