---
layout: solution
title: "Discord channel plugin missing stopAccount — health-monitor hot restart leaves stale WS, enters death loop"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/50913
---

# Discord channel plugin missing stopAccount — health-monitor hot restart leaves stale WS, enters death loop

## 증상
When the Discord WebSocket disconnects (close code 1006), the health-monitor detects `stale-socket` or `disconnected` and calls `stopChannel` → `startChannel` to restart the Discord provider. However, the Discord channel plugin **does not implement `stopAccount`**, so the old WebSocket connection is never properly closed.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Full process restart (`SIGUSR1` or `openclaw gateway restart`) clears all in-memory state and allows clean reconnection, but may require waiting for Discord to expire the old session (~1-5 minutes).

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/50913
