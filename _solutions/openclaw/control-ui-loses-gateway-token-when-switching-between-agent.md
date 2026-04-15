---
layout: solution
title: "Control UI loses gateway token when switching between agent sessions"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/43037
description: "The Control UI (webchat) successfully authenticates with the gateway token for the default (main) agent, but when switching to a different agent session"
---

# Control UI loses gateway token when switching between agent sessions

## 증상
The Control UI (webchat) successfully authenticates with the gateway token for the default (main) agent, but when switching to a different agent session (e.g. `navi-coder`), the WebSocket reconnects **without sending the stored token**, resulting in:

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Set `gateway.auth.mode` to `"none"` in `~/.openclaw/openclaw.json` (safe when gateway bind is `loopback`):

```json
"gateway": {
  "auth": {
    "mode": "none"
  }
}
```

Then restart the gateway (`launchctl unload/load` the plist or `openclaw gateway install --force`).

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/43037
