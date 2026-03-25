---
layout: solution
title: "Codex 5.4 is more expensive than 5.3, if current limit drain is the new normal not a glitch it will be unusable after the 2x rate limit ends"
category: token-cost
source: Reddit r/ClaudeAI https://reddit.com/r/codex/comments/1rp1oe0/codex_54_is_more_e
---

# Codex 5.4 is more expensive than 5.3, if current limit drain is the new normal not a glitch it will be unusable after the 2x rate limit ends

## 증상
Almost everyone noticed limits drain faster but openai insist it's something affecting just minority of people, they officially reduced gpt 5.4 limits and the current situation may not be a glitch but the new normal they wanna impose, quotas finish in 2 days with the 2x limits still applied
So under current conditions, after that offer ends in 2 april codex will not be usable and will be just like

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
Reddit r/ClaudeAI https://reddit.com/r/codex/comments/1rp1oe0/codex_54_is_more_expensive_than_53_if_current/
