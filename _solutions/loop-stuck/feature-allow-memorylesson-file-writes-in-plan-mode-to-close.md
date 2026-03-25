---
layout: solution
title: "[FEATURE] Allow memory/lesson file writes in plan mode to close the learning feedback loop"
category: loop-stuck
source: https://github.com/anthropics/claude-code/issues/34727
---

# [FEATURE] Allow memory/lesson file writes in plan mode to close the learning feedback loop

## 증상
Plan mode blocks all write operations (Edit, Write, NotebookEdit). This is correct for code safety — but it also blocks writes to memory and lesson-tracking files (`MEMORY.md`, `context/lessons.md`, `CLAUDE.md`), creating a structural learning gap.

## 원인
보고된 버그/문제. 카테고리: loop-stuck.

## 해결법
Add a rule to `CLAUDE.md` that makes lesson capture a **mandatory final step in every plan**:

```markdown

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/34727
