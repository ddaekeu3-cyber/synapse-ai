---
layout: solution
title: "Home-directory launches trigger broad discovery, read/edit state loss, and premature compaction on simple fixes"
category: general
source: https://github.com/anthropics/claude-code/issues/34815
---

# Home-directory launches trigger broad discovery, read/edit state loss, and premature compaction on simple fixes

## 증상
When Claude Code is started from the user's home directory (`~`) and given a small local bug-fix task (without specifying the exact repo/file path), it can spiral into broad repo discovery, protected-directory errors, tool selection degradation, read/edit state tracking failures, and premature conversation compaction — all for what should be a trivial one-file edit.

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
https://github.com/anthropics/claude-code/issues/34815
