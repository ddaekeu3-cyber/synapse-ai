---
layout: solution
title: "[Feature]: Remove memory_search/memory_get from sub-agent default deny list"
category: general
source: https://github.com/openclaw/openclaw/issues/16214
---

# [Feature]: Remove memory_search/memory_get from sub-agent default deny list

## 증상
Sub-agents have a hardcoded default deny list that includes `memory_search` and `memory_get` (rationale: "Pass relevant info in spawn prompt instead"). There is no way to re-enable a default-denied tool — custom deny entries are added to the defaults, and allow is filtered by the default deny list (deny always wins).

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
https://github.com/openclaw/openclaw/issues/16214
