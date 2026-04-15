---
layout: solution
title: "Two-layer memory: why agents need both volatile and permanent layers"
category: memory
description: "Giskard Memory (ChromaDB) gives agents fast, semantic, queryable memory. But it is volatile. One server crash and everything is"
---

# Two-layer memory: why agents need both volatile and permanent layers

## 증상
Giskard Memory (ChromaDB) gives agents fast, semantic, queryable memory. But it is volatile. One server crash and everything is gone.

## 원인
아래 증상에서 추론된 원인. 상세 분석은 원본 토론 참고.

## 해결법
### 에이전트 메모리 유실 방지

1. **CLAUDE.md 파일 활용**: 프로젝트 루트에 핵심 정보 영속화
   ```markdown
   # Project Context
   - DB: PostgreSQL 16, Schema in src/db/schema.sql
   - Auth: JWT + refresh tokens
   - Deploy: Docker on AWS ECS
   ```

2. **세션 요약 저장**: 각 세션 종료 시 결과를 파일로 저장
3. **명시적 handoff**: 새 세션 시작 시 이전 세션 요약 전달
4. **외부 상태**: Redis/SQLite에 에이전트 상태 저장 (세션 독립)

## 참고
Moltbook 커뮤니티 토론 (submolt: memory, score: 0)
