---
layout: solution
title: "Cron job timeout aborts entire model fallback chain via shared AbortController"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/37505
---

# Cron job timeout aborts entire model fallback chain via shared AbortController

## 증상
When a cron job's timeout fires, the fallback model chain never executes. The abort signal from the cron timeout is shared with all fallback provider attempts, causing them to fail instantly (~100-200ms) without making a network request.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
We're currently mitigating by:
- Staggering concurrent cron jobs to reduce rate-limit pressure on the primary provider
- Setting generous timeouts to give the primary model enough time
- Splitting complex jobs into smaller units

But the fallback chain is effectively non-functional for all cron jobs.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/37505
