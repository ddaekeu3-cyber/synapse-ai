---
layout: solution
title: "[Linux/systemd] Gateway restart from within agent session causes SIGTERM crash loop"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/32348
description: "On Linux with systemd, triggering a gateway restart from within a running agent session causes the session to be killed via SIGTERM, often leading to a"
---

# [Linux/systemd] Gateway restart from within agent session causes SIGTERM crash loop

## 증상
On Linux with systemd, triggering a gateway restart from within a running agent session causes the session to be killed via SIGTERM, often leading to a crash loop.

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
- Use `kill -HUP <gateway-pid>` for config reloads (non-disruptive)
- Only do full restarts manually from outside the gateway session
- Set `KillMode=control-group` + `TimeoutStopSec=15` in service file to prevent port conflicts on restart

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/32348
