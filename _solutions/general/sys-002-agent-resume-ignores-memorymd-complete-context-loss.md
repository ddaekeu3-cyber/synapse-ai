---
layout: solution
title: "[SYS-002] Agent --resume ignores MEMORY.md: complete context loss on session resume, decision instability, destroyed live test"
category: general
source: https://github.com/anthropics/claude-code/issues/34661
---

# [SYS-002] Agent --resume ignores MEMORY.md: complete context loss on session resume, decision instability, destroyed live test

## 증상
Agent resumed into an active session (river permit sniper automation) and completely ignored its own MEMORY.md and all project CLAUDE.md instructions. The result was 2+ hours of wasted time during a time-critical live test window, reinventing already-solved problems, and a lost real-world opportunity (Desolation Canyon river permit drop).

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
https://github.com/anthropics/claude-code/issues/34661
