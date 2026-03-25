---
layout: solution
title: "How much does LLM inference actually cost per million tokens?"
category: token-cost
source: Reddit r/ClaudeAI https://reddit.com/r/costlyinfra/comments/1rmho66/how_much_doe
---

# How much does LLM inference actually cost per million tokens?

## 증상
Running LLMs is powerful — but the economics are often misunderstood.

Many teams think about **model quality**, but not enough about **cost per million tokens**.

Here’s a rough comparison using public pricing (rounded for simplicity).

# API Model Costs (approx)

|Model|Cost per 1M Input Tokens|Cost per 1M Output Tokens|
|:-|:-|:-|
|GPT-4o|\~$5|\~$15|
|Claude 3.5 Sonnet|\~$3|\~$15|
|Claude Haiku

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
Reddit r/ClaudeAI https://reddit.com/r/costlyinfra/comments/1rmho66/how_much_does_llm_inference_actually_cost_per/
