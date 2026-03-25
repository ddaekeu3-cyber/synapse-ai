---
layout: solution
title: "Claude fails to remember read-only tool permissions in active session despite updating CLAUDE.md"
category: memory
source: https://github.com/anthropics/claude-code/issues/29381
---

# Claude fails to remember read-only tool permissions in active session despite updating CLAUDE.md

## 증상
I've had to tell claude over and over again to "just do read-only things" and "update .claude/CLAUDE.md to remember you can do read-only things" ... things like aws describe- list- operations, things like terraform-plan, things like cat/grep/find, and it doesn't remember. Even after it adds this permission to the CLAUDE.md file, it isn't remembering in the active session. It makes me much less pro

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
https://github.com/anthropics/claude-code/issues/29381
