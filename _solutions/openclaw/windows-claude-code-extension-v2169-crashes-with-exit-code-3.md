---
layout: solution
title: "Windows: Claude Code extension v2.1.69 crashes with exit code 3221225781 (Access Violation)"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/31209
---

# Windows: Claude Code extension v2.1.69 crashes with exit code 3221225781 (Access Violation)

## 증상
- [x] I have searched [existing issues](https://github.com/anthropics/claude-code/issues?q=is%3Aissue%20state%3Aopen%20label%3Abug) and this hasn't been reported yet

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Using claude command directly in VS Code's integrated terminal works as a temporary alternative.
Environment

Platform: Windows 10/11
VS Code Extension Version: anthropic.claude-code-2.1.69-win32-x64
Claude Code CLI Version (npm): 2.1.69 ✅ Works correctly
Node.js Version: v24.14.0
Terminal: PowerShell

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/31209
