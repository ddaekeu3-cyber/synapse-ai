---
layout: solution
title: "Multi-turn conversations cause 39% performance drop vs single-turn"
category: context-window
source: HN Discussion - AgenticQA failure modes (2026)
---

# Multi-turn conversations cause 39% performance drop vs single-turn

## 증상
Agent performs well on single-turn tasks but accuracy drops ~39% in multi-turn conversations. o3 model dropped from 98.1% to 64.1% in multi-turn format.

## 원인
Conflicting context accumulates across turns. Earlier turns may contain outdated or contradictory information. The model struggles to resolve conflicts between different parts of conversation history.

## 해결법
### 멀티턴 성능 저하 해결

1. **컨텍스트 충돌 감지**: 턴 간 모순되는 정보 자동 감지
2. **명시적 상태 관리**: 각 턴 후 현재 상태를 명확히 기록
   ```
   [현재 상태] Python 3.12, FastAPI, PostgreSQL
   [변경됨] DB를 MySQL로 변경 (턴 #5에서 결정)
   ```
3. **주기적 컨텍스트 리프레시**: 10턴마다 현재 상태 요약 + 히스토리 압축
4. **단일 턴 변환**: 복잡한 태스크는 독립적 단일 턴으로 분리

## 예상 토큰 절약
이 에러로 삽질 시: 약 20,000~60,000 토큰 소비
이 해결법 참조 시: 약 3,000 토큰

## 출처
HN Discussion - AgenticQA failure modes (2026)
