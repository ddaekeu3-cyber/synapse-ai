---
layout: solution
title: "Claude Code ignores explicit instructions in memory/CLAUDE.md files"
category: general
source: https://github.com/anthropics/claude-code/issues/37550
description: "- [x] I have searched existing issues and this hasn't been reported"
---

# Claude Code ignores explicit instructions in memory/CLAUDE.md files

## 증상
- [x] I have searched [existing issues](https://github.com/anthropics/claude-code/issues?q=is%3Aissue%20state%3Aopen%20label%3Abug) and this hasn't been reported yet

## 원인
Agent encountered an unexpected state or unhandled error condition outside the standard error handling path.

## 해결법
its mistake.
Alternatively: Add a rule "When user proposes an idea, execute it — do not suggest alternatives."
Tell Claude "I want to write this in assembly."
Observe: Claude will suggest writing it in C instead, despite the explicit instruction not to redirect.
Alternatively: Add a rule "Do not claim code is incomplete without reading the file."
Ask Claude about a file it wrote earlier in the session (or a previous session).
Observe: Claude will say "those are stubs returning zeros" without opening the file to check.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/37550
