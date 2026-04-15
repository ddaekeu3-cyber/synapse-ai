---
layout: solution
title: "POST /hooks/wake silently drops requests in 2026.3.2 (returns 200 but never wakes session)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/33271
description: "accepts webhook requests and returns with , but the session is never actually woken. No system event is enqueued, no heartbeat is triggered, and no log"
---

# POST /hooks/wake silently drops requests in 2026.3.2 (returns 200 but never wakes session)

## 증상
`POST /hooks/wake` accepts webhook requests and returns `200 OK` with `{"ok":true,"mode":"now"}`, but the session is never actually woken. No system event is enqueued, no heartbeat is triggered, and no log entry is produced for the wake attempt.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Switch from `/hooks/wake` (or mapped wake hooks) to `/hooks/agent`, which correctly runs isolated agent turns:
```bash
curl -X POST http://127.0.0.1:18789/hooks/agent \
  -H "Authorization: Bearer your-token" \
  -H "Content-Type: application/json" \
  -d '{"message":"Your message here","name":"Voice","wakeMode":"now","deliver":false}'
```

Downside: `/hooks/agent` runs in an isolated session without main session context.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/33271
