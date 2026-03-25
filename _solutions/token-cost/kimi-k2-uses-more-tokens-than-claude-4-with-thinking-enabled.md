---
layout: solution
title: "Kimi K2 uses more tokens than Claude 4 with thinking enabled. Think of it as a reasoning model when it comes to cost and latency considerations"
category: token-cost
source: Reddit r/ClaudeAI https://reddit.com/r/LLMDevs/comments/1m7aqyf/kimi_k2_uses_mor
---

# Kimi K2 uses more tokens than Claude 4 with thinking enabled. Think of it as a reasoning model when it comes to cost and latency considerations

## 증상
When considering cost, it is important to consider not just cost per token, but how many tokens are used to get to an answer. In the Kimi K2 paper, they compare to non-reasoning models. Despite not being a "reasoning" model, it uses more tokens than claude 4 opus and claude 4 sonnet with thinking enabled.

It is still cheaper to complete a task than those 2 models because of the large difference i

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
Reddit r/ClaudeAI https://reddit.com/r/LLMDevs/comments/1m7aqyf/kimi_k2_uses_more_tokens_than_claude_4_with/
