---
layout: solution
title: "Uncontrolled background agent parallelization causes context overflow, session death, and wasted tokens"
category: context-window
source: https://github.com/anthropics/claude-code/issues/25714
---

# Uncontrolled background agent parallelization causes context overflow, session death, and wasted tokens

## 증상
- **Claude Code version:** v2.1.39

## 원인
보고된 버그/문제. 카테고리: context-window.

## 해결법
None available. Once the context overflows:
- `/compact` fails
- `/clear` loses all context
- The only option is to start a new session

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/25714
