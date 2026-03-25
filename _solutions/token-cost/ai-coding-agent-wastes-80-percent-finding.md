---
layout: solution
title: "AI coding agent wastes 80% of tokens on orientation, not problem-solving"
category: token-cost
source: Medium - Jake Nesler (Context Compression, 2026)
---

# AI coding agent wastes 80% of tokens on orientation, not problem-solving

## 증상
AI coding agent spends most of its token budget exploring the codebase rather than actually solving the problem. Token costs are 5x higher than expected for simple tasks.

## 원인
Agent does not have a map of the codebase. It spends tokens reading files, searching for definitions, and understanding structure before it can start actual work.

## 해결법
### 코드 에이전트 토큰 낭비 80% 줄이기

1. **코드 지식 그래프 구축**:
   - 파일 구조, 함수/클래스 정의, import 관계를 사전 인덱싱
   - 에이전트에게 "지도"를 제공하여 탐색 불필요

2. **컨텍스트 압축**:
   - 전체 파일 대신 관련 함수/클래스만 주입
   - 토큰 40-95% 절약 가능

3. **프로젝트 요약 파일**:
   ```markdown
   # Project Map
   - src/api/ → REST endpoints (auth, users, posts)
   - src/db/ → Database models (SQLAlchemy)
   - src/services/ → Business logic
   ```

4. **CLAUDE.md / AGENTS.md 활용**: 프로젝트 구조를 사전 문서화

## 예상 토큰 절약
이 에러로 삽질 시: 약 50,000~200,000 토큰 소비
이 해결법 참조 시: 약 5,000 토큰

## 출처
Medium - Jake Nesler (Context Compression, 2026)
