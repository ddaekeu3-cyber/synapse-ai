---
layout: solution
title: "Agent fabricates memories of previous interactions that never happened"
category: hallucination
source: SparkCo - Agent Context Windows in 2026
description: "Agent claims to remember previous conversations or states that never occurred. Makes decisions based on false memories. \"Last time we discussed X\" when X"
---

# Agent fabricates memories of previous interactions that never happened

## 증상
Agent claims to remember previous conversations or states that never occurred. Makes decisions based on false memories. "Last time we discussed X" when X never happened.

## 원인
Stateless designs lead to hallucinated state transitions. Without persistent records to ground reasoning, the model generates plausible-sounding but fictional memories of prior interactions.

## 해결법
### 가짜 기억 방지

1. **불변 메모리 로그**
   - 모든 상호작용을 변경 불가능한 로그에 기록
   - "기억"은 반드시 로그에서 검색 → 없으면 "기록 없음" 반환

2. **명시적 상태 확인**
   ```
   시스템 프롬프트:
   "이전 대화를 추측하지 마. 기록된 것만 참조해.
   기록이 없으면 '이전 기록이 없습니다'라고 답해."
   ```

3. **엔티티 해상도 그래프**
   - 사용자/프로젝트/결정 등을 그래프로 관리
   - 각 노드에 타임스탬프 + 출처 기록

4. **감사 추적**
   - 모든 에이전트 결정에 근거 명시 요구
   - "이 결정의 근거: [세션 #12, 2026-03-20]" 형태

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
SparkCo - Agent Context Windows in 2026
