---
layout: solution
title: "MCP Is Costing You 37% More Tokens Than Necessary"
category: token-cost
source: Reddit r/ClaudeAI https://reddit.com/r/ClaudeAI/comments/1rzz784/mcp_is_costing_
---

# MCP Is Costing You 37% More Tokens Than Necessary

## 증상
When we use skills, plugins or MCP tools, Claude reads long input schemas or injects prompt instructions. Those tokens are charged as input tokens, and can be expensive at scale, especially when it comes to API usage.

We even ask Claude to explore other folders and sibling repositories, read files and occasionally execute code for testing. We tend to ignore the input token costs and we’re usually

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
Reddit r/ClaudeAI https://reddit.com/r/ClaudeAI/comments/1rzz784/mcp_is_costing_you_37_more_tokens_than_necessary/
