---
layout: solution
title: "Gateway Heartbeat timer stops after 1-2 triggers (introduced in v2026.3.8)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/45772
description: "Gateway heartbeat feature stops working after triggering 1-2 times. The timer does not reschedule after firing, causing heartbeat to permanently"
---

# Gateway Heartbeat timer stops after 1-2 triggers (introduced in v2026.3.8)

## 증상
Gateway heartbeat feature stops working after triggering 1-2 times. The timer does not reschedule after firing, causing heartbeat to permanently stop.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Use cron job instead of built-in heartbeat:
```json
{
  "schedule": {
    "kind": "cron",
    "expr": "0,30 7-23 * * *",
    "tz": "Asia/Shanghai"
  },
  "payload": {
    "kind": "agentTurn",
    "message": "Execute heartbeat tasks"
  }
}
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/45772
