---
layout: solution
title: "Expose context window usage to hooks via environment variable"
category: config
source: https://github.com/anthropics/claude-code/issues/34340
---

# Expose context window usage to hooks via environment variable

## 증상
Hooks (PreToolUse, PostToolUse, Stop) have no way to know how much of the context window is consumed. The only context-related env vars available are `CLAUDE_TOOL_NAME`, `CLAUDECODE`, and `CLAUDE_CODE_SSE_PORT` — none expose token usage or context percentage.

## 원인
보고된 버그/문제. 카테고리: config.

## 해결법
Counting tool calls as a proxy for context consumption. This is:
- **Fragile** — breaks when context window size changes
- **Imprecise** — a `Read` of a 10-line file vs a 2000-line file consume very different amounts of context
- **Non-portable** — threshold must be manually recalibrated per model/plan

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/34340
