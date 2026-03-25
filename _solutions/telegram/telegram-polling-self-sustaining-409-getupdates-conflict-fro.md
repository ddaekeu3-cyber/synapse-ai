---
layout: solution
title: "Telegram polling: self-sustaining 409 getUpdates conflict from probe + health-monitor re-triggering transport"
category: telegram
source: https://github.com/openclaw/openclaw/issues/50064
---

# Telegram polling: self-sustaining 409 getUpdates conflict from probe + health-monitor re-triggering transport

## 증상
The gateway enters a permanent 409 `getUpdates` conflict loop because the Telegram probe client and polling client create competing `getUpdates` connections. Once triggered, the loop is self-sustaining because the long-poll timeout (30s) equals the max retry interval (30s), so each retry's server-side connection overlaps with the next.

## 원인
보고된 버그/문제. 카테고리: telegram.

## 해결법
Runtime patch reducing `fetch.timeout` from 30 to 10 in `createTelegramRunnerOptions()` resolves the self-sustaining loop. An initial 409 may still occur but recovers within one retry cycle.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/50064
