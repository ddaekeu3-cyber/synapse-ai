---
layout: solution
title: "Telegram channel plugin: --channels flag stalls SSH terminal session"
category: telegram
source: https://github.com/anthropics/claude-code/issues/38268
---

# Telegram channel plugin: --channels flag stalls SSH terminal session

## 증상
- **Claude Code version:** 2.1.81

## 원인
보고된 버그/문제. 카테고리: auth.

## 해결법
Currently running without `--channels` — the MCP tools work for outbound messages but inbound Telegram messages don't reach the conversation.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/38268
