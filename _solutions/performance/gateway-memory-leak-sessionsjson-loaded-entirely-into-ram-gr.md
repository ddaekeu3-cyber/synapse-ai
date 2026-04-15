---
layout: solution
title: "Gateway memory leak: sessions.json loaded entirely into RAM, grows unbounded"
category: performance
source: https://github.com/openclaw/openclaw/issues/51097
description: "Platform: macOS (Darwin arm64, Apple"
---

# Gateway memory leak: sessions.json loaded entirely into RAM, grows unbounded

## 증상
**Platform:** macOS (Darwin arm64, Apple Silicon)

## 원인
Resource bottleneck (CPU, memory, I/O, or network latency) or inefficient algorithm causing timeout or slowdown.

## 해결법
Daily gateway restart via launchd cron when RSS exceeds threshold:
```bash
RSS=$(ps -o rss= -p $(pgrep -f openclaw-gateway))
if [ "$RSS" -gt 700000 ]; then
  launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/ai.openclaw.gateway.plist
  sleep 3
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.openclaw.gateway.plist
fi
```

This works but is clearly a bandaid. The underlying issue needs to be addressed in the gateway session management code.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/51097
