---
layout: solution
title: "MCP OAuth tokens not auto-refreshing despite valid refresh tokens"
category: auth
source: https://github.com/anthropics/claude-code/issues/28262
---

# MCP OAuth tokens not auto-refreshing despite valid refresh tokens

## 증상
MCP OAuth tokens for HTTP-based servers (Atlassian, Notion) are not being automatically refreshed when they expire, despite valid refresh tokens being stored in `~/.claude/.credentials.json`.

## 원인
보고된 버그/문제. 카테고리: auth.

## 해결법
Manually running `/mcp` to re-authenticate when tool calls start failing. A `SessionStart` hook can be used to check `expiresAt` and warn early, but cannot trigger the refresh itself.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/28262
