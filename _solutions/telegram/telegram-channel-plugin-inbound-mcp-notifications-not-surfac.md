---
layout: solution
title: "Telegram channel plugin: inbound MCP notifications not surfaced in conversation"
category: telegram
source: https://github.com/anthropics/claude-code/issues/37812
---

# Telegram channel plugin: inbound MCP notifications not surfaced in conversation

## 증상
The Telegram channel plugin (`telegram@claude-plugins-official` v0.0.1) sends MCP notifications via `notifications/claude/channel` on inbound messages, but Claude Code (v2.1.81) never surfaces them as `<channel>` tags in the conversation.

## 원인
보고된 버그/문제. 카테고리: telegram.

## 해결법
Direct outbound messages work (calling `reply` tool with the user's chat_id). Users can type messages in the CLI and have them relayed to Telegram, but the bidirectional channel flow is broken.

## 예상 토큰 절약
이 에러로 삽질 시: 약 5,000~15,000 토큰 소비
이 해결법 참조 시: 약 500 토큰

## 출처
https://github.com/anthropics/claude-code/issues/37812
