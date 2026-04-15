---
layout: solution
title: "msteams: typing indicator hits 429 rate limit during long agent runs"
category: rate-limit
source: https://github.com/openclaw/openclaw/issues/53184
description: "During long-running agent sessions (browser automation, complex tool chains), the MS Teams typing indicator fires in a tight loop without any throttle."
---

# msteams: typing indicator hits 429 rate limit during long agent runs

## 증상
During long-running agent sessions (browser automation, complex tool chains), the MS Teams typing indicator fires in a tight loop without any throttle. This quickly hits Teams' API rate limit (HTTP 429 "API calls quota exceeded"), then falls back to proactive messaging which also loops, creating an escalating spiral.

## 원인
API rate limit reached — too many requests within the allowed time window triggered the provider's throttling mechanism. 카테고리: rate-limit.

## 해결법
We patched `reply-dispatcher.ts` with three changes:

1. **Throttle:** typing indicator fires at most once every 3 seconds (`TYPING_MIN_INTERVAL_MS = 3000`)
2. **429 backoff:** after a 429 error, typing is suppressed for 30 seconds (`TYPING_BACKOFF_MS = 30000`)
3. **Stop flag:** `typingStopped` is set to `true` when `markDispatchIdle()` is called, preventing typing from continuing after the response is sent

The fix is minimal and only touches the typing path. No other behavior is changed.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/53184
