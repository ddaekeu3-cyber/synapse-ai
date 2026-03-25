---
layout: solution
title: "I gave 12 LLMs $2,000 and a food truck. Only 4 survived."
category: token-cost
source: Reddit r/ClaudeAI https://reddit.com/r/LocalLLaMA/comments/1r77swh/i_gave_12_llm
---

# I gave 12 LLMs $2,000 and a food truck. Only 4 survived.

## 증상
Built a business sim where AI agents run a food truck for 30 days — location, menu, pricing, staff, inventory. Same scenario for all models.
    
Opus made $49K. GPT-5.2 $28K. 8 went bankrupt. Every model that took a loan went bankrupt (8/8).
   
There's also a playable mode — same simulation, same 34 tools, same leaderboard. You either survive 30 days or go bankrupt, get a result card and land on

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
Reddit r/ClaudeAI https://reddit.com/r/LocalLLaMA/comments/1r77swh/i_gave_12_llms_2000_and_a_food_truck_only_4/
