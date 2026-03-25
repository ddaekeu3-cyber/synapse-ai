---
layout: solution
title: "Feature: Graceful gateway self-restart — prevent zombie processes when agent calls `openclaw gateway restart`"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/47142
---

# Feature: Graceful gateway self-restart — prevent zombie processes when agent calls `openclaw gateway restart`

## 증상
When an OpenClaw agent (or any script running inside the gateway process) calls `openclaw gateway restart`, the command spawns a **new gateway process without stopping the old one**. This creates zombie gateway processes that compete for shared resources.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
We built `agentctl gateway graceful-restart` which:
1. Sends a WhatsApp notification before restart
2. Writes a session-handoff file
3. Kills zombie gateway processes (keeps only the systemd service PID)
4. Writes a detached restart script (`setsid nohup ... &`)
5. The detached script waits 10s, then runs `systemctl --user restart openclaw-gateway`
6. Sends WhatsApp notification after restart

Plus a zombie watchdog cron that runs hourly to detect and clean up any orphaned gateway processes.

This works but should be a core OpenClaw feature.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/47142
