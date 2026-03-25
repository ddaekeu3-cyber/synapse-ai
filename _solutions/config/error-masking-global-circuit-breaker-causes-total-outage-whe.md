---
layout: solution
title: "Error masking + global circuit breaker causes total outage when any single provider fails"
category: config
source: https://github.com/openclaw/openclaw/issues/48988
---

# Error masking + global circuit breaker causes total outage when any single provider fails

## 증상
- **OpenClaw Version:** 2026.3.8

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
config and restart
- **User experience**: All Telegram bots unresponsive, "rate limit reached" on every message
- **Workaround**: Only include verified-working models in the fallback chain. Exclude any model that might fail.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/48988
