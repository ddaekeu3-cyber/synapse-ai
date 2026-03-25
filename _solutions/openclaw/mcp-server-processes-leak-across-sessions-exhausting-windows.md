---
layout: solution
title: "MCP server processes leak across sessions, exhausting Windows commit charge (0xC000012D)"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/38228
---

# MCP server processes leak across sessions, exhausting Windows commit charge (0xC000012D)

## 증상
MCP servers spawned via `uv`/`uvx` (and their child `python`/`node` processes) are not properly terminated when Claude Code sessions end or reconnect. Over multiple sessions, these orphan processes accumulate until the Windows commit charge limit is exhausted, causing `STATUS_COMMITMENT_LIMIT (0xC000012D)` errors that make the entire system unable to fork new processes.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Users can run this PowerShell one-liner to clean up:
```powershell
Get-Process -Name uv,uvx -EA 0 | Stop-Process -Force; Get-Process -Name cmd -EA 0 | ? {$_.MainWindowTitle -eq ''} | Stop-Process -Force
```

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38228
