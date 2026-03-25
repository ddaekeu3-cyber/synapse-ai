---
layout: solution
title: "409 Conflict loop when polling multiple Telegram bot accounts simultaneously"
category: telegram
source: https://github.com/openclaw/openclaw/issues/43569
---

# 409 Conflict loop when polling multiple Telegram bot accounts simultaneously

## 증상
When running multiple Telegram bot accounts under a single OpenClaw gateway instance, frequent `409 Conflict` errors occur on `getUpdates` calls, causing a restart loop.

## 원인
보고된 버그/문제. 카테고리: telegram.

## 해결법
Setting `channelHealthCheckMinutes: 0` in config disables the health monitor and reduces 409 frequency significantly, but does not fully eliminate the issue during config hot-reloads.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/43569
