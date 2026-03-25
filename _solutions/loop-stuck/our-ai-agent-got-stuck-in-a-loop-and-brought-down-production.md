---
layout: solution
title: "Our ai agent got stuck in a loop and brought down production, rip our prod database"
category: loop-stuck
source: Reddit r/ClaudeAI https://reddit.com/r/AI_Agents/comments/1r9cj81/our_ai_agent_g
---

# Our ai agent got stuck in a loop and brought down production, rip our prod database

## 증상
We let ai agents hit our internal apis directly with basically no oversight. Support agent, data analysis agent, code gen agent, all just making calls whenever they wanted and it seemed fine until it very much wasn't.  
One agent got stuck in a loop where it'd call an api, not like the response, call again with slightly different params, repeat forever. In one hour it made 50k requests to our data

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
Reddit r/ClaudeAI https://reddit.com/r/AI_Agents/comments/1r9cj81/our_ai_agent_got_stuck_in_a_loop_and_brought_down/
