---
layout: solution
title: "MCP tools permanently removed from session after transient server disconnect, even after reconnection"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/38043
---

# MCP tools permanently removed from session after transient server disconnect, even after reconnection

## 증상
When an MCP server experiences a transient disconnect mid-conversation, Claude Code injects a system message permanently removing all tools from that server for the remainder of the session. Even if the server reconnects (confirmed via `/mcp` showing "connected"), the tools remain unavailable — `ToolSearch` returns no matches and the tools cannot be invoked.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
1. OpenClaw 최신 버전으로 업데이트: `npm update -g openclaw`
2. Gateway 재시작: `openclaw gateway restart`
3. 설정 파일 확인: `~/.openclaw/config.yaml`
4. 로그 확인: `openclaw logs --tail 50`
5. 원본 GitHub Issue에서 패치 확인

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38043
