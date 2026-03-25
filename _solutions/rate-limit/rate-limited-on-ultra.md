---
layout: solution
title: "Rate limited on Ultra!!"
category: rate-limit
source: Reddit r/ClaudeAI https://reddit.com/r/google_antigravity/comments/1r2whan/rate_
---

# Rate limited on Ultra!!

## 증상
https://preview.redd.it/80dm9x5313jg1.png?width=884&amp;format=png&amp;auto=webp&amp;s=e6cf68910e98d6b53799f5506de720acc812673c

Complete bait-and-switch, after saying that there were no weekly rate limits on Ultra, I've discovered that there are now rate limits. Unreal!! Requesting a refund now, and I'll keep you all posted on how that goes.

Please for your sake, with the continuous change of go

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
Reddit r/ClaudeAI https://reddit.com/r/google_antigravity/comments/1r2whan/rate_limited_on_ultra/
