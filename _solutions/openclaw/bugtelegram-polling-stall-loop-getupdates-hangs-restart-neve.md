---
layout: solution
title: "bug(telegram): Polling stall loop — getUpdates hangs, restart never recovers (since 2026.3.7)"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/47458
---

# bug(telegram): Polling stall loop — getUpdates hangs, restart never recovers (since 2026.3.7)

## 증상
Telegram long-polling enters an unrecoverable stall loop after upgrading to `2026.3.7`. The poller detects no `getUpdates` for ~15 minutes, attempts restart, times out after 15s, force-restarts, and the cycle repeats indefinitely. This also causes `sendMessage` failures (network request errors).

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Gateway restart temporarily clears the stall, but it resumes within hours.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/47458
