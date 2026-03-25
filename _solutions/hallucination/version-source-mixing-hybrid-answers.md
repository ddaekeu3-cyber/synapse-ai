---
layout: solution
title: "RAG mixes different document versions creating incorrect hybrid answers"
category: hallucination
source: Perivitta Rajendran - Why Hallucination Happens
---

# RAG mixes different document versions creating incorrect hybrid answers

## 증상
Agent combines information from v1.0 and v2.0 documentation, creating answers that match neither version. Configuration syntax from old version mixed with new version features.

## 원인
Multiple versions of same documentation exist in vector database. Model combines chunks from different versions into factually incorrect hybrids.

## 해결법
### 버전 혼합 방지

1. **메타데이터 버전 태깅**: 모든 청크에 버전 번호 메타데이터 부여
2. **검색 시 버전 필터**: 특정 버전만 검색하도록 메타데이터 필터 적용
3. **벡터 DB 분리**: 다른 버전은 다른 컬렉션/인덱스에 저장
4. **청크 내 버전 표기**: 청크 텍스트 자체에 "v2.0:" 등 버전 식별자 포함

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
Perivitta Rajendran - Why Hallucination Happens
