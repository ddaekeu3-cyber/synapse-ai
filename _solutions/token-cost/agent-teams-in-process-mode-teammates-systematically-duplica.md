---
layout: solution
title: "Agent Teams (in-process mode): teammates systematically duplicated, causing 2x token usage"
category: token-cost
source: https://github.com/anthropics/claude-code/issues/32996
---

# Agent Teams (in-process mode): teammates systematically duplicated, causing 2x token usage

## 증상
When using Agent Teams (`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`) in in-process mode, the Lead systematically creates each teammate **twice**, causing every task to be fully executed 2x and doubling token consumption. The user has no visibility into this duplication.

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
https://github.com/anthropics/claude-code/issues/32996
