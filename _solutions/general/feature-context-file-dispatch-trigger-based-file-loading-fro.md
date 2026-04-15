---
layout: solution
title: "Feature: context file dispatch — trigger-based file loading from CLAUDE.md"
category: general
source: https://github.com/anthropics/claude-code/issues/31575
description: "Claude Code's auto-memory () has a 200-line cap, and only and are auto-loaded into context. Other memory files persist on disk but are invisible unless"
---

# Feature: context file dispatch — trigger-based file loading from CLAUDE.md

## 증상
Claude Code's auto-memory (`MEMORY.md`) has a 200-line cap, and only `MEMORY.md` and `CLAUDE.md` are auto-loaded into context. Other memory files persist on disk but are invisible unless Claude happens to read them.

## 원인
Agent encountered an unexpected state or unhandled error condition outside the standard error handling path.

## 해결법
is adding a manual index to `MEMORY.md`:
```markdown

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/31575
