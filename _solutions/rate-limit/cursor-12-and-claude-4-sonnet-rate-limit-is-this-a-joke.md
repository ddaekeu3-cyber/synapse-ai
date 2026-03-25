---
layout: solution
title: "Cursor 1.2 and Claude 4 Sonnet Rate Limit – Is This a Joke?"
category: rate-limit
source: Reddit r/ClaudeAI https://reddit.com/r/cursor/comments/1lqvl21/cursor_12_and_cla
---

# Cursor 1.2 and Claude 4 Sonnet Rate Limit – Is This a Joke?

## 증상
I’ve been using Cursor for a few months now, and honestly, I’m at my wit’s end. I just updated to version 1.2, and after **only three prompts** with Claude 4 Sonnet, I’m hit with the rate limit window. Three prompts! And suddenly, I can’t code with an AI agent anymore. This is beyond frustrating - I’ve paid $20 a month for the past four months, and this is what I get? It feels like a scam at this 

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
Reddit r/ClaudeAI https://reddit.com/r/cursor/comments/1lqvl21/cursor_12_and_claude_4_sonnet_rate_limit_is_this/
