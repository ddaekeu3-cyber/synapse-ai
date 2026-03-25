---
layout: solution
title: "[Feature] memory_write: allow orchestrator sessions to push learnings into sub-agent memory"
category: general
source: https://github.com/openclaw/openclaw/issues/27863
---

# [Feature] memory_write: allow orchestrator sessions to push learnings into sub-agent memory

## 증상
When an orchestrator session spawns sub-agents via `sessions_spawn`, knowledge discovered by one sub-agent cannot be propagated to future sub-agents. Every spawned session starts cold — it can read its own memory store but has no way to receive targeted, curated knowledge from the session that created it.

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
https://github.com/openclaw/openclaw/issues/27863
