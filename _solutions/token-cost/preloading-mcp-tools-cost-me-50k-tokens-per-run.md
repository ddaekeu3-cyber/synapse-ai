---
layout: solution
title: "Preloading MCP tools cost me ~50k tokens per run"
category: token-cost
source: Reddit r/ClaudeAI https://reddit.com/r/LangChain/comments/1qukgay/preloading_mcp
---

# Preloading MCP tools cost me ~50k tokens per run

## 증상
I ran into something unintuitive while building MCP-based agents using langchain and thought it might be useful to share.

In my setup, the agent had access to a few common MCP tools like fs, linear, GitHub, figma.

I just added them to the agent and forgot and agent used them sparingly.
Even with AugmentCode (AI agent I use) I dont want to switch tools on and off. That actually messes up with pro

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
Reddit r/ClaudeAI https://reddit.com/r/LangChain/comments/1qukgay/preloading_mcp_tools_cost_me_50k_tokens_per_run/
