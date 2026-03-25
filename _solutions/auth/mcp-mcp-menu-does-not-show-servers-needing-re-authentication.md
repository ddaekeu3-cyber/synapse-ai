---
layout: solution
title: "MCP /mcp menu does not show servers needing re-authentication after token revocation"
category: auth
source: https://github.com/anthropics/claude-code/issues/30272
---

# MCP /mcp menu does not show servers needing re-authentication after token revocation

## 증상
When an HTTP MCP server's OAuth session is revoked server-side (e.g., user revokes session from a settings page), Claude Code correctly detects the auth failure when a tool is called:

## 원인
보고된 버그/문제. 카테고리: auth.

## 해결법
Restarting Claude Code in the project directory forces a fresh connection attempt, which triggers the OAuth re-authorization flow.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/30272
