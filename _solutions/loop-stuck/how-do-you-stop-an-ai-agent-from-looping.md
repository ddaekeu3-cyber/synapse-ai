---
layout: solution
title: "How do you stop an AI agent from looping?"
category: loop-stuck
source: Reddit r/ClaudeAI https://reddit.com/r/aiagents/comments/1rauxi9/how_do_you_stop
---

# How do you stop an AI agent from looping?

## 증상
Hi, I'm the founder of [Arlo](http://arlocua.com/), a desktop automation agent and Arlo's main agent basically runs in a loop:

Collect context, ask the LLM for a plan, execute tools, and repeat until finish is true or loop detection triggers.

The planner has two heuristics:

* Duplicate-chain detection, which checks if the same sequence of tools is planned again
* No-progress detection, which ch

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
Reddit r/ClaudeAI https://reddit.com/r/aiagents/comments/1rauxi9/how_do_you_stop_an_ai_agent_from_looping/
