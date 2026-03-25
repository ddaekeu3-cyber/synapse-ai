---
layout: solution
title: "Discord MCP plugin intermittently loses gateway connection, reply fails with 'not allowlisted'"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/37748
---

# Discord MCP plugin intermittently loses gateway connection, reply fails with 'not allowlisted'

## 증상
The Discord MCP plugin (`discord@claude-plugins-official`) intermittently loses its Discord gateway connection during long-running Claude Code sessions (tmux-based, persistent). When this happens, outbound tools (`reply`, `fetch_messages`) fail with:

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Manual `/mcp` reconnect from the Claude Code REPL restores functionality immediately.

---
Filed by an ai (Claude Opus 4.6) on behalf of a user running a persistent tmux-based Claude Code session with Discord as the primary communication channel.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/37748
