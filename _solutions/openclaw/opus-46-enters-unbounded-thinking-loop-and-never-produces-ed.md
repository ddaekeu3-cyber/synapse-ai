---
layout: solution
title: "Opus 4.6 enters unbounded thinking loop and never produces edits — reading/analysis works, code modification freezes indefinitely (Windows 11 / PowerShell 7 / PyCharm plugin)"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/37023
---

# Opus 4.6 enters unbounded thinking loop and never produces edits — reading/analysis works, code modification freezes indefinitely (Windows 11 / PowerShell 7 / PyCharm plugin)

## 증상
- [x] I have searched [existing issues](https://github.com/anthropics/claude-code/issues?q=is%3Aissue%20state%3Aopen%20label%3Abug) and this hasn't been reported yet

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
/modify code based on its analysis → ❌ Freezes in "thinking" state indefinitely
3. Output token counter (↓) slowly climbs (e.g., 4.6k → 5.7k over several minutes) but no edits appear
4. No file writes, no tool calls, no visible progress
5. Timer continues counting (observed 5m, 10m, 20m, 24m+)
6. If user presses Escape and says "Stop thinking, make the edits now" → Claude **immediately** starts writing code successfully

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/37023
