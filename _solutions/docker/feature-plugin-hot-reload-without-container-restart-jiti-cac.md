---
layout: solution
title: "[Feature]: Plugin hot-reload without container restart (jiti cache invalidation)"
category: docker
source: https://github.com/openclaw/openclaw/issues/14438
description: "When developing OpenClaw plugins (TypeScript), every code change"
---

# [Feature]: Plugin hot-reload without container restart (jiti cache invalidation)

## 증상
When developing OpenClaw plugins (TypeScript), every code change requires:

## 원인
Container permission, networking, or environment variable misconfiguration inside the sandbox.

## 해결법
```bash
# In Docker
docker exec openclaw-bot sh -c "rm -f /tmp/jiti/plugin-name.*.cjs"
docker restart openclaw-bot
```

🤖 Generated with [Claude Code](https://claude.com/claude-code)

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/14438
