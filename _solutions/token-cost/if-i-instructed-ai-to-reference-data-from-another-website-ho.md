---
layout: solution
title: "If I instructed AI to reference data from another website, how would that factor into the input tokens cost?"
category: token-cost
source: Reddit r/ClaudeAI https://reddit.com/r/SillyTavernAI/comments/1q0x6bg/if_i_instr
---

# If I instructed AI to reference data from another website, how would that factor into the input tokens cost?

## 증상
I know most of the token cost is for outputs, and that people use caching to minimize input cost, but would this be a way to get around the input cost by having the AI reference most of the input from another website?

Specifically talking about Sonnet 4.5 API as it is one of the most expensive options.

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
Reddit r/ClaudeAI https://reddit.com/r/SillyTavernAI/comments/1q0x6bg/if_i_instructed_ai_to_reference_data_from_another/
