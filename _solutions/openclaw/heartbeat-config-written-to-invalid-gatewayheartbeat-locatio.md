---
layout: solution
title: "Heartbeat config written to invalid `gateway.heartbeat` location instead of `agents.defaults.heartbeat`"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/43728
description: "Repository:"
---

# Heartbeat config written to invalid `gateway.heartbeat` location instead of `agents.defaults.heartbeat`

## 증상
**Repository:** openclaw/openclaw

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Run `openclaw doctor --fix` to remove the invalid key. However, this is only temporary - any subsequent modification to heartbeat settings will reintroduce the issue.

---

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/43728
