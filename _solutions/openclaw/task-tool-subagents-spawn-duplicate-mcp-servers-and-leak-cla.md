---
layout: solution
title: "Task tool subagents spawn duplicate MCP servers and leak ~/.claude/tasks/ directories on Windows"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/28126
---

# Task tool subagents spawn duplicate MCP servers and leak ~/.claude/tasks/ directories on Windows

## 증상
Two problems compound to cause escalating resource leaks on Windows:

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
```bash
# Kill all orphaned node processes (kills current session too)
taskkill //F //IM node.exe

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/28126
