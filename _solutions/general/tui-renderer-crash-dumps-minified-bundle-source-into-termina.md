---
layout: solution
title: "TUI renderer crash dumps minified bundle source into terminal during long session"
category: general
source: https://github.com/anthropics/claude-code/issues/31913
---

# TUI renderer crash dumps minified bundle source into terminal during long session

## 증상
During a long-running Claude Code session (~12 hours), the terminal UI crashed and rendered a large chunk of its own minified JavaScript (Ink/Yoga renderer internals) directly into the terminal buffer, followed by a stack trace. The application did not recover gracefully — raw bundle source replaced normal UI output.

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
https://github.com/anthropics/claude-code/issues/31913
