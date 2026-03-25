---
layout: solution
title: "Ethan Ding: (technically correct) argument 'LLM cost per tokens gets cheaper 1 OOM/year' is wrong because frontier model cost stays the same, &amp; with the rise of inference scaling SOTA models are actually becoming more expensive due to increased token consumption"
category: token-cost
source: Reddit r/ClaudeAI https://reddit.com/r/mlscaling/comments/1mulev4/ethan_ding_tec
---

# Ethan Ding: (technically correct) argument "LLM cost per tokens gets cheaper 1 OOM/year" is wrong because frontier model cost stays the same, &amp; with the rise of inference scaling SOTA models are actually becoming more expensive due to increased token consumption

## 증상
Also includes a good discussion of flat-fee business model being unsustainable due to power users abusing the quotas.

If you prefer watching videos to reading texts, Theo t3dotgg Browne has a decent discussion of this article with his own experiences running T3 Chat:
https://www.youtube.com/watch?v=2tNp2vsxEzk

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
Reddit r/ClaudeAI https://reddit.com/r/mlscaling/comments/1mulev4/ethan_ding_technically_correct_argument_llm_cost/
