---
layout: solution
title: "CLI enters infinite CPU loop when agent deletes .claude/commands/ with duplicate slash commands from nested directories"
category: loop-stuck
source: https://github.com/anthropics/claude-code/issues/27756
---

# CLI enters infinite CPU loop when agent deletes .claude/commands/ with duplicate slash commands from nested directories

## 증상
- [x] I have searched [existing issues](https://github.com/anthropics/claude-code/issues?q=is%3Aissue%20state%3Aopen%20label%3Abug) and this hasn't been reported yet

## 원인
보고된 버그/문제. 카테고리: loop-stuck.

## 해결법
this by running `rm -r .claude/commands/` on the inner directory, the CLI enters an infinite CPU-burning loop and becomes completely unresponsive.

The process consumed 33% CPU sustained for 13+ hours before being killed externally with SIGTERM. It was unresponsive to Ctrl-C and Ctrl-Z from the terminal — only an external `kill -TERM` worked.

Key detail: **the deletion was agent-initiated, not user-initiated.** The user told Claude about the duplicate commands; Claude decided `rm -r .claude/commands/` was the fix and executed it via the Bash tool. The Bash tool completed successfully (directo

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/27756
