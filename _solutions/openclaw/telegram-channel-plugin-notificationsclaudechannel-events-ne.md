---
layout: solution
title: "Telegram channel plugin: notifications/claude/channel events never reach the session"
category: openclaw
source: https://github.com/anthropics/claude-code/issues/38285
---

# Telegram channel plugin: notifications/claude/channel events never reach the session

## 증상
The Telegram channel plugin's MCP server starts, authenticates, polls Telegram, and fires `notifications/claude/channel` JSON-RPC events correctly, but the Claude Code session never receives or displays them. Messages are silently dropped.

## 원인
보고된 버그/문제. 카테고리: openclaw.

## 해결법
Built a standalone bot using grammy + Anthropic SDK directly, bypassing Claude Code Channels entirely. Works perfectly but loses project context and tool access.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38285
