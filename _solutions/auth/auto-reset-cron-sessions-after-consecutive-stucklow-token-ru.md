---
layout: solution
title: "Auto-reset cron sessions after consecutive stuck/low-token runs"
category: auth
source: https://github.com/openclaw/openclaw/issues/20835
---

# Auto-reset cron sessions after consecutive stuck/low-token runs

## 증상
Cron jobs reuse the same isolated session across runs. When a run hits a transient error (e.g. Anthropic OAuth 401, rate limit, exec failure), the error context gets baked into the session. Subsequent runs then pattern-match stale responses instead of actually calling tools — the job appears healthy (status: ok, consecutiveErrors: 0) but is silently broken.

## 원인
보고된 버그/문제. 카테고리: auth.

## 해결법
Currently using an external watchdog cron job that monitors run histories and recreates stuck jobs. This works but is fragile and wasteful.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/20835
