---
layout: solution
title: "Context caching almost always does not work"
category: openclaw
source: https://github.com/openclaw/openclaw/issues/51873
---

# Context caching almost always does not work

## 증상
Context caching feature is enabled but cache hit rate is near 0%. Every request is processed from scratch. No cost savings from caching.

## 원인
Cache key generation is too strict. Minor variations in context (timestamps, dynamic content) cause cache misses. Cache TTL may be too short.

## 해결법
### 컨텍스트 캐싱 미작동 해결
1. 캐시 키에서 동적 요소 제외: 타임스탬프, 랜덤값 등 제거
2. 시스템 프롬프트를 고정 문자열로 유지 (변수 치환 최소화)
3. cache TTL 증가: 기본값에서 최소 1시간으로 설정
4. 캐시 히트율 모니터링: `openclaw cache stats` (지원 시)
5. Anthropic API의 경우 cache_control 블록을 명시적으로 설정

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/51873
