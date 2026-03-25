---
layout: solution
title: "Discord WebSocket stale-socket restart loop on v2026.3.13 — multi-account setup, VPS/Linux"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/47535
---

# Discord WebSocket stale-socket restart loop on v2026.3.13 — multi-account setup, VPS/Linux

## 증상
- **OpenClaw:** 2026.3.13 (61d171a)

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
.

## Log Pattern

```
11:31:07 [discord] WebSocket connection closed with code 1005
11:40:43 [health-monitor] [discord:jarvis-family] health-monitor: restarting (reason: stale-socket)
11:40:47 [discord] discord startup [jarvis-family] gateway-debug: WebSocket connection opened
11:40:47 [discord] logged in to discord as <bot_id> (J.A.R.V.I.S.)
12:05:43 [health-monitor] [discord:jarvis-gaming] health-monitor: restarting (reason: stale-socket)
12:05:49 [discord] discord startup [jarvis-gaming] gateway-debug: WebSocket connection opened
12:05:50 [discord] logged in to discord as <bot_id> (J.A.R.V

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/47535
