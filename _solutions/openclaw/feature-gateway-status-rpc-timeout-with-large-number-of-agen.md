---
layout: solution
title: "[Feature]: Gateway status RPC timeout with large number of agents (9000+)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/52905
description: "- OpenClaw Version:"
---

# [Feature]: Gateway status RPC timeout with large number of agents (9000+)

## 증상
- **OpenClaw Version**: 2026.3.2

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Increase timeout:
```bash
openclaw status --timeout 30000
```

This confirms the issue is performance-related, not a connectivity problem.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/52905
