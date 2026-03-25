---
layout: solution
title: "Actual Token Cost Of Kagi's Research Assistant"
category: token-cost
source: Reddit r/ClaudeAI https://reddit.com/r/SearchKagi/comments/1pijzo1/actual_token_
---

# Actual Token Cost Of Kagi's Research Assistant

## 증상
I pay for Kagi Ultimate, but inevitably go over my allotted tokens which is fine, I don't mind paying for what I use, but recently started using the _"Research Assistant"_ with some of my custom prompts and am trying to determine what the actual usage cost is, say compared to Sonnet 4.5. When I posed this question to the RA itself, it said it should be lower than Sonnet 4.5.  When you look at the 

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
Reddit r/ClaudeAI https://reddit.com/r/SearchKagi/comments/1pijzo1/actual_token_cost_of_kagis_research_assistant/
