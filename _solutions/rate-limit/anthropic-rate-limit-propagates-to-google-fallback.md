---
layout: solution
title: "Anthropic rate limit cooldown blocks independent Google fallback"
category: rate-limit
source: https://github.com/openclaw/openclaw/issues/53233
---

# Anthropic rate limit cooldown blocks independent Google fallback

## 증상
When Anthropic provider hits rate limit, the cooldown period incorrectly propagates to the independent Google Vertex fallback provider. Both providers become unavailable.

## 원인
Rate limit cooldown is applied globally instead of per-provider. All providers share the same cooldown state.

## 해결법
### Provider 간 Rate limit 전파 해결
1. Provider별 독립적 rate limit 설정 확인
2. `perProviderCooldown: true` 설정 (지원 시)
3. 임시 해결: Anthropic과 Google을 별도 인스턴스로 분리
4. Gateway 설정에서 fallback chain 대신 독립적 provider 라우팅 사용

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/53233
