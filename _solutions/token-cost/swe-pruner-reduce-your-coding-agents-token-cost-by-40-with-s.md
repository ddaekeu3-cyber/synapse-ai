---
layout: solution
title: "SWE-Pruner: Reduce your Coding Agent's token cost by 40% with 'Semantic Highlighting' (Open Source)"
category: token-cost
source: Reddit r/ClaudeAI https://reddit.com/r/ClaudeAI/comments/1qvdsdm/swepruner_reduc
---

# SWE-Pruner: Reduce your Coding Agent's token cost by 40% with "Semantic Highlighting" (Open Source)

## 증상
Hey everyone,

I've been working on optimizing long-context interactions for coding agents and wanted to share SWE-Pruner, an open-source tool designed to significantly reduce token usage (and cost!) for agents like Claude Code or OpenHands without sacrificing performance\*\*(Especially for long code files)\*

**The Problem:**

We all know that dumping entire files into an LLM's context window is 

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
Reddit r/ClaudeAI https://reddit.com/r/ClaudeAI/comments/1qvdsdm/swepruner_reduce_your_coding_agents_token_cost_by/
