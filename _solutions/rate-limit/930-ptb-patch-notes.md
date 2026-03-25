---
layout: solution
title: "9.3.0 | PTB Patch Notes"
category: rate-limit
source: Reddit r/ClaudeAI https://reddit.com/r/deadbydaylight/comments/1ooa5k4/930_ptb_p
---

# 9.3.0 | PTB Patch Notes

## 증상
https://preview.redd.it/o3rdhdsya9zf1.jpg?width=1000&amp;format=pjpg&amp;auto=webp&amp;s=2dd53d006d2f11053bd25c788e5804291dc57ab5

# Important

* All characters you have unlocked via Steam will be unlocked on the PTB, including any new character(s) introduced in this update.
   * *If DLC content has been unlocked via a platform other than Steam (including the in-game Store), this will not be autom

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
Reddit r/ClaudeAI https://reddit.com/r/deadbydaylight/comments/1ooa5k4/930_ptb_patch_notes/
