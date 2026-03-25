---
layout: solution
title: "Mid-conversation rate limits bypass model fallback (retry_limit return instead of FailoverError throw)"
category: rate-limit
source: https://github.com/openclaw/openclaw/issues/50421
---

# Mid-conversation rate limits bypass model fallback (retry_limit return instead of FailoverError throw)

## 증상
When a model succeeds on turn N but gets rate-limited on turn N+1 within the same embedded agent run, the model fallback mechanism is never triggered. The run is killed with a user-facing error instead of falling back to the next model in the chain.

## 원인
보고된 버그/문제. 카테고리: rate-limit.

## 해결법
The retry-limit exit at line 887-917 should throw a `FailoverError` (instead of returning an error result) when:
- `fallbackConfigured` is true, AND
- the last failure reason is failover-worthy (rate_limit, overloaded, auth, billing)

This allows `runFallbackCandidate()` to catch it and the model fallback loop to try the next candidate.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/50421
