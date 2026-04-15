---
layout: solution
title: "Hooks with shell commands cause 5+ minute hangs/crashes on Windows"
category: performance
source: https://github.com/anthropics/claude-code/issues/34457
description: "Claude Code version:"
---

# Hooks with shell commands cause 5+ minute hangs/crashes on Windows

## 증상
**Claude Code version:** 2.1.73

## 원인
Resource bottleneck (CPU, memory, I/O, or network latency) or inefficient algorithm causing timeout or slowdown.

## 해결법
Remove all hooks from `.claude/settings.json`. Quality checks can still be triggered manually via CLAUDE.md instructions directing Claude to run linters/tests via the Bash tool.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/34457
