---
layout: solution
title: "MCP servers with many tools silently fail to connect on startup"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/38462
---

# MCP servers with many tools silently fail to connect on startup

## 증상
MCP servers configured in `~/.claude.json` that register a large number of tools intermittently fail to connect at Claude Code startup. When they fail, they are silently dropped for the entire session with no error message or notification.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Restart Claude Code and hope the server connects. There is no way to force a reconnect mid-session or to diagnose which servers are connected without asking the model to check the tools list.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38462
