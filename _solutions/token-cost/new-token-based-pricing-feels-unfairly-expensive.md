---
layout: solution
title: "new token-based pricing feels unfairly expensive"
category: token-cost
source: Reddit r/ClaudeAI https://reddit.com/r/Trae_ai/comments/1rdibwd/new_tokenbased_p
---

# new token-based pricing feels unfairly expensive

## 증상
I’ve been using TRAE for a while, and I’m frustrated with the recent change in the billing model.

Previously, pricing was based on the **number of requests**, which felt predictable and manageable. But now it’s shifted to **token usage**, and the costs have jumped significantly.

For example, my account page shows usage that doesn’t even sum up to what’s being charged which is around **0.41** in 

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
Reddit r/ClaudeAI https://reddit.com/r/Trae_ai/comments/1rdibwd/new_tokenbased_pricing_feels_unfairly_expensive/
