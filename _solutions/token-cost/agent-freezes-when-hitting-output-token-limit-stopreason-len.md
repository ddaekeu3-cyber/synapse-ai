---
layout: solution
title: "Agent freezes when hitting output token limit (stopReason: length)"
category: token-cost
source: https://github.com/openclaw/openclaw/issues/28632
---

# Agent freezes when hitting output token limit (stopReason: length)

## 증상
When an agent's response hits the model's output token limit (`stopReason: "length"`), the session **completely freezes** instead of continuing or recovering gracefully.

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
https://github.com/openclaw/openclaw/issues/28632
