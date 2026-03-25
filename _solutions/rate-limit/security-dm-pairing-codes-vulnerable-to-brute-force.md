---
layout: solution
title: "Security: DM pairing codes vulnerable to brute-force"
category: rate-limit
source: https://github.com/openclaw/openclaw/issues/16458
---

# Security: DM pairing codes vulnerable to brute-force

## 증상
The DM pairing code system uses 8-character codes from a 32-character alphabet (`ABCDEFGHJKLMNPQRSTUVWXYZ23456789`), yielding ~40 bits of entropy. Combined with the absence of rate limiting on pairing code validation attempts, this creates a brute-force vulnerability.

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
https://github.com/openclaw/openclaw/issues/16458
