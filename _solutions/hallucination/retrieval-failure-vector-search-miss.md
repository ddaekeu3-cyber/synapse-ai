---
layout: solution
title: "RAG vector search fails to find correct document, agent guesses instead"
category: hallucination
source: Perivitta Rajendran - Why Hallucination Happens
description: "Agent has access to a knowledge base but gives wrong answers because the vector search retrieves irrelevant documents. The correct answer exists in the"
---

# RAG vector search fails to find correct document, agent guesses instead

## 증상
Agent has access to a knowledge base but gives wrong answers because the vector search retrieves irrelevant documents. The correct answer exists in the database but is not found.

## 원인
Vector search fails to locate the correct information. Poor chunking strategy, missing metadata, duplicate chunks, or suboptimal embedding model.

## 해결법
### RAG 검색 실패 해결

1. **시맨틱 청킹**
   - 고정 크기 대신 의미 단위로 분할
   - 청크 오버랩 추가 (경계에서 정보 손실 방지)
   - 문서 제목/헤딩을 청크 텍스트에 포함

2. **중복 제거**
   - 중복 청크 제거 → 검색 정확도 향상

3. **메타데이터 필터링**
   - 버전, 제품, 언어, 날짜 등 메타데이터 활용
   - 검색 시 메타데이터 필터로 범위 좁히기

4. **리랭킹**
   - Bi-encoder로 후보 검색 → Cross-encoder로 상위 5-8개 선별
   - 양보다 질: 50개 청크보다 5개 정확한 청크

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
Perivitta Rajendran - Why Hallucination Happens
