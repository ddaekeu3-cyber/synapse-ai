---
layout: solution
title: "Auto-memory system lacks clear boundary with CLAUDE.md, leading to scope creep"
category: general
source: https://github.com/anthropics/claude-code/issues/34075
---

# Auto-memory system lacks clear boundary with CLAUDE.md, leading to scope creep

## 증상
The auto-memory system (`MEMORY.md` + sub-files) has no clear boundary separating it from `CLAUDE.md`, which causes the memory system to gradually absorb operational instructions, how-tos, and behavioral rules that belong in `CLAUDE.md`. Over time, power users end up with two competing instruction systems with no well-defined frontier.

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
https://github.com/anthropics/claude-code/issues/34075
