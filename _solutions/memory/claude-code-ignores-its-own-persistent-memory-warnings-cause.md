---
layout: solution
title: "Claude Code ignores its own persistent memory warnings, causes irreversible data loss"
category: memory
source: https://github.com/anthropics/claude-code/issues/37586
---

# Claude Code ignores its own persistent memory warnings, causes irreversible data loss

## 증상
Claude Code performed a destructive action that caused irreversible data loss on a production system, despite having **multiple persistent memory entries** explicitly warning about this exact failure scenario.

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
https://github.com/anthropics/claude-code/issues/37586
