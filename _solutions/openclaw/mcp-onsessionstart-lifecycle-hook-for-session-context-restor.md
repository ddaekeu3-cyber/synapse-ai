---
layout: solution
title: "MCP on_session_start lifecycle hook for session context restoration"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/38095
---

# MCP on_session_start lifecycle hook for session context restoration

## 증상
When a session starts (new or resumed) with a remote MCP server configured, there's no way for the MCP server to proactively provide context to the session. The session has to know what to search for, but a just-started session doesn't know what it doesn't know.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Sessions must be instructed (via CLAUDE.md or human prompting) to call specific MCP tools at startup. This is fragile — new sessions don't know what to call, and the instructions add to the system prompt size.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38095
