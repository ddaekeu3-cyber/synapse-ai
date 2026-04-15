---
layout: solution
title: "Subagent Docker containers not auto-removed after completion, causing maxConcurrent slot exhaustion"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/46254
description: "Subagent Docker containers are not automatically cleaned up after task completion, causing them to accumulate until maxConcurrent limit is reached and"
---

# Subagent Docker containers not auto-removed after completion, causing maxConcurrent slot exhaustion

## 증상
Subagent Docker containers are not automatically cleaned up after task completion, causing them to accumulate until maxConcurrent limit is reached and blocking new subagent spawns.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Manual cleanup:
```bash
docker ps --filter name=subagent --format {{.Names}} | xargs docker rm -f
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/46254
