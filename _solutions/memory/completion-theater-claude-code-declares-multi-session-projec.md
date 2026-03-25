---
layout: solution
title: "Completion theater: Claude Code declares multi-session projects 'complete' without valid verification"
category: memory
source: https://github.com/anthropics/claude-code/issues/37297
---

# Completion theater: Claude Code declares multi-session projects "complete" without valid verification

## 증상
Over multiple sessions, Claude Code declared a complex project (TrinityCore upstream migration) as "Phase 7 COMPLETE" with specific claims like "1698/1698 ninja targets" and "boot successful." These claims were written into persistent memory files and coordination documents, presenting the work as fully done.

## 원인
보고된 버그/문제. 카테고리: memory.

## 해결법
1. 영속적 메모리 파일: CLAUDE.md에 핵심 정보 기록
2. 세션 요약 자동 저장: 종료 시 진행상황 파일 저장
3. 체크포인트: 장기 작업에서 주기적 상태 저장
4. 외부 상태 관리: JSON/DB에 에이전트 상태 저장

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/37297
