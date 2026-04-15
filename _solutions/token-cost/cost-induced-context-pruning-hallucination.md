---
layout: solution
title: "Aggressive context pruning to save costs causes 50% more hallucinations"
category: token-cost
source: SparkCo - Agent Context Windows in 2026
description: "To reduce token costs, system aggressively removes older context. But this causes agent to hallucinate more frequently — 50% increase in hallucination"
---

# Aggressive context pruning to save costs causes 50% more hallucinations

## 증상
To reduce token costs, system aggressively removes older context. But this causes agent to hallucinate more frequently — 50% increase in hallucination rate. Key business logic and constraints get lost.

## 원인
Evicting tokens to cut expenses omits key details needed for accurate reasoning. The cost-saving measure backfires by creating more errors that require more tokens to fix.

## 해결법
### 비용 절감 vs 품질 균형

1. **정책 기반 보존**
   - 모든 컨텍스트를 동등하게 취급하지 않기
   - 우선순위: 시스템 지시 > 사용자 제약조건 > 최근 대화 > 과거 대화

2. **티어드 스토리지**
   - Hot (in-memory): 활성 컨텍스트 → 빠른 접근
   - Cold (archival): 전체 히스토리 → 필요시 검색
   - 비용 효율적으로 전체 히스토리 유지

3. **스마트 요약**
   - 삭제 대신 요약으로 대체
   - 핵심 결정/제약조건은 요약에 반드시 포함

4. **비용 모니터링 + 알림**
   - 토큰 사용량 실시간 추적
   - 임계값 초과 시 사람에게 알림 (자동 삭제 X)

## 예상 토큰 절약
이 에러로 삽질 시: 약 20,000~50,000 토큰 소비
이 해결법 참조 시: 약 3,000 토큰

## 출처
SparkCo - Agent Context Windows in 2026
