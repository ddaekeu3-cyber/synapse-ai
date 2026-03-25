---
layout: solution
title: "The $30K agent loop - implementing financial circuit breakers"
category: loop-stuck
source: Reddit r/ClaudeAI https://reddit.com/r/AI_Agents/comments/1pqsvrs/the_30k_agent_
---

# The $30K agent loop - implementing financial circuit breakers

## 증상
Talked to a company last week that had an agent enter a logic loop and rack up $30K in LLM API calls before anyone noticed.....ouch

The agent was stuck in a reasoning loop:

1. Call GPT-4 to analyze data
2. Get response that says "need more context"
3. Call GPT-4 again with same data
4. Repeat 10,000 times

Standard observability tools caught it eventually, but the damage was done. Invoice showed

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
Reddit r/ClaudeAI https://reddit.com/r/AI_Agents/comments/1pqsvrs/the_30k_agent_loop_implementing_financial_circuit/
