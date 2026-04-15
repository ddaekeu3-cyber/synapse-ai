---
layout: solution
title: "Gateway fails to clean up stale .jsonl.lock file on hung/crashed session"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/52929
description: "The gateway process fails to clean up session lock files when a session hangs or the gateway is killed uncleanly. This causes subsequent gateway starts to"
---

# Gateway fails to clean up stale .jsonl.lock file on hung/crashed session

## 증상
The gateway process fails to clean up session lock files when a session hangs or the gateway is killed uncleanly. This causes subsequent gateway starts to get stuck indefinitely (shows as "typing..." in Discord) until the lock file is manually deleted.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Manually delete the lock file and restart the gateway:

```bash
rm ~/.openclaw/agents/main/sessions/<session-id>.jsonl.lock
launchctl stop ai.openclaw.gateway
launchctl start ai.openclaw.gateway
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/52929
