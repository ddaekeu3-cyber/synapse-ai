---
layout: solution
title: "OOM crash on start without NODE_OPTIONS=--max-old-space-size=1536 (v2026.3.12+)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/45160
description: "Starting with v2026.3.12, the OpenClaw gateway crashes with an out-of-memory error on startup unless is set. This was introduced alongside the Dashboard"
---

# OOM crash on start without NODE_OPTIONS=--max-old-space-size=1536 (v2026.3.12+)

## 증상
Starting with v2026.3.12, the OpenClaw gateway crashes with an out-of-memory error on startup unless `NODE_OPTIONS=--max-old-space-size=1536` is set. This was introduced alongside the Dashboard v2 / plugin architecture changes.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Set the environment variable in your `docker-compose.yml`:

```yaml
environment:
  - NODE_OPTIONS=--max-old-space-size=1536
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/45160
