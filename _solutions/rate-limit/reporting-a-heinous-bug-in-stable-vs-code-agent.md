---
layout: solution
title: "Reporting a heinous bug in stable VS Code agent"
category: rate-limit
source: Reddit r/ClaudeAI https://reddit.com/r/GithubCopilot/comments/1rxu4ak/reporting_
---

# Reporting a heinous bug in stable VS Code agent

## 증상
I was using the gpt-5.4 mini model and it was working properly. 

**It was in explore subagent screen, suddenly the status showing what the agent is doing was going at 10x regular speed as if the agent is doing 10 tool call in no time. It looked like a 10x replay of regular speed.**

**I was rate limited within a minute of that.**

I believe this to be a server side or a client side bug, I don't k

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
Reddit r/ClaudeAI https://reddit.com/r/GithubCopilot/comments/1rxu4ak/reporting_a_heinous_bug_in_stable_vs_code_agent/
