---
layout: solution
title: "Is the rate limit basically broken for everyone else right now too?"
category: rate-limit
source: Reddit r/ClaudeAI https://reddit.com/r/grok/comments/1rqfo7l/is_the_rate_limit_b
---

# Is the rate limit basically broken for everyone else right now too?

## 증상
I can't make more than 3 6 second 480p videos before hitting the rate limit, normally it's like 30-50 per day... Hoping there's some issue here and they haven't just absolutely throttled it to oblivion or I'm in a bit of trouble...

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
Reddit r/ClaudeAI https://reddit.com/r/grok/comments/1rqfo7l/is_the_rate_limit_basically_broken_for_everyone/
