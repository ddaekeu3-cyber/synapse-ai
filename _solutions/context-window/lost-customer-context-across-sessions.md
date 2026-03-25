---
layout: solution
title: "Agent loses all context when starting a new session"
category: context-window
source: SparkCo - Agent Context Windows in 2026
---

# Agent loses all context when starting a new session

## 증상
Agent cannot recall information from previous interactions. User must repeat explanations and context every new session. Multi-step workflows break across session boundaries.

## 원인
Stateless pipelines drop multi-step task context. Ephemeral memory is capped at 128k tokens and provides no persistence across sessions.

## 해결법
### 세션 간 컨텍스트 유지

1. **영속적 메모리 파일**
   - CLAUDE.md / AGENTS.md에 핵심 정보 기록
   - 프로젝트별 `.memory/` 디렉토리에 상태 저장

2. **세션 요약 자동 저장**
   ```
   세션 종료 시:
   1. 주요 결정사항 요약
   2. 현재 진행상황 기록
   3. 다음 세션에서 할 일 목록
   → session-summary.md에 저장
   ```

3. **메모리 스티칭**
   - 지식 그래프로 세션 간 엔티티 연결
   - 이전 세션 요약을 새 세션 컨텍스트에 자동 주입

4. **외부 상태 관리**
   - JSON/SQLite에 에이전트 상태 저장
   - 세션 시작 시 마지막 상태 로드

## 예상 토큰 절약
이 에러로 삽질 시: 약 15,000~30,000 토큰 소비
이 해결법 참조 시: 약 2,000 토큰

## 출처
SparkCo - Agent Context Windows in 2026
