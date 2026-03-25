---
layout: solution
title: "Gateway freezes after nested subagent activity, stops all Telegram polling"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/53745
---

# Gateway freezes after nested subagent activity, stops all Telegram polling

## 증상
After a coordinator agent calls subagents via `sessions_send`, the gateway process stays alive (HTTP 200 on healthcheck, PID running) but completely stops processing Telegram updates. Log output stops entirely — no incoming messages, no `health-monitor`, no `sendMessage`, nothing. All Telegram bots go silent simultaneously.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Kill and restart the gateway process:
```bash
launchctl kickstart -k gui/$(id -u)/ai.openclaw.gateway
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/53745
