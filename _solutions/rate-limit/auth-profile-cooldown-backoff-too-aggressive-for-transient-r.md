---
layout: solution
title: "Auth profile cooldown backoff too aggressive for transient rate limits"
category: rate-limit
source: https://github.com/openclaw/openclaw/issues/11352
description: "When a single transient HTTP 429 rate limit error occurs (e.g., per-minute burst limit), the auth profile cooldown system enters an exponential backoff"
---

# Auth profile cooldown backoff too aggressive for transient rate limits

## 증상
When a single transient HTTP 429 rate limit error occurs (e.g., per-minute burst limit), the auth profile cooldown system enters an exponential backoff spiral that can lock out the provider for 20+ minutes, even when the actual API quota is barely used (e.g., 10% of Max plan).

## 원인
API rate limit reached — too many requests within the allowed time window triggered the provider's throttling mechanism. 카테고리: rate-limit.

## 해결법
Manually edit `auth-profiles.json` to clear the `usageStats` for the affected profile, then SIGUSR1 the gateway.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/11352
