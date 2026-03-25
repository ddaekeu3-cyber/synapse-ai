---
layout: solution
title: "OpenAI just pulled the biggest bait-and-switch in AI history and I'm done."
category: rate-limit
source: Reddit r/ClaudeAI https://reddit.com/r/ChatGPT/comments/1mkobei/openai_just_pull
---

# OpenAI just pulled the biggest bait-and-switch in AI history and I'm done.

## 증상
I woke up this morning to find that OpenAI deleted 8 models overnight.

No warning. No choice. No "legacy option."

**They just... deleted them.**

4o? Gone. o3? Gone. o3-Pro? Gone. 4.5? Gone.

Everything that made ChatGPT actually useful for my workflow - **deleted.**

Here's what they replaced it with:

❌ GPT-5 gives shorter, more corporate responses ❌ Hits rate limits faster (pushing Pro upgrad

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
Reddit r/ClaudeAI https://reddit.com/r/ChatGPT/comments/1mkobei/openai_just_pulled_the_biggest_baitandswitch_in/
