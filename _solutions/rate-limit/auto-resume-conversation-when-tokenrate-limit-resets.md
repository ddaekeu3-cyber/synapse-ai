---
layout: solution
title: "Auto-resume conversation when token/rate limit resets"
category: rate-limit
source: https://github.com/anthropics/claude-code/issues/38263
---

# Auto-resume conversation when token/rate limit resets

## 증상
When a conversation is interrupted because the token or rate limit is exhausted, Claude Code currently just stops. The user has to manually resume once the limit resets.

## 원인
보고된 버그/문제. 카테고리: rate-limit.

## 해결법
1. 지수 백오프: 1초→2초→4초→8초 재시도 간격
2. 지터 추가: 랜덤 지터로 thundering herd 방지
3. 캐싱: 동일 요청 결과 캐싱
4. Retry-After 헤더 준수
5. 배치 처리: 개별 요청을 배치로 묶기

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38263
