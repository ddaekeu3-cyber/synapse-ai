---
layout: solution
title: "40% reasoning accuracy drop beyond 50k tokens (attention dilution)"
category: context-window
source: SparkCo - Agent Context Windows in 2026
---

# 40% reasoning accuracy drop beyond 50k tokens (attention dilution)

## 증상
Agent reasoning quality degrades significantly in long conversations. After 50k+ tokens, the agent starts making mistakes, forgetting instructions, and giving inconsistent answers. 40% accuracy degradation measured.

## 원인
Attention dilution in long contexts. Transformer models dilute their focus across expanded sequences. Model pruning and cost-driven windowing exacerbate the problem. Bigger context windows actually make hallucination rates WORSE.

## 해결법
### 긴 컨텍스트 품질 유지

1. **선택적 컨텍스트 주입**
   - 전체 히스토리의 5-10%만 선택적으로 포함 → 토큰 40-70% 절감
   - 최근 5-10개 메시지만 전체 유지, 나머지는 요약

2. **리랭킹으로 관련성 우선**
   - Cross-encoder 리랭킹으로 가장 관련 있는 컨텍스트 우선 배치
   - 최신성 + 관련성 가중치 적용

3. **티어드 스토리지**
   - Hot (메모리): 현재 대화 (최근 5턴)
   - Warm (인덱스): 이번 세션 요약
   - Cold (파일): 과거 세션 전체

4. **주기적 컨텍스트 리프레시**
   - 20턴마다 컨텍스트 요약 + 재시작
   - 핵심 지시사항은 항상 프롬프트 시작에 배치

## 예상 토큰 절약
이 에러로 삽질 시: 약 30,000~100,000 토큰 소비
이 해결법 참조 시: 약 3,000 토큰

## 출처
SparkCo - Agent Context Windows in 2026
