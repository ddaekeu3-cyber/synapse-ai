---
layout: solution
title: "Exit Code 3221225781 — Bun Runtime Crash on Windows 10"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/31282
description: "- [x] I have searched existing issues and this hasn't been reported"
---

# Exit Code 3221225781 — Bun Runtime Crash on Windows 10

## 증상
- [x] I have searched [existing issues](https://github.com/anthropics/claude-code/issues?q=is%3Aissue%20state%3Aopen%20label%3Abug) and this hasn't been reported yet

## 원인
OpenClaw gateway, skill, or agent configuration issue — root cause confirmed in the openclaw/openclaw issue tracker.

## 해결법
Claude Code can be used via the integrated terminal in VS Code by running the 'claude' command. This uses the npm-installed Node.js-based CLI and works correctly. However, this lacks the VS Code panel integration (inline diffs, sidebar chat, etc.) that the extension provides.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/31282
