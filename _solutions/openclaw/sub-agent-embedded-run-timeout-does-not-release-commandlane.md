---
layout: solution
title: "Sub-agent embedded run timeout does not release CommandLane, causing all subsequent webchat messages to be queued indefinitely"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/49398
description: "When a spawned sub-agent (embedded run) times out, the parent agent's CommandLane lock is not released. This causes all subsequent messages sent to the"
---

# Sub-agent embedded run timeout does not release CommandLane, causing all subsequent webchat messages to be queued indefinitely

## 증상
When a spawned sub-agent (embedded run) times out, the parent agent's CommandLane lock is not released. This causes all subsequent messages sent to the parent session (via webchat) to be queued indefinitely — the webchat shows a permanent loading spinner and never receives a response.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Restart the gateway service:
```bash
systemctl --user restart openclaw-gateway
```

This clears the stuck lane state and allows messages to be processed again.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/49398
