---
layout: solution
title: "Browser tool timeout blocks Telegram polling loop, causing cascading gateway restarts"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/43181
---

# Browser tool timeout blocks Telegram polling loop, causing cascading gateway restarts

## 증상
Browser tool timeouts (locator.click, page navigation) block the Node.js event loop, stalling the Telegram long-polling loop (`getUpdates`). This causes a cascading failure: polling stall → health monitor SIGTERM → gateway restart → cron jobs re-trigger browser → repeat.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
We implemented two mitigations:
1. **Reduced cron frequency** for browser-heavy jobs (15min → 3hr)
2. **Enhanced watchdog** (`telegram-watchdog.py`) that detects polling stalls via log scanning and restarts with exponential backoff to prevent restart loops

Neither is a real fix — they reduce frequency and recover faster, but the underlying event loop blocking remains.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/43181
