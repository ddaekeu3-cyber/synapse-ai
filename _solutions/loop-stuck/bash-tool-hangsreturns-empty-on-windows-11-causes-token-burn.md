---
layout: solution
title: "Bash tool hangs/returns empty on Windows 11 — causes token-burning retry loops"
category: loop-stuck
source: https://github.com/anthropics/claude-code/issues/34453
description: "- [x] I have searched existing issues and this hasn't been reported"
---

# Bash tool hangs/returns empty on Windows 11 — causes token-burning retry loops

## 증상
- [x] I have searched [existing issues](https://github.com/anthropics/claude-code/issues?q=is%3Aissue%20state%3Aopen%20label%3Abug) and this hasn't been reported yet

## 원인
Agent entered a retry or decision loop without an exit condition, consuming tokens indefinitely without making progress. 카테고리: loop-stuck.

## 해결법
Running commands with `run_in_background: true` and redirecting to a temp file, then using the Read tool to retrieve
  output. This is clunky and still costs 3 tool calls per command.

  ## Impact

  This is a significant usability and cost issue on Windows. Token burn from retries makes extended coding sessions
  expensive and frustrating. It particularly affects workflows that rely heavily on git operations (commit, diff,
  status, log).

  ## Possibly Related Issues

  - #21915 — Bash tool produces no output on Windows
  - #26545 — Bash tool returns exit code 1 with no output on Git Bash si

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/34453
