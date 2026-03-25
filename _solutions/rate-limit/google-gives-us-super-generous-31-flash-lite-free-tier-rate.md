---
layout: solution
title: "Google gives us super generous 3.1 Flash Lite FREE tier rate limits"
category: rate-limit
source: Reddit r/ClaudeAI https://reddit.com/r/Bard/comments/1rnofb4/google_gives_us_sup
---

# Google gives us super generous 3.1 Flash Lite FREE tier rate limits

## 증상
Guys, am i dreaming? 500 requests per day? No way this is real, this has to be a bug, because that's crazy good for free tier

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
Reddit r/ClaudeAI https://reddit.com/r/Bard/comments/1rnofb4/google_gives_us_super_generous_31_flash_lite_free/
