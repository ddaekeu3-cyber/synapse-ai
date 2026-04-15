---
layout: solution
title: "[Feature]: Extend Internal Hooks to support cron lifecycle events"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/37855
description: "Add cron event type to Internal Hooks system, enabling external systems to react to scheduled task changes (create/update/delete) and execution status"
---

# [Feature]: Extend Internal Hooks to support cron lifecycle events

## 증상
Add cron event type to Internal Hooks system, enabling external systems to react to scheduled task changes (create/update/delete) and execution status (started/finished).

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
The cron system broadcasts events to WebSocket clients (`src/gateway/server-cron.ts:358-359`):

```typescript
onEvent: (evt) => {
  params.broadcast("cron", evt, { dropIfSlow: true });
  if (evt.action === "finished") {
    // webhook delivery for finished jobs only
  }
}
```

But this requires a WebSocket connection and only `finished` triggers external webhooks via `delivery.mode: "webhook"`.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/37855
