---
layout: solution
title: "New rate limits for SuperGrok?"
category: rate-limit
source: Reddit r/ClaudeAI https://reddit.com/r/grok/comments/1rw9460/new_rate_limits_for
---

# New rate limits for SuperGrok?

## 증상
It looks like the new limit is 100 generations per 8 hours.  
I generated videos at 4:52 AM.  
Next I was able to generate videos at 12:54 PM (8 hours gap, instead of 2 hours gap).  
Now it's 3:05 PM (more than 2 hours after) and I can't generate videos.

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
Reddit r/ClaudeAI https://reddit.com/r/grok/comments/1rw9460/new_rate_limits_for_supergrok/
