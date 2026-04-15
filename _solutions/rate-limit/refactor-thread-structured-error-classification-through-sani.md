---
layout: solution
title: "Refactor: thread structured error classification through sanitizer pipeline"
category: rate-limit
source: https://github.com/openclaw/openclaw/issues/16521
description: "in uses regex and substring matching on error text to classify errors (billing, rate-limit, timeout, etc.). This causes false positives when non-billing"
---

# Refactor: thread structured error classification through sanitizer pipeline

## 증상
`sanitizeUserFacingText()` in `src/agents/pi-embedded-helpers/errors.ts` uses regex and substring matching on error text to classify errors (billing, rate-limit, timeout, etc.). This causes false positives when non-billing errors happen to contain billing-adjacent keywords.

## 원인
API rate limit reached — too many requests within the allowed time window triggered the provider's throttling mechanism. 카테고리: rate-limit.

## 해결법
this with better heuristics (#12777, #12702, #12226, #8661, #12720, #11680, #12052, #13318, #13467, #15109). Each one either:
- Adds another guard that can still be fooled by certain text patterns
- Fixes one false-positive path while leaving others open
- Places the fix in the wrong scope or function

The merged `errorContext` guard (#12988) helps by gating rewrites behind `{ errorContext: true }`, but the fundamental problem remains: **error type information that exists at the source (provider adapters know the HTTP status code and its origin) is discarded before reaching the sanitizer.**

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/16521
