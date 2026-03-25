---
layout: solution
title: "Agent stuck in endless “plan → approve → refine plan” loop, never executes (fresh VPS install, twice now)"
category: config
source: Reddit r/ClaudeAI https://reddit.com/r/openclaw/comments/1rocr4m/agent_stuck_in_
---

# Agent stuck in endless “plan → approve → refine plan” loop, never executes (fresh VPS install, twice now)

## 증상
Hey all, looking for help debugging a really frustrating behavior with my OpenClaw setup.

I’m on my second clean setup of OpenClaw on a VPS because I assumed my first install was misconfigured. I blew everything away, spun up a new VPS, reinstalled OpenClaw from scratch, reconnected my channels, and I’m still hitting the exact same issue.

**What’s happening**

When I ask my main OpenClaw agent t

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
1. 공식 문서 참조: 최신 설정 가이드 확인
2. 환경변수 확인: 필수 변수 설정 확인
3. 버전 호환성: 설정 포맷이 현재 버전과 맞는지 확인
4. 로그 확인: 시작 로그에서 설정 관련 경고 확인
5. 최소 설정으로 시작해서 하나씩 추가

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
Reddit r/ClaudeAI https://reddit.com/r/openclaw/comments/1rocr4m/agent_stuck_in_endless_plan_approve_refine_plan/
