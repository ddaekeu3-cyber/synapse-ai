---
layout: solution
title: "requestHeartbeatNow with sessionKey silently skips for channel sessions (agent:main:slack:channel:*)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/34338
---

# requestHeartbeatNow with sessionKey silently skips for channel sessions (agent:main:slack:channel:*)

## 증상
`api.runtime.system.requestHeartbeatNow({ sessionKey, reason, coalesceMs: 0 })` silently skips when targeting channel sessions (`agent:main:slack:channel:*`). No heartbeat turn runs, no error is logged, the system event sits in the queue indefinitely.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Reverted to `POST /tools/invoke → sessions_send` (with `gateway.tools.allow: ["sessions_send"]` + `tools.sessions.visibility: "all"` config). Dead code for `requestHeartbeatNow` preserved in plugin for fast migration.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/34338
