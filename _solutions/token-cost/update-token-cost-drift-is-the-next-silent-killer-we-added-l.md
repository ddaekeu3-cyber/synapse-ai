---
layout: solution
title: "Update: token cost drift is the next silent killer (we added local trend history)"
category: token-cost
source: Reddit r/ClaudeAI https://reddit.com/r/AI_Agents/comments/1rf28wy/update_token_c
---

# Update: token cost drift is the next silent killer (we added local trend history)

## 증상
Quick follow-up to my runaway token loops thread. Once we added max-iter / token budgets / similarity breakers, the next issue we hit was quieter: token cost drift across releases. Diffs stayed green  but over a couple weeks the same workflows got 2–3 more expensive (prompt creep, tool retries, longer reasoning). You only notice after the bill and by then it’s already in prod behavior. So we added

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
Reddit r/ClaudeAI https://reddit.com/r/AI_Agents/comments/1rf28wy/update_token_cost_drift_is_the_next_silent_killer/
