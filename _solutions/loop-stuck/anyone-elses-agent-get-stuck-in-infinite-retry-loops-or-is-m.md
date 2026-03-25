---
layout: solution
title: "anyone else's agent get stuck in infinite retry loops or is my ReActAgent just broken"
category: loop-stuck
source: Reddit r/ClaudeAI https://reddit.com/r/LangChain/comments/1qxgdkz/anyone_elses_a
---

# anyone else's agent get stuck in infinite retry loops or is my ReActAgent just broken

## 증상
been using LangChain for a few weeks and keep running into this: agent tries a tool → tool fails → agent decides to retry → fails again → retries the exact same input 200+ times until i manually kill it or my API credits die.

last week it cost me $63 because i let it run overnight.

the issue seems to be that AgentExecutor has no memory of previous states in the current execution chain. so if ste

## 원인
보고된 버그/문제. 카테고리: loop-stuck.

## 해결법
1. 최대 재시도 제한: 동일 작업 3-5회 제한
2. 에러 패턴 감지: 같은 에러 반복 시 다른 접근법 전환
3. 타임아웃: 단일 작업 시간 제한 설정
4. 상태 체크포인트: 진행상황 기록으로 반복 방지
5. 에스컬레이션: 실패 시 사람에게 보고

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
Reddit r/ClaudeAI https://reddit.com/r/LangChain/comments/1qxgdkz/anyone_elses_agent_get_stuck_in_infinite_retry/
