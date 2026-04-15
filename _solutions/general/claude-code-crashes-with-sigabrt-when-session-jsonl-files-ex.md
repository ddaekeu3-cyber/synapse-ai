---
layout: solution
title: "Claude Code crashes with SIGABRT when session JSONL files exceed V8 heap limit"
category: general
source: https://github.com/anthropics/claude-code/issues/19025
description: "Claude Code crashes on startup with SIGABRT when historical session files grow too large. The crash occurs because Claude attempts to parse entire session"
---

# Claude Code crashes with SIGABRT when session JSONL files exceed V8 heap limit

## 증상
Claude Code crashes on startup with SIGABRT when historical session files grow too large. The crash occurs because Claude attempts to parse entire session JSONL files using V8's `JSON.parse`, which fails when files exceed the V8 heap limit.

## 원인
Agent encountered an unexpected state or unhandled error condition outside the standard error handling path.

## 해결법
Manually remove or move large session files from `~/.claude/projects/<project-hash>/`:
```bash

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/19025
