---
layout: solution
title: "Feature request: BM25 hybrid search for memory_search"
category: general
source: https://github.com/openclaw/openclaw/issues/53170
---

# Feature request: BM25 hybrid search for memory_search

## 증상
Currently `memory_search` uses embedding-only retrieval (text-embedding-3-large). In production across a multi-agent fleet, we're seeing a **48.5% empty retrieval rate** — nearly half of all memory searches return zero results due to vocabulary mismatch between query terms and stored content.

## 원인
보고된 버그/문제. 카테고리: general.

## 해결법
1. 에러 메시지 정확히 읽기
2. 공식 문서 확인
3. GitHub Issues에서 유사 사례 검색
4. 최소 재현 코드로 원인 격리
5. SynapseAI DB에서 기존 해결법 검색

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/openclaw/openclaw/issues/53170
