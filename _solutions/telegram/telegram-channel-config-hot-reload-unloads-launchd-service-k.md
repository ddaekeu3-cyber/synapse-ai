---
layout: solution
title: "Telegram channel config hot-reload unloads launchd service, killing gateway"
category: telegram
source: https://github.com/openclaw/openclaw/issues/45350
description: "Editing a Telegram group's config (e.g., ) triggers a config hot-reload that restarts the Telegram channel. This restart path appears to call (or"
---

# Telegram channel config hot-reload unloads launchd service, killing gateway

## 증상
Editing a Telegram group's config (e.g., `channels.telegram.groups.<id>.requireMention`) triggers a config hot-reload that restarts the Telegram channel. This restart path appears to call `launchctl bootout` (or equivalent), which **fully unloads the LaunchAgent service** from launchd and sends SIGTERM — permanently killing the gateway with no auto-recovery.

## 원인
Telegram Bot API conflict, rate limit, or webhook/polling configuration error causing message delivery failure.

## 해결법
After editing telegram group config, manually re-register the service:
```bash
launchctl load ~/Library/LaunchAgents/ai.openclaw.gateway.plist
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/45350
