---
layout: solution
title: "Bug: cache_control TTL ordering error when hooks/MCP inject additionalContext into long conversations"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/38542
---

# Bug: cache_control TTL ordering error when hooks/MCP inject additionalContext into long conversations

## 증상
Claude Code intermittently returns a 400 API error when hooks or MCP servers inject `additionalContext` into conversations, particularly in longer sessions (100+ messages):

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
None currently. The error is recoverable by retrying the conversation, but it disrupts active sessions. Reducing hook frequency (e.g., throttling PreToolUse to every 10+ seconds) may reduce occurrence but does not eliminate it.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38542
