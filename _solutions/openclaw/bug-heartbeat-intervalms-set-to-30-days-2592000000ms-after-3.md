---
layout: solution
title: "Bug: heartbeat intervalMs set to ~30 days (2,592,000,000ms) after 3.8 update — Node.js 32-bit overflow causes 500MB log spam"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/41240
---

# Bug: heartbeat intervalMs set to ~30 days (2,592,000,000ms) after 3.8 update — Node.js 32-bit overflow causes 500MB log spam

## 증상
After updating to OpenClaw 3.8 and restarting, the heartbeat subsystem sets `intervalMs` to **2,592,000,000ms** (~30 days) regardless of the configured value. Node.js cannot fit this into a 32-bit signed integer, clamps the timeout to 1ms, and logs a `TimeoutOverflowWarning` error on every iteration — generating 500MB+ of log spam until the file size cap is hit.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
None found. Config override path `heartbeat` schema not found via `config.schema.lookup`. The 30-day value is not coming from user config.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/41240
