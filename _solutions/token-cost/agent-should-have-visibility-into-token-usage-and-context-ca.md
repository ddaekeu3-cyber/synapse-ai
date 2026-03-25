---
layout: solution
title: "Agent should have visibility into token usage and context cap proximity"
category: token-cost
source: https://github.com/anthropics/claude-code/issues/34013
---

# Agent should have visibility into token usage and context cap proximity

## 증상
In agentic sessions, Claude has no visibility into current token usage, remaining context, or billing cap proximity. This is a meaningful gap for long-running tasks — the agent can't warn you when you're running low, can't self-manage session length, and can't answer basic questions like \"how much of my budget did this use?\"

## 원인
보고된 버그/문제. 카테고리: token-cost.

## 해결법
1. 모델 선택 최적화: 단순 작업은 Haiku, 복잡한 작업만 Opus 사용
2. 프롬프트 캐싱 활성화: 반복 시스템 프롬프트 캐싱으로 90% 절감
3. 컨텍스트 최소화: 필요한 정보만 포함
4. 에러 루프 방지: 3회 실패 시 다른 접근법으로 전환
5. 토큰 사용량 모니터링 대시보드 확인

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/34013
